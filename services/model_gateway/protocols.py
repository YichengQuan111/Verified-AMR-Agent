"""业务层依赖的模型 Provider 接口。

``Protocol`` 使用 Python 的结构化类型：对象只要实现这些方法就符合接口，
无需继承某个基类。生产环境可传入真实 ModelProvider，测试中可传入 Fake，
从而让业务代码完全不知道 llama.cpp、GGUF 路径和 OpenAI SDK 的细节。
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, TypeVar

from pydantic import BaseModel

from services.model_gateway.contracts import (
    ChatMessage,
    GatewayHealth,
    ModelCallResult,
    ModelVersionRecord,
    StructuredGeneration,
)


T = TypeVar("T", bound=BaseModel)
MessageInput = ChatMessage | Mapping[str, str]


class ModelProviderProtocol(Protocol):
    @property
    def version_record(self) -> ModelVersionRecord | None:
        """最近一次启动门禁确认的模型版本；尚未确认时为 None。"""

        ...

    def startup(self) -> ModelVersionRecord:
        """执行启动门禁并返回模型身份。"""

        ...

    def health_check(self) -> GatewayHealth:
        """重新探测服务，而不是只返回缓存。"""

        ...

    def generate_text(self, messages: Sequence[MessageInput]) -> ModelCallResult:
        """生成普通文本。"""

        ...

    def generate_structured(
        self,
        messages: Sequence[MessageInput],
        response_model: type[T],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredGeneration[T]:
        """按 Schema 生成结果；可选预算只能收紧全局 Token 和时间上限。"""

        ...
