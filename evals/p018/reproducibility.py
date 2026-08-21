"""P0-18 复现指纹和固定输入版本快照。

评测报告不能只记录一个“使用了 Fast 模型”的字符串。这里对地图、AMR、订单、
Prompt、评测配置和数据集做 SHA-256，对运行时实际注册的九个 ToolSpec 记录版本，
并保存每例 seed。模型脚本在本机存在时额外做摘要；模型服务不启动也不影响离线
Harness，但报告会明确标记 ``online_service_required=false``，避免把 oracle 结果
误写成真实 LLM 在线验收。后续若接入在线模式，应新增独立 execution_mode，不能
覆盖当前离线结果。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from pydantic import BaseModel

from agent.tools import get_tool_specs

from .contracts import EvalDataset
from .dataset import PROJECT_ROOT


def canonical_digest(value: Any) -> str:
    """对 JSON/Pydantic 值做稳定摘要；不允许 NaN 或隐式 repr。"""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    """分块读取文件，避免把 Prompt/地图正文复制进报告。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_fingerprint(path: Path, *, required: bool) -> dict[str, object]:
    """生成可读路径、大小和摘要；固定输入缺失时 fail closed。"""

    if not path.is_file():
        if required:
            raise FileNotFoundError(f"P0-18 固定输入不存在: {path}")
        return {"path": str(path), "exists": False, "sha256": None, "size": None}
    try:
        display_path = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(path).replace("\\", "/")
    return {
        "path": display_path,
        "exists": True,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def _git_snapshot() -> dict[str, object]:
    """记录源码 revision 和 dirty 状态；没有 Git 时保留可解释的 null。"""

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            shell=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            shell=False,
        )
    except OSError:
        return {"revision": None, "worktree_dirty": None}
    return {
        "revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "worktree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def build_reproducibility(
    *,
    dataset: EvalDataset,
    dataset_path: Path,
    config: Mapping[str, object],
    config_path: Path,
) -> dict[str, object]:
    """构建一次评测必须携带的固定输入、版本和运行时摘要。"""

    def rooted(relative: str) -> Path:
        """只解析仓库内配置路径，拒绝通过配置逃逸到任意输入。"""

        candidate = (PROJECT_ROOT / relative).resolve()
        try:
            candidate.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError(f"P0-18 配置路径必须位于仓库内: {relative}") from exc
        return candidate

    map_path = rooted(str(config["map_path"]))
    amrs_path = rooted(str(config["amrs_path"]))
    orders_path = rooted(str(config["orders_path"]))
    input_files = {
        "dataset": _file_fingerprint(dataset_path.resolve(), required=True),
        "config": _file_fingerprint(config_path.resolve(), required=True),
        "map": _file_fingerprint(map_path, required=True),
        "amrs": _file_fingerprint(amrs_path, required=True),
        "orders": _file_fingerprint(orders_path, required=True),
    }

    prompt_payload = config.get("prompts")
    if not isinstance(prompt_payload, Mapping):
        raise ValueError("P0-18 配置缺少 prompts 映射")
    prompt_files: dict[str, object] = {}
    prompt_versions: dict[str, str] = {}
    for prompt_id, raw_definition in sorted(prompt_payload.items()):
        if not isinstance(raw_definition, Mapping):
            raise ValueError(f"Prompt 配置必须是对象: {prompt_id}")
        prompt_path = rooted(str(raw_definition["path"]))
        prompt_files[str(prompt_id)] = _file_fingerprint(prompt_path, required=True)
        prompt_versions[str(prompt_id)] = str(raw_definition["version"])

    specs = get_tool_specs()
    actual_tool_versions = {
        spec.tool_name.value: spec.version
        for spec in sorted(specs, key=lambda item: item.tool_name.value)
    }
    expected_tools = config.get("tools")
    if not isinstance(expected_tools, Mapping):
        raise ValueError("P0-18 配置缺少 tools 映射")
    expected_names = sorted(str(item) for item in expected_tools.get("names", []))
    if expected_names != sorted(actual_tool_versions):
        raise ValueError(
            "P0-18 工具白名单与运行时 ToolSpec 不一致: "
            f"expected={expected_names}, actual={sorted(actual_tool_versions)}"
        )

    model = config.get("model")
    if not isinstance(model, Mapping):
        raise ValueError("P0-18 配置缺少 model 映射")
    artifact_ref = Path(str(model.get("artifact_ref", "")))
    model_artifact = _file_fingerprint(artifact_ref, required=False) if str(artifact_ref) else None

    case_seed_map = {case.case_id: case.seed for case in dataset.cases}
    return {
        "execution_mode": str(config["execution_mode"]),
        "environment_ref": str(config["environment_ref"]),
        "dataset_version": dataset.version,
        "case_seeds": case_seed_map,
        "case_seed_digest": canonical_digest(case_seed_map),
        "input_files": input_files,
        "prompt_versions": prompt_versions,
        "prompt_files": prompt_files,
        "toolset_version": str(expected_tools["contract_version"]),
        "tool_spec_versions": actual_tool_versions,
        "model": {
            "profile": str(model["profile"]),
            "alias": str(model["alias"]),
            "family": str(model["family"]),
            "quantization": str(model["quantization"]),
            "context_window": int(model["context_window"]),
            "temperature": float(model["temperature"]),
            "reasoning_enabled": bool(model["reasoning_enabled"]),
            "reasoning_budget_tokens": int(model["reasoning_budget_tokens"]),
            "artifact_ref": str(model.get("artifact_ref", "")),
            "artifact": model_artifact,
            "online_service_required": bool(model["online_service_required"]),
        },
        "runtime": {
            "python": str(Path(sys.executable).resolve()),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git": _git_snapshot(),
        },
    }


__all__ = ["build_reproducibility", "canonical_digest", "sha256_file"]
