"""创建 P0-06 八张核心业务表。

Revision ID: 0001_p006_core
Revises: None
Create Date: 2026-08-19
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_p006_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """前向创建八张核心表和面向查询/幂等性的索引约束。"""

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("run_kind", sa.String(length=16), server_default="agent", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=True),
        sa.Column("environment_ref", sa.String(length=256), nullable=True),
        sa.Column("plan_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_task_id", sa.String(length=128), nullable=True),
        sa.Column("prompt_id", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("context_digest", sa.String(length=64), nullable=True),
        sa.Column("task_contract_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "run_state_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("plan_version >= 0", name="ck_runs_plan_version_nonnegative"),
        sa.CheckConstraint("run_kind IN ('agent', 'eval')", name="ck_runs_run_kind"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"])
    op.create_index("ix_runs_contract_id", "runs", ["contract_id"])

    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("role_scope", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.String(length=512), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "metadata_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index(
        "ix_documents_status_created_at",
        "documents",
        ["status", "created_at"],
    )
    op.create_index("ix_documents_checksum", "documents", ["checksum"])
    op.create_index(
        "ix_documents_role_scope_gin",
        "documents",
        ["role_scope"],
        postgresql_using="gin",
    )

    op.create_table(
        "plans",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_observation_id", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("plan_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("plan_version >= 1", name="ck_plans_version_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("run_id", "plan_version", name="uq_plans_run_version"),
    )
    op.create_index("ix_plans_run_status", "plans", ["run_id", "status"])

    op.create_table(
        "tasks",
        sa.Column("task_record_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("target_amr", sa.String(length=128), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("effect_id", sa.String(length=128), nullable=True),
        sa.Column("tool_arguments", postgresql.JSONB(), nullable=False),
        sa.Column("task_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("plan_version >= 1", name="ck_tasks_plan_version_positive"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.plan_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("task_record_id"),
        sa.UniqueConstraint(
            "run_id",
            "plan_version",
            "task_id",
            name="uq_tasks_run_version_task",
        ),
    )
    op.create_index("ix_tasks_run_status", "tasks", ["run_id", "status"])
    op.create_index("ix_tasks_tool_name", "tasks", ["tool_name"])
    op.create_index("ix_tasks_target_amr", "tasks", ["target_amr"])

    op.create_table(
        "tool_calls",
        sa.Column("call_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_record_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("plan_version", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("tool_arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("effect_id", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_tool_calls_attempt_positive"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_calls_duration_nonnegative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["task_record_id"],
            ["tasks.task_record_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index("ix_tool_calls_run_status", "tool_calls", ["run_id", "status"])
    op.create_index("ix_tool_calls_tool_name", "tool_calls", ["tool_name"])
    op.create_index("ix_tool_calls_task_id", "tool_calls", ["run_id", "task_id"])

    op.create_table(
        "effects",
        sa.Column("effect_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_record_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "payload_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["task_record_id"],
            ["tasks.task_record_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.call_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("effect_id"),
        sa.UniqueConstraint("tool_call_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_effects_idempotency_key"),
        sa.UniqueConstraint(
            "run_id",
            "plan_version",
            "task_id",
            name="uq_effects_run_version_task",
        ),
    )
    op.create_index("ix_effects_run_status", "effects", ["run_id", "status"])

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_record_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("plan_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("required_role", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("request_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "plan_version >= 0",
            name="ck_approvals_plan_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["task_record_id"],
            ["tasks.task_record_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index("ix_approvals_run_status", "approvals", ["run_id", "status"])
    op.create_index(
        "ix_approvals_task",
        "approvals",
        ["run_id", "plan_version", "task_id"],
    )

    op.create_table(
        "events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("severity", sa.String(length=16), server_default="info", nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence_no >= 1", name="ck_events_sequence_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_events_run_sequence"),
    )
    op.create_index("ix_events_run_created_at", "events", ["run_id", "created_at"])
    op.create_index(
        "ix_events_type_created_at",
        "events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    """核心表采用前向迁移；禁止自动删除用户明确要求保留的八张表。"""

    raise RuntimeError("P0-06 核心表禁止自动 downgrade；请创建新的前向修复迁移")
