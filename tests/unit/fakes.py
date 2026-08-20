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
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or ["ok"])
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("fake completion response queue is empty")
        content = self.responses.pop(0)
        return SimpleNamespace(
            id=f"response-{len(self.calls)}",
            system_fingerprint="fake-fingerprint",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )


class FakeOpenAIClient:
    def __init__(
        self,
        aliases: list[str],
        responses: list[str] | None = None,
    ) -> None:
        self.models = FakeModels(aliases)
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)

