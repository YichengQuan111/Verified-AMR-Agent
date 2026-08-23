"""P0-18 固定数据集加载与配额校验。"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import EvalDataset


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_PATH = Path(__file__).with_name("dataset.json")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")


def load_dataset(path: str | Path = DEFAULT_DATASET_PATH) -> EvalDataset:
    """以 UTF-8 读取固定 JSON，并在进入执行器前一次性验证全部配额。"""

    dataset_path = Path(path)
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P0-18 数据集顶层必须是对象，不能接受无用途声明的数组")
    dataset = EvalDataset.model_validate(payload)
    if dataset.is_training_data is not False or dataset.purpose != "evaluation_only":
        raise ValueError("P0-18 数据集必须保持 evaluation_only 且 is_training_data=false")
    return dataset


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    """读取评测配置；配置本身也会在报告中做摘要和 SHA-256 留档。"""

    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P0-18 config.json 顶层必须是对象")
    mode = payload.get("execution_mode")
    version = payload.get("version")
    if mode == "offline_deterministic_oracle":
        if version not in {"p0-18.v1", "p0-18.v2"}:
            raise ValueError("P0-18 离线评测配置版本不匹配")
    elif mode == "online_fast_closed_loop":
        if version != "p0-18.online.v1":
            raise ValueError("P0-18 在线闭环配置版本不匹配")
        model = payload.get("model")
        if not isinstance(model, dict) or model.get("online_service_required") is not True:
            raise ValueError("在线闭环必须设置 model.online_service_required=true")
    else:
        raise ValueError("P0-18 只允许 offline_deterministic_oracle 或 online_fast_closed_loop")
    return payload


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DATASET_PATH",
    "PROJECT_ROOT",
    "load_config",
    "load_dataset",
]
