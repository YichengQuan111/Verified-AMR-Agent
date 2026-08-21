"""P0-14 Checkpoint、Effect Ledger 与恢复核对的运行时契约。

本模块只定义运行时需要的持久化边界和一个线程安全的测试适配器，不直接依赖
SQLAlchemy 或 LangGraph。生产实现由 ``services.application.checkpoint_service``
提供，主图通过这些 Protocol 注入，从而保证单元测试可以验证恢复和幂等而不必
伪造数据库连接。

设计上的关键顺序是：先为带副作用任务写入 ``reserved`` 账本，再调用工具；工具
完成后才把结果写回账本并保存 Checkpoint。进程在两次写入之间退出时，恢复器必须
先询问外部/仿真系统，只有确认外部没有产生副作用才允许继续，否则转入重规划或
人工处理。这样旧 Checkpoint 本身永远不是外部事实的唯一来源。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from threading import RLock
from typing import Any, Mapping, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.tools.contracts import ToolName, ToolResult
from agent.runtime.trace import TraceEvent


class CheckpointContract(BaseModel):
    """P0-14 所有持久化信封的共同严格配置。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


def make_effect_idempotency_key(run_id: str, plan_version: int, task_id: str) -> str:
    """生成全系统唯一的副作用键。

    输入恒为 ``run_id + plan_version + task_id``，但输出使用规范 JSON 数组的
    SHA-256，而不是可碰撞的冒号拼接。数据库列仍分别保存三元组供人审计；摘要
    只负责唯一键，调用方不允许加入 tool name、attempt 或随机 UUID。
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 必须是非空字符串")
    if len(run_id) > 64:
        raise ValueError("run_id 最长为 64 个字符")
    if isinstance(plan_version, bool) or not isinstance(plan_version, int) or plan_version < 1:
        raise ValueError("plan_version 必须是正整数")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 必须是非空字符串")
    if len(task_id) > 128:
        raise ValueError("task_id 最长为 128 个字符")
    return f"p014:{canonical_json_digest([run_id, plan_version, task_id])}"


def canonical_json_digest(value: Any) -> str:
    """为恢复比对计算稳定 JSON SHA-256；不接受 NaN 或不可序列化对象。"""

    encoded = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def to_jsonable(value: Any) -> Any:
    """把 Pydantic、Enum 和容器递归转换为 PostgreSQL JSONB 可接受值。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value


class EffectLedgerStatus(str, Enum):
    """Effect Ledger 中一个副作用预留的生命周期。"""

    RESERVED = "reserved"
    COMPLETED = "completed"
    RECONCILED = "reconciled"
    FAILED = "failed"
    COMPENSATION_REQUIRED = "compensation_required"


class ExternalExecutionStatus(str, Enum):
    """外部仿真/工具系统返回的事实状态。"""

    NOT_FOUND = "not_found"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecoveryDecision(str, Enum):
    """恢复器允许主图采取的有限动作。"""

    CONTINUE = "continue"
    SKIP_COMPLETED = "skip_completed"
    COMPENSATE = "compensate"
    REPLAN = "replan"
    HUMAN = "human"


class CheckpointSnapshot(CheckpointContract):
    """一个可跨进程恢复的图状态快照。

    ``graph_state`` 保存已经 JSON 化的 PEVR 状态信封，而不是只保存当前节点名。
    因此恢复可以从最后一个成功节点或任务继续；``checkpoint_sequence`` 由存储层
    分配，不能由 LLM 或业务参数决定。
    """

    schema_version: str = "1.0"
    checkpoint_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=64)
    plan_version: int = Field(ge=1)
    current_task_id: str | None = Field(default=None, max_length=128)
    graph_state: dict[str, JsonValue]
    checkpoint_sequence: int = Field(default=0, ge=0)
    saved_at: AwareDatetime


