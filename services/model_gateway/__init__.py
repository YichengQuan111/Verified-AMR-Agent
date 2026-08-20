"""Safe, OpenAI-compatible access to the single active local model."""

from services.model_gateway.contracts import (
    ChatMessage,
    GatewayHealth,
    ModelCallResult,
    ModelVersionRecord,
    StructuredGeneration,
)
from services.model_gateway.provider import ModelProvider
from services.model_gateway.protocols import ModelProviderProtocol

__all__ = [
    "ChatMessage",
    "GatewayHealth",
    "ModelCallResult",
    "ModelProvider",
    "ModelProviderProtocol",
    "ModelVersionRecord",
    "StructuredGeneration",
]

