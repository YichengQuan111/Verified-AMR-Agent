"""P0-15 故障分类、预算门禁与确定性恢复控制器。

本模块把工具、Validator、仿真器和 Checkpoint 恢复产生的不同错误收敛为一组
稳定的故障类别，再用有限预算决定 retry、replan、fallback、human 或 fatal。
它不执行工具，也不替换 P0-14 的 Effect Ledger；局部重规划仍必须调用
``LocalReplanner``，并由调用方在新版本 Checkpoint 中保存结果。

设计上故意不直接导入 ``agent.context.contracts``，避免 RuntimeState ↔ Context
的导入环。``RecoveryUsage`` 与 P0-05 的 ``BudgetUsage`` 保持相同字段语义，
并提供显式转换方法；这样分类器可以在恢复边界独立使用，最终仍能回写同一份
Checkpoint JSON。任何未知错误默认 fatal，外部副作用超时若没有明确的
``not_found`` 核对也不允许盲目重试，防止跨进程状态窗口造成重复派发。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.planning.contracts import ExecutionBudgets, PlanTask, TaskContract
from agent.planning.replanner import (
    AffectedEntitySet,
    LocalReplanResult,
    LocalReplanner,
    TaskResourceProvenance,
)
from agent.tools.contracts import ToolErrorCategory, ToolName, ToolResult

if TYPE_CHECKING:
    from agent.context.contracts import BudgetUsage
    from agent.planning.validator import PlanValidationResult
    from agent.runtime.checkpoint import CheckpointSnapshot, RuntimePersistenceProtocol
    from agent.runtime.state import RunState
    from agent.context.contracts import ReplanOutput
    from agent.tools.contracts import ToolSpec


class FaultContract(BaseModel):
    """P0-15 严格对象基类；故障载荷不能静默扩展恢复权限。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class FaultCategory(str, Enum):
    """跨工具稳定故障分类；枚举值是审计和策略路由的公共契约。"""

    LOW_BATTERY = "low_battery"
    AMR_OFFLINE = "amr_offline"
    CHANNEL_CLOSED = "channel_closed"
    WORKSTATION_OCCUPIED = "workstation_occupied"
    TOOL_TIMEOUT = "tool_timeout"
    PLAN_INFEASIBLE = "plan_infeasible"
    STATE_CONFLICT = "state_conflict"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    """故障处理器允许的五种终止/恢复动作。"""

    RETRY = "retry"
    REPLAN = "replan"
    FALLBACK = "fallback"
    HUMAN = "human"
    FATAL = "fatal"


