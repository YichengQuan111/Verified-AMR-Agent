"""P0 应用的类型化、分层配置入口。

配置优先级从低到高依次为：

1. Python 代码中的安全默认值；
2. ``config/default.toml``；
3. ``config/<environment>.toml``；
4. 调用方显式指定的 TOML 文件；
5. 环境变量。

后加载的配置只覆盖自己提供的字段，而不是把整棵配置树替换掉。
密码、API Key、数据库 DSN 使用 ``SecretStr``，避免在日志或调试输出中泄漏明文。
"""

from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


# settings.py 位于 services/config/ 下，向上两级就是项目根目录。
# 使用文件位置推导根目录，可以避免程序依赖“当前工作目录”。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class StrictSettingsModel(BaseModel):
    """所有配置模型的共同基类。

    ``extra='forbid'`` 会拒绝拼错或未知的配置名。例如把 ``profile`` 写成
    ``profiel`` 时应立刻报错，而不是静默使用默认值。
    ``populate_by_name`` 允许字段既使用 Python 名称，也使用 TOML 中的别名。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AppInfoSettings(StrictSettingsModel):
    """应用自身的名称、版本和运行环境。"""

    name: str = "amr-agent-api"
    version: str = "0.1.0"
    environment: str = "development"


class LoggingSettings(StrictSettingsModel):
    """结构化日志配置。TOML 中使用更直观的 ``json`` 字段名。"""

    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")

    @field_validator("level")
    @classmethod
    def normalise_level(cls, value: str) -> str:
        """统一转成大写，并尽早拒绝 logging 不认识的级别。"""

        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported log level: {value}")
        return level


class ModelProfileSettings(StrictSettingsModel):
    """单个模型 Profile 的确定性运行参数。

    Profile 只描述应用允许使用的参数。业务代码不能在每次调用时随意覆盖它们，
    这样 Fast/Smart 的行为才可复现、可审计。
    """

    alias: str = Field(min_length=1)
    context_window: int = Field(gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    reasoning_enabled: bool = False
    reasoning_budget_tokens: int = Field(default=0, ge=0)
    # Profile 可以保留 alias/量化等审计信息但禁止实际调用。是否启用不接受环境
    # 变量覆盖，避免部署残留的 LLM_PROFILE 把尚未验收的模型重新带回生产链。
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_disabled_profile(self) -> "ModelProfileSettings":
        """禁用状态必须给出可交接的原因，不能只留下一个含义不明的布尔值。"""

        if not self.enabled and not self.disabled_reason:
            raise ValueError("disabled model profile must provide disabled_reason")
        return self


def _default_profiles() -> dict[str, ModelProfileSettings]:
    """返回全新的 Profile 字典，避免多个 Settings 实例共享可变对象。"""

    return {
        "fast": ModelProfileSettings(
            alias="qwen3.6-fast",
            context_window=8192,
            temperature=0.0,
            reasoning_enabled=False,
            reasoning_budget_tokens=0,
        ),
        "smart": ModelProfileSettings(
            alias="qwen3.8-smart",
            context_window=12288,
            temperature=0.0,
            reasoning_enabled=True,
            reasoning_budget_tokens=512,
            enabled=False,
            disabled_reason="Smart 在线 P0-05 验收仅通过 2/5，等待用户明确指示后再启用",
        ),
    }


class ModelGatewaySettings(StrictSettingsModel):
    """模型网关配置，以及当前进程唯一允许使用的活动 Profile。"""

    # profile 决定本次进程使用 Fast 还是 Smart；运行过程中不热切换。
    profile: str = "fast"
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: SecretStr = SecretStr("dummy")
    # LLM_MODEL 环境变量写入这里。它只能用于重复确认 alias，不能改写 Profile。
    expected_alias_override: str | None = None
    connect_timeout_seconds: float = Field(default=3.0, gt=0)
    generation_timeout_seconds: float = Field(default=120.0, gt=0)
    # le=1 是硬边界：即使配置文件写成 2，也会在启动前被 Pydantic 拒绝。
    max_schema_repair_attempts: int = Field(default=1, ge=0, le=1)
    # 代码级 fallback 保持保守；仓库默认 TOML 会把本地 P0-13 网关上限
    # 提高到 4096，具体节点仍由 Context 预算继续收紧。
    max_output_tokens: int = Field(default=1024, gt=0)
    validate_on_startup: bool = True
    profiles: dict[str, ModelProfileSettings] = Field(default_factory=_default_profiles)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """去掉末尾斜杠，并限制为 HTTP(S) 地址。"""

        normalised = value.rstrip("/")
        if not normalised.startswith(("http://", "https://")):
            raise ValueError("model gateway base_url must use http:// or https://")
        return normalised

    @model_validator(mode="after")
    def validate_selected_profile(self) -> "ModelGatewaySettings":
        """检查 Profile 存在，且 LLM_MODEL 与 Profile 的固定 alias 一致。"""

        if self.profile not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise ValueError(f"unknown LLM profile {self.profile!r}; available: {available}")
        if (
            self.expected_alias_override is not None
            and self.expected_alias_override != self.profiles[self.profile].alias
        ):
            raise ValueError(
                "LLM_MODEL does not match the alias configured for LLM_PROFILE: "
                f"{self.expected_alias_override!r} != {self.profiles[self.profile].alias!r}"
            )
        return self

    @property
    def active_profile(self) -> ModelProfileSettings:
        """取得本次进程真正使用的 Profile 对象。"""

        return self.profiles[self.profile]

    @property
    def active_alias(self) -> str:
        """取得启动门禁要在 ``/v1/models`` 中寻找的精确 alias。"""

        return self.expected_alias_override or self.active_profile.alias


class DatabaseSettings(StrictSettingsModel):
    """PostgreSQL 连接配置；SecretStr 防止 DSN 中的密码被直接打印。"""

    postgres_dsn: SecretStr = SecretStr(
        "postgresql://amr:123456@localhost:5432/amr_agent"
    )


class RetrievalSettings(StrictSettingsModel):
    """P0-07 仓储知识检索的可复现配置。

    模型维度不在这里声明，而是在 ``Embedder`` 加载模型后动态读取。融合权重、
    chunk 上限和拒答阈值必须进入配置，避免在检索代码中散落难以审计的常量。
    """

    qdrant_url: str = "http://localhost:6333"
    collection_name: str = Field(
        default="amr_warehouse_knowledge",
        min_length=1,
        max_length=128,
    )
    embedding_model_path: str = r"E:\Llama.cpp\Embedding"
    embedding_device: str = "cpu"
    embedding_batch_size: int = Field(default=8, ge=1, le=128)
    chunk_max_chars: int = Field(default=1800, ge=256, le=10000)
    default_top_k: int = Field(default=5, ge=1, le=50)
    candidate_multiplier: int = Field(default=4, ge=1, le=20)
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    bm25_saturation: float = Field(default=3.0, gt=0.0)
    # 由 20 例真实 Qwen3-Embedding 评测的可答最低 0.821220 与不可答最高
    # 0.797038 取中点并保留三位小数；仍允许配置覆盖并应在语料/模型变化后重测。
    minimum_hybrid_score: float = Field(default=0.809, ge=0.0, le=1.0)
    # 对 hybrid 未达标的短语义改写，top vector=0.597388；同组不可答最高
    # top vector=0.400358，二者中点约 0.499。该门禁只作为 hybrid 的补充。
    minimum_vector_score: float = Field(default=0.499, ge=-1.0, le=1.0)

    @field_validator("qdrant_url")
    @classmethod
    def validate_qdrant_url(cls, value: str) -> str:
        """限制为 HTTP(S) 并移除末尾斜杠，保证 collection 地址稳定。"""

        normalised = value.rstrip("/")
        if not normalised.startswith(("http://", "https://")):
            raise ValueError("retrieval qdrant_url must use http:// or https://")
        return normalised

    @model_validator(mode="after")
    def validate_hybrid_weights(self) -> "RetrievalSettings":
        """两路权重必须恰好组成一个凸组合，便于解释 hybrid score。"""

        if abs((self.vector_weight + self.bm25_weight) - 1.0) > 1e-9:
            raise ValueError("vector_weight + bm25_weight 必须等于 1.0")
        return self


class AppSettings(StrictSettingsModel):
    """整个应用最终使用的配置树。"""

    app: AppInfoSettings = Field(default_factory=AppInfoSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    model_gateway: ModelGatewaySettings = Field(default_factory=ModelGatewaySettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)


# 先把类型化默认值转换成普通字典，后续各层都在这份字典上进行合并。
DEFAULT_CONFIG: dict[str, Any] = AppSettings().model_dump(mode="python", by_alias=True)


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并两棵配置树，并且不修改调用方传入的原字典。

    例如 overlay 只提供 ``logging.level`` 时，``logging.json`` 仍保留 base 的值。
    如果使用普通的 ``dict.update``，嵌套的 logging 字典会被整体覆盖。
    """

    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _load_toml(path: Path, *, required: bool = False) -> dict[str, Any]:
    """读取 TOML；可选层不存在时返回空字典，显式层不存在时立即报错。"""

    if not path.exists():
        if required:
            raise FileNotFoundError(f"configuration file not found: {path}")
        return {}
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _parse_bool(name: str, value: str) -> bool:
    """把常见环境变量布尔写法转换为 bool，拒绝含义不明确的字符串。"""

    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """按照 (一级键, 二级键, ...) 把环境变量写入嵌套字典。"""

    current = data
    for part in path[:-1]:
        current = current.setdefault(part, {})
    current[path[-1]] = value


