"""八张核心表的无事务 Repository。

Repository 只负责 SQL 与 ORM 对象，不调用 commit/rollback。这样一个 Service 可以
在同一 Session 事务中组合 runs、events、approvals 等多个仓储，避免部分提交。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.persistence.models import (
    ApprovalRecord,
    DocumentRecord,
    EffectRecord,
    EventRecord,
    PlanRecord,
    RunRecord,
    TaskRecord,
    ToolCallRecord,
)


class RunRepository:
    """runs 表读写；行锁由需要跨表一致性的 Service 显式请求。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: RunRecord) -> None:
        self.session.add(record)

    def get(self, run_id: str, *, for_update: bool = False) -> RunRecord | None:
        statement = select(RunRecord).where(RunRecord.run_id == run_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


class PlanRepository:
    """plans 表读写和当前版本查询。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: PlanRecord) -> None:
        self.session.add(record)

    def get_by_version(self, run_id: str, plan_version: int) -> PlanRecord | None:
        return self.session.scalar(
            select(PlanRecord).where(
                PlanRecord.run_id == run_id,
                PlanRecord.plan_version == plan_version,
            )
        )

    def get_latest(self, run_id: str) -> PlanRecord | None:
        return self.session.scalar(
            select(PlanRecord)
            .where(PlanRecord.run_id == run_id)
            .order_by(PlanRecord.plan_version.desc())
            .limit(1)
        )


class TaskRepository:
    """tasks 表读写；业务 task_id 在 run + plan_version 内唯一。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_all(self, records: Sequence[TaskRecord]) -> None:
        self.session.add_all(records)

    def list_for_plan(self, plan_id: str) -> list[TaskRecord]:
        return list(
            self.session.scalars(
                select(TaskRecord)
                .where(TaskRecord.plan_id == plan_id)
                .order_by(TaskRecord.task_id)
            )
        )

    def get_by_business_key(
        self,
        run_id: str,
        plan_version: int,
        task_id: str,
        *,
        for_update: bool = False,
    ) -> TaskRecord | None:
        """按运行/计划版本/任务 ID 读取任务，供 Effect Ledger 建立可选外键。"""

        statement = select(TaskRecord).where(
            TaskRecord.run_id == run_id,
            TaskRecord.plan_version == plan_version,
            TaskRecord.task_id == task_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


class ToolCallRepository:
    """tool_calls 表读写，不在 Repository 内重试工具。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: ToolCallRecord) -> None:
        self.session.add(record)

    def get(self, call_id: str) -> ToolCallRecord | None:
        return self.session.get(ToolCallRecord, call_id)


class EffectRepository:
    """effects 幂等账本读写。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: EffectRecord) -> None:
        self.session.add(record)

    def get_by_idempotency_key(
        self,
        key: str,
        *,
        for_update: bool = False,
    ) -> EffectRecord | None:
        """读取唯一幂等键；更新账本状态时由 Service 请求行锁。"""

        statement = select(EffectRecord).where(EffectRecord.idempotency_key == key)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_for_run(self, run_id: str) -> list[EffectRecord]:
        """按稳定业务键返回一个运行的全部副作用记录。"""

        return list(
            self.session.scalars(
                select(EffectRecord)
                .where(EffectRecord.run_id == run_id)
                .order_by(EffectRecord.plan_version, EffectRecord.task_id)
            )
        )

    def list_by_external_execution_id(self, execution_id: str) -> list[EffectRecord]:
        """按 Effect JSONB 中的真实执行 ID 查询跨进程仿真快照。"""

        return list(
            self.session.scalars(
                select(EffectRecord)
                .where(
                    EffectRecord.payload_snapshot.contains(
                        {"external_execution": {"run_id": execution_id}}
                    )
                )
                .order_by(EffectRecord.updated_at.desc(), EffectRecord.effect_id)
            )
        )

    def list_by_external_lookup_id(self, lookup_id: str) -> list[EffectRecord]:
        """按迁移后的唯一查询 ID 读取 Effect，不改写历史仿真证据。"""

        return list(
            self.session.scalars(
                select(EffectRecord)
                .where(
                    EffectRecord.payload_snapshot.contains(
                        {"external_execution": {"lookup_id": lookup_id}}
                    )
                )
                .order_by(EffectRecord.updated_at.desc(), EffectRecord.effect_id)
            )
        )

    def list_with_external_execution(self) -> list[EffectRecord]:
        """返回含外部快照的账本行，供一次性身份迁移逐行加锁处理。"""

        return list(
            self.session.scalars(
                select(EffectRecord)
                .where(EffectRecord.payload_snapshot.has_key("external_execution"))  # type: ignore[attr-defined]
                .order_by(EffectRecord.run_id, EffectRecord.plan_version, EffectRecord.task_id)
            )
        )


class ApprovalRepository:
    """approvals 表读写与待处理请求查询。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: ApprovalRecord) -> None:
        self.session.add(record)

    def get(
        self,
        approval_id: str,
        *,
        for_update: bool = False,
    ) -> ApprovalRecord | None:
        """按稳定审批 ID 读取记录；恢复/决定审批时可选行锁。"""

        statement = select(ApprovalRecord).where(ApprovalRecord.approval_id == approval_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_pending(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        for_update: bool = False,
    ) -> ApprovalRecord | None:
        statement = (
            select(ApprovalRecord)
            .where(
                ApprovalRecord.run_id == run_id,
                ApprovalRecord.status == "pending",
            )
            .order_by(ApprovalRecord.requested_at.desc())
            .limit(1)
        )
        if task_id is not None:
            statement = statement.where(ApprovalRecord.task_id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


class EventRepository:
    """events 表读写和 run 内单调序号计算。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: EventRecord) -> None:
        self.session.add(record)

    def next_sequence(self, run_id: str) -> int:
        """在 Service 已锁定 runs 行的前提下计算下一个序号。"""

        current = self.session.scalar(
            select(func.coalesce(func.max(EventRecord.sequence_no), 0)).where(
                EventRecord.run_id == run_id
            )
        )
        return int(current or 0) + 1

    def list_for_run(self, run_id: str, *, after_sequence: int = 0) -> list[EventRecord]:
        return list(
            self.session.scalars(
                select(EventRecord)
                .where(
                    EventRecord.run_id == run_id,
                    EventRecord.sequence_no > after_sequence,
                )
                .order_by(EventRecord.sequence_no)
            )
        )


class DocumentRepository:
    """documents 表读写；默认查询不返回额外派生表。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, record: DocumentRecord) -> None:
        self.session.add(record)

    def get(
        self,
        document_id: str,
        *,
        for_update: bool = False,
    ) -> DocumentRecord | None:
        statement = select(DocumentRecord).where(
            DocumentRecord.document_id == document_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


__all__ = [
    "ApprovalRepository",
    "DocumentRepository",
    "EffectRepository",
    "EventRepository",
    "PlanRepository",
    "RunRepository",
    "TaskRepository",
    "ToolCallRepository",
]
