"""运行、计划、事件与审批的事务型应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent.context import PlanTasksOutput
from agent.planning import TaskContract
from services.application.contracts import ApprovalView, EventView, PlanView, RunView
from services.application.exceptions import (
    InvalidOperationError,
    PersistenceConflictError,
    ResourceNotFoundError,
)
from services.persistence import (
    ApprovalRecord,
    ApprovalRepository,
    EventRecord,
    EventRepository,
    PlanRecord,
    PlanRepository,
    RunRecord,
    RunRepository,
    SessionFactory,
    TaskRecord,
    TaskRepository,
)


IdentifierFactory = Callable[[str], str]


def new_identifier(kind: str) -> str:
    """生成仍保持 JSON 字符串兼容的内部 ID。"""

    return f"{kind}_{uuid4().hex}"


class RunService:
    """在一个事务中编排多个 Repository，并向 Router 返回稳定 DTO。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        identifier_factory: IdentifierFactory = new_identifier,
    ) -> None:
        self._session_factory = session_factory
        self._new_id = identifier_factory

    def create_run(
        self,
        task_contract: TaskContract,
        *,
        prompt_id: str | None = None,
        prompt_version: str | None = None,
        context_digest: str | None = None,
        run_state_snapshot: dict[str, object] | None = None,
    ) -> RunView:
        """创建普通运行；runs 与首个 events INSERT 必须同成同败。"""

        return self._create_run(
            task_contract,
            run_kind="agent",
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            context_digest=context_digest,
            run_state_snapshot=run_state_snapshot or {},
        )

    def create_eval_run(
        self,
        task_contract: TaskContract,
        *,
        suite_id: str,
        case_ids: list[str],
        requested_by: str,
    ) -> RunView:
        """创建评测运行；这里只持久化评测入口，不执行 P0-16 评测逻辑。"""

        return self._create_run(
            task_contract,
            run_kind="eval",
            run_state_snapshot={
                "evaluation": {
                    "suite_id": suite_id,
                    "case_ids": case_ids,
                    "requested_by": requested_by,
                }
            },
        )

    def _create_run(
        self,
        task_contract: TaskContract,
        *,
        run_kind: Literal["agent", "eval"],
        prompt_id: str | None = None,
        prompt_version: str | None = None,
        context_digest: str | None = None,
        run_state_snapshot: dict[str, object],
    ) -> RunView:
        """执行创建事务；显式 flush 让回滚测试验证两次真实 INSERT。"""

        run_id = self._new_id("run")
        now = datetime.now(timezone.utc)
        initial_status = (
            "waiting_approval" if task_contract.approval.required else "created"
        )
        run_record = RunRecord(
            run_id=run_id,
            run_kind=run_kind,
            status=initial_status,
            contract_id=task_contract.contract_id,
            environment_ref=task_contract.environment_ref,
            plan_version=0,
            current_task_id=None,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            context_digest=context_digest,
            task_contract_snapshot=task_contract.model_dump(mode="json"),
            run_state_snapshot=run_state_snapshot,
            created_at=now,
            updated_at=now,
        )

        try:
            with self._session_factory() as session:
                with session.begin():
                    run_repository = RunRepository(session)
                    event_repository = EventRepository(session)
                    run_repository.add(run_record)

                    # 第一次 flush 确保 PostgreSQL 已实际执行 INSERT runs。若随后
                    # INSERT events 失败，外层 transaction context 会回滚这次写入。
                    session.flush()

                    created_event = EventRecord(
                        event_id=self._new_id("event"),
                        run_id=run_id,
                        sequence_no=1,
                        event_type="run.created",
                        node_name=None,
                        task_id=None,
                        severity="info",
                        payload={
                            "run_kind": run_kind,
                            "status": initial_status,
                            "contract_id": task_contract.contract_id,
                            "task_contract_schema_version": task_contract.schema_version,
                        },
                        created_at=now,
                    )
                    event_repository.add(created_event)

                    # 这是第二次独立 SQL INSERT，也是 P0-06 回滚验收的故障注入点。
                    session.flush()

                    if task_contract.approval.required:
                        self._create_contract_approval(
                            session=session,
                            run_record=run_record,
                            task_contract=task_contract,
                            requested_at=now,
                        )
                    result = self._to_run_view(run_record)
        except IntegrityError as exc:
            # 进入这里前 session.begin() 已完成 rollback；绝不尝试部分提交或重试。
            raise PersistenceConflictError(
                "创建运行时发生持久化冲突，runs/events 事务已整体回滚"
            ) from exc
        return result

    def _create_contract_approval(
        self,
        *,
        session: Session,
        run_record: RunRecord,
        task_contract: TaskContract,
        requested_at: datetime,
    ) -> None:
        """把合同级审批和第二条事件追加到当前创建事务。"""

        approval_repository = ApprovalRepository(session)
        event_repository = EventRepository(session)
        approval = ApprovalRecord(
            approval_id=self._new_id("approval"),
            run_id=run_record.run_id,
            task_record_id=None,
            task_id=None,
            plan_version=0,
            status="pending",
            required_role=task_contract.approval.required_role.value,
            requested_by="system",
            decided_by=None,
            reason=task_contract.approval.reason or "合同要求人工审批",
            decision_comment=None,
            request_snapshot=task_contract.approval.model_dump(mode="json"),
            requested_at=requested_at,
        )
        approval_repository.add(approval)
        event_repository.add(
            EventRecord(
                event_id=self._new_id("event"),
                run_id=run_record.run_id,
                sequence_no=2,
                event_type="approval.requested",
                node_name=None,
                task_id=None,
                severity="info",
                payload={
                    "approval_id": approval.approval_id,
                    "required_role": approval.required_role,
                    "reason": approval.reason,
                },
                created_at=requested_at,
            )
        )
        session.flush()

    def get_run(self, run_id: str) -> RunView:
        """从 PostgreSQL 读取运行，进程重启后仍可恢复查询。"""

        with self._session_factory() as session:
            record = RunRepository(session).get(run_id)
            if record is None:
                raise ResourceNotFoundError(f"运行不存在: {run_id}")
            return self._to_run_view(record)

    def get_plan(self, run_id: str, *, plan_version: int | None = None) -> PlanView:
        """读取指定或最新计划版本。"""

        with self._session_factory() as session:
            if RunRepository(session).get(run_id) is None:
                raise ResourceNotFoundError(f"运行不存在: {run_id}")
            repository = PlanRepository(session)
            record = (
                repository.get_latest(run_id)
                if plan_version is None
                else repository.get_by_version(run_id, plan_version)
            )
            if record is None:
                raise ResourceNotFoundError(f"运行尚无对应计划: {run_id}")
            return self._to_plan_view(record)

    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[EventView]:
        """按持久化序号返回事件，不依赖内存队列。"""

        with self._session_factory() as session:
            if RunRepository(session).get(run_id) is None:
                raise ResourceNotFoundError(f"运行不存在: {run_id}")
            records = EventRepository(session).list_for_run(
                run_id,
                after_sequence=after_sequence,
            )
            return [self._to_event_view(record) for record in records]

    def save_plan(
        self,
        run_id: str,
        plan: PlanTasksOutput,
        *,
        reason: str | None = None,
        trigger_observation_id: str | None = None,
    ) -> PlanView:
        """原子保存计划、任务、运行版本、审批请求和计划事件。"""

        now = datetime.now(timezone.utc)
        try:
            with self._session_factory() as session:
                with session.begin():
                    run = RunRepository(session).get(run_id, for_update=True)
                    if run is None:
                        raise ResourceNotFoundError(f"运行不存在: {run_id}")
                    expected_version = run.plan_version + 1
                    if plan.plan_version != expected_version:
                        raise InvalidOperationError(
                            f"计划版本必须从 {run.plan_version} 递增为 {expected_version}"
                        )

                    plan_record = PlanRecord(
                        plan_id=self._new_id("plan"),
                        run_id=run_id,
                        plan_version=plan.plan_version,
                        status="active",
                        trigger_observation_id=trigger_observation_id,
                        reason=reason,
                        plan_snapshot=plan.model_dump(mode="json"),
                        created_at=now,
                        updated_at=now,
                    )
                    PlanRepository(session).add(plan_record)
                    # 先实际写入 plans，后续 tasks 的外键顺序不依赖 ORM 对无
                    # relationship 映射对象的偶然排序；仍处于同一个外层事务。
                    session.flush()
                    task_records: list[TaskRecord] = []
                    for task in plan.tasks:
                        task_records.append(
                            TaskRecord(
                                task_record_id=self._new_id("task"),
                                task_id=task.task_id,
                                run_id=run_id,
                                plan_id=plan_record.plan_id,
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
                        )
                    TaskRepository(session).add_all(task_records)
                    session.flush()

                    requires_approval = any(task.approval_required for task in plan.tasks)
                    run.plan_version = plan.plan_version
                    run.status = "waiting_approval" if requires_approval else "planning"
                    run.updated_at = now

                    event_repository = EventRepository(session)
                    sequence = event_repository.next_sequence(run_id)
                    event_repository.add(
                        EventRecord(
                            event_id=self._new_id("event"),
                            run_id=run_id,
                            sequence_no=sequence,
                            event_type="plan.saved",
                            node_name="plan_tasks",
                            task_id=None,
                            severity="info",
                            payload={
                                "plan_id": plan_record.plan_id,
                                "plan_version": plan.plan_version,
                                "task_count": len(plan.tasks),
                                "status": plan_record.status,
                            },
                            created_at=now,
                        )
                    )

                    # 计划任务审批与计划本体保持同一事务；任何一个约束失败都不会
                    # 留下“有 tasks、无 approval/event”的半成品版本。
                    approval_repository = ApprovalRepository(session)
                    task_by_id = {record.task_id: record for record in task_records}
                    for task in plan.tasks:
                        if not task.approval_required:
                            continue
                        sequence += 1
                        approval = ApprovalRecord(
                            approval_id=self._new_id("approval"),
                            run_id=run_id,
                            task_record_id=task_by_id[task.task_id].task_record_id,
                            task_id=task.task_id,
                            plan_version=plan.plan_version,
                            status="pending",
                            required_role="operator",
                            requested_by="system",
                            decided_by=None,
                            reason=f"计划任务 {task.task_id} 要求人工审批",
                            decision_comment=None,
                            request_snapshot=task.model_dump(mode="json"),
                            requested_at=now,
                        )
                        approval_repository.add(approval)
                        event_repository.add(
                            EventRecord(
                                event_id=self._new_id("event"),
                                run_id=run_id,
                                sequence_no=sequence,
                                event_type="approval.requested",
                                node_name="plan_tasks",
                                task_id=task.task_id,
                                severity="info",
                                payload={
                                    "approval_id": approval.approval_id,
                                    "required_role": "operator",
                                },
                                created_at=now,
                            )
                        )
                    session.flush()
                    result = self._to_plan_view(plan_record)
        except IntegrityError as exc:
            raise PersistenceConflictError("保存计划发生冲突，事务已整体回滚") from exc
        return result

    def decide_approval(
        self,
        run_id: str,
        *,
        decision: Literal["approved", "rejected"],
        decided_by: str,
        comment: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalView:
        """锁定运行和待审批行，原子保存决定、运行状态与审计事件。"""

        now = datetime.now(timezone.utc)
        try:
            with self._session_factory() as session:
                with session.begin():
                    run = RunRepository(session).get(run_id, for_update=True)
                    if run is None:
                        raise ResourceNotFoundError(f"运行不存在: {run_id}")
                    approval = ApprovalRepository(session).get_pending(
                        run_id,
                        task_id=task_id,
                        for_update=True,
                    )
                    if approval is None:
                        raise ResourceNotFoundError("没有匹配的待处理审批")

                    approval.status = decision
                    approval.decided_by = decided_by
                    approval.decision_comment = comment
                    approval.decided_at = now
                    # 先让当前决定在本事务内对后续 SELECT 可见，再判断是否还有
                    # 其他待审批项；这次 flush 不是 commit，事件失败仍会整体回滚。
                    session.flush()
                    remaining_approval = ApprovalRepository(session).get_pending(
                        run_id,
                        for_update=True,
                    )
                    if decision == "approved":
                        if remaining_approval is not None:
                            run.status = "waiting_approval"
                        elif run.plan_version > 0:
                            run.status = "planning"
                        else:
                            run.status = "created"
                    else:
                        run.status = "cancelled"
                    run.updated_at = now
                    if decision == "rejected":
                        run.completed_at = now

                    events = EventRepository(session)
                    events.add(
                        EventRecord(
                            event_id=self._new_id("event"),
                            run_id=run_id,
                            sequence_no=events.next_sequence(run_id),
                            event_type=f"approval.{decision}",
                            node_name=None,
                            task_id=approval.task_id,
                            severity="info" if decision == "approved" else "warning",
                            payload={
                                "approval_id": approval.approval_id,
                                "decided_by": decided_by,
                                "comment": comment,
                            },
                            created_at=now,
                        )
                    )
                    session.flush()
                    result = self._to_approval_view(approval)
        except IntegrityError as exc:
            raise PersistenceConflictError("审批写入发生冲突，事务已整体回滚") from exc
        return result

    @staticmethod
    def _to_run_view(record: RunRecord) -> RunView:
        """在 Session 关闭前把 ORM 行转换为严格业务视图。"""

        return RunView(
            run_id=record.run_id,
            run_kind=record.run_kind,
            status=record.status,
            contract_id=record.contract_id,
            environment_ref=record.environment_ref,
            plan_version=record.plan_version,
            current_task_id=record.current_task_id,
            prompt_id=record.prompt_id,
            prompt_version=record.prompt_version,
            context_digest=record.context_digest,
            task_contract=TaskContract.model_validate(record.task_contract_snapshot),
            run_state_snapshot=record.run_state_snapshot,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _to_plan_view(record: PlanRecord) -> PlanView:
        """用当前 Pydantic 契约重新验证数据库中的计划快照。"""

        return PlanView(
            plan_id=record.plan_id,
            run_id=record.run_id,
            plan_version=record.plan_version,
            status=record.status,
            trigger_observation_id=record.trigger_observation_id,
            reason=record.reason,
            plan=PlanTasksOutput.model_validate(record.plan_snapshot),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _to_event_view(record: EventRecord) -> EventView:
        """把事件行转换为 SSE/JSON 共用视图。"""

        return EventView(
            event_id=record.event_id,
            run_id=record.run_id,
            sequence_no=record.sequence_no,
            event_type=record.event_type,
            node_name=record.node_name,
            task_id=record.task_id,
            severity=record.severity,
            payload=record.payload,
            created_at=record.created_at,
        )

    @staticmethod
    def _to_approval_view(record: ApprovalRecord) -> ApprovalView:
        """把审批 ORM 行转换为稳定响应。"""

        return ApprovalView(
            approval_id=record.approval_id,
            run_id=record.run_id,
            task_id=record.task_id,
            plan_version=record.plan_version,
            status=record.status,
            required_role=record.required_role,
            requested_by=record.requested_by,
            decided_by=record.decided_by,
            reason=record.reason,
            decision_comment=record.decision_comment,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            expires_at=record.expires_at,
        )


__all__ = ["IdentifierFactory", "RunService", "new_identifier"]