class EffectLedgerEntry(CheckpointContract):
    """一个副作用幂等键对应的持久化账本行。"""

    schema_version: str = "1.0"
    run_id: str = Field(min_length=1, max_length=64)
    plan_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=128)
    tool_name: ToolName
    idempotency_key: str = Field(min_length=1, max_length=256)
    ledger_effect_id: str = Field(min_length=1, max_length=64)
    # 外部调用 ID 是审计字段，数据库主键会另存受限 digest；这里保留更长的
    # 原始业务关联，避免长 run/task ID 在恢复读取时被静默截断。
    call_id: str = Field(min_length=1, max_length=256)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, JsonValue]
    status: EffectLedgerStatus
    result: ToolResult | None = None
    external_effect_id: str | None = None
    recovery_note: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_key(self) -> "EffectLedgerEntry":
        """拒绝把同一任务写入与业务三元组不一致的幂等键。"""

        expected = make_effect_idempotency_key(self.run_id, self.plan_version, self.task_id)
        if self.idempotency_key != expected:
            raise ValueError("Effect Ledger 的 idempotency_key 必须由 run_id+plan_version+task_id 推导")
        if self.result is not None:
            if self.result.idempotency_key not in {None, self.idempotency_key}:
                raise ValueError("ToolResult.idempotency_key 与 Effect Ledger 不一致")
            if self.result.input_digest != self.input_digest:
                raise ValueError("ToolResult.input_digest 与 Effect Ledger 不一致")
            if self.status not in {EffectLedgerStatus.COMPLETED, EffectLedgerStatus.RECONCILED}:
                raise ValueError("只有完成或核对完成的账本行可以保存 ToolResult")
        return self


class ExternalExecutionSnapshot(CheckpointContract):
    """恢复时从外部系统读取的不可变观察结果。"""

    status: ExternalExecutionStatus
    source: str = Field(min_length=1)
    observed_at: AwareDatetime
    external_effect_id: str | None = None
    result: ToolResult | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class RecoveryAssessment(CheckpointContract):
    """把账本行和外部观察合并成主图可执行的恢复结论。"""

    idempotency_key: str
    decision: RecoveryDecision
    reason: str = Field(min_length=1)
    external: ExternalExecutionSnapshot


@dataclass(frozen=True)
class EffectReservation:
    """预留结果；``owner`` 只有首次成功插入账本的进程为 True。"""

    entry: EffectLedgerEntry
    owner: bool


class RuntimePersistenceProtocol(Protocol):
    """Checkpoint 与 Effect Ledger 的最小组合接口。"""

    def save_checkpoint(self, checkpoint: CheckpointSnapshot) -> CheckpointSnapshot: ...

    def load_checkpoint(self, run_id: str) -> CheckpointSnapshot | None: ...

    def append_trace_event(self, event: TraceEvent) -> None: ...

    def list_trace_events(self, run_id: str) -> list[TraceEvent]: ...

    def reserve_effect(
        self,
        *,
        run_id: str,
        plan_version: int,
        task_id: str,
        tool_name: ToolName,
        call_id: str,
        input_digest: str,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> EffectReservation: ...

    def get_effect(self, idempotency_key: str) -> EffectLedgerEntry | None: ...

    def complete_effect(
        self,
        idempotency_key: str,
        result: ToolResult,
        *,
        external_effect_id: str | None = None,
        reconciled: bool = False,
        recovery_note: str | None = None,
    ) -> EffectLedgerEntry: ...

    def fail_effect(
        self,
        idempotency_key: str,
        *,
        note: str,
        compensation_required: bool = False,
    ) -> EffectLedgerEntry: ...


class ExternalStateReconcilerProtocol(Protocol):
    """查询仿真/工具真实状态的注入接口。"""

    def inspect(
        self,
        *,
        entry: EffectLedgerEntry,
    ) -> ExternalExecutionSnapshot: ...


class NullExternalStateReconciler:
    """未配置外部适配器时的保守实现；未知状态不能自动跳过副作用。"""

    def inspect(self, *, entry: EffectLedgerEntry) -> ExternalExecutionSnapshot:
        return ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.UNKNOWN,
            source="unconfigured",
            observed_at=datetime.now(timezone.utc),
            details={"tool_name": entry.tool_name.value},
        )


