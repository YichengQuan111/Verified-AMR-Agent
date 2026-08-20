"""P0-06 八张核心表的 SQLAlchemy 2.0 ORM 映射。

设计采用“高频查询字段关系化 + 完整 Pydantic 快照 JSONB”：状态、run_id、
plan_version、tool_name 等都能直接筛选，复杂合同、计划、任务和工具载荷保留
原始结构快照。文档正文使用 BYTEA，不把二进制内容伪装成 JSONB。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 P0-06 ORM 表的共同元数据根。"""


class TimestampMixin:
    """统一保存带时区创建/更新时间，更新值由 SQLAlchemy 发出。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RunRecord(TimestampMixin, Base):
    """一次 Agent 或评测运行的可查询状态与完整合同/运行态快照。"""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("plan_version >= 0", name="ck_runs_plan_version_nonnegative"),
        CheckConstraint(
            "run_kind IN ('agent', 'eval')",
            name="ck_runs_run_kind",
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_contract_id", "contract_id"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="agent")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_id: Mapped[str | None] = mapped_column(String(128))
    environment_ref: Mapped[str | None] = mapped_column(String(256))
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_task_id: Mapped[str | None] = mapped_column(String(128))
    prompt_id: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    context_digest: Mapped[str | None] = mapped_column(String(64))
    task_contract_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    run_state_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanRecord(TimestampMixin, Base):
    """一个不可混淆的计划版本；完整 DAG 保存在 plan_snapshot。"""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("run_id", "plan_version", name="uq_plans_run_version"),
        CheckConstraint("plan_version >= 1", name="ck_plans_version_positive"),
        Index("ix_plans_run_status", "run_id", "status"),
    )

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_observation_id: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class TaskRecord(TimestampMixin, Base):
    """计划中的高频任务字段与完整 PlanTask 快照。"""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "plan_version",
            "task_id",
            name="uq_tasks_run_version_task",
        ),
        CheckConstraint("plan_version >= 1", name="ck_tasks_plan_version_positive"),
        Index("ix_tasks_run_status", "run_id", "status"),
        Index("ix_tasks_tool_name", "tool_name"),
        Index("ix_tasks_target_amr", "target_amr"),
    )

    task_record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.plan_id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    target_amr: Mapped[str | None] = mapped_column(String(128))
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    effect_id: Mapped[str | None] = mapped_column(String(128))
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ToolCallRecord(Base):
    """一次工具尝试的参数、结果、错误、耗时和版本化关联。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_tool_calls_attempt_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_calls_duration_nonnegative",
        ),
        Index("ix_tool_calls_run_status", "run_id", "status"),
        Index("ix_tool_calls_tool_name", "tool_name"),
        Index("ix_tool_calls_task_id", "run_id", "task_id"),
    )

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_record_id", ondelete="RESTRICT")
    )
    task_id: Mapped[str | None] = mapped_column(String(128))
    plan_version: Mapped[int | None] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_category: Mapped[str | None] = mapped_column(String(64))
    effect_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class EffectRecord(TimestampMixin, Base):
    """副作用账本；唯一键防止恢复或重试时重复执行同一任务副作用。"""

    __tablename__ = "effects"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "plan_version",
            "task_id",
            name="uq_effects_run_version_task",
        ),
        UniqueConstraint("idempotency_key", name="uq_effects_idempotency_key"),
        Index("ix_effects_run_status", "run_id", "status"),
    )

    effect_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_record_id", ondelete="RESTRICT")
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="RESTRICT"),
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )


class ApprovalRecord(Base):
    """运行级或任务级人工审批请求与不可覆盖的决定证据。"""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("plan_version >= 0", name="ck_approvals_plan_version_nonnegative"),
        Index("ix_approvals_run_status", "run_id", "status"),
        Index("ix_approvals_task", "run_id", "plan_version", "task_id"),
    )

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_record_id", ondelete="RESTRICT")
    )
    task_id: Mapped[str | None] = mapped_column(String(128))
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    required_role: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_comment: Mapped[str | None] = mapped_column(Text)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventRecord(Base):
    """按 run 内严格序号持久化的审计/SSE 事件。"""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_events_run_sequence"),
        CheckConstraint("sequence_no >= 1", name="ck_events_sequence_positive"),
        Index("ix_events_run_created_at", "run_id", "created_at"),
        Index("ix_events_type_created_at", "event_type", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DocumentRecord(TimestampMixin, Base):
    """上传文档的关系化索引字段、原始内容与完整元数据快照。"""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        Index("ix_documents_status_created_at", "status", "created_at"),
        Index("ix_documents_checksum", "checksum"),
        Index(
            "ix_documents_role_scope_gin",
            "role_scope",
            postgresql_using="gin",
        ),
    )

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    role_scope: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_uri: Mapped[str | None] = mapped_column(String(512))
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "ApprovalRecord",
    "Base",
    "DocumentRecord",
    "EffectRecord",
    "EventRecord",
    "PlanRecord",
    "RunRecord",
    "TaskRecord",
    "ToolCallRecord",
]
