from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.config.settings import load_settings


def test_environment_variables_have_highest_precedence() -> None:
    settings = load_settings(
        environ={
            "AMR_ENV": "test",
            "LOG_LEVEL": "debug",
            "LOG_JSON": "false",
            "LLM_PROFILE": "smart",
            "LLM_MODEL": "qwen3.8-smart",
            "OPENAI_BASE_URL": "http://127.0.0.1:18080/v1/",
            "LLM_CONNECT_TIMEOUT_SECONDS": "2.5",
            "LLM_GENERATION_TIMEOUT_SECONDS": "45",
            "MODEL_GATEWAY_VALIDATE_ON_STARTUP": "false",
        }
    )

    assert settings.app.environment == "test"
    assert settings.logging.level == "DEBUG"
    assert settings.logging.json_output is False
    assert settings.model_gateway.profile == "smart"
    assert settings.model_gateway.active_alias == "qwen3.8-smart"
    assert settings.model_gateway.active_profile.reasoning_budget_tokens == 512
    assert settings.model_gateway.base_url == "http://127.0.0.1:18080/v1"
    assert settings.model_gateway.connect_timeout_seconds == 2.5
    assert settings.model_gateway.generation_timeout_seconds == 45
    assert settings.model_gateway.validate_on_startup is False


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


def test_schema_repairs_can_never_exceed_one(tmp_path) -> None:
    override = tmp_path / "invalid.toml"
    override.write_text(
        "[model_gateway]\nmax_schema_repair_attempts = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_settings(override, environ={})
