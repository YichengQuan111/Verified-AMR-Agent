"""P0-14 PostgreSQL Checkpoint 与 Effect Ledger 应用服务。

该服务复用 P0-06 的 ``runs``、``plans``、``tasks``、``tool_calls``、``effects`` 和
``events``，没有另造一套进程内状态数据库。Checkpoint 更新、计划版本保存、工具
预留和审计事件都在明确的 Service 事务中完成；Repository 只提供查询/挂载对象，
这样进程中断后可以从 PostgreSQL 读取最后一致快照，而不是依赖 Python 堆内存。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent.context import PlanTasksOutput
from agent.planning import PlanTask, TaskContract
from agent.runtime.state import RunState
from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    EffectLedgerEntry,
    EffectLedgerStatus,
    EffectReservation,
    RuntimePersistenceProtocol,
    canonical_json_digest,
    make_effect_idempotency_key,
    to_jsonable,
)
from agent.tools.contracts import ToolName, ToolResult
from pydantic import BaseModel, JsonValue
from services.application.exceptions import (
    PersistenceConflictError,
    ResourceNotFoundError,
)
from services.persistence import (
    EffectRecord,
    EffectRepository,
    EventRecord,
    EventRepository,
    PlanRecord,
    PlanRepository,
    RunRecord,
    RunRepository,
    SessionFactory,
    TaskRecord,
    TaskRepository,
    ToolCallRecord,
)


IdentifierFactory = Callable[[str], str]
Clock = Callable[[], datetime]


def _new_identifier(kind: str) -> str:
    """生成数据库主键；具体业务幂等键仍由三元组函数决定。"""

    return f"{kind}_{uuid4().hex}"


def _utc_now() -> datetime:
    """集中时钟以便故障恢复测试使用固定时间。"""

    return datetime.now(timezone.utc)


class PostgresRuntimeStore(RuntimePersistenceProtocol):
    """以 P0-06 PostgreSQL 表实现 Checkpoint 和 Effect Ledger。

    ``save_checkpoint`` 会锁定 ``runs`` 行，并把最新图状态写入 ``run_state_snapshot``；
    ``reserve_effect`` 单独提交一个 ``reserved`` 行后才允许外部工具执行，避免把
    长时间工具调用放进数据库事务。唯一约束冲突会重新读取赢家行，不会再次执行
    handler。未知或损坏的快照直接报错，防止恢复逻辑默默猜测状态。
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        identifier_factory: IdentifierFactory = _new_identifier,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._new_id = identifier_factory
        self._clock = clock

    def ensure_run(self, run_id: str, task_contract: TaskContract) -> None:
        """确保 PEVR 运行已经有可被 Checkpoint 更新的 ``runs`` 行。"""

        if len(run_id) > 64:
            raise ValueError("PostgreSQL runs.run_id 最长为 64 个字符")
        now = self._clock()
        try:
            with self._session_factory() as session:
                with session.begin():
                    run = RunRepository(session).get(run_id, for_update=True)
                    if run is not None:
                        self._assert_run_contract(run, task_contract)
                        return
                    status = "waiting_approval" if task_contract.approval.required else "planning"
                    run = RunRecord(
                        run_id=run_id,
                        run_kind="agent",
                        status=status,
                        contract_id=task_contract.contract_id,
                        environment_ref=task_contract.environment_ref,
                        plan_version=0,
                        current_task_id=None,
                        task_contract_snapshot=task_contract.model_dump(mode="json"),
                        run_state_snapshot={},
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(run)
                    session.flush()
                    events = EventRepository(session)
                    events.add(
                        EventRecord(
                            event_id=self._new_id("event"),
                            run_id=run_id,
                            sequence_no=events.next_sequence(run_id),
                            event_type="run.created",
                            node_name="p014_checkpoint",
                            task_id=None,
                            severity="info",
                            payload={
                                "run_kind": "agent",
                                "status": status,
                                "contract_id": task_contract.contract_id,
                            },
                            created_at=now,
                        )
                    )
                    session.flush()
        except IntegrityError as exc:
            raise PersistenceConflictError("初始化 Checkpoint 运行时发生并发冲突") from exc

    def save_checkpoint(self, checkpoint: CheckpointSnapshot) -> CheckpointSnapshot:
        """原子保存最新状态、计划快照和审计事件。"""

        now = checkpoint.saved_at
        try:
            with self._session_factory() as session:
                with session.begin():
                    run = RunRepository(session).get(checkpoint.run_id, for_update=True)
                    if run is None:
                        raise ResourceNotFoundError(f"运行不存在: {checkpoint.run_id}")
                    if run.plan_version > checkpoint.plan_version:
                        raise PersistenceConflictError(
                            f"Checkpoint 计划版本 {checkpoint.plan_version} 落后于数据库版本 {run.plan_version}"
                        )

                    plan = self._plan_from_graph_state(checkpoint.graph_state)
                    if plan is not None:
                        self._persist_plan_if_missing(
                            session,
                            run_id=checkpoint.run_id,
                            plan=plan,
                            now=now,
                        )
                        task_state = self._task_state_from_graph_state(
                            checkpoint.graph_state,
                            fallback=plan.tasks,
                        )
                        self._sync_task_records(
                            session,
                            run_id=checkpoint.run_id,
                            plan=plan,
                            tasks=task_state,
                            now=now,
                        )

                    events = EventRepository(session)
                    sequence = events.next_sequence(checkpoint.run_id)
                    saved = checkpoint.model_copy(update={"checkpoint_sequence": sequence})
                    run.run_state_snapshot = saved.model_dump(mode="json")
                    run.status = checkpoint.status
                    run.plan_version = checkpoint.plan_version
                    run.current_task_id = checkpoint.current_task_id
                    run.updated_at = now
                    if checkpoint.status in {"completed", "failed", "cancelled"}:
                        run.completed_at = now
                    events.add(
                        EventRecord(
                            event_id=self._new_id("event"),
                            run_id=checkpoint.run_id,
                            sequence_no=sequence,
                            event_type="checkpoint.saved",
                            node_name=checkpoint.stage,
                            task_id=checkpoint.current_task_id,
                            severity="info",
                            payload={
                                "checkpoint_id": checkpoint.checkpoint_id,
                                "checkpoint_sequence": sequence,
                                "stage": checkpoint.stage,
                                "status": checkpoint.status,
                                "plan_version": checkpoint.plan_version,
                            },
                            created_at=now,
                        )
                    )
                    session.flush()
                    return saved
        except IntegrityError as exc:
            raise PersistenceConflictError("保存 Checkpoint/计划时发生约束冲突，事务已回滚") from exc

    def load_checkpoint(self, run_id: str) -> CheckpointSnapshot | None:
        """读取并重新验证最后快照；空快照表示 P0-06 尚未进入图执行。"""

        with self._session_factory() as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                raise ResourceNotFoundError(f"运行不存在: {run_id}")
            if not run.run_state_snapshot:
                return None
            try:
                return CheckpointSnapshot.model_validate(run.run_state_snapshot)
            except Exception as exc:
                raise PersistenceConflictError("数据库中的 Checkpoint 快照无法通过当前契约") from exc

    def reserve_effect(
        self,
        *,
        run_id: str,
        plan_version: int,
        task_id: str,
        tool_name: ToolName,
        call_id: str,
        input_digest: str,
        arguments: Mapping[str, object],
        now: datetime | None = None,
    ) -> EffectReservation:
        """以唯一约束预留副作用；并发失败方只读取赢家，绝不重放 handler。"""

        key = make_effect_idempotency_key(run_id, plan_version, task_id)
        timestamp = now or self._clock()
        persisted_call_id = self._bounded_call_id(call_id)
        try:
            with self._session_factory() as session:
                with session.begin():
                    effects = EffectRepository(session)
                    existing = effects.get_by_idempotency_key(key, for_update=True)
                    if existing is not None:
                        self._assert_effect_request(existing, tool_name, input_digest)
                        return EffectReservation(self._effect_entry(existing), owner=False)

                    task = TaskRepository(session).get_by_business_key(
                        run_id,
                        plan_version,
                        task_id,
                    )
                    tool_call = ToolCallRecord(
                        call_id=persisted_call_id,
                        run_id=run_id,
                        task_record_id=task.task_record_id if task is not None else None,
                        task_id=task_id,
                        plan_version=plan_version,
                        tool_name=tool_name.value,
                        status="reserved",
                        attempt=1,
                        tool_arguments=to_jsonable(dict(arguments)),
                        result_snapshot=None,
                        error_category=None,
                        effect_id=None,
                        started_at=timestamp,
                    )
                    session.add(tool_call)
                    session.flush()
                    ledger_id = f"effect_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"
                    effect = EffectRecord(
                        effect_id=ledger_id,
                        run_id=run_id,
                        task_record_id=task.task_record_id if task is not None else None,
                        task_id=task_id,
                        plan_version=plan_version,
                        tool_call_id=persisted_call_id,
                        idempotency_key=key,
                        status=EffectLedgerStatus.RESERVED.value,
                        payload_snapshot={
                            "tool_name": tool_name.value,
                            "call_id": call_id,
                            "input_digest": input_digest,
                            "arguments": to_jsonable(dict(arguments)),
                            "result": None,
                            "external_effect_id": None,
                            "recovery_note": None,
                            "started_at": timestamp.isoformat(),
                        },
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    effects.add(effect)
                    session.flush()
                    return EffectReservation(self._effect_entry(effect), owner=True)
        except IntegrityError:
            # 唯一约束是并发协调点；冲突后新 Session 才能看到已提交的赢家行。
            with self._session_factory() as session:
                existing = EffectRepository(session).get_by_idempotency_key(key)
                if existing is None:
                    raise PersistenceConflictError("Effect Ledger 唯一键冲突但无法读取赢家行")
                self._assert_effect_request(existing, tool_name, input_digest)
                return EffectReservation(self._effect_entry(existing), owner=False)

    def get_effect(self, idempotency_key: str) -> EffectLedgerEntry | None:
        """读取一个业务幂等键的最新账本状态。"""

        with self._session_factory() as session:
            record = EffectRepository(session).get_by_idempotency_key(idempotency_key)
            return None if record is None else self._effect_entry(record)

    def list_effects(self, run_id: str) -> list[EffectLedgerEntry]:
        """按数据库唯一键顺序读取，供恢复审计统计副作用次数。"""

        with self._session_factory() as session:
            return [self._effect_entry(item) for item in EffectRepository(session).list_for_run(run_id)]

    def put(
        self,
        run_id: str,
        snapshot: BaseModel | Mapping[str, JsonValue],
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """在副作用 handler 返回前持久化真实仿真快照。

        该事务刻意早于 ``complete_effect``：若进程在 handler 返回与账本完成之间
        被杀，新进程仍能通过 query_execution_state 读取真实完成事实。没有业务
        幂等键时拒绝写入，避免生成无法归属到 run/plan/task 的孤儿状态。
        """

        if not idempotency_key:
            raise PersistenceConflictError("PostgreSQL 执行状态必须关联 Effect 幂等键")
        snapshot_payload = to_jsonable(snapshot)
        if not isinstance(snapshot_payload, Mapping):
            raise PersistenceConflictError("外部执行快照必须是 JSON 对象")
        new_value = {
            "run_id": run_id,
            "snapshot": dict(snapshot_payload),
            "snapshot_digest": canonical_json_digest(snapshot_payload),
        }
        with self._session_factory() as session:
            with session.begin():
                record = EffectRepository(session).get_by_idempotency_key(
                    idempotency_key,
                    for_update=True,
                )
                if record is None:
                    raise ResourceNotFoundError(f"未知 Effect Ledger 键: {idempotency_key}")
                payload = dict(record.payload_snapshot or {})
                if payload.get("tool_name") != ToolName.DISPATCH_SIMULATION.value:
                    raise PersistenceConflictError("只有 dispatch_simulation 可写执行状态快照")
                existing = payload.get("external_execution")
                if existing is not None:
                    if not isinstance(existing, Mapping) or canonical_json_digest(existing) != canonical_json_digest(new_value):
                        raise PersistenceConflictError("同一 Effect 幂等键不能覆盖不同外部执行快照")
                    return
                payload["external_execution"] = new_value
                record.payload_snapshot = to_jsonable(payload)
                record.updated_at = self._clock()
                session.flush()

    def get(self, run_id: str) -> dict[str, JsonValue] | None:
        """按仿真 ID 读取持久化外部快照；多条归属视为账本损坏。"""

        with self._session_factory() as session:
            records = EffectRepository(session).list_by_external_execution_id(run_id)
            if not records:
                return None
            if len(records) != 1:
                raise PersistenceConflictError(f"外部执行 ID 关联了多条 Effect: {run_id}")
            external = (records[0].payload_snapshot or {}).get("external_execution")
            if not isinstance(external, Mapping):
                raise PersistenceConflictError("Effect 外部执行载荷不是 JSON 对象")
            snapshot = external.get("snapshot")
            digest = external.get("snapshot_digest")
            if not isinstance(snapshot, Mapping) or digest != canonical_json_digest(snapshot):
                raise PersistenceConflictError("Effect 外部执行快照摘要校验失败")
            return deepcopy(dict(snapshot))

    def complete_effect(
        self,
        idempotency_key: str,
        result: ToolResult,
        *,
        external_effect_id: str | None = None,
        reconciled: bool = False,
        recovery_note: str | None = None,
    ) -> EffectLedgerEntry:
        """在原账本行上更新结果；重复完成同一结果是幂等的。"""

        try:
            with self._session_factory() as session:
                with session.begin():
                    record = EffectRepository(session).get_by_idempotency_key(
                        idempotency_key,
                        for_update=True,
                    )
                    if record is None:
                        raise ResourceNotFoundError(f"未知 Effect Ledger 键: {idempotency_key}")
                    if result.idempotency_key not in {None, idempotency_key}:
                        raise PersistenceConflictError("ToolResult 幂等键与 Effect Ledger 不一致")
                    current = self._effect_entry(record)
                    if result.input_digest != current.input_digest:
                        raise PersistenceConflictError("ToolResult input_digest 与 Effect Ledger 不一致")
                    if current.status is EffectLedgerStatus.COMPENSATION_REQUIRED:
                        raise PersistenceConflictError("需要补偿的副作用不能直接标记完成")
                    if current.result is not None:
                        if current.result.output_digest != result.output_digest:
                            raise PersistenceConflictError("同一副作用键不能覆盖不同结果")
                        return current

                    payload = dict(record.payload_snapshot)
                    payload.update(
                        {
                            "result": result.model_dump(mode="json"),
                            "external_effect_id": external_effect_id or result.effect_id,
                            "recovery_note": recovery_note,
                        }
                    )
                    record.payload_snapshot = to_jsonable(payload)
                    record.status = (
                        EffectLedgerStatus.RECONCILED if reconciled else EffectLedgerStatus.COMPLETED
                    ).value
                    record.updated_at = self._clock()
                    if record.tool_call_id:
                        tool_call = session.get(ToolCallRecord, record.tool_call_id)
                        if tool_call is not None:
                            tool_call.status = result.status.value
                            tool_call.result_snapshot = result.model_dump(mode="json")
                            tool_call.error_category = result.error.category.value if result.error else None
                            tool_call.effect_id = result.effect_id
                            tool_call.finished_at = result.finished_at
                            tool_call.duration_ms = result.duration_ms
                    session.flush()
                    return self._effect_entry(record)
        except IntegrityError as exc:
            raise PersistenceConflictError("完成 Effect Ledger 时发生约束冲突") from exc

    def fail_effect(
        self,
        idempotency_key: str,
        *,
        note: str,
        compensation_required: bool = False,
    ) -> EffectLedgerEntry:
        """持久化失败/补偿状态，避免失败预留被误认为可安全重放。"""

        with self._session_factory() as session:
            with session.begin():
                record = EffectRepository(session).get_by_idempotency_key(
                    idempotency_key,
                    for_update=True,
                )
                if record is None:
                    raise ResourceNotFoundError(f"未知 Effect Ledger 键: {idempotency_key}")
                record.status = (
                    EffectLedgerStatus.COMPENSATION_REQUIRED
                    if compensation_required
                    else EffectLedgerStatus.FAILED
                ).value
                payload = dict(record.payload_snapshot)
                payload["recovery_note"] = note
                record.payload_snapshot = to_jsonable(payload)
                record.updated_at = self._clock()
                if record.tool_call_id:
                    tool_call = session.get(ToolCallRecord, record.tool_call_id)
                    if tool_call is not None:
                        tool_call.status = "failed"
                        tool_call.finished_at = self._clock()
                session.flush()
                return self._effect_entry(record)

    def _persist_plan_if_missing(
        self,
        session: Session,
        *,
        run_id: str,
        plan: PlanTasksOutput,
        now: datetime,
    ) -> None:
        """把 Checkpoint 中首次出现的计划版本补写到 P0-06 关系表。"""

        plans = PlanRepository(session)
        if plans.get_by_version(run_id, plan.plan_version) is not None:
            return
        record = PlanRecord(
            plan_id=self._new_id("plan"),
            run_id=run_id,
            plan_version=plan.plan_version,
            status="active",
            trigger_observation_id=None,
            reason="p014 checkpoint",
            plan_snapshot=plan.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        plans.add(record)
        session.flush()
        task_records = [
            TaskRecord(
                task_record_id=self._new_id("task"),
                task_id=task.task_id,
                run_id=run_id,
                plan_id=record.plan_id,
                plan_version=plan.plan_version,
                status=task.status.value,
                tool_name=task.tool_name.value,
                target_amr=task.target_amr,
                risk_level=task.risk_level.value,
                approval_required=task.approval_required,
                effect_id=task.effect_id,
                tool_arguments=task.tool_arguments,
                task_snapshot=task.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            for task in plan.tasks
        ]
        TaskRepository(session).add_all(task_records)
        session.flush()

    @staticmethod
    def _sync_task_records(
        session: Session,
        *,
        run_id: str,
        plan: PlanTasksOutput,
        tasks: list[PlanTask],
        now: datetime,
    ) -> None:
        """把 Checkpoint 中任务运行态同步到 P0-06 tasks 表。

        ``plans.plan_snapshot`` 是版本化的不可变 DAG 快照，而 ``tasks`` 的高频
        status/effect_id 字段是可更新索引。两者都在保存 Checkpoint 的同一事务中
        更新：进程若在提交前退出，恢复时不会看到一半的任务状态；若发现任务行
        缺失则直接报错，避免把 Effect Ledger 锚点挂到不存在的任务上。
        """

        task_repository = TaskRepository(session)
        for task in tasks:
            record = task_repository.get_by_business_key(
                run_id,
                plan.plan_version,
                task.task_id,
                for_update=True,
            )
            if record is None:
                raise PersistenceConflictError(
                    f"Checkpoint 计划 {plan.plan_version} 缺少任务行: {task.task_id}"
                )
            record.status = task.status.value
            record.effect_id = task.effect_id
            record.tool_arguments = to_jsonable(task.tool_arguments)
            record.task_snapshot = task.model_dump(mode="json")
            record.updated_at = now

    @staticmethod
    def _task_state_from_graph_state(
        graph_state: Mapping[str, object],
        *,
        fallback: list[PlanTask],
    ) -> list[PlanTask]:
        """优先提取 RunState 中的最新任务状态，而不是静态 plan 节点副本。"""

        runtime_payload = graph_state.get("run_state")
        if not isinstance(runtime_payload, Mapping):
            return fallback
        try:
            runtime_state = RunState.model_validate(runtime_payload)
        except Exception as exc:
            raise PersistenceConflictError("Checkpoint 中的 RunState 无法通过当前契约") from exc
        plan_payload = graph_state.get("plan")
        if isinstance(plan_payload, Mapping):
            try:
                plan_version = int(plan_payload.get("plan_version", 0))
            except (TypeError, ValueError) as exc:
                raise PersistenceConflictError("Checkpoint 计划版本不是整数") from exc
            if runtime_state.plan_version != plan_version:
                raise PersistenceConflictError("Checkpoint 的 RunState 与 plan 版本不一致")
        return list(runtime_state.plan_tasks)

    @staticmethod
    def _plan_from_graph_state(graph_state: Mapping[str, object]) -> PlanTasksOutput | None:
        """从 JSON 化图状态提取计划；没有进入 plan 节点时返回 None。"""

        payload = graph_state.get("plan")
        if not isinstance(payload, Mapping):
            return None
        return PlanTasksOutput.model_validate(payload)

    @staticmethod
    def _assert_run_contract(run: RunRecord, task_contract: TaskContract) -> None:
        """防止同一 run_id 被另一份合同复用，避免跨运行污染副作用。"""

        if run.contract_id != task_contract.contract_id or run.environment_ref != task_contract.environment_ref:
            raise PersistenceConflictError("run_id 已绑定另一份合同或环境快照")

    @staticmethod
    def _assert_effect_request(record: EffectRecord, tool_name: ToolName, input_digest: str) -> None:
        """唯一键命中时必须确认工具和规范化输入仍相同。"""

        payload = record.payload_snapshot or {}
        if payload.get("tool_name") != tool_name.value or payload.get("input_digest") != input_digest:
            raise PersistenceConflictError("同一幂等键被不同工具或不同输入复用")

    @staticmethod
    def _bounded_call_id(call_id: str) -> str:
        """数据库 call_id 列只有 64 字符，超长调用仍以 digest 保留可追溯身份。"""

        if len(call_id) <= 64:
            return call_id
        return f"call_{hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:56]}"

    @staticmethod
    def _effect_entry(record: EffectRecord) -> EffectLedgerEntry:
        """把关系行和 JSONB 载荷重新收口为严格账本契约。"""

        payload = record.payload_snapshot or {}
        result_payload = payload.get("result")
        result = ToolResult.model_validate(result_payload) if isinstance(result_payload, Mapping) else None
        try:
            status = EffectLedgerStatus(record.status)
            tool_name = ToolName(str(payload.get("tool_name")))
        except (TypeError, ValueError) as exc:
            raise PersistenceConflictError("Effect Ledger 行包含未知状态或工具名称") from exc
        created_at = record.created_at or datetime.now(timezone.utc)
        updated_at = record.updated_at or created_at
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise PersistenceConflictError("Effect Ledger arguments 不是 JSON 对象")
        return EffectLedgerEntry(
            run_id=record.run_id,
            plan_version=record.plan_version,
            task_id=record.task_id,
            tool_name=tool_name,
            idempotency_key=record.idempotency_key,
            ledger_effect_id=record.effect_id,
            call_id=str(payload.get("call_id") or record.tool_call_id or "unknown"),
            input_digest=str(payload.get("input_digest") or "0" * 64),
            arguments=dict(arguments),
            status=status,
            result=result,
            external_effect_id=(
                str(payload["external_effect_id"])
                if payload.get("external_effect_id") is not None
                else None
            ),
            recovery_note=(str(payload["recovery_note"]) if payload.get("recovery_note") else None),
            created_at=created_at,
            updated_at=updated_at,
        )


# 语义别名让调用方可以按专题名称导入，而数据库实现仍只有一个状态存储。
PostgresCheckpointStore = PostgresRuntimeStore


__all__ = ["PostgresCheckpointStore", "PostgresRuntimeStore"]
