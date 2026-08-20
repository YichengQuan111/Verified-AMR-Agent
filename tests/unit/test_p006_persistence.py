"""P0-06 ORM、迁移保护和 HTTP 输入边界的离线测试。"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB

from apps.api.schemas import CreateRunRequest
from scripts.migrate_database import CORE_TABLES
from services.persistence import Base
from services.persistence.repositories import RunRepository
from tests.unit.test_p004_contracts import task_contract_payload


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_orm_contains_exactly_eight_core_business_tables() -> None:
    """ORM 不得漏掉或擅自替换用户指定的八张核心表。"""

    assert set(Base.metadata.tables) == CORE_TABLES


def test_frequent_query_columns_are_relational_and_snapshots_use_jsonb() -> None:
    """检查“高频字段关系化 + 完整快照 JSONB”没有退化成大 JSON 桶。"""

    relational_columns = {
        "runs": ("run_id", "status", "plan_version"),
        "plans": ("run_id", "status", "plan_version"),
        "tasks": ("run_id", "status", "plan_version", "tool_name"),
        "tool_calls": ("run_id", "status", "plan_version", "tool_name"),
        "effects": ("run_id", "status", "plan_version"),
        "approvals": ("run_id", "status", "plan_version"),
        "events": ("run_id", "sequence_no", "event_type"),
        "documents": ("status", "version", "checksum"),
    }
    for table_name, column_names in relational_columns.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            assert column_name in table.c
            assert not isinstance(table.c[column_name].type, JSONB)

    jsonb_snapshots = {
        "runs": ("task_contract_snapshot", "run_state_snapshot"),
        "plans": ("plan_snapshot",),
        "tasks": ("tool_arguments", "task_snapshot"),
        "tool_calls": ("tool_arguments", "result_snapshot"),
        "effects": ("payload_snapshot",),
        "approvals": ("request_snapshot",),
        "events": ("payload",),
        "documents": ("metadata_snapshot",),
    }
    for table_name, column_names in jsonb_snapshots.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            assert isinstance(table.c[column_name].type, JSONB)


def test_all_business_foreign_keys_restrict_implicit_deletion() -> None:
    """核心记录不能通过级联删除悄悄丢失审计证据。"""

    foreign_keys = [
        key
        for table in Base.metadata.tables.values()
        for key in table.foreign_keys
    ]
    assert foreign_keys
    assert all(key.ondelete == "RESTRICT" for key in foreign_keys)


def test_repository_does_not_own_commit_or_rollback() -> None:
    """事务边界必须留在 Service，Repository 不能偷偷部分提交。"""

    source = inspect.getsource(RunRepository)
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_migration_downgrade_refuses_to_drop_core_tables() -> None:
    """即使误调用 downgrade，也必须在执行 drop_table 前明确失败。"""

    migration_path = (
        PROJECT_ROOT / "migrations" / "versions" / "0001_p006_core_tables.py"
    )
    spec = importlib.util.spec_from_file_location("p006_core_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with pytest.raises(RuntimeError, match="禁止自动 downgrade"):
        migration.downgrade()
    assert "drop_table" not in inspect.getsource(migration.downgrade)


def test_create_run_request_rejects_unknown_fields() -> None:
    """HTTP 请求同样禁止把未声明字段绕过 Pydantic 塞进 JSONB。"""

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateRunRequest.model_validate(
            {
                "task_contract": task_contract_payload(),
                "unexpected_history": ["不允许进入运行创建请求"],
            }
        )
