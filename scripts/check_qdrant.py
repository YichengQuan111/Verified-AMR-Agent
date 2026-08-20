"""使用类型化配置检查 Qdrant 连通性，并输出可机器读取的 collection 清单。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from qdrant_client import QdrantClient

from services.config import load_settings


def main() -> int:
    """执行真实 API 请求；连接或协议错误直接返回非零退出码。"""

    settings = load_settings()
    client = QdrantClient(url=settings.retrieval.qdrant_url, timeout=10)
    try:
        collections = client.get_collections().collections
        print(
            json.dumps(
                {
                    "status": "ok",
                    "qdrant_url": settings.retrieval.qdrant_url,
                    "collections": sorted(item.name for item in collections),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
