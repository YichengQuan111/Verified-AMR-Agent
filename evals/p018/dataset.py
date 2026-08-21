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
    if payload.get("version") not in {"p0-18.v1", "p0-18.v2"}:
        raise ValueError("P0-18 评测配置版本不匹配")
    if payload.get("execution_mode") != "offline_deterministic_oracle":
        raise ValueError("P0-18 默认只允许确定性离线 oracle 模式")
    return payload


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DATASET_PATH",
    "PROJECT_ROOT",
    "load_config",
    "load_dataset",
]
