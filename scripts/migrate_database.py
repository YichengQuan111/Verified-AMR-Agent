"""执行或检查 P0-06 Alembic 迁移，不提供删除核心表的命令。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from services.config import load_settings
from services.persistence import normalise_postgres_dsn


CORE_TABLES = frozenset(
    {
        "runs",
        "plans",
        "tasks",
        "tool_calls",
        "effects",
        "approvals",
        "events",
        "documents",
    }
)


def _alembic_config() -> Config:
    """从项目根目录加载 Alembic 配置，避免依赖当前工作目录。"""

    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _inspect_tables() -> tuple[list[str], list[str]]:
    """返回已存在表和缺失核心表；DSN 不写入输出。"""

    settings = load_settings()
    dsn = normalise_postgres_dsn(settings.database.postgres_dsn.get_secret_value())
    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        existing = sorted(inspect(engine).get_table_names(schema="public"))
    finally:
        engine.dispose()
    missing = sorted(CORE_TABLES - set(existing))
    return existing, missing


def main() -> int:
    """支持 upgrade/current/check；故意不暴露 downgrade。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("upgrade", "current", "check"))
    args = parser.parse_args()

    config = _alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, "head")
    elif args.action == "current":
        command.current(config, verbose=True)

    existing, missing = _inspect_tables()
    report = {
        "status": "ok" if not missing else "failed",
        "core_tables": sorted(CORE_TABLES),
        "existing_tables": existing,
        "missing_core_tables": missing,
        "auxiliary_tables": sorted(set(existing) - CORE_TABLES),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
