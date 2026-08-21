"""检查或迁移 P0-14 历史外部执行查询身份。

迁移只在 Effect JSONB 中补 ``lookup_id`` 与身份版本，不会改写旧 simulation_id、
ToolResult、Trace 或报告摘要。``check`` 完全只读；``apply`` 在单个数据库事务中
逐行验证后更新，任何损坏或归属漂移都会整体回滚。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from services.application import PostgresRuntimeStore  # noqa: E402
from services.config import load_settings  # noqa: E402
from services.persistence import create_database_runtime  # noqa: E402


def main() -> int:
    """执行只读检查或显式迁移，并用退出码暴露未迁移状态。"""

    parser = argparse.ArgumentParser(description="P0-14 external execution identity migration")
    parser.add_argument("action", choices=("check", "apply"))
    args = parser.parse_args()

    runtime = create_database_runtime(load_settings().database)
    try:
        store = PostgresRuntimeStore(runtime.session_factory)
        before = store.audit_external_execution_lookups()
        applied = store.migrate_external_execution_lookups() if args.action == "apply" else None
        after = store.audit_external_execution_lookups()
    finally:
        runtime.dispose()
    report = {
        "status": "ok" if after["pending"] == 0 else "migration_required",
        "action": args.action,
        "before": before,
        "applied": applied,
        "after": after,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if after["pending"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
