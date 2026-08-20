"""白名单工具的声明与调用结果契约。

P0-04 只固定工具名称、参数边界和返回数据形状；真正的工具注册与执行属于 P0-12，
本模块不会访问网络、数据库、C++ 可执行程序或仿真器。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import NamedTuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class ToolContract(BaseModel):
    """工具契约基类：所有未声明字段都在进入运行时前被拒绝。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class ToolName(str, Enum):
    """P0-12 计划实现的九个工具名称；P0-04 仅定义白名单。"""

    RETRIEVE_KNOWLEDGE = "retrieve_knowledge"
    GET_FLEET_STATE = "get_fleet_state"
    ALLOCATE_TASKS = "allocate_tasks"
    PLAN_MULTI_AMR_ROUTES = "plan_multi_amr_routes"
    VALIDATE_FLEET_PLAN = "validate_fleet_plan"
    DISPATCH_SIMULATION = "dispatch_simulation"
    QUERY_EXECUTION_STATE = "query_execution_state"
    RUN_VERIFICATION_SUITE = "run_verification_suite"
    REQUEST_APPROVAL = "request_approval"


class UserRole(str, Enum):
    """P0 固定的两级工具访问角色。"""

    VIEWER = "viewer"
    OPERATOR = "operator"


class ToolResultStatus(str, Enum):
    """一次工具调用的终态。"""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DENIED = "denied"


class ToolErrorCategory(str, Enum):
    """稳定错误分类，供后续重试、重规划和人工接管策略使用。"""

    INVALID_ARGUMENT = "invalid_argument"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNSAFE_PLAN = "unsafe_plan"
    INTERNAL = "internal"


class ToolArgumentPolicy(NamedTuple):
    """一个工具允许出现的必填和可选顶层参数名。"""

    required: frozenset[str]
    optional: frozenset[str]

    @property
    def allowed(self) -> frozenset[str]:
        """返回必填参数与可选参数的并集。"""

        return self.required | self.optional


# 参数白名单是 P0-04 的确定性安全边界。这里只校验顶层键；每个参数的精细 JSON
# 类型与范围由 ToolSpec.input_schema 描述，并将在 P0-12 注册工具时继续校验。
TOOL_ARGUMENT_POLICIES: dict[ToolName, ToolArgumentPolicy] = {
    ToolName.RETRIEVE_KNOWLEDGE: ToolArgumentPolicy(
        required=frozenset({"query"}),
        optional=frozenset({"top_k", "role_scope", "document_ids"}),
    ),
    ToolName.GET_FLEET_STATE: ToolArgumentPolicy(
        required=frozenset({"environment_ref"}),
        optional=frozenset({"amr_ids"}),
    ),
    ToolName.ALLOCATE_TASKS: ToolArgumentPolicy(
        required=frozenset({"order_ids", "environment_ref"}),
        optional=frozenset({"amr_ids"}),
    ),
    ToolName.PLAN_MULTI_AMR_ROUTES: ToolArgumentPolicy(
        required=frozenset({"assignments", "environment_ref"}),
        optional=frozenset({"blocked_cells", "max_time"}),
    ),
    ToolName.VALIDATE_FLEET_PLAN: ToolArgumentPolicy(
        required=frozenset({"plan", "environment_ref"}),
        optional=frozenset({"ruleset_version"}),
    ),
    ToolName.DISPATCH_SIMULATION: ToolArgumentPolicy(
        required=frozenset({"plan", "seed"}),
        optional=frozenset({"until_time"}),
    ),
    ToolName.QUERY_EXECUTION_STATE: ToolArgumentPolicy(
        required=frozenset({"run_id"}),
        optional=frozenset({"task_ids", "amr_ids"}),
    ),
    ToolName.RUN_VERIFICATION_SUITE: ToolArgumentPolicy(
        required=frozenset({"suite_id"}),
        optional=frozenset({"run_id", "case_ids"}),
    ),
    ToolName.REQUEST_APPROVAL: ToolArgumentPolicy(
        required=frozenset({"run_id", "task_id", "reason"}),
        optional=frozenset({"expires_at"}),
    ),
}


