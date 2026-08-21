"""模型网关的稳定错误分类。

上层状态图应依据 ``code`` 决定 retry、fallback、human 或 fatal，
而不是解析经常变化的第三方异常文本。
"""

from __future__ import annotations


class ModelGatewayError(RuntimeError):
    """所有网关错误的共同基类。"""

    code = "MODEL_GATEWAY_ERROR"


class ModelGatewayStartupError(ModelGatewayError):
    """启动门禁失败；此时 API 不应开始接受业务请求。"""

    code = "MODEL_GATEWAY_STARTUP_FAILED"


class ModelProfileDisabledError(ModelGatewayStartupError):
    """配置中保留了 Profile，但当前策略明确禁止启动或生成。"""

    code = "MODEL_PROFILE_DISABLED"

    def __init__(self, profile: str, reason: str | None) -> None:
        self.profile = profile
        self.reason = reason
        message = f"model profile {profile!r} is temporarily disabled"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


class ModelConnectionError(ModelGatewayStartupError):
    """模型服务不可达或连接超时。"""

    code = "MODEL_CONNECTION_FAILED"


class ModelAliasMismatchError(ModelGatewayStartupError):
    """服务存在，但没有暴露配置要求的精确 alias。"""

    code = "MODEL_ALIAS_MISMATCH"

    def __init__(self, expected_alias: str, available_aliases: tuple[str, ...]) -> None:
        self.expected_alias = expected_alias
        self.available_aliases = available_aliases
        available = ", ".join(available_aliases) if available_aliases else "<none>"
        super().__init__(
            f"configured model alias {expected_alias!r} is not served; available: {available}"
        )


class MultipleModelsServedError(ModelGatewayStartupError):
    """同一服务同时暴露多个模型，违反 P0 单模型常驻约束。"""

    code = "MULTIPLE_MODELS_SERVED"

    def __init__(self, available_aliases: tuple[str, ...]) -> None:
        self.available_aliases = available_aliases
        super().__init__(
            "P0 permits exactly one loaded model per process; served aliases: "
            + ", ".join(available_aliases)
        )


class ModelGenerationError(ModelGatewayError):
    """模型已经通过启动门禁，但生成阶段失败。"""

    code = "MODEL_GENERATION_FAILED"


class ModelGenerationTimeoutError(ModelGenerationError):
    """一次生成超过配置的生成超时。"""

    code = "MODEL_GENERATION_TIMEOUT"


class EmptyModelResponseError(ModelGenerationError):
    """服务返回成功响应，但没有可用的文本内容。"""

    code = "MODEL_EMPTY_RESPONSE"


class StructuredOutputError(ModelGenerationError):
    """首次生成及允许的一次修复都未通过 Schema。"""

    code = "MODEL_SCHEMA_VALIDATION_FAILED"

    def __init__(self, attempts: int, last_error: str) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"structured output did not satisfy the schema after {attempts} attempt(s): "
            f"{last_error}"
        )
