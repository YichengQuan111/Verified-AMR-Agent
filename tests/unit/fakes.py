from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeModel:
    id: str
    created: int = 1_786_000_000
    owned_by: str = "llama.cpp"


class FakeModels:
    def __init__(self, aliases: list[str]) -> None:
        self.aliases = aliases
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(data=[FakeModel(alias) for alias in self.aliases])


class FakeCompletions:
    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        cached_input_tokens: int | None = None,
    ) -> None:
        self.responses = list(responses or ["ok"])
        self.calls: list[dict[str, Any]] = []
        self.cached_input_tokens = cached_input_tokens

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("fake completion response queue is empty")
        content = self.responses.pop(0)
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        if self.cached_input_tokens is not None:
            usage.prompt_tokens_details = SimpleNamespace(
                cached_tokens=self.cached_input_tokens
            )
        response = SimpleNamespace(
            id=f"response-{len(self.calls)}",
            system_fingerprint="fake-fingerprint",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=usage,
        )
        if self.cached_input_tokens is not None:
            response.timings = SimpleNamespace(
                cache_n=self.cached_input_tokens,
                prompt_n=2,
            )
        return response


class FakeOpenAIClient:
    def __init__(
        self,
        aliases: list[str],
        responses: list[str] | None = None,
        *,
        cached_input_tokens: int | None = None,
    ) -> None:
        self.models = FakeModels(aliases)
        self.completions = FakeCompletions(
            responses,
            cached_input_tokens=cached_input_tokens,
        )
        self.chat = SimpleNamespace(completions=self.completions)

