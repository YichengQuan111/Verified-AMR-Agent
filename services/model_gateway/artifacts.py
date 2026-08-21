"""Fast 模型制品 manifest 的严格读取、参数核对与字节级校验。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.config.settings import ModelGatewaySettings, PROJECT_ROOT


class _ManifestModel(BaseModel):
    """manifest 的所有层级都拒绝未知字段，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class ArtifactFile(_ManifestModel):
    """一个需要按大小与 SHA-256 精确定位的宿主文件。"""

    path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        """统一大写十六进制，并拒绝截断或非 SHA-256 值。"""

        normalised = value.upper()
        if len(normalised) != 64 or any(
            character not in "0123456789ABCDEF" for character in normalised
        ):
            raise ValueError("artifact sha256 必须是 64 位十六进制")
        return normalised


class LlamaCppBuild(_ManifestModel):
    """llama.cpp 可复建的版本身份。"""

    version: str = Field(min_length=1)
    build: int = Field(gt=0)
    commit: str = Field(min_length=7)


class FastArtifactManifest(_ManifestModel):
    """Qwen3.6 Fast 的文件、量化与确定性运行参数契约。"""

    schema_version: Literal["amr.fast-model-manifest.v1"]
    artifact_id: str = Field(min_length=1)
    profile: Literal["fast"]
    alias: Literal["qwen3.6-fast"]
    quantization: Literal["IQ4_NL"]
    context_window: Literal[16384]
    temperature: Literal[0.1]
    top_p: Literal[0.95]
    top_k: Literal[20]
    parallel_slots: Literal[1]
    reasoning_enabled: Literal[False]
    # false 时启动只确认文件存在和大小，不扫描 19GB GGUF；sha256 字段仅作身份记录。
    verify_sha256: bool = True
    llama_cpp: LlamaCppBuild
    model: ArtifactFile
    runtime_binary: ArtifactFile
    launch_script: ArtifactFile
    # 旧外部脚本只作为审查来源证据保留；发布实际执行 launch_script。
    source_script: ArtifactFile | None = None
    observed_at: datetime


class VerifiedFastArtifact(_ManifestModel):
    """已通过本机字节校验、可写入运行报告的不可变证据。"""

    manifest_path: str
    manifest_sha256: str
    artifact_id: str
    model_path: str
    model_size_bytes: int
    model_sha256: str
    quantization: str
    context_window: int
    temperature: float
    top_p: float
    top_k: int
    parallel_slots: int
    reasoning_enabled: bool
    llama_cpp_version: str
    llama_cpp_build: int
    llama_cpp_commit: str
    runtime_binary_path: str
    runtime_binary_sha256: str
    launch_script_path: str
    launch_script_sha256: str


def _resolve_path(recorded: str, *, root: Path, override: str | None = None) -> Path:
    """路径覆盖仅用于换盘符；相对路径始终以仓库根目录解析。"""

    candidate = Path(override or recorded).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _sha256(path: Path) -> str:
    """分块计算大模型哈希，避免把 19GB GGUF 读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def _verify_file(
    name: str,
    path: Path,
    expected: ArtifactFile,
    *,
    verify_sha256: bool,
) -> None:
    """文件必须存在且大小一致；SHA-256 仅在 manifest 要求时计算。"""

    if not path.is_file():
        raise ValueError(f"{name} 不存在: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected.size_bytes:
        raise ValueError(
            f"{name} 大小与 manifest 不一致: {actual_size} != {expected.size_bytes}"
        )
    if verify_sha256 and _sha256(path) != expected.sha256:
        raise ValueError(f"{name} SHA-256 与 manifest 不一致: {path}")


def load_and_verify_fast_artifact(
    settings: ModelGatewaySettings,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> VerifiedFastArtifact:
    """核验 Profile 参数、文件存在与大小；SHA-256 仅在 ``verify_sha256=true`` 时扫描。

    服务 alias 仍由 Provider 的 ``/v1/models`` 门禁独立确认。关闭哈希时，报告里的
    sha256 字段只是 manifest 记录值，不能当成已经重新计算过的证据。
    """

    root = Path(project_root).resolve()
    manifest_path = _resolve_path(settings.artifact_manifest_path, root=root)
    if not manifest_path.is_file():
        raise ValueError(f"Fast artifact manifest 不存在: {manifest_path}")
    raw = manifest_path.read_bytes()
    try:
        manifest = FastArtifactManifest.model_validate(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Fast artifact manifest 不是合法 UTF-8 JSON: {manifest_path}") from exc

    profile = settings.active_profile
    expected_parameters = {
        "alias": profile.alias,
        "context_window": profile.context_window,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "top_k": profile.top_k,
        "parallel_slots": profile.parallel_slots,
        "quantization": profile.quantization,
        "reasoning_enabled": profile.reasoning_enabled,
    }
    manifest_parameters = {
        key: getattr(manifest, key) for key in expected_parameters
    }
    if manifest_parameters != expected_parameters:
        raise ValueError(
            "Fast Profile 与 artifact manifest 参数不一致: "
            f"{expected_parameters!r} != {manifest_parameters!r}"
        )

    model_path = _resolve_path(
        manifest.model.path,
        root=root,
        override=settings.artifact_model_path_override,
    )
    runtime_path = _resolve_path(
        manifest.runtime_binary.path,
        root=root,
        override=settings.artifact_runtime_path_override,
    )
    launch_path = _resolve_path(manifest.launch_script.path, root=root)
    _verify_file(
        "Fast GGUF", model_path, manifest.model, verify_sha256=manifest.verify_sha256
    )
    _verify_file(
        "llama-server",
        runtime_path,
        manifest.runtime_binary,
        verify_sha256=manifest.verify_sha256,
    )
    _verify_file(
        "Fast 启动器",
        launch_path,
        manifest.launch_script,
        verify_sha256=manifest.verify_sha256,
    )

    return VerifiedFastArtifact(
        manifest_path=str(manifest_path),
        manifest_sha256=hashlib.sha256(raw).hexdigest().upper(),
        artifact_id=manifest.artifact_id,
        model_path=str(model_path),
        model_size_bytes=manifest.model.size_bytes,
        model_sha256=manifest.model.sha256,
        quantization=manifest.quantization,
        context_window=manifest.context_window,
        temperature=manifest.temperature,
        top_p=manifest.top_p,
        top_k=manifest.top_k,
        parallel_slots=manifest.parallel_slots,
        reasoning_enabled=manifest.reasoning_enabled,
        llama_cpp_version=manifest.llama_cpp.version,
        llama_cpp_build=manifest.llama_cpp.build,
        llama_cpp_commit=manifest.llama_cpp.commit,
        runtime_binary_path=str(runtime_path),
        runtime_binary_sha256=manifest.runtime_binary.sha256,
        launch_script_path=str(launch_path),
        launch_script_sha256=manifest.launch_script.sha256,
    )


__all__ = [
    "ArtifactFile",
    "FastArtifactManifest",
    "LlamaCppBuild",
    "VerifiedFastArtifact",
    "load_and_verify_fast_artifact",
]