class RecoveryUsage(FaultContract):
    """P0-15 恢复侧预算用量，与 P0-05 BudgetUsage 字段逐项对应。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_steps: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)

    @classmethod
    def from_budget_usage(cls, usage: Any | None) -> "RecoveryUsage":
        """从 P0-05 BudgetUsage 或其 JSON 快照安全读取恢复计数。"""

        if usage is None:
            return cls()
        if isinstance(usage, cls):
            return usage.model_copy(deep=True)
        if isinstance(usage, BaseModel):
            usage = usage.model_dump(mode="python")
        if not isinstance(usage, Mapping):
            raise TypeError("budget usage 必须是 Pydantic 对象或 JSON 对象")
        return cls.model_validate(dict(usage))

    def to_budget_usage(self) -> "BudgetUsage":
        """显式转换回 P0-05 BudgetUsage，避免两套预算对象隐式漂移。"""

        from agent.context.contracts import BudgetUsage

        return BudgetUsage.model_validate(self.model_dump(mode="python"))


class FaultSignal(FaultContract):
    """一次已归一化的故障信号及其受控影响实体。"""

    fault_id: str = Field(min_length=1, max_length=128)
    category: FaultCategory
    # code 是 P0-15 稳定码；raw_code 保留底层 C++/工具码供证据回溯。
    code: str = Field(min_length=1, max_length=64)
    raw_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    stage: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=128)
    tool_name: ToolName | None = None
    retryable: bool = False
    idempotent: bool = True
    has_side_effects: bool = False
    # side_effect_not_found 是 P0-14 外部核对器的明确事实，默认 False 表示未知。
    side_effect_not_found: bool = False
    affected_entities: AffectedEntitySet = Field(default_factory=AffectedEntitySet)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_signal(self) -> "FaultSignal":
        """故障身份必须稳定，不能用空 ID 或重复证据制造新恢复机会。"""

        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("FaultSignal.evidence_refs 不能重复")
        return self


class FaultPolicy(FaultContract):
    """一类故障的默认动作、升级方向和最终终止动作。"""

    category: FaultCategory
    default_action: RecoveryAction
    exhausted_action: RecoveryAction
    # 当 exhausted_action 本身是 replan 且重规划额度也耗尽时，必须有
    # 明确终态；不能让“重规划额度耗尽”再次返回 replan 形成循环。
    final_action: RecoveryAction = RecoveryAction.HUMAN
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_final_action(self) -> "FaultPolicy":
        """最终动作只能是终态动作，策略表才不会产生隐式继续分支。"""

        if self.final_action not in {
            RecoveryAction.FALLBACK,
            RecoveryAction.HUMAN,
            RecoveryAction.FATAL,
        }:
            raise ValueError("FaultPolicy.final_action 必须是 fallback/human/fatal")
        return self


class FaultDecision(FaultContract):
    """处理一次故障后的决策；终态重复复用，非终态重复继续消耗有限额度。"""

    fault: FaultSignal
    action: RecoveryAction
    terminal: bool
    reason: str = Field(min_length=1)
    retry_count: int = Field(ge=0)
    replan_count: int = Field(ge=0)
    budget_usage: RecoveryUsage

    @model_validator(mode="after")
    def validate_terminal_action(self) -> "FaultDecision":
        """五种动作的终态语义固定，避免调用方把 human/fatal 当成继续信号。"""

        terminal_actions = {
            RecoveryAction.FALLBACK,
            RecoveryAction.HUMAN,
            RecoveryAction.FATAL,
        }
        if self.terminal != (self.action in terminal_actions):
            raise ValueError("FaultDecision.terminal 与 action 不一致")
        return self


class FaultRecoveryResult(FaultContract):
    """局部重规划后的状态和版本化结果，便于事务边界外的调用方落 Checkpoint。"""

    decision: FaultDecision
    state: Any
    replan_result: LocalReplanResult


FAULT_POLICIES: dict[FaultCategory, FaultPolicy] = {
    FaultCategory.LOW_BATTERY: FaultPolicy(
        category=FaultCategory.LOW_BATTERY,
        default_action=RecoveryAction.REPLAN,
        exhausted_action=RecoveryAction.HUMAN,
        final_action=RecoveryAction.HUMAN,
        description="更换健康 AMR 或重新安排充电/路线；不能强行继续原路线。",
    ),
    FaultCategory.AMR_OFFLINE: FaultPolicy(
        category=FaultCategory.AMR_OFFLINE,
        default_action=RecoveryAction.REPLAN,
        exhausted_action=RecoveryAction.HUMAN,
        final_action=RecoveryAction.HUMAN,
        description="隔离离线 AMR，只重建其未完成影响子图。",
    ),
    FaultCategory.CHANNEL_CLOSED: FaultPolicy(
        category=FaultCategory.CHANNEL_CLOSED,
        default_action=RecoveryAction.REPLAN,
        exhausted_action=RecoveryAction.HUMAN,
        final_action=RecoveryAction.HUMAN,
        description="保留已完成锚点，绕开封闭 cell/edge 后重新验证。",
    ),
    FaultCategory.WORKSTATION_OCCUPIED: FaultPolicy(
        category=FaultCategory.WORKSTATION_OCCUPIED,
        default_action=RecoveryAction.RETRY,
        exhausted_action=RecoveryAction.REPLAN,
        final_action=RecoveryAction.HUMAN,
        description="先有限等待/重试；仍占用时重排受影响工位子图。",
    ),
    FaultCategory.TOOL_TIMEOUT: FaultPolicy(
        category=FaultCategory.TOOL_TIMEOUT,
        default_action=RecoveryAction.RETRY,
        exhausted_action=RecoveryAction.FALLBACK,
        final_action=RecoveryAction.FALLBACK,
        description="只对无副作用且幂等工具重试；外部副作用状态未知时停止。",
    ),
    FaultCategory.PLAN_INFEASIBLE: FaultPolicy(
        category=FaultCategory.PLAN_INFEASIBLE,
        default_action=RecoveryAction.REPLAN,
        exhausted_action=RecoveryAction.HUMAN,
        final_action=RecoveryAction.HUMAN,
        description="不得放宽 Validator；仅允许有限局部替换并重新验证。",
    ),
    FaultCategory.STATE_CONFLICT: FaultPolicy(
        category=FaultCategory.STATE_CONFLICT,
        default_action=RecoveryAction.HUMAN,
        exhausted_action=RecoveryAction.HUMAN,
        final_action=RecoveryAction.HUMAN,
        description="Effect/Checkpoint/外部身份冲突不可猜测，交由人工核对。",
    ),
    FaultCategory.UNKNOWN: FaultPolicy(
        category=FaultCategory.UNKNOWN,
        default_action=RecoveryAction.FATAL,
        exhausted_action=RecoveryAction.FATAL,
        final_action=RecoveryAction.FATAL,
        description="未知故障 fail closed，避免未分类异常进入自动恢复。",
    ),
}


def fault_classification_table() -> tuple[FaultPolicy, ...]:
    """按稳定枚举顺序导出分类表，文档和测试不依赖 dict 插入顺序。"""

    return tuple(FAULT_POLICIES[item] for item in FaultCategory)


def _jsonable(value: Any) -> Any:
    """把错误载荷压缩成有限 JSON，拒绝把异常对象或路径带入恢复信号。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