def validate_tool_arguments(tool_name: ToolName, arguments: dict[str, JsonValue]) -> None:
    """按工具白名单拒绝缺少的必填参数和越权参数。

    此函数不执行工具，仅用于 PlanTask 与未来工具注册表共享同一套顶层参数规则。
    """

    policy = TOOL_ARGUMENT_POLICIES[tool_name]
    argument_names = set(arguments)
    missing = sorted(policy.required - argument_names)
    unauthorized = sorted(argument_names - policy.allowed)
    if missing:
        raise ValueError(f"工具 {tool_name.value} 缺少必填参数: {', '.join(missing)}")
    if unauthorized:
        raise ValueError(f"工具 {tool_name.value} 包含未授权参数: {', '.join(unauthorized)}")


class ToolError(ToolContract):
    """工具失败时可序列化、可分类的错误证据。"""

    category: ToolErrorCategory
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, JsonValue]


class ToolSpec(ToolContract):
    """一个白名单工具的静态注册说明。"""

    tool_name: ToolName
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    allowed_roles: list[UserRole] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, le=300)
    idempotent: bool
    has_side_effects: bool
    requires_approval: bool
    error_categories: list[ToolErrorCategory] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_spec(self) -> "ToolSpec":
        """保证 Schema 为封闭对象，并与工具参数白名单保持一致。"""

        if len(self.allowed_roles) != len(set(self.allowed_roles)):
            raise ValueError("allowed_roles 不能包含重复角色")
        if len(self.error_categories) != len(set(self.error_categories)):
            raise ValueError("error_categories 不能包含重复分类")
        if self.has_side_effects and UserRole.VIEWER in self.allowed_roles:
            raise ValueError("viewer 不能执行带副作用的工具")

        self._validate_json_object_schema(self.input_schema, schema_name="input_schema")
        self._validate_json_object_schema(self.output_schema, schema_name="output_schema")

        policy = TOOL_ARGUMENT_POLICIES[self.tool_name]
        properties = set(self.input_schema["properties"])
        required_value = self.input_schema.get("required", [])
        if not isinstance(required_value, list) or not all(
            isinstance(item, str) for item in required_value
        ):
            raise ValueError("input_schema.required 必须是字符串数组")
        required = set(required_value)

        unauthorized = sorted(properties - policy.allowed)
        missing_properties = sorted(policy.required - properties)
        missing_required = sorted(policy.required - required)
        undeclared_required = sorted(required - properties)
        if unauthorized:
            raise ValueError(f"input_schema 包含未授权参数: {', '.join(unauthorized)}")
        if missing_properties:
            raise ValueError(f"input_schema 缺少必填参数定义: {', '.join(missing_properties)}")
        if missing_required:
            raise ValueError(f"input_schema.required 缺少: {', '.join(missing_required)}")
        if undeclared_required:
            raise ValueError(f"input_schema.required 引用了未声明参数: {', '.join(undeclared_required)}")
        return self

    @staticmethod
    def _validate_json_object_schema(schema: dict[str, JsonValue], *, schema_name: str) -> None:
        """检查工具输入/输出 Schema 是否拒绝额外属性。"""

        if schema.get("type") != "object":
            raise ValueError(f"{schema_name}.type 必须为 object")
        if not isinstance(schema.get("properties"), dict):
            raise ValueError(f"{schema_name}.properties 必须为对象")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{schema_name}.additionalProperties 必须为 false")


class ToolResult(ToolContract):
    """一次工具调用的统一结果、错误和审计证据。"""

    tool_name: ToolName
    call_id: str = Field(min_length=1)
    status: ToolResultStatus
    output: JsonValue | None
    error: ToolError | None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    evidence_refs: list[str]
    effect_id: str | None

    @model_validator(mode="after")
    def validate_result_consistency(self) -> "ToolResult":
        """保证成功/失败载荷和时间信息不会互相矛盾。"""

        if self.status is ToolResultStatus.SUCCESS and self.error is not None:
            raise ValueError("成功结果不能携带 error")
        if self.status is not ToolResultStatus.SUCCESS and self.error is None:
            raise ValueError("非成功结果必须携带 error")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at 不能早于 started_at")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs 不能重复")
        return self


__all__ = [
    "TOOL_ARGUMENT_POLICIES",
    "ToolError",
    "ToolErrorCategory",
    "ToolName",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "UserRole",
    "validate_tool_arguments",
]
