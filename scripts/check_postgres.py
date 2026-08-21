"""使用类型化 SecretStr DSN 检查 PostgreSQL，禁止回退到仓库公开旧密码。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config import load_settings


def main() -> int:
    """执行只读 ``SELECT 1``；连接、认证或查询失败均由进程非零退出表达。"""

    settings = load_settings()
    dsn = settings.database.postgres_dsn.get_secret_value()
    # connect_timeout 防止错误主机/IPv6 路径让一键启动器无限等待；输出不包含 DSN。
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            row = cursor.fetchone()
    if row != (1,):
        raise RuntimeError(f"PostgreSQL SELECT 1 返回异常结果: {row!r}")
    print(json.dumps({"status": "ok", "query": "SELECT 1"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