def _canonical_digest(value: Any) -> str:
    """用规范 JSON 生成故障去重 ID；相同事实不能反复消耗预算。"""

    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_mappings(value: Any) -> Iterable[tuple[str, Any]]:
    """遍历有限错误对象的键值，不执行任何字符串表达式。"""

    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_mappings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_mappings(item)


def _text_tokens(value: Any) -> str:
    """提取用于分类的低熵文本；完整正文仍只保留在原始证据里。"""

    pieces: list[str] = []
    for key, item in _walk_mappings(value):
        if isinstance(item, (str, int, float, bool)):
            pieces.append(f"{key}:{item}")
    if isinstance(value, str):
        pieces.append(value)
    return " ".join(pieces).lower().replace("-", "_")


def _first_string(mapping: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    """从一组稳定字段中取第一个非空字符串。"""

    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class FaultClassifier:
    """把 ToolResult、ToolError、异常或结构化输出映射为 FaultSignal。"""

    @classmethod
    def classify(
        cls,
        source: Any,
        *,
        stage: str | None = None,
        task_id: str | None = None,
        tool_name: ToolName | str | None = None,
        idempotent: bool = True,
        has_side_effects: bool = False,
        side_effect_not_found: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> FaultSignal:
        """按稳定优先级分类；更具体的资源故障优先于泛化 unsafe_plan。"""

        source_mapping = (
            source.model_dump(mode="json", exclude_none=False)
            if isinstance(source, BaseModel)
            else dict(source)
            if isinstance(source, Mapping)
            else {}
        )
        raw_error = getattr(source, "error", None)
        error_mapping = raw_error.model_dump(mode="json", exclude_none=False) if isinstance(raw_error, BaseModel) else {}
        if not error_mapping and isinstance(source, Mapping):
            error_mapping = dict(source.get("error", {})) if isinstance(source.get("error"), Mapping) else {}
        raw_code = (
            _first_string(error_mapping, ("code",))
            or _first_string(source_mapping, ("code", "reason_code"))
            or str(getattr(source, "code", "unknown"))
        )
        message = (
            _first_string(error_mapping, ("message",))
            or _first_string(source_mapping, ("message", "reason", "blocked_reason"))
            or str(source)
        )
        raw_category = (
            _first_string(error_mapping, ("category",))
            or _first_string(source_mapping, ("category",))
            or str(getattr(raw_error, "category", ""))
            or str(getattr(source, "category", ""))
        ).lower()
        payload: dict[str, Any] = {
            "source": source_mapping or _jsonable(source),
            "error": error_mapping,
            "details": _jsonable(details or {}),
            "raw_code": raw_code,
            "message": message,
        }
        text = _text_tokens(payload)
        category = cls._category(raw_category, raw_code, text, source_mapping)
        source_tool = tool_name or source_mapping.get("tool_name")
        parsed_tool: ToolName | None
        try:
            parsed_tool = source_tool if isinstance(source_tool, ToolName) else ToolName(source_tool) if source_tool else None
        except ValueError:
            parsed_tool = None
        if parsed_tool in {
            ToolName.DISPATCH_SIMULATION,
            ToolName.REQUEST_APPROVAL,
            ToolName.RUN_VERIFICATION_SUITE,
        }:
            # 调用方若没有显式提供 ToolSpec，也不能把已知副作用工具当成纯读
            # 操作来重试；P0-14 的 Effect Ledger 仍需先完成外部核对。
            has_side_effects = True
        source_retryable = getattr(raw_error, "retryable", None)
        if source_retryable is None and isinstance(raw_error, Mapping):
            source_retryable = raw_error.get("retryable")
        if source_retryable is None:
            source_retryable = source_mapping.get("retryable")
        if source_retryable is None:
            source_retryable = category in {FaultCategory.TOOL_TIMEOUT, FaultCategory.WORKSTATION_OCCUPIED}
        evidence = source_mapping.get("evidence_refs", [])
        if not isinstance(evidence, list):
            evidence = []
        entity_strings = cls._entity_strings(payload)
        try:
            entities = AffectedEntitySet.from_strings(entity_strings)
        except ValueError:
            # 错误证据中的越界坐标不能阻断错误分类；保留任务/工具等安全 ID，
            # 让后续 Replanner 再用地图契约拒绝不合法几何替换。
            entities = AffectedEntitySet.from_strings(
                [item for item in entity_strings if not item.startswith(("cell:", "edge:"))]
            )
        stable_payload = {
            "category": category.value,
            "raw_code": raw_code,
            "stage": stage,
            "task_id": task_id or _first_string(source_mapping, ("task_id", "related_task_id")),
            "tool_name": parsed_tool.value if parsed_tool else None,
            "entities": entities.model_dump(mode="json"),
        }
        fault_id = f"p015:{_canonical_digest(stable_payload)[:32]}"
        return FaultSignal(
            fault_id=fault_id,
            category=category,
            code=category.value,
            raw_code=raw_code,
            message=message,
            stage=stage,
            task_id=task_id or _first_string(source_mapping, ("task_id", "related_task_id")),
            tool_name=parsed_tool,
            retryable=bool(source_retryable),
            idempotent=idempotent,
            has_side_effects=has_side_effects,
            side_effect_not_found=side_effect_not_found,
            affected_entities=entities,
            details=_jsonable({
                **(dict(error_mapping.get("details", {})) if isinstance(error_mapping.get("details"), Mapping) else {}),
                **(dict(source_mapping.get("details", {})) if isinstance(source_mapping.get("details"), Mapping) else {}),
                **(
                    {"duration_ms": source_mapping["duration_ms"]}
                    if isinstance(source_mapping.get("duration_ms"), (int, float))
                    and not isinstance(source_mapping.get("duration_ms"), bool)
                    else {}
                ),
                **dict(details or {}),
                "raw_category": raw_category,
                "raw_code": raw_code,
            }),
            evidence_refs=list(dict.fromkeys(str(item) for item in evidence if isinstance(item, str))),
        )

    @staticmethod
    def _category(
        raw_category: str,
        raw_code: str,
        text: str,
        source_mapping: Mapping[str, Any],
    ) -> FaultCategory:
        """故障分类优先级：冲突/超时/资源事实/不可行/未知。"""

        code = raw_code.lower().replace("-", "_")
        if raw_category == ToolErrorCategory.CONFLICT.value or any(
            token in code for token in ("state_conflict", "checkpoint_conflict", "effect_conflict", "idempotency_key_reused")
        ):
            return FaultCategory.STATE_CONFLICT
        if raw_category == ToolErrorCategory.TIMEOUT.value or "timeout" in code or "timeout" in text or "timed_out" in text:
            return FaultCategory.TOOL_TIMEOUT
        resource_text = " ".join((code, text))
        if any(token in resource_text for token in (
            "low_battery", "battery_low", "battery_drain", "battery_safety_reserve", "battery_below", "battery_critical", "amr_battery",
        )):
            return FaultCategory.LOW_BATTERY
        if any(token in resource_text for token in (
            "amr_offline", "connection_offline", "connection_lost", "amr_unavailable", "offline",
        )):
            return FaultCategory.AMR_OFFLINE
        if any(token in resource_text for token in (
            "workstation_occupied", "workstation_capacity", "station_occupied", "workstation_full",
        )):
            return FaultCategory.WORKSTATION_OCCUPIED
        if any(token in resource_text for token in (
            "channel_closed", "channel_blocked", "blocked_channel", "blocked_edge", "forbidden_edge", "blocked_cell", "forbidden_zone", "one_way_violation",
        )):
            return FaultCategory.CHANNEL_CLOSED
        if raw_category == ToolErrorCategory.UNSAFE_PLAN.value or any(token in resource_text for token in (
            "infeasible", "plan_invalid", "validator_postcondition", "unsafe_plan", "route_not_planned", "vertex_conflict", "swap_edge_conflict", "safety_distance",
        )) or source_mapping.get("status") == "infeasible":
            return FaultCategory.PLAN_INFEASIBLE
        if raw_category == ToolErrorCategory.CONFLICT.value:
            return FaultCategory.STATE_CONFLICT
        return FaultCategory.UNKNOWN

    @staticmethod
    def _entity_strings(payload: Mapping[str, Any]) -> list[str]:
        """从 Validator/仿真证据提取可被 LocalReplanner 精确匹配的标签。"""

        def position_text(value: Any) -> str | None:
            """只接受严格整数坐标，避免错误载荷伪造几何影响集合。"""

            if not isinstance(value, Mapping):
                return None
            x, y = value.get("x"), value.get("y")
            if not (isinstance(x, int) and not isinstance(x, bool)):
                return None
            if not (isinstance(y, int) and not isinstance(y, bool)):
                return None
            return f"{x},{y}"

        values: list[str] = []
        for key, value in _walk_mappings(payload):
            key_lower = key.lower()
            if isinstance(value, str) and value.strip():
                if key_lower in {"amr_id", "related_amr_id"}:
                    values.append(f"amr:{value}")
                elif key_lower in {"workstation_id", "workstation", "station_id"}:
                    values.append(f"workstation:{value}")
                elif key_lower in {"channel_id", "channel"}:
                    values.append(f"channel:{value}")
                elif key_lower in {"task_id", "related_task_id"}:
                    values.append(f"task:{value}")
            coordinate = position_text(value)
            if coordinate is not None:
                values.append(f"cell:{coordinate}")
            if isinstance(value, Mapping):
                source_position = position_text(value.get("from"))
                target_position = position_text(value.get("to"))
                if source_position is not None and target_position is not None:
                    # P0-14 的 blocked edge 需要完整有向边，而不是两个孤立 cell；
                    # 这样 LocalReplanner 才能精确失效对应路线，避免扩大重规划范围。
                    values.append(f"edge:{source_position}->{target_position}")
        return list(dict.fromkeys(values))


class FaultRecoveryController:
    """执行 P0-15 的有限状态恢复决策，并适配 P0-14 LocalReplanner。"""

    def __init__(
        self,
        contract: TaskContract,
        *,
        usage: Any | None = None,
        run_state: "RunState | None" = None,
        replanner: LocalReplanner | None = None,
        clock: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.contract = contract
        initial_usage = RecoveryUsage.from_budget_usage(usage)
        if run_state is not None:
            initial_usage.replans = max(initial_usage.replans, int(run_state.replan_count))
            initial_usage.retries = max(initial_usage.retries, int(getattr(run_state, "retry_count", 0)))
        self._usage = initial_usage
        self.run_state = run_state
        self.replanner = replanner or LocalReplanner()
        self._clock = clock
        self._decisions: dict[str, FaultDecision] = {}

    @property
    def budget_usage(self) -> RecoveryUsage:
        """返回深拷贝式预算快照，调用方不能通过它回写内部计数。"""

        return self._usage.model_copy(deep=True)

    def handle_failure(
        self,
        source: Any,
        *,
        stage: str | None = None,
        task_id: str | None = None,
        tool_name: ToolName | str | None = None,
        idempotent: bool = True,
        has_side_effects: bool = False,
        side_effect_not_found: bool = False,
        details: Mapping[str, Any] | None = None,
        usage: Any | None = None,
    ) -> FaultDecision:
        """分类并消费一次恢复预算；终态重复幂等，非终态重复继续消耗额度。"""

        attached_fault = getattr(source, "fault", None)
        if isinstance(source, FaultSignal):
            # 已归一化信号不能再次生成新 fault_id；重复上报必须命中同一预算记录。
            signal = source.model_copy(deep=True)
        elif isinstance(attached_fault, FaultSignal):
            # PEVRExecutionError 已经在产生点绑定了任务/工具/Effect 证据，
            # 恢复层直接消费该信号，不能重新从 message 猜测并丢失影响集合。
            signal = attached_fault.model_copy(deep=True)
        else:
            signal = FaultClassifier.classify(
                source,
                stage=stage,
                task_id=task_id,
                tool_name=tool_name,
                idempotent=idempotent,
                has_side_effects=has_side_effects,
                side_effect_not_found=side_effect_not_found,
                details=details,
            )
        previous = self._decisions.get(signal.fault_id)
        if previous is not None and previous.terminal:
            # 终态重复上报必须幂等；调用方不能用网络重试把 FAILED 变回执行中。
            return previous.model_copy(deep=True)
        # 同一 fault_id 的非终态重复表示“上一有限 retry/replan 仍失败”，
        # 继续走同一预算门禁而不是无限复用 RETRY/REPLAN。Effect Ledger 的
        # 业务键仍负责避免副作用重放；这里只推进有限决策计数。
        if usage is not None:
            self._usage = RecoveryUsage.from_budget_usage(usage)
        elif isinstance(source, ToolResult):
            self._usage = self._account_tool_result(self._usage, source)
        elif signal.tool_name is not None:
            # 只有带工具身份的原始异常才自动计入失败步骤；纯 Validator/合同错误
            # 不凭空增加工具步。这样 raw Mapping 和 PEVRExecutionError 也不能
            # 通过省略 ToolResult 绕过总步骤上限。
            duration_ms = signal.details.get("duration_ms", 0)
            self._usage = self._account_failure(
                self._usage,
                duration_ms if isinstance(duration_ms, (int, float)) else 0,
            )
        decision = self._decide(signal)
        self._decisions[signal.fault_id] = decision
        self._usage = decision.budget_usage.model_copy(deep=True)
        return decision.model_copy(deep=True)

    def _decide(self, signal: FaultSignal) -> FaultDecision:
        """按总预算→策略→安全属性的固定顺序生成动作。"""

        budget_reason = self._budget_exhausted_reason()
        if budget_reason is not None:
            return self._make_decision(
                signal,
                RecoveryAction.FALLBACK,
                f"{budget_reason}；禁止继续恢复",
            )
        policy = FAULT_POLICIES[signal.category]
        action = policy.default_action
        reason = policy.description
        if action is RecoveryAction.RETRY:
            if signal.has_side_effects and not signal.side_effect_not_found:
                action = RecoveryAction.HUMAN
                reason = "副作用工具超时/失败且外部状态未明确 not_found，禁止重复派发"
            elif not signal.retryable or not signal.idempotent:
                action = policy.exhausted_action
                reason = "错误不可安全重试，进入该类别的升级动作"
            elif self._usage.retries >= self.contract.budgets.max_retries:
                action = policy.exhausted_action
                reason = "普通重试额度已耗尽，进入该类别的升级动作"
            else:
                self._usage = self._usage.model_copy(update={"retries": self._usage.retries + 1})
                reason = f"允许第 {self._usage.retries} 次有限重试"
        if action is RecoveryAction.REPLAN:
            if self._usage.replans >= self.contract.budgets.max_replans:
                action = policy.final_action
                reason = f"局部重规划额度已耗尽，最终动作 {action.value}"
            else:
                self._usage = self._usage.model_copy(update={"replans": self._usage.replans + 1})
                reason = f"允许第 {self._usage.replans} 次局部重规划"
        return self._make_decision(signal, action, reason)

    def _make_decision(
        self,
        signal: FaultSignal,
        action: RecoveryAction,
        reason: str,
    ) -> FaultDecision:
        """集中构造动作结果，保证终态布尔值不可漂移。"""

        return FaultDecision(
            fault=signal,
            action=action,
            terminal=action in {RecoveryAction.FALLBACK, RecoveryAction.HUMAN, RecoveryAction.FATAL},
            reason=reason,
            retry_count=self._usage.retries,
            replan_count=self._usage.replans,
            budget_usage=self._usage.model_copy(deep=True),
        )

    def _budget_exhausted_reason(self) -> str | None:
        """总步骤、时长和 Token 是恢复前的硬门禁。"""

        limits = self.contract.budgets
        usage = self._usage
        if usage.tool_steps >= limits.max_tool_steps:
            return "总工具步骤预算已耗尽"
        if usage.elapsed_seconds >= limits.max_total_seconds:
            return "总时长预算已耗尽"
        if usage.input_tokens >= limits.max_input_tokens:
            return "总输入 Token 预算已耗尽"
        if usage.output_tokens >= limits.max_output_tokens:
            return "总输出 Token 预算已耗尽"
        return None

    @staticmethod
    def _account_tool_result(usage: RecoveryUsage, result: ToolResult) -> RecoveryUsage:
        """将失败工具本身计入总步骤/时长，避免失败重试绕过上限。"""

        return FaultRecoveryController._account_failure(usage, result.duration_ms)

    @staticmethod
    def _account_failure(usage: RecoveryUsage, duration_ms: int | float) -> RecoveryUsage:
        """把一个已确认的工具失败计入步骤和时长，兼容非 ToolResult 适配器。"""

        return usage.model_copy(
            update={
                "tool_steps": usage.tool_steps + 1,
                "elapsed_seconds": usage.elapsed_seconds + max(0.0, float(duration_ms)) / 1000.0,
            }
        )

    def record_on_run_state(self, state: "RunState", decision: FaultDecision) -> "RunState":
        """把决策写回 RunState，形成跨 Checkpoint 的故障去重和终止事实。"""

        from agent.planning.contracts import PlanTaskStatus
        from agent.runtime.state import RunStatus, FaultRecord

        if self.run_state is not None and state.run_id != self.run_state.run_id:
            raise ValueError("FaultRecoveryController 与 RunState.run_id 不一致")
        if state.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            # 终态 Checkpoint 不能被网络重试或迟到故障重新打开；完成状态尤其
            # 不能回写成 FAILED，否则会破坏 P0-14 已核对副作用的最终事实。
            if decision.terminal and any(
                item.fault_id == decision.fault.fault_id for item in state.fault_history
            ):
                return state.model_copy(deep=True)
            raise ValueError("终态 RunState 不能再次进入故障恢复")
        payload = state.model_dump(mode="python")
        payload["retry_count"] = max(state.retry_count, decision.retry_count)
        # replan_count 只有 LocalReplanner 真正生成版本 +1 后才增加；本方法也
        # 支持先记录“准备重规划”再调用 apply_replan，因此不能把未来计数提前
        # 写进 RunState。已有记录在 apply_replan 后会被下面分支补齐到实际版本。
        record_replan_count = state.replan_count
        record = FaultRecord(
            fault_id=decision.fault.fault_id,
            category=decision.fault.category.value,
            code=decision.fault.code,
            action=decision.action.value,
            task_id=decision.fault.task_id,
            tool_name=decision.fault.tool_name,
            observed_at=self._clock(),
            retry_count=decision.retry_count,
            replan_count=record_replan_count,
            terminal=decision.terminal,
        )
        history = list(payload.get("fault_history", []))
        found = False
        for index, item in enumerate(history):
            existing = item if isinstance(item, FaultRecord) else FaultRecord.model_validate(item)
            if existing.fault_id != decision.fault.fault_id:
                continue
            # 同一事实只保留一条审计记录，但其动作和已实际消耗的预算会
            # 随状态推进更新；这样既防重复日志，又不会把非终态循环隐藏起来。
            history[index] = record.model_copy(
                update={
                    "observed_at": existing.observed_at,
                    "retry_count": max(existing.retry_count, record.retry_count),
                    "replan_count": max(existing.replan_count, record.replan_count),
                }
            )
            found = True
            break
        if not found:
            history.append(record)
        payload["fault_history"] = history
        if decision.action is RecoveryAction.REPLAN:
            payload["status"] = RunStatus.REPLANNING
            payload["current_task_id"] = None
        elif decision.action is RecoveryAction.RETRY:
            payload["status"] = RunStatus.EXECUTING
        else:
            payload["status"] = RunStatus.FAILED
            payload["current_task_id"] = None
            failed_id = decision.fault.task_id
            if failed_id and failed_id in {task.task_id for task in state.plan_tasks}:
                tasks: list[PlanTask] = []
                for task in state.plan_tasks:
                    if task.task_id != failed_id or task.status is PlanTaskStatus.COMPLETED:
                        tasks.append(task)
                        continue
                    tasks.append(task.model_copy(update={"status": PlanTaskStatus.FAILED}))
                payload["plan_tasks"] = tasks
                payload["failed_task_ids"] = sorted(set(state.failed_task_ids) | {failed_id})
        return type(state).model_validate(payload)

    def apply_replan(
        self,
        state: "RunState",
        plan: Any,
        decision: FaultDecision,
        replacement_tasks: Iterable[PlanTask],
        *,
        tool_specs: Iterable["ToolSpec"],
        expected_seed: int,
        runtime_resources: Iterable[TaskResourceProvenance] = (),
    ) -> FaultRecoveryResult:
        """用确定性 LocalReplanner 应用替换子图，保留完成任务和 Effect Ledger 锚点。"""

        if decision.action is not RecoveryAction.REPLAN:
            raise ValueError("只有 action=replan 的故障决策可以应用局部重规划")
        result = self.replanner.apply(
            plan,
            self.replanner.analyze(
                plan,
                completed_task_ids=state.completed_task_ids,
                affected_entities=decision.fault.affected_entities,
                failed_task_id=decision.fault.task_id,
                failed_tool_name=decision.fault.tool_name,
                runtime_resources=runtime_resources,
            ),
            replacement_tasks,
            reason=decision.reason,
            contract=self.contract,
            tool_specs=tool_specs,
            expected_seed=expected_seed,
        )
        updated = self.replanner.apply_to_run_state(state, result, updated_at=self._clock())
        updated = self.record_on_run_state(updated, decision)
        return FaultRecoveryResult(decision=decision, state=updated, replan_result=result)

    def apply_model_replan(
        self,
        state: "RunState",
        plan: Any,
        output: "ReplanOutput",
        decision: FaultDecision,
        *,
        tool_specs: Iterable["ToolSpec"],
        expected_seed: int,
        runtime_resources: Iterable[TaskResourceProvenance] = (),
    ) -> FaultRecoveryResult:
        """验证模型 ReplanOutput 与确定性影响集合一致后再落版本。"""

        if decision.action is not RecoveryAction.REPLAN:
            raise ValueError("只有 action=replan 的故障决策可以应用模型重规划")
        result = self.replanner.apply_model_output(
            plan,
            output,
            completed_task_ids=state.completed_task_ids,
            affected_entities=decision.fault.affected_entities,
            failed_task_id=decision.fault.task_id,
            failed_tool_name=decision.fault.tool_name,
            runtime_resources=runtime_resources,
            contract=self.contract,
            tool_specs=tool_specs,
            expected_seed=expected_seed,
        )
        updated = self.replanner.apply_to_run_state(state, result, updated_at=self._clock())
        updated = self.record_on_run_state(updated, decision)
        return FaultRecoveryResult(decision=decision, state=updated, replan_result=result)

    def save_replan_checkpoint(
        self,
        store: "RuntimePersistenceProtocol",
        *,
        request: Any,
        state: "RunState",
        graph_state: Mapping[str, Any],
        stage: str = "replanning",
    ) -> "CheckpointSnapshot":
        """按 P0-14 同一 store 写新版本快照；旧 Effect Ledger 不会被删除。"""

        from agent.runtime.checkpoint import CheckpointSnapshot, to_jsonable

        checkpoint = CheckpointSnapshot(
            checkpoint_id=f"cp_p015_{state.run_id}_{state.plan_version}_{len(state.fault_history)}",
            run_id=state.run_id,
            stage=stage,
            status=state.status.value,
            plan_version=state.plan_version,
            current_task_id=state.current_task_id,
            graph_state=dict(to_jsonable(graph_state)),
            saved_at=self._clock(),
        )
        return store.save_checkpoint(checkpoint)


__all__ = [
    "FAULT_POLICIES",
    "FaultCategory",
    "FaultClassifier",
    "FaultDecision",
    "FaultPolicy",
    "FaultRecoveryController",
    "FaultRecoveryResult",
    "FaultSignal",
    "RecoveryAction",
    "RecoveryUsage",
    "fault_classification_table",
]
