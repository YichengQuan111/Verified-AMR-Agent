"""模型网关暴露给业务层的稳定数据契约。

这些 Pydantic 模型把 OpenAI SDK 的动态响应转换为项目内部可校验、可序列化的对象。
以后替换底层模型服务时，业务层仍可以继续使用同一组契约。
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class GatewayContract(BaseModel):
    """所有网关契约都拒绝未知字段，防止上游悄悄改变数据形状。"""

    model_config = ConfigDict(extra="forbid")


class ChatMessage(GatewayContract):
    """允许发送给模型的消息。

    role 只允许 system/user/assistant，所以 tool、file 等角色会在发请求前被拒绝。
    """

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class TokenUsage(GatewayContract):
    """统一后的 Token 用量；服务未返回某项时允许为 None。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelVersionRecord(GatewayContract):
    """启动门禁确认过的模型身份快照。"""

    provider: Literal["llama.cpp-openai-compatible"] = "llama.cpp-openai-compatible"
    profile: str
    configured_alias: str
    served_alias: str
    base_url: str
    model_created: int | None = None
    model_owned_by: str | None = None
    openai_sdk_version: str
    observed_at: datetime


class GatewayHealth(GatewayContract):
    """模型健康检查成功时的响应。失败通过稳定异常表达。"""

    status: Literal["ok"] = "ok"
    version: ModelVersionRecord


class ModelCallResult(GatewayContract):
    """一次普通模型调用的文本、用量、结束原因和模型版本证据。"""

    content: str
    response_id: str | None = None
    finish_reason: str | None = None
    system_fingerprint: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    version: ModelVersionRecord


T = TypeVar("T", bound=BaseModel)


class StructuredGeneration(GatewayContract, Generic[T]):
    """一次结构化生成结果。

    ``value`` 已通过调用方给出的 Pydantic 模型校验；``attempts`` 最大为 2，
    分别对应首次生成和最多一次 Schema 修复。
    """

    value: T
    attempts: int = Field(ge=1, le=2)
    repaired: bool
    call: ModelCallResult
    # ``call`` 保留最终响应证据；total_usage 累加首次生成和唯一一次修复，
    # 供 P0-05 的跨节点 Token 预算正确记账。
    total_usage: TokenUsage = Field(default_factory=TokenUsage)