def _apply_environment(data: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """应用白名单环境变量。

    这里没有把任意环境变量自动映射进配置，避免意外字段影响运行行为。
    每个条目同时声明目标位置和类型转换函数。
    """

    result = deepcopy(data)
    mappings: dict[str, tuple[tuple[str, ...], Any]] = {
        "AMR_ENV": (("app", "environment"), str),
        "LOG_LEVEL": (("logging", "level"), str),
        "LOG_JSON": (("logging", "json"), lambda value: _parse_bool("LOG_JSON", value)),
        "OPENAI_BASE_URL": (("model_gateway", "base_url"), str),
        "OPENAI_API_KEY": (("model_gateway", "api_key"), str),
        "LLM_PROFILE": (("model_gateway", "profile"), str),
        "LLM_MODEL": (("model_gateway", "expected_alias_override"), str),
        "LLM_CONNECT_TIMEOUT_SECONDS": (
            ("model_gateway", "connect_timeout_seconds"),
            float,
        ),
        "LLM_GENERATION_TIMEOUT_SECONDS": (
            ("model_gateway", "generation_timeout_seconds"),
            float,
        ),
        "MODEL_GATEWAY_VALIDATE_ON_STARTUP": (
            ("model_gateway", "validate_on_startup"),
            lambda value: _parse_bool("MODEL_GATEWAY_VALIDATE_ON_STARTUP", value),
        ),
        "POSTGRES_DSN": (("database", "postgres_dsn"), str),
        "QDRANT_URL": (("retrieval", "qdrant_url"), str),
        "RAG_COLLECTION_NAME": (("retrieval", "collection_name"), str),
        "RAG_EMBEDDING_MODEL_PATH": (("retrieval", "embedding_model_path"), str),
        "RAG_EMBEDDING_DEVICE": (("retrieval", "embedding_device"), str),
        "RAG_EMBEDDING_BATCH_SIZE": (("retrieval", "embedding_batch_size"), int),
        "RAG_CHUNK_MAX_CHARS": (("retrieval", "chunk_max_chars"), int),
        "RAG_DEFAULT_TOP_K": (("retrieval", "default_top_k"), int),
        "RAG_CANDIDATE_MULTIPLIER": (("retrieval", "candidate_multiplier"), int),
        "RAG_VECTOR_WEIGHT": (("retrieval", "vector_weight"), float),
        "RAG_BM25_WEIGHT": (("retrieval", "bm25_weight"), float),
        "RAG_BM25_SATURATION": (("retrieval", "bm25_saturation"), float),
        "RAG_MINIMUM_HYBRID_SCORE": (
            ("retrieval", "minimum_hybrid_score"),
            float,
        ),
        "RAG_MINIMUM_VECTOR_SCORE": (
            ("retrieval", "minimum_vector_score"),
            float,
        ),
    }
    for name, (path, parser) in mappings.items():
        if name in environ and environ[name] != "":
            _set_path(result, path, parser(environ[name]))
    return result


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    project_root: str | Path | None = None,
) -> AppSettings:
    """按照固定优先级加载配置，最后交给 Pydantic 做完整类型校验。"""

    env = os.environ if environ is None else environ
    root = Path(project_root) if project_root is not None else PROJECT_ROOT

    # 第 1～2 层：Python 默认值 + 项目默认 TOML。
    merged = _deep_merge(DEFAULT_CONFIG, _load_toml(root / "config" / "default.toml"))

    # 第 3 层：按 AMR_ENV 选择环境专属文件。文件不存在是允许的。
    environment_name = env.get(
        "AMR_ENV", str(merged.get("app", {}).get("environment", "development"))
    )
    environment_path = root / "config" / f"{environment_name}.toml"
    if environment_path.name != "default.toml":
        merged = _deep_merge(merged, _load_toml(environment_path))

    # 第 4 层：显式文件是调用方明确要求的，因此不存在时必须报错。
    explicit_path = config_path or env.get("AMR_CONFIG_FILE")
    if explicit_path:
        merged = _deep_merge(merged, _load_toml(Path(explicit_path), required=True))

    # 第 5 层：环境变量拥有最高优先级。最终统一进行 Pydantic 校验。
    merged = _apply_environment(merged, env)
    return AppSettings.model_validate(merged)
