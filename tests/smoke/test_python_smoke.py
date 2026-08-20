from __future__ import annotations

from apps.api.main import create_app
from services.config import load_settings
from services.model_gateway import ModelProvider


def test_python_project_skeleton_imports() -> None:
    settings = load_settings(environ={})
    settings.model_gateway.validate_on_startup = False

    provider = ModelProvider(settings.model_gateway)
    application = create_app(settings=settings, model_provider=provider)

    assert application.title == "AMR Agent API"
    assert settings.model_gateway.active_alias == "qwen3.6-fast"