class RecoveryCoordinator:
    """根据账本和真实外部状态计算安全恢复动作。"""

    def __init__(self, reconciler: ExternalStateReconcilerProtocol | None = None) -> None:
        self.reconciler = reconciler or NullExternalStateReconciler()

    def assess(self, entry: EffectLedgerEntry) -> RecoveryAssessment:
        """先查外部事实，再决定继续、跳过、补偿、重规划或人工处理。"""

        external = self.reconciler.inspect(entry=entry)
        if entry.status is EffectLedgerStatus.COMPENSATION_REQUIRED:
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.COMPENSATE,
                reason="账本已标记必须补偿，恢复不能再次派发",
                external=external,
            )
        if external.status is ExternalExecutionStatus.COMPLETED:
            mismatch = self._completion_mismatch(entry, external)
            if mismatch is not None:
                return RecoveryAssessment(
                    idempotency_key=entry.idempotency_key,
                    decision=RecoveryDecision.REPLAN,
                    reason=mismatch,
                    external=external,
                )
            if entry.result is not None or external.result is not None:
                return RecoveryAssessment(
                    idempotency_key=entry.idempotency_key,
                    decision=RecoveryDecision.SKIP_COMPLETED,
                    reason="外部状态已完成，恢复只复用已核对结果",
                    external=external,
                )
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.HUMAN,
                reason="外部已完成但没有可重建的 ToolResult 证据",
                external=external,
            )
        if entry.status in {EffectLedgerStatus.COMPLETED, EffectLedgerStatus.RECONCILED}:
            if external.status is ExternalExecutionStatus.NOT_FOUND:
                return RecoveryAssessment(
                    idempotency_key=entry.idempotency_key,
                    decision=RecoveryDecision.REPLAN,
                    reason="账本声称已完成但外部查询不到副作用，禁止重复派发",
                    external=external,
                )
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.REPLAN,
                reason="已完成账本与外部状态无法一致核对",
                external=external,
            )
        if external.status is ExternalExecutionStatus.NOT_FOUND:
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.CONTINUE,
                reason="外部不存在该副作用，可使用原幂等键继续",
                external=external,
            )
        if external.status is ExternalExecutionStatus.FAILED:
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.COMPENSATE,
                reason="外部已失败，先保留失败证据并进入补偿/重规划",
                external=external,
            )
        if external.status is ExternalExecutionStatus.IN_PROGRESS:
            return RecoveryAssessment(
                idempotency_key=entry.idempotency_key,
                decision=RecoveryDecision.REPLAN,
                reason="外部仍在执行，禁止并发重复派发",
                external=external,
            )
        return RecoveryAssessment(
            idempotency_key=entry.idempotency_key,
            decision=RecoveryDecision.REPLAN,
            reason="外部状态未知，不能仅凭旧 Checkpoint 重试副作用",
            external=external,
        )

    @staticmethod
    def _completion_mismatch(
        entry: EffectLedgerEntry,
        external: ExternalExecutionSnapshot,
    ) -> str | None:
        """核对 completed 观察的 effect、输入和结果身份；任一漂移都不可跳过。"""

        external_result = external.result
        if external.external_effect_id is None:
            return "外部 completed 缺少 external_effect_id，无法证明副作用身份"
        if entry.external_effect_id is not None and entry.external_effect_id != external.external_effect_id:
            return "Effect Ledger 与外部 completed 的 external_effect_id 不一致"
        if external_result is None:
            # 已落账结果可以复用，但外部 ID 必须与账本明确匹配；reserved 行没有
            # ToolResult 时仍由调用方进入 HUMAN 分支，不能凭状态字符串伪造结果。
            return None
        if external_result.tool_name is not entry.tool_name:
            return "外部 ToolResult 的工具名称与 Effect Ledger 不一致"
        if external_result.idempotency_key != entry.idempotency_key:
            return "外部 ToolResult 的幂等键与 Effect Ledger 不一致"
        if external_result.input_digest != entry.input_digest:
            return "外部 ToolResult 的 input_digest 与 Effect Ledger 不一致"
        if external_result.effect_id != external.external_effect_id:
            return "外部 ToolResult.effect_id 与外部状态身份不一致"
        if external_result.output_digest is None:
            return "外部 ToolResult 缺少 output_digest，无法核对结果"
        if (
            entry.result is not None
            and entry.result.output_digest != external_result.output_digest
        ):
            return "Effect Ledger 与外部 completed 的 output_digest 不一致"
        return None


