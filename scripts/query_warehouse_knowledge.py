"""对已构建的 P0-07 混合索引执行一次带 ACL 的可审计查询。"""

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

from agent.tools import UserRole
from services.config import load_settings
from services.retrieval import build_hybrid_retriever


DEFAULT_KNOWLEDGE_ROOT = PROJECT_ROOT / "domains" / "amr_warehouse" / "knowledge"


def main() -> int:
    """加载进程内 BM25 与本地 Embedder，并输出 RetrievalResponse JSON。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--role", choices=[role.value for role in UserRole], required=True)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--document-id", action="append", dest="document_ids")
    parser.add_argument("--knowledge-root", type=Path, default=DEFAULT_KNOWLEDGE_ROOT)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    top_k = args.top_k or settings.retrieval.default_top_k
    retriever = build_hybrid_retriever(
        settings.retrieval,
        args.knowledge_root,
    )
    response = retriever.retrieve(
        args.query,
        role_scope=UserRole(args.role),
        top_k=top_k,
        document_ids=args.document_ids,
    )
    print(
        json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
