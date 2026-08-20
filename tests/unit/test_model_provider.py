from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.config.settings import ModelGatewaySettings
from services.model_gateway import ChatMessage, ModelProvider
from services.model_gateway.exceptions import (
    ModelAliasMismatchError,
    MultipleModelsServedError,
    StructuredOutputError,
)
from tests.unit.fakes import FakeOpenAIClient


class TransportExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pickup: str
    dropoff: str
    quantity: int = Field(gt=0)


def make_settings(**overrides: object) -> ModelGatewaySettings:
    values: dict[str, object] = {
        "profile": "fast",
        "connect_timeout_seconds": 2.0,
        "generation_timeout_seconds": 30.0,
        "max_schema_repair_attempts": 1,
    }
    values.update(overrides)
    return ModelGatewaySettings(**values)


MESSAGES = [
    ChatMessage(role="system", content="Return the requested transport JSON."),
    ChatMessage(role="user", content="Move two boxes from P1 to S3."),
]


def test_startup_records_the_verified_served_alias() -> None:
    client = FakeOpenAIClient(["qwen3.6-fast"])
    provider = ModelProvider(make_settings(), client=client)

    version = provider.startup()

    assert version.profile == "fast"
    assert version.configured_alias == "qwen3.6-fast"
    assert version.served_alias == "qwen3.6-fast"
    assert version.model_owned_by == "llama.cpp"
    assert version.openai_sdk_version
    assert client.models.calls
    startup_timeout = client.models.calls[0]["timeout"]
    assert isinstance(startup_timeout, httpx.Timeout)
    assert startup_timeout.connect == 2.0
    assert startup_timeout.read == 2.0


def test_startup_rejects_a_wrong_served_alias() -> None:
    provider = ModelProvider(
        make_settings(),
        client=FakeOpenAIClient(["qwen3.8-smart"]),
    )

    with pytest.raises(ModelAliasMismatchError) as error:
        provider.startup()

    assert error.value.expected_alias == "qwen3.6-fast"
    assert error.value.available_aliases == ("qwen3.8-smart",)
    assert provider.version_record is None


def test_startup_rejects_more_than_one_loaded_model() -> None:
    provider = ModelProvider(
        make_settings(),
        client=FakeOpenAIClient(["qwen3.6-fast", "qwen3.8-smart"]),
    )

    with pytest.raises(MultipleModelsServedError):
        provider.startup()


def test_structured_output_is_validated_without_exposing_tools() -> None:
    client = FakeOpenAIClient(
        ["qwen3.6-fast"],
        ['{"pickup":"P1","dropoff":"S3","quantity":2}'],
    )
    provider = ModelProvider(make_settings(), client=client)

    result = provider.generate_structured(MESSAGES, TransportExtraction)

    assert result.value.quantity == 2
    assert result.attempts == 1
    assert result.repaired is False
    request = client.completions.calls[0]
    assert request["model"] == "qwen3.6-fast"
    assert request["temperature"] == 0.0
    assert request["stream"] is False
    assert request["response_format"]["type"] == "json_object"
    assert "tools" not in request
    assert "tool_choice" not in request
    assert request["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_budget": 0,
    }
    generation_timeout = request["timeout"]
    assert isinstance(generation_timeout, httpx.Timeout)
    assert generation_timeout.connect == 2.0
    assert generation_timeout.read == 30.0


def test_structured_call_budget_can_only_tighten_global_limits() -> None:
    client = FakeOpenAIClient(
        ["qwen3.6-fast"],
        ['{"pickup":"P1","dropoff":"S3","quantity":2}'],
    )
    provider = ModelProvider(
        make_settings(max_output_tokens=1024),
        client=client,
    )

    provider.generate_structured(
        MESSAGES,
        TransportExtraction,
        max_output_tokens=128,
        timeout_seconds=5,
    )

    request = client.completions.calls[0]
    assert request["max_tokens"] == 128
    assert request["timeout"].read == 5
    assert request["timeout"].connect == 2


def test_structured_call_budget_cannot_relax_global_limits() -> None:
    client = FakeOpenAIClient(
        ["qwen3.6-fast"],
        ['{"pickup":"P1","dropoff":"S3","quantity":2}'],
    )
    provider = ModelProvider(
        make_settings(max_output_tokens=1024, generation_timeout_seconds=30),
        client=client,
    )

    provider.generate_structured(
        MESSAGES,
        TransportExtraction,
        max_output_tokens=4096,
        timeout_seconds=120,
    )

    request = client.completions.calls[0]
    assert request["max_tokens"] == 1024
    assert request["timeout"].read == 30


def test_invalid_output_gets_exactly_one_schema_repair() -> None:
    client = FakeOpenAIClient(
        ["qwen3.6-fast"],
        [
            '{"pickup":"P1"}',
            '{"pickup":"P1","dropoff":"S3","quantity":2}',
        ],
    )
    provider = ModelProvider(make_settings(), client=client)

    result = provider.generate_structured(MESSAGES, TransportExtraction)

    assert result.repaired is True
    assert result.attempts == 2
    assert result.total_usage.input_tokens == 20
    assert result.total_usage.output_tokens == 10
    assert result.total_usage.total_tokens == 30
    assert len(client.completions.calls) == 2
    assert client.completions.calls[0]["max_tokens"] == 1024
    assert client.completions.calls[1]["max_tokens"] == 1019
    repair_messages = client.completions.calls[1]["messages"]
    assert repair_messages[-1]["role"] == "user"
    assert "failed JSON Schema validation" in repair_messages[-1]["content"]


def test_a_second_invalid_output_stops_after_the_single_repair() -> None:
    client = FakeOpenAIClient(
        ["qwen3.6-fast"],
        ['{"pickup":"P1"}', '{"dropoff":"S3"}'],
    )
    provider = ModelProvider(make_settings(), client=client)

    with pytest.raises(StructuredOutputError) as error:
        provider.generate_structured(MESSAGES, TransportExtraction)

    assert error.value.attempts == 2
    assert len(client.completions.calls) == 2


def test_tool_role_is_rejected_before_any_request() -> None:
    client = FakeOpenAIClient(["qwen3.6-fast"])
    provider = ModelProvider(make_settings(), client=client)

    with pytest.raises(ValidationError):
        provider.generate_text([{"role": "tool", "content": "run shell"}])

    assert client.models.calls == []
    assert client.completions.calls == []
