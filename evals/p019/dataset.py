"""P0-19 实验配置和 P0-18 源报告加载。

配置只允许引用仓库内的 P0-18 固定输入，不能让对照实验通过参数替换任务集、
工具或模型。独立执行是默认发布验收；同源 replay 仍可显式加载，但不能冒充独立对照。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_SOURCE_REPORT_PATH = PROJECT_ROOT / "tmp" / "p018_eval_final" / "p018_eval.json"
ALLOWED_EXECUTION_MODES = {"offline_independent_oracle", "offline_trace_replay"}
ALLOWED_VERSIONS = {"p0-19.v1", "p0-19.v2"}


def _rooted(relative: str) -> Path:
    """解析仓库内路径，拒绝配置逃逸到任意外部文件。"""

    path = (PROJECT_ROOT / relative).resolve()
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"P0-19 配置路径必须位于仓库内: {relative}") from exc
    return path


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    """读取并校验固定策略、Fast 身份和 Smart 延期配置。"""

    config_path = Path(path).resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P0-19 config.json 顶层必须是对象")
    if payload.get("version") not in ALLOWED_VERSIONS:
        raise ValueError("P0-19 评测配置版本不匹配")
    if payload.get("execution_mode") not in ALLOWED_EXECUTION_MODES:
        raise ValueError("P0-19 只允许独立 oracle 或显式 Trace Replay")
    strategies = payload.get("strategies")
    strategy_ids = [item.get("id") if isinstance(item, Mapping) else None for item in strategies] if isinstance(strategies, list) else None
    if strategy_ids != ["fixed_workflow", "react", "pevr"]:
        raise ValueError("P0-19 策略顺序必须固定为 fixed_workflow/react/pevr")
    model = payload.get("model")
    if not isinstance(model, Mapping) or model.get("profile") != "fast" or model.get("alias") != "qwen3.6-fast":
        raise ValueError("P0-19 必须固定使用 Qwen3.6 Fast")
    smart = payload.get("smart_comparison")
    if not isinstance(smart, Mapping) or smart.get("status") != "deferred" or smart.get("started") is not False or smart.get("completed") is not False:
        raise ValueError("P0-19 Smart 必须保持 deferred/未启动/未完成")
    for key in ("p018_dataset_path", "p018_config_path"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise ValueError(f"P0-19 缺少仓库内 {key}")
        _rooted(value)
    return payload


def rooted_path(relative: str) -> Path:
    """供执行器读取已经通过配置门禁的仓库内路径。"""

    return _rooted(relative)


__all__ = [
    "ALLOWED_EXECUTION_MODES",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SOURCE_REPORT_PATH",
    "PROJECT_ROOT",
    "load_config",
    "rooted_path",
]
