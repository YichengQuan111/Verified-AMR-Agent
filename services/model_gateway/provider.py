"""统一的 OpenAI 兼容模型 Provider。

这个文件是 P0-03 的核心：它把 llama.cpp 的动态 HTTP 接口收窄为一个受控入口，
负责启动 alias 门禁、超时、结构化输出校验、一次修复和版本证据记录。
业务层不能通过它传入文件、Shell、工具定义或任意 OpenAI 请求参数。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from importlib import metadata
from threading import Lock
from typing import Any, Mapping, Sequence, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError

from services.config.settings import ModelGatewaySettings
from services.model_gateway.contracts import (
    ChatMessage,
    GatewayHealth,
    ModelCallResult,
    ModelVersionRecord,
    StructuredGeneration,
    TokenUsage,
)
from services.model_gateway.exceptions import (
    EmptyModelResponseError,
    ModelAliasMismatchError,
    ModelConnectionError,
    ModelGenerationError,
    ModelGenerationTimeoutError,
    ModelGatewayStartupError,
    MultipleModelsServedError,
    StructuredOutputError,
)
from services.observability import get_logger


# T 表示调用方期望的任意 Pydantic 输出类型，例如 TransportExtraction。
T = TypeVar("T", bound=BaseModel)
MessageInput = ChatMessage | Mapping[str, str]


def _read_field(value: Any, name: str, default: Any = None) -> Any:
    """兼容 SDK 对象和测试用字典两种响应形式。

    真实 OpenAI SDK 通常用 ``response.id``，Fake 或原始 JSON 常用
    ``response['id']``。统一读取后，核心流程无需到处判断类型。
    """

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class ModelProvider:
    """P0 业务代码访问模型的唯一入口。

    公共方法刻意不提供 ``**kwargs``、tools、文件等参数，因此调用方无法绕过
    网关策略。所有请求都使用活动 Profile 的固定参数，并绑定到 ``startup``
    已核验的模型 alias。
    """

    def __init__(self, settings: ModelGatewaySettings, client: Any | None = None) -> None:
        self.settings = settings
        # client 参数用于依赖注入：生产环境创建真实 OpenAI 客户端，单元测试传 Fake。
        self._client = client or self._build_client(settings)
        # 只有启动门禁成功后才写入版本记录；None 表示模型身份尚未确认。
        self._version_record: ModelVersionRecord | None = None
        # 防止多个线程同时首次调用 startup，造成重复的 /v1/models 请求。
        self._startup_lock = Lock()
        self._logger = get_logger(
            __name__, component="model_gateway", profile=settings.profile
        )

    @staticmethod
    def _build_client(settings: ModelGatewaySettings) -> OpenAI:
        """创建真实 OpenAI 客户端，并区分连接超时与响应读取超时。"""

        # timeout 是整次生成/读取上限，connect 单独限制 TCP 连接阶段。
        timeout = httpx.Timeout(
            timeout=settings.generation_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        )
        return OpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout=timeout,
            # 禁用 SDK 隐式重试：上层必须明确知道一次调用是否发生，
            # 后续涉及副作用工具时才能保证幂等和可审计。
            max_retries=0,
        )

    @property
    def version_record(self) -> ModelVersionRecord | None:
        """返回缓存的模型身份；该属性本身不会发起网络请求。"""

        return self._version_record

    def startup(self) -> ModelVersionRecord:
        """执行启动门禁：模型身份不符合预期时拒绝应用启动。"""

        # 快路径：已经成功验证过时直接复用证据，不重复探测。
        if self._version_record is not None:
            return self._version_record

        # 双重检查锁：等待锁的线程拿到锁后先看其他线程是否已经完成验证。
        with self._startup_lock:
            if self._version_record is not None:
                return self._version_record
            try:
                # 启动阶段只需要确认连接和模型列表，因此使用较短的连接超时。
                response = self._client.models.list(
                    timeout=httpx.Timeout(
                        timeout=self.settings.connect_timeout_seconds,
                        connect=self.settings.connect_timeout_seconds,
                    )
                )
            except (APITimeoutError, APIConnectionError) as exc:
                raise ModelConnectionError(
                    f"cannot connect to local model service at {self.settings.base_url}"
                ) from exc
            except APIStatusError as exc:
                raise ModelGatewayStartupError(
                    f"model list request failed with HTTP {exc.status_code}"
                ) from exc

            # llama.cpp 的 /v1/models 返回 data 数组，每个元素至少应包含 id(alias)。
            models = list(_read_field(response, "data", []) or [])
            aliases = tuple(
                str(alias)
                for model in models
                if (alias := _read_field(model, "id")) is not None
            )
            expected_alias = self.settings.active_alias
            # P0 明确规定同一进程只常驻一个模型。即使目标 alias 在列表中，
            # 同时出现第二个模型也应拒绝，以免实际使用对象不确定。
            if len(aliases) > 1:
                raise MultipleModelsServedError(aliases)
            matching_model = next(
                (model for model in models if _read_field(model, "id") == expected_alias),
                None,
            )
            if matching_model is None:
                raise ModelAliasMismatchError(expected_alias, aliases)

            # 只有所有门禁通过后才保存版本证据，避免失败状态被误认为已验证。
            self._version_record = ModelVersionRecord(
                profile=self.settings.profile,
                configured_alias=expected_alias,
                served_alias=str(_read_field(matching_model, "id")),
                base_url=self.settings.base_url,
                model_created=_read_field(matching_model, "created"),
                model_owned_by=_read_field(matching_model, "owned_by"),
                openai_sdk_version=metadata.version("openai"),
                observed_at=datetime.now(timezone.utc),
            )
            self._logger.info(
                "model_gateway_started",
                model_alias=expected_alias,
                served_aliases=aliases,
            )
            return self._version_record

    def health_check(self) -> GatewayHealth:
        """丢弃旧缓存并重新探测，用于 ``/health/model`` 主动健康检查。"""

        self._version_record = None
        return GatewayHealth(version=self.startup())

    def generate_text(self, messages: Sequence[MessageInput]) -> ModelCallResult:
        """生成普通文本；请求前仍会验证消息角色和模型启动门禁。"""

        normalised_messages = self._normalise_messages(messages)
        return self._request_completion(normalised_messages)

    def generate_structured(
        self,
        messages: Sequence[MessageInput],
        response_model: type[T],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredGeneration[T]:
        """生成结构化结果，并最多允许一次由模型执行的 Schema 修复。

        节点可以按自己的剩余预算收紧输出 Token 和超时，但不能超过进程级配置。
        同一上限也用于唯一一次 Schema 修复，避免修复请求绕过节点预算。
        """

        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        # 首次生成和唯一一次修复共享同一份调用级 Token/时间预算；修复不能重新
        # 获得一整份额度。全局设置仍是不可放宽的上限。
        remaining_output_tokens = min(
            self.settings.max_output_tokens,
            max_output_tokens or self.settings.max_output_tokens,
        )
        total_timeout_seconds = min(
            self.settings.generation_timeout_seconds,
            timeout_seconds or self.settings.generation_timeout_seconds,
        )
        deadline = time.monotonic() + total_timeout_seconds

        # 先把字典消息转换成严格 ChatMessage，非法角色会在网络调用前失败。
        normalised_messages = self._normalise_messages(messages)
        # Pydantic 模型是唯一真相来源：同一份 Schema 同时用于约束模型和校验结果。
        schema = response_model.model_json_schema()
        response_format = {
            "type": "json_object",
            "schema": schema,
        }

        attempts = 0
        last_error = "unknown validation failure"
        current_messages = list(normalised_messages)
        final_call: ModelCallResult | None = None
        token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        token_fields_seen: set[str] = set()
        # 默认 maximum_attempts=2：首次生成 + 最多一次修复。
        maximum_attempts = 1 + self.settings.max_schema_repair_attempts

        while attempts < maximum_attempts:
            # 首次请求沿用精确配置值；只有修复请求按单调时钟扣除已耗时。
            remaining_timeout_seconds = (
                total_timeout_seconds
                if attempts == 0
                else deadline - time.monotonic()
            )
            if remaining_timeout_seconds <= 0:
                raise ModelGenerationTimeoutError(
                    f"structured generation exceeded {total_timeout_seconds}s total budget"
                )
            if remaining_output_tokens <= 0:
                break
            attempts += 1
            final_call = self._request_completion(
                current_messages,
                response_format=response_format,
                max_output_tokens=remaining_output_tokens,
                timeout_seconds=remaining_timeout_seconds,
            )
            # Schema 修复也是一次真实模型调用，必须计入节点总预算，不能只记录末次响应。
            for field_name in token_totals:
                value = getattr(final_call.usage, field_name)
                if value is not None:
                    token_totals[field_name] += value
                    token_fields_seen.add(field_name)
            reported_output_tokens = final_call.usage.output_tokens
            if reported_output_tokens is None:
                # 服务缺少 usage 时使用确定性近似，至少扣除 1 Token，避免修复无限复用额度。
                reported_output_tokens = max(1, len(final_call.content.encode("utf-8")) // 4)
            remaining_output_tokens = max(
                0,
                remaining_output_tokens - reported_output_tokens,
            )
            try:
                # 不手写 JSON 字段判断，交给 Pydantic 同时完成 JSON 解析和类型校验。
                value = response_model.model_validate_json(final_call.content)
                return StructuredGeneration[T](
                    value=value,
                    attempts=attempts,
                    repaired=attempts > 1,
                    call=final_call,
                    total_usage=TokenUsage(
                        **{
                            field_name: total
                            if field_name in token_fields_seen
                            else None
                            for field_name, total in token_totals.items()
                        }
                    ),
                )
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempts >= maximum_attempts:
                    break
                # 修复请求保留原始需求，同时附上失败输出、错误摘要和完整 Schema。
                current_messages = self._repair_messages(
                    normalised_messages,
                    invalid_output=final_call.content,
                    validation_error=last_error,
                    schema=schema,
                )
                self._logger.warning(
                    "structured_output_repair_requested",
                    attempt=attempts,
                    response_model=response_model.__name__,
                )

        # 第二次仍失败时必须终止，不能无限循环调用模型。
        raise StructuredOutputError(attempts=attempts, last_error=last_error)

    @staticmethod
    def _normalise_messages(messages: Sequence[MessageInput]) -> list[ChatMessage]:
        """把输入统一成 ChatMessage，并利用其 Literal 角色白名单做安全校验。"""

        if not messages:
            raise ValueError("at least one chat message is required")
        return [
            message
            if isinstance(message, ChatMessage)
            else ChatMessage.model_validate(dict(message))
            for message in messages
        ]

    @staticmethod
    def _repair_messages(
        original_messages: Sequence[ChatMessage],
        *,
        invalid_output: str,
        validation_error: str,
        schema: dict[str, Any],
    ) -> list[ChatMessage]:
        """构造唯一一次修复对话。

        对失败输出和错误文本做截断，防止异常大响应挤占上下文；Schema 保持完整，
        确保修复目标没有歧义。修复提示要求只返回 JSON，不接受解释或 Markdown。
        """

        clipped_output = invalid_output[:4000]
        clipped_error = validation_error[:2000]
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        return [
            *original_messages,
            ChatMessage(role="assistant", content=clipped_output or "{}"),
            ChatMessage(
                role="user",
                content=(
                    "The previous response failed JSON Schema validation. "
                    "Return only one corrected JSON object, with no Markdown or explanation. "
                    f"Validation error: {clipped_error}. JSON Schema: {schema_text}"
                ),
            ),
        ]

    def _request_completion(
        self,
        messages: Sequence[ChatMessage],
        *,
        response_format: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ModelCallResult:
        """组装并发送一条受控请求，再把 SDK 响应归一化为项目契约。

        调用级预算只能取“调用方要求”和“全局配置”中的较小值，防止业务节点
        通过新增参数放宽模型网关的安全边界。
        """

        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        effective_output_tokens = min(
            self.settings.max_output_tokens,
            max_output_tokens or self.settings.max_output_tokens,
        )
        effective_timeout_seconds = min(
            self.settings.generation_timeout_seconds,
            timeout_seconds or self.settings.generation_timeout_seconds,
        )

        # generate_* 即使被直接调用，也必须先经过 startup 门禁。
        version = self.startup()

        # 请求字段在代码中逐项白名单构造。这里没有 **kwargs、tools、tool_choice，
        # 调用方也不能注入 llama.cpp 的文件/Shell/Agent 功能。
        request: dict[str, Any] = {
            "model": version.configured_alias,
            "messages": [message.model_dump() for message in messages],
            "temperature": self.settings.active_profile.temperature,
            "max_tokens": effective_output_tokens,
            "stream": False,
            # 这是 Provider 内部固定的 llama.cpp 模板参数：Fast 关闭思考，
            # Smart 开启但限制为 512 Token。调用方不能覆盖这段 extra_body。
            "extra_body": {
                "chat_template_kwargs": {
                    "enable_thinking": self.settings.active_profile.reasoning_enabled
                },
                "reasoning_budget": self.settings.active_profile.reasoning_budget_tokens,
            },
            "timeout": httpx.Timeout(
                timeout=effective_timeout_seconds,
                connect=min(
                    self.settings.connect_timeout_seconds,
                    effective_timeout_seconds,
                ),
            ),
        }
        if response_format is not None:
            # 只有 generate_structured 会加入 JSON Schema 约束。
            request["response_format"] = response_format

        try:
            response = self._client.chat.completions.create(**request)
        except APITimeoutError as exc:
            raise ModelGenerationTimeoutError(
                f"model generation exceeded {effective_timeout_seconds}s"
            ) from exc
        except APIConnectionError as exc:
            raise ModelGenerationError("model connection failed during generation") from exc
        except APIStatusError as exc:
            raise ModelGenerationError(
                f"model generation failed with HTTP {exc.status_code}"
            ) from exc

        # HTTP 200 不代表响应一定可用，仍要检查 choices 和最终 content。
        choices = list(_read_field(response, "choices", []) or [])
        if not choices:
            raise EmptyModelResponseError("model response contains no choices")
        choice = choices[0]
        message = _read_field(choice, "message")
        content = _read_field(message, "content")
        if not isinstance(content, str) or not content.strip():
            raise EmptyModelResponseError("model response contains no text content")

        # 最后只向业务层返回稳定字段，隐藏 OpenAI SDK 的具体响应类。
        usage = _read_field(response, "usage")
        return ModelCallResult(
            content=content,
            response_id=_read_field(response, "id"),
            finish_reason=_read_field(choice, "finish_reason"),
            system_fingerprint=_read_field(response, "system_fingerprint"),
            usage=TokenUsage(
                input_tokens=_read_field(usage, "prompt_tokens"),
                output_tokens=_read_field(usage, "completion_tokens"),
                total_tokens=_read_field(usage, "total_tokens"),
            ),
            version=version,
        )
