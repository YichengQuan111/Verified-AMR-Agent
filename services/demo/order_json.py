"""演示闭环动态订单 JSON 的文件名与路径门禁。

CLI ``--order-json`` 只接受仓库 ``tmp/`` 根目录下、服务端生成的
``demo_nl_order_*.json``。这是防路径穿越的代码质量约束：调用方不能把任意
绝对路径或 ``tmp/../domains/...`` 喂给 PEVR 当快照。不涉及认证门禁。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from domains.amr_warehouse import TransportOrder


DEMO_NL_ORDER_JSON_NAME = re.compile(r"^demo_nl_order_[A-Za-z0-9._-]{1,80}\.json$")
DEMO_NL_ORDER_SCHEMA_VERSION = "demo-nl-order.v1"


def demo_nl_order_json_filename(run_id: str) -> str:
    """由服务端 run_id 派生文件名；run_id 只能含白名单字符。"""

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run_id):
        raise ValueError(f"run_id 不能用于动态订单文件名: {run_id}")
    return f"demo_nl_order_{run_id}.json"


def resolve_demo_nl_order_json_path(path: Path, *, repository_root: Path) -> Path:
    """把 --order-json 解析为 tmp/ 下的合法文件；拒绝子目录与路径穿越。"""

    tmp_root = (repository_root / "tmp").resolve()
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(tmp_root)
    except ValueError as exc:
        raise ValueError("order-json 必须位于仓库 tmp/ 目录内") from exc
    if resolved.parent != tmp_root:
        raise ValueError("order-json 不允许位于 tmp/ 子目录")
    if not DEMO_NL_ORDER_JSON_NAME.fullmatch(resolved.name):
        raise ValueError("order-json 文件名必须匹配 demo_nl_order_<run_id>.json")
    if not resolved.is_file():
        raise FileNotFoundError(f"order-json 不存在: {resolved.name}")
    return resolved


def dump_demo_nl_order_json(path: Path, order: TransportOrder) -> None:
    """写入服务端重建的订单信封；调用方负责把 path 放在 tmp/ 且文件名合法。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": DEMO_NL_ORDER_SCHEMA_VERSION,
        "order": order.model_dump(mode="json"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_demo_nl_order_json(path: Path) -> TransportOrder:
    """读取并校验动态订单信封；未知 schema 或非法订单字段直接失败。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != DEMO_NL_ORDER_SCHEMA_VERSION:
        raise ValueError("order-json schema_version 必须是 demo-nl-order.v1")
    return TransportOrder.model_validate(payload["order"])


__all__ = [
    "DEMO_NL_ORDER_JSON_NAME",
    "DEMO_NL_ORDER_SCHEMA_VERSION",
    "demo_nl_order_json_filename",
    "dump_demo_nl_order_json",
    "load_demo_nl_order_json",
    "resolve_demo_nl_order_json_path",
]