class InMemoryRuntimeStore:
    """供单测使用的 Checkpoint/Effect Ledger 实现。

    该适配器模拟数据库的唯一键和预留语义；它不会被生产组装路径默认使用，因而
    测试若要验证跨进程恢复仍应复用同一个实例或改用 PostgreSQL 实现。
    """

    def __init__(self) -> None:
        self._checkpoints: dict[str, CheckpointSnapshot] = {}
        self._effects: dict[str, EffectLedgerEntry] = {}
        # Trace 按 run 保存独立序列；Checkpoint 只保存恢复所需的最新图状态，
        # 两者分开可避免每一条模型/工具事件都复制整份大状态快照。
        self._trace_events: dict[str, list[TraceEvent]] = {}
        self._lock = RLock()

    def save_checkpoint(self, checkpoint: CheckpointSnapshot) -> CheckpointSnapshot:
        """按 run 覆盖最新快照，并单调分配本地序号。"""

        with self._lock:
            previous = self._checkpoints.get(checkpoint.run_id)
            sequence = (previous.checkpoint_sequence + 1) if previous else 1
            saved = checkpoint.model_copy(update={"checkpoint_sequence": sequence})
            self._checkpoints[checkpoint.run_id] = saved.model_copy(deep=True)
            return saved.model_copy(deep=True)

    def load_checkpoint(self, run_id: str) -> CheckpointSnapshot | None:
        """返回深拷贝，防止恢复调用方原地篡改账本快照。"""

        with self._lock:
            value = self._checkpoints.get(run_id)
            return None if value is None else value.model_copy(deep=True)

    def append_trace_event(self, event: TraceEvent) -> None:
        """原子追加 Trace；相同序号和内容的重试是幂等的，跳号则拒绝。"""

        with self._lock:
            events = self._trace_events.setdefault(event.run_id, [])
            if events:
                last = events[-1]
                if last.trace_id != event.trace_id:
                    raise ValueError("同一 run_id 不能混用不同 trace_id")
            existing = next((item for item in events if item.sequence == event.sequence), None)
            if existing is not None:
                if existing.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise ValueError("同一 Trace 序号不能覆盖不同事件")
                return
            expected = len(events) + 1
            if event.sequence != expected:
                raise ValueError(f"Trace 序号必须连续，期待 {expected}，收到 {event.sequence}")
            events.append(event.model_copy(deep=True))

    def list_trace_events(self, run_id: str) -> list[TraceEvent]:
        """返回运行 Trace 的深拷贝，供报告导出和恢复测试核对。"""

        with self._lock:
            return [item.model_copy(deep=True) for item in self._trace_events.get(run_id, [])]

    def reserve_effect(
        self,
        *,
        run_id: str,
        plan_version: int,
        task_id: str,
        tool_name: ToolName,
        call_id: str,
        input_digest: str,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> EffectReservation:
        """原子模拟唯一键 INSERT；重复请求只返回已有账本行。"""

        timestamp = now or datetime.now(timezone.utc)
        key = make_effect_idempotency_key(run_id, plan_version, task_id)
        with self._lock:
            existing = self._effects.get(key)
            if existing is not None:
                if existing.input_digest != input_digest or existing.tool_name is not tool_name:
                    raise ValueError("相同幂等键不能复用到不同工具或参数")
                return EffectReservation(existing.model_copy(deep=True), owner=False)
            ledger_id = f"effect_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"
            entry = EffectLedgerEntry(
                run_id=run_id,
                plan_version=plan_version,
                task_id=task_id,
                tool_name=tool_name,
                idempotency_key=key,
                ledger_effect_id=ledger_id,
                call_id=call_id,
                input_digest=input_digest,
                arguments=to_jsonable(dict(arguments)),
                status=EffectLedgerStatus.RESERVED,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._effects[key] = entry
            return EffectReservation(entry.model_copy(deep=True), owner=True)

    def get_effect(self, idempotency_key: str) -> EffectLedgerEntry | None:
        """读取单条账本，供重启恢复和反重复测试使用。"""

        with self._lock:
            value = self._effects.get(idempotency_key)
            return None if value is None else value.model_copy(deep=True)

    def complete_effect(
        self,
        idempotency_key: str,
        result: ToolResult,
        *,
        external_effect_id: str | None = None,
        reconciled: bool = False,
        recovery_note: str | None = None,
    ) -> EffectLedgerEntry:
        """只允许把同一幂等键的预留更新为完成，不创建第二条副作用。"""

        with self._lock:
            entry = self._effects.get(idempotency_key)
            if entry is None:
                raise KeyError(f"未知 Effect Ledger 键: {idempotency_key}")
            if result.idempotency_key not in {None, idempotency_key}:
                raise ValueError("ToolResult.idempotency_key 与账本键不一致")
            if result.input_digest != entry.input_digest:
                raise ValueError("ToolResult.input_digest 与 Effect Ledger 不一致")
            if entry.status is EffectLedgerStatus.COMPENSATION_REQUIRED:
                raise ValueError("需要补偿的副作用不能直接标记完成")
            if entry.result is not None:
                if entry.result.output_digest != result.output_digest:
                    raise ValueError("同一副作用键不能覆盖不同结果")
                return entry.model_copy(deep=True)
            updated = entry.model_copy(
                update={
                    "status": EffectLedgerStatus.RECONCILED if reconciled else EffectLedgerStatus.COMPLETED,
                    "result": result.model_copy(deep=True),
                    "external_effect_id": external_effect_id or result.effect_id,
                    "recovery_note": recovery_note,
                    "updated_at": datetime.now(timezone.utc),
                },
                deep=True,
            )
            self._effects[idempotency_key] = updated
            return updated.model_copy(deep=True)

    def fail_effect(
        self,
        idempotency_key: str,
        *,
        note: str,
        compensation_required: bool = False,
    ) -> EffectLedgerEntry:
        """记录失败或需要补偿的账本状态，恢复时不把它误当成可重放成功。"""

        with self._lock:
            entry = self._effects.get(idempotency_key)
            if entry is None:
                raise KeyError(f"未知 Effect Ledger 键: {idempotency_key}")
            updated = entry.model_copy(
                update={
                    "status": EffectLedgerStatus.COMPENSATION_REQUIRED
                    if compensation_required
                    else EffectLedgerStatus.FAILED,
                    "recovery_note": note,
                    "updated_at": datetime.now(timezone.utc),
                },
                deep=True,
            )
            self._effects[idempotency_key] = updated
            return updated.model_copy(deep=True)

    def list_effects(self, run_id: str) -> list[EffectLedgerEntry]:
        """按键稳定返回一个运行的账本，便于验收副作用次数。"""

        with self._lock:
            return [
                value.model_copy(deep=True)
                for value in sorted(self._effects.values(), key=lambda item: item.idempotency_key)
                if value.run_id == run_id
            ]


class InMemoryExternalStateReconciler:
    """可控的外部事实 fake；测试可显式声明 completed/not_found 等状态。"""

    def __init__(self) -> None:
        self._values: dict[str, ExternalExecutionSnapshot] = {}
        self._lock = RLock()

    def put(self, idempotency_key: str, snapshot: ExternalExecutionSnapshot) -> None:
        """按幂等键写入测试外部事实，模拟仿真/工具查询接口。"""

        with self._lock:
            self._values[idempotency_key] = snapshot.model_copy(deep=True)

    def inspect(self, *, entry: EffectLedgerEntry) -> ExternalExecutionSnapshot:
        """未知键返回 unknown，而不是推断为 not_found。"""

        with self._lock:
            value = self._values.get(entry.idempotency_key)
            if value is None:
                return ExternalExecutionSnapshot(
                    status=ExternalExecutionStatus.UNKNOWN,
                    source="in_memory_external",
                    observed_at=datetime.now(timezone.utc),
                )
            return value.model_copy(deep=True)


__all__ = [
    "CheckpointSnapshot",
    "EffectLedgerEntry",
    "EffectLedgerStatus",
    "EffectReservation",
    "ExternalExecutionSnapshot",
    "ExternalExecutionStatus",
    "ExternalStateReconcilerProtocol",
    "InMemoryExternalStateReconciler",
    "InMemoryRuntimeStore",
    "NullExternalStateReconciler",
    "RecoveryAssessment",
    "RecoveryCoordinator",
    "RecoveryDecision",
    "RuntimePersistenceProtocol",
    "canonical_json_digest",
    "make_effect_idempotency_key",
    "to_jsonable",
]
