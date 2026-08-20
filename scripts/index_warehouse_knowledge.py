"""可重复构建 P0-07 PostgreSQL/Qdrant 仓储知识索引。"""

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

from services.application import DocumentService
from services.config.settings import load_settings
from services.persistence import create_database_runtime
from services.retrieval import WarehouseKnowledgeIndexer


DEFAULT_KNOWLEDGE_ROOT = PROJECT_ROOT / "domains" / "amr_warehouse" / "knowledge"


def main() -> int:
    """加载配置、同步文档、写向量并输出 JSON 索引报告。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--knowledge-root", type=Path, default=DEFAULT_KNOWLEDGE_ROOT)
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="保留 collection，并替换本批 doc_id 的旧 points",
    )
    parser.add_argument(
        "--skip-postgres-sync",
        action="store_true",
        help="仅用于隔离调试；正式 P0-07 索引应复用 documents 表",
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    runtime = None
    try:
        document_service = None
        if not args.skip_postgres_sync:
            runtime = create_database_runtime(settings.database)
            document_service = DocumentService(runtime.session_factory)
        report = WarehouseKnowledgeIndexer(
            settings.retrieval,
            document_service=document_service,
        ).index_directory(
            args.knowledge_root,
            rebuild=not args.no_rebuild,
        )
        print(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if runtime is not None:
            runtime.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
