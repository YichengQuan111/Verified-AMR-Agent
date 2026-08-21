from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.config.settings import ModelGatewaySettings
from services.model_gateway.artifacts import load_and_verify_fast_artifact
from services.model_gateway.exceptions import ModelGatewayStartupError
from services.model_gateway.provider import ModelProvider
from tests.unit.fakes import FakeOpenAIClient


def _sha256(path: Path) -> str:
    """测试制品很小，仍使用与生产相同的 SHA-256 身份语义。"""

    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _write_manifest(tmp_path: Path, **overrides: object) -> Path:
    """构造三份独立文件，便于逐项 mutation 而不读取真实 19GB GGUF。"""

    model = tmp_path / "model.gguf"
    runtime = tmp_path / "llama-server.exe"
    launcher = tmp_path / "start.ps1"
    model.write_bytes(b"model-artifact")
    runtime.write_bytes(b"runtime-artifact")
    launcher.write_bytes(b"launcher-artifact")

    def artifact(path: Path) -> dict[str, object]:
        return {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }

    payload: dict[str, object] = {
        "schema_version": "amr.fast-model-manifest.v1",
        "artifact_id": "unit-fast",
        "profile": "fast",
        "alias": "qwen3.6-fast",
        "quantization": "IQ4_NL",
        "context_window": 16384,
        "temperature": 0.1,
        "top_p": 0.95,
        "top_k": 20,
        "parallel_slots": 1,
        "reasoning_enabled": False,
        "verify_sha256": True,
        "llama_cpp": {"version": "test", "build": 1, "commit": "abcdef0"},
        "model": artifact(model),
        "runtime_binary": artifact(runtime),
        "launch_script": artifact(launcher),
        "observed_at": "2026-08-21T00:00:00Z",
    }
    payload.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _settings(manifest: Path) -> ModelGatewaySettings:
    """显式打开发布制品门禁；其余字段沿用固定 Fast Profile。"""

    return ModelGatewaySettings(
        artifact_manifest_path=str(manifest),
        artifact_verification_required=True,
    )


def test_fast_manifest_verifies_all_three_files_and_parameters(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)

    record = load_and_verify_fast_artifact(_settings(manifest))

    assert record.artifact_id == "unit-fast"
    assert record.model_sha256 == _sha256(tmp_path / "model.gguf")
    assert record.runtime_binary_sha256 == _sha256(tmp_path / "llama-server.exe")
    assert record.launch_script_sha256 == _sha256(tmp_path / "start.ps1")
    assert record.context_window == 16384
    assert record.temperature == 0.1
    assert record.quantization == "IQ4_NL"


def test_verify_sha256_false_skips_gguf_hash_but_still_checks_size(tmp_path: Path) -> None:
    """关闭哈希后字节改动不再扫描 SHA-256；截断文件仍因大小不一致失败。"""

    manifest = _write_manifest(tmp_path, verify_sha256=False)
    model = tmp_path / "model.gguf"
    original_size = model.stat().st_size
    model.write_bytes(b"X" * original_size)
    record = load_and_verify_fast_artifact(_settings(manifest))
    assert record.model_sha256 == json.loads(manifest.read_text(encoding="utf-8"))["model"]["sha256"]

    model.write_bytes(b"short")
    with pytest.raises(ValueError, match="大小与 manifest 不一致"):
        load_and_verify_fast_artifact(_settings(manifest))


@pytest.mark.parametrize("filename", ["model.gguf", "llama-server.exe", "start.ps1"])
def test_any_artifact_byte_mutation_fails_closed(tmp_path: Path, filename: str) -> None:
    manifest = _write_manifest(tmp_path)
    path = tmp_path / filename
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(ValueError, match="SHA-256"):
        load_and_verify_fast_artifact(_settings(manifest))


def test_profile_parameter_mutation_fails_closed(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    settings = _settings(manifest)
    changed_profile = settings.active_profile.model_copy(update={"temperature": 0.2})
    settings = settings.model_copy(
        update={"profiles": {**settings.profiles, "fast": changed_profile}}
    )

    with pytest.raises(ValueError, match="参数不一致"):
        load_and_verify_fast_artifact(settings)


def test_provider_version_record_contains_verified_artifact_evidence(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    provider = ModelProvider(
        _settings(manifest),
        client=FakeOpenAIClient(["qwen3.6-fast"]),
    )

    version = provider.startup()

    assert version.artifact_id == "unit-fast"
    assert version.model_sha256 == _sha256(tmp_path / "model.gguf")
    assert version.runtime_binary_sha256 == _sha256(tmp_path / "llama-server.exe")
    assert version.launch_script_sha256 == _sha256(tmp_path / "start.ps1")
    assert version.context_window == 16384
    assert version.temperature == 0.1


def test_provider_wraps_manifest_failure_as_stable_startup_error(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    (tmp_path / "model.gguf").write_bytes(b"tampered-model")
    provider = ModelProvider(
        _settings(manifest),
        client=FakeOpenAIClient(["qwen3.6-fast"]),
    )

    with pytest.raises(ModelGatewayStartupError, match="artifact verification failed"):
        provider.startup()
