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
DEFAULT_ONLINE_CONFIG_PATH = Path(__file__).with_name("online_config.json")
DEFAULT_SOURCE_REPORT_PATH = PROJECT_ROOT / "tmp" / "p018_eval_final" / "p018_eval.json"
ALLOWED_EXECUTION_MODES = {
    "offline_independent_oracle",
    "offline_trace_replay",
    "online_fast_three_strategy_closed_loop",
}
ALLOWED_VERSIONS = {"p0-19.v1", "p0-19.v2", "p0-19.online.v1"}


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
        raise ValueError("P0-19 只允许独立 oracle、显式 Trace Replay 或在线三策略闭环")
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
    if payload.get("execution_mode") == "online_fast_three_strategy_closed_loop":
        if payload.get("version") != "p0-19.online.v1":
            raise ValueError("在线三策略闭环必须使用 p0-19.online.v1")
        if payload.get("p018_dataset_path") != "evals/p018/dataset.json":
            raise ValueError("在线三策略必须精确复用 P0-18 固定 60 例数据集")
        if payload.get("p018_config_path") != "evals/p018/online_config.json":
            raise ValueError("在线三策略必须精确复用 P0-18 在线闭环配置")
        controller = payload.get("react_controller")
        if not isinstance(controller, Mapping):
            raise ValueError("在线 ReAct 必须声明评测层控制器")
        if (
            controller.get("prompt_id") != "amr.eval.p019.react_recovery"
            or controller.get("prompt_version") != "1.0.0"
            or controller.get("max_retries") != 1
            or controller.get("max_replans") != 0
        ):
            raise ValueError("ReAct 控制器身份或有界恢复额度不匹配")
        schedule = payload.get("schedule")
        if not isinstance(schedule, Mapping) or schedule.get("policy") != "latin_square_interleaved":
            raise ValueError("在线三策略必须使用固定 Latin-square 交错顺序")
    return payload


def rooted_path(relative: str) -> Path:
    """供执行器读取已经通过配置门禁的仓库内路径。"""

    return _rooted(relative)


__all__ = [
    "ALLOWED_EXECUTION_MODES",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_ONLINE_CONFIG_PATH",
    "DEFAULT_SOURCE_REPORT_PATH",
    "PROJECT_ROOT",
    "load_config",
    "rooted_path",
]
