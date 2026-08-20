from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.config.settings import AppSettings, ModelGatewaySettings
from services.model_gateway.exceptions import ModelAliasMismatchError
from services.model_gateway.provider import ModelProvider
from tests.unit.fakes import FakeOpenAIClient


def test_api_smoke_without_live_model_for_isolated_test_mode() -> None:
    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model_validated"] is False


def test_wrong_alias_prevents_api_startup() -> None:
    gateway_settings = ModelGatewaySettings(profile="fast", validate_on_startup=True)
    settings = AppSettings(model_gateway=gateway_settings)
    provider = ModelProvider(
        gateway_settings,
        client=FakeOpenAIClient(["qwen3.8-smart"]),
    )
    app = create_app(settings=settings, model_provider=provider)

    with pytest.raises(ModelAliasMismatchError):
        with TestClient(app):
            pass


def test_model_health_endpoint_returns_version_record() -> None:
    gateway_settings = ModelGatewaySettings(profile="fast", validate_on_startup=True)
    settings = AppSettings(model_gateway=gateway_settings)
    provider = ModelProvider(
        gateway_settings,
        client=FakeOpenAIClient(["qwen3.6-fast"]),
    )
    app = create_app(settings=settings, model_provider=provider)

    with TestClient(app) as client:
        response = client.get("/health/model")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]["served_alias"] == "qwen3.6-fast"

