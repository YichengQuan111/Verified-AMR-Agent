"""任务理解与结构化计划使用的数据契约。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.planning.dag import validate_dag
from agent.tools.contracts import ToolName, UserRole, validate_tool_arguments
from domains.amr_warehouse.contracts import GridPosition, TransportOrder


class PlanningContract(BaseModel):
    """规划契约基类：拒绝额外字段，避免模型静默创造新参数。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class RiskLevel(str, Enum):
    """任务或计划步骤的风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FallbackStrategy(str, Enum):
    """步骤失败后允许选择的确定性处置方向。"""

    RETRY = "retry"
    REPLAN = "replan"
    FALLBACK = "fallback"
    HUMAN = "human"
    FATAL = "fatal"


class PlanTaskStatus(str, Enum):
    """计划子任务在运行生命周期中的状态。"""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskConstraints(PlanningContract):
    """P0 固定场景中可由确定性校验器检查的业务约束。"""

    map_width: Literal[30]
    map_height: Literal[20]
    blocked_cells: list[GridPosition]
    minimum_battery_percent: float = Field(ge=0, le=100)
    maximum_load_kg: float = Field(gt=0)
    enforce_time_windows: bool

    @model_validator(mode="after")
    def validate_blocked_cells(self) -> "TaskConstraints":
        """同一个封闭栅格只允许声明一次。"""

        coordinates = [(cell.x, cell.y) for cell in self.blocked_cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("blocked_cells 不能包含重复坐标")
        return self


class ApprovalRequirement(PlanningContract):
    """任务合同中的人工审批门禁。"""

    required: bool
    reason: str | None
    required_role: UserRole | None

    @model_validator(mode="after")
    def validate_approval_requirement(self) -> "ApprovalRequirement":
        """需要审批时必须说明原因，且只能由 operator 批准。"""

        if self.required:
            if not self.reason:
                raise ValueError("需要审批时必须提供 reason")
            if self.required_role is not UserRole.OPERATOR:
                raise ValueError("需要审批时 required_role 必须为 operator")
        elif self.required_role is not None:
            raise ValueError("不需要审批时 required_role 必须为 null")
        return self


class ExecutionBudgets(PlanningContract):
    """一次 Agent 运行的硬预算；所有计数都是上限。"""

    max_total_seconds: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_tool_steps: int = Field(gt=0)
    max_replans: int = Field(ge=0, le=2)


class TaskContract(PlanningContract):
    """把自然语言目标冻结为可验证、带预算的业务合同。"""

    contract_id: str = Field(min_length=1)
    schema_version: Literal["1.0"] = "1.0"
    goal: str = Field(min_length=1)
    orders: list[TransportOrder] = Field(min_length=1)
    environment_ref: str = Field(min_length=1)
    constraints: TaskConstraints
    completion_criteria: list[str] = Field(min_length=1)
    risk_level: RiskLevel
    approval: ApprovalRequirement
    budgets: ExecutionBudgets
    missing_information: list[str]

    @model_validator(mode="after")
    def validate_contract(self) -> "TaskContract":
        """校验订单 ID、订单依赖 DAG、条件去重及高风险审批门禁。"""

        order_ids = [order.order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("orders 不能包含重复 order_id")
        validate_dag({order.order_id: order.dependencies for order in self.orders})

        if len(self.completion_criteria) != len(set(self.completion_criteria)):
            raise ValueError("completion_criteria 不能重复")
        if len(self.missing_information) != len(set(self.missing_information)):
            raise ValueError("missing_information 不能重复")
        if self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} and not self.approval.required:
            raise ValueError("high 或 critical 风险合同必须要求人工审批")
        return self


class PlanTask(PlanningContract):
    """计划 DAG 中一个可独立校验、执行和追踪的原子步骤。"""

    task_id: str = Field(min_length=1)
    dependencies: list[str]
    tool_name: ToolName
    tool_arguments: dict[str, JsonValue]
    target_amr: str | None
    pickup: str | None
    dropoff: str | None
    workstation: str | None
    preconditions: list[str]
    completion_criteria: list[str] = Field(min_length=1)
    time_budget: int = Field(gt=0, description="此步骤最多允许的执行时间，单位为秒")
    energy_budget: float = Field(ge=0, le=100, description="此步骤最多允许消耗的电量百分比")
    risk_level: RiskLevel
    approval_required: bool
    fallback_strategy: FallbackStrategy
    status: PlanTaskStatus
    evidence_refs: list[str]
    effect_id: str | None

    @model_validator(mode="after")
    def validate_plan_task(self) -> "PlanTask":
        """拒绝自依赖、重复值、未知参数及绕过高风险审批的步骤。"""

        if self.task_id in self.dependencies:
            raise ValueError("PlanTask 不能依赖自身")
        for field_name, values in (
            ("dependencies", self.dependencies),
            ("preconditions", self.preconditions),
            ("completion_criteria", self.completion_criteria),
            ("evidence_refs", self.evidence_refs),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 不能包含重复值")

        validate_tool_arguments(self.tool_name, self.tool_arguments)
        if self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} and not self.approval_required:
            raise ValueError("high 或 critical 风险步骤必须要求人工审批")
        return self


__all__ = [
    "ApprovalRequirement",
    "ExecutionBudgets",
    "FallbackStrategy",
    "PlanTask",
    "PlanTaskStatus",
    "RiskLevel",
    "TaskConstraints",
    "TaskContract",
]
