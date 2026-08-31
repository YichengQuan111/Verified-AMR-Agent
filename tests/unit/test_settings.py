from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.config.settings import _read_dotenv, load_settings


def test_environment_variables_have_highest_precedence() -> None:
    settings = load_settings(
        environ={
            "AMR_ENV": "test",
            "LOG_LEVEL": "debug",
            "LOG_JSON": "false",
            "LLM_PROFILE": "fast",
            "LLM_MODEL": "qwen3.6-fast",
            "OPENAI_BASE_URL": "http://127.0.0.1:18080/v1/",
            "LLM_CONNECT_TIMEOUT_SECONDS": "2.5",
            "LLM_GENERATION_TIMEOUT_SECONDS": "45",
            "MODEL_GATEWAY_VALIDATE_ON_STARTUP": "false",
            "RAG_COLLECTION_NAME": "test_warehouse_knowledge",
            "RAG_EMBEDDING_MODEL_PATH": "D:\\models\\embedding",
            "RAG_EMBEDDING_DEVICE": "cuda",
            "RAG_VECTOR_WEIGHT": "0.6",
            "RAG_BM25_WEIGHT": "0.4",
            "RAG_MINIMUM_HYBRID_SCORE": "0.75",
            "RAG_MINIMUM_VECTOR_SCORE": "0.55",
        }
    )

    assert settings.app.environment == "test"
    assert settings.logging.level == "DEBUG"
    assert settings.logging.json_output is False
    assert settings.model_gateway.profile == "fast"
    assert settings.model_gateway.active_alias == "qwen3.6-fast"
    assert settings.model_gateway.active_profile.enabled is True
    assert settings.model_gateway.base_url == "http://127.0.0.1:18080/v1"
    assert settings.model_gateway.connect_timeout_seconds == 2.5
    assert settings.model_gateway.generation_timeout_seconds == 45
    assert settings.model_gateway.validate_on_startup is False
    assert settings.retrieval.collection_name == "test_warehouse_knowledge"
    assert settings.retrieval.embedding_model_path == "D:\\models\\embedding"
    assert settings.retrieval.embedding_device == "cuda"
    assert settings.retrieval.vector_weight == 0.6
    assert settings.retrieval.bm25_weight == 0.4
    assert settings.retrieval.minimum_hybrid_score == 0.75
    assert settings.retrieval.minimum_vector_score == 0.55


def test_explicit_toml_overlays_defaults(tmp_path) -> None:
    override = tmp_path / "override.toml"
    override.write_text(
        "[logging]\nlevel = 'WARNING'\n"
        "[model_gateway]\nmax_output_tokens = 512\n",
        encoding="utf-8",
    )

    settings = load_settings(override, environ={})

    assert settings.logging.level == "WARNING"
    assert settings.model_gateway.max_output_tokens == 512
    assert settings.model_gateway.active_alias == "qwen3.6-fast"


def test_profile_and_explicit_alias_must_agree() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL does not match"):
        load_settings(
            environ={
                "LLM_PROFILE": "smart",
                "LLM_MODEL": "qwen3.6-fast",
            }
        )


def test_smart_profile_is_declared_but_hard_disabled() -> None:
    """Smart 保留版本信息供审计，但环境变量不能把它偷偷重新启用。"""

    settings = load_settings(
        environ={
            "LLM_PROFILE": "smart",
            "LLM_MODEL": "qwen3.8-smart",
        }
    )

    assert settings.model_gateway.active_profile.enabled is False
    assert settings.model_gateway.active_profile.disabled_reason


def test_schema_repairs_can_never_exceed_one(tmp_path) -> None:
    override = tmp_path / "invalid.toml"
    override.write_text(
        "[model_gateway]\nmax_schema_repair_attempts = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_settings(override, environ={})


def test_compose_environment_rejects_all_public_development_secrets() -> None:
    """只改环境名也不能让仓库默认 JWT/DB/dummy model key 进入发布。"""

    with pytest.raises(ValidationError, match="发布环境禁止"):
        load_settings(environ={"AMR_ENV": "compose"})


def test_compose_environment_accepts_independent_strong_secrets() -> None:
    """五个独立凭据与制品门禁齐全时，发布配置才可实例化。"""

    settings = load_settings(
        environ={
            "AMR_ENV": "compose",
            "POSTGRES_DSN": "postgresql://amr:secure-db-password@postgres/amr_agent",
            "AMR_JWT_SECRET": "jwt-secret-0123456789-0123456789-ab",
            "AMR_HITL_SIGNING_SECRET": "hitl-secret-0123456789-0123456789-a",
            "OPENAI_API_KEY": "model-secret-0123456789-0123456789-a",
            "QDRANT_API_KEY": "qdrant-secret-0123456789-0123456789",
            "FAST_MODEL_VERIFY_ARTIFACT": "true",
        }
    )

    assert settings.app.environment == "compose"
    assert settings.model_gateway.artifact_verification_required is True
    assert settings.retrieval.qdrant_api_key is not None


def test_project_dotenv_supplies_rotated_dsn_without_process_env(tmp_path) -> None:
    """轮换后的库密码只在 .env；显式空 environ 叠加 dotenv 必须能读到它。"""

    (tmp_path / ".env").write_text(
        "POSTGRES_DSN=postgresql://amr:rotated-local-secret@127.0.0.1:5432/amr_agent\n"
        "# comment should be ignored\n"
        "EMPTY_VALUE=\n",
        encoding="utf-8",
    )
    parsed = _read_dotenv(tmp_path / ".env")
    assert "EMPTY_VALUE" not in parsed
    settings = load_settings(
        project_root=tmp_path,
        environ={},
        load_dotenv_file=True,
    )
    assert (
        settings.database.postgres_dsn.get_secret_value()
        == "postgresql://amr:rotated-local-secret@127.0.0.1:5432/amr_agent"
    )


def test_prompt_cache_can_be_disabled_by_environment() -> None:
    settings = load_settings(environ={"LLM_PROMPT_CACHE_ENABLED": "false"})
    assert settings.model_gateway.prompt_cache_enabled is False
