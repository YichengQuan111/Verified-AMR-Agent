"""P0-12 九个白名单工具的输入/输出 Schema。

工具参数不是把任意 JSON 直接交给处理函数：先由这里的 Pydantic 模型校验顶层
字段、枚举、范围、ID 唯一性和跨字段关系，再由注册表启动 RAG、固定 C++ CLI、
仿真或受控验证器。输出模型则把跨层结果重新收口，避免 C++/仿真器返回一份
看似 JSON 但下游无法审计的半结构化载荷。
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    field_validator,
    model_validator,
)

from agent.tools.contracts import UserRole
from domains.amr_warehouse import AMRState, GridPosition
from services.amr_simulator.contracts import RouteStep, SimulationPlan, SimulationResult
from services.retrieval.contracts import RetrievalResponse


class ToolSchema(BaseModel):
    """工具 Schema 的共同基类；嵌套对象同样禁止未声明字段。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _validate_unique_ids(
    values: list[str],
    field_name: str,
    *,
    max_length: int = 128,
) -> list[str]:
    """拒绝重复或空白 ID，避免同一请求被 C++/存储层解释成两种语义。"""

    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} 不能包含空白 ID")
    if any(len(value) > max_length for value in values):
        raise ValueError(f"{field_name} 中的 ID 最长为 {max_length} 个字符")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} 不能包含重复 ID")
    return values


class RetrieveKnowledgeInput(ToolSchema):
    """检索查询；role_scope 只是请求范围，不能提升调用者权限。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: StrictInt = Field(default=5, ge=1, le=50)
    role_scope: UserRole | None = None
    document_ids: list[str] | None = None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """在构造 Embedding/Qdrant 前拒绝空白查询，并统一审计摘要。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能只包含空白字符")
        return normalized

    @model_validator(mode="after")
    def validate_document_ids(self) -> "RetrieveKnowledgeInput":
        """文档过滤列表必须稳定且不重复。"""

        if self.document_ids is not None:
            _validate_unique_ids(self.document_ids, "document_ids")
        return self


class GetFleetStateInput(ToolSchema):
    """读取固定环境快照中的 AMR 状态。"""

    environment_ref: str = Field(min_length=1, max_length=256)
    amr_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_amr_ids(self) -> "GetFleetStateInput":
        """筛选 ID 在执行固定快照查询前先完成重复检查。"""

        if self.amr_ids is not None:
            _validate_unique_ids(self.amr_ids, "amr_ids")
        return self


class AllocateTasksInput(ToolSchema):
    """Hungarian 分配只接收环境快照引用和显式订单/车辆 ID。"""

    order_ids: list[str] = Field(min_length=1)
    environment_ref: str = Field(min_length=1, max_length=256)
    amr_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> "AllocateTasksInput":
        """在构造 C++ 请求前拒绝重复订单或 AMR。"""

        _validate_unique_ids(self.order_ids, "order_ids")
        if self.amr_ids is not None:
            _validate_unique_ids(self.amr_ids, "amr_ids")
        return self


class RouteAssignmentInput(ToolSchema):
    """P0-08 分配结果转给 P0-09 时允许保留 components 审计快照。"""

    amr_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=128)
    components: dict[str, JsonValue] | None = None


class PlanMultiAMRRoutesInput(ToolSchema):
    """多车 A* 输入；blocked_cells 是本次任务的临时封路叠加。"""

    assignments: list[RouteAssignmentInput] = Field(min_length=1)
    environment_ref: str = Field(min_length=1, max_length=256)
    blocked_cells: list[GridPosition] = Field(default_factory=list)
    # P0-09 C++ 在进入搜索前固定拒绝 2000 以上的时间域；工具 Schema 与其
    # 保持同一上限，避免已知非法参数仍启动子进程。
    max_time: StrictInt = Field(default=120, ge=1, le=2000)

    @model_validator(mode="after")
    def validate_assignments(self) -> "PlanMultiAMRRoutesInput":
        """同一 AMR 或订单不能在一份路线请求中重复绑定。"""

        amr_ids = [item.amr_id for item in self.assignments]
        order_ids = [item.order_id for item in self.assignments]
        _validate_unique_ids(amr_ids, "assignments.amr_id")
        _validate_unique_ids(order_ids, "assignments.order_id")
        cells = [(cell.x, cell.y) for cell in self.blocked_cells]
        if len(cells) != len(set(cells)):
            raise ValueError("blocked_cells 不能重复")
        return self


class ValidateFleetPlanInput(ToolSchema):
    """P0-10 计划验证请求；业务非法计划仍交给 C++ 产生错误证据。"""

    plan: SimulationPlan
    environment_ref: str = Field(min_length=1, max_length=256)
    ruleset_version: Literal["p0-10.v1"] = "p0-10.v1"

    @model_validator(mode="after")
    def validate_environment_identity(self) -> "ValidateFleetPlanInput":
        """顶层审计引用必须与计划 envelope 一致，防止错绑环境。"""

        if self.plan.environment_ref != self.environment_ref:
            raise ValueError("environment_ref 必须与 plan.environment_ref 一致")
        if self.plan.ruleset_version != self.ruleset_version:
            raise ValueError("ruleset_version 必须与 plan.ruleset_version 一致")
        return self


class DispatchSimulationInput(ToolSchema):
    """仿真派发输入；故障注入故意不在此 Schema 中出现。"""

    plan: SimulationPlan
    seed: StrictInt
    until_time: StrictInt | None = None

    @model_validator(mode="after")
    def validate_until_time(self) -> "DispatchSimulationInput":
        """执行窗口必须落在已经验证计划的时间范围内。"""

        if self.until_time is not None and not (
            self.plan.start_time <= self.until_time <= self.plan.max_time
        ):
            raise ValueError("until_time 必须落在 plan.start_time..plan.max_time 内")
        return self


class QueryExecutionStateInput(ToolSchema):
    """查询运行/仿真状态的稳定筛选参数。"""

    # 既用于 runs.run_id，也用于固定长度的 simulation ID；两者都不能超过
    # PostgreSQL runs/tool/effect 公共边界的 64 字符。
    run_id: str = Field(min_length=1, max_length=64)
    task_ids: list[str] | None = None
    amr_ids: list[str] | None = None

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        """状态键去除首尾空白，空白 ID 不访问存储。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("run_id 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_filters(self) -> "QueryExecutionStateInput":
        """筛选条件在访问状态存储前完成去重。"""

        if self.task_ids is not None:
            _validate_unique_ids(self.task_ids, "task_ids")
        if self.amr_ids is not None:
            _validate_unique_ids(self.amr_ids, "amr_ids")
        return self


VerificationSuiteId = Literal["p0_12", "p0-12", "p0_python", "p0_cpp", "p0_smoke"]


class RunVerificationSuiteInput(ToolSchema):
    """验证套件选择器只允许注册表内的固定套件 ID。"""

    suite_id: VerificationSuiteId
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    case_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_case_ids(self) -> "RunVerificationSuiteInput":
        """case_ids 仍由固定 runner 再做套件内白名单校验。"""

        if self.case_ids is not None:
            if not self.case_ids:
                raise ValueError("case_ids 不能是空数组")
            _validate_unique_ids(self.case_ids, "case_ids")
        return self


class RequestApprovalInput(ToolSchema):
    """高风险步骤的人工审批请求，不接受自动批准或任意决策字段。"""

    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: AwareDatetime | None = None

    @field_validator("run_id", "task_id", "reason")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        """审批键和原因在写入副作用存储前必须包含实际文本。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("审批字段不能只包含空白字符")
        return normalized


class FleetStateOutput(ToolSchema):
    """车队状态工具的可追溯响应。"""

    environment_ref: str
    state_version: str
    source: Literal["warehouse_seed", "execution_store"]
    amrs: list[AMRState]


class AllocationCostBreakdown(ToolSchema):
    """P0-08 返回的可解释代价分解。"""

    distance_to_pickup: float
    route_distance: float
    estimated_completion_time: float
    lateness_risk: float
    priority_bonus: float
    battery_risk: float
    estimated_battery_after: float
    load_penalty: float
    total_cost: float


class AllocationAssignmentOutput(ToolSchema):
    amr_id: str
    order_id: str
    components: AllocationCostBreakdown


class AllocationPairOutput(ToolSchema):
    amr_id: str
    order_id: str
    cost: float | Literal["INF"]
    reason_codes: list[str]
    reasons: list[str]
    status: Literal["feasible", "infeasible"]
    components: AllocationCostBreakdown | None


class AllocationCandidateReason(ToolSchema):
    amr_id: str
    reason_codes: list[str]
    reasons: list[str]


class AllocationUnassignedOrder(ToolSchema):
    order_id: str
    reason_code: str
    reason_codes: list[str]
    candidate_reasons: list[AllocationCandidateReason]


class AllocationResponse(ToolSchema):
    """P0-08 JSON 响应；INF 只能是字符串，不能污染标准 JSON。"""

    algorithm: Literal["hungarian"]
    amr_ids: list[str]
    assignments: list[AllocationAssignmentOutput]
    cost_matrix: list[list[float | Literal["INF"]]]
    order_ids: list[str]
    pair_evaluations: list[AllocationPairOutput]
    schema_version: Literal["1.0"]
    status: Literal["complete", "partial", "no_feasible_assignment"]
    total_cost: float
    unassigned_amrs: list[str]
    unassigned_orders: list[AllocationUnassignedOrder]


class RouteOutput(ToolSchema):
    """P0-09 单车路线输出，保留不可行原因但不伪造 path。"""

    amr_id: str
    dropoff_time: int
    expanded_states: int
    order_id: str
    path: list[RouteStep]
    pickup_time: int
    priority: int
    reason: str | None
    reason_code: str | None
    status: Literal["planned", "infeasible"]
    total_cost: float


class RoutePlanResponse(ToolSchema):
    """P0-09 多车路线响应。"""

    algorithm: Literal["astar"]
    cell_reservation_count: int
    edge_reservation_count: int
    planned_count: int
    routes: list[RouteOutput]
    schema_version: Literal["1.0"]
    status: Literal["complete", "infeasible"]
    total_cost: float
    total_expanded_states: int


class ValidationEvidenceOutput(ToolSchema):
    """P0-10 错误证据的完整定位字段；不因字段不适用而省略。"""

    code: str
    constraint: str
    message: str
    task_id: str
    related_task_id: str
    order_id: str
    related_order_id: str
    amr_id: str
    related_amr_id: str
    coordinate: GridPosition | None
    related_coordinate: GridPosition | None
    time: int | None
    related_time: int | None
    observed: float | None
    limit: float | None
    path_index: int
    related_path_index: int


class ValidationResponse(ToolSchema):
    """P0-10 验证响应；valid=false 是安全结论而非进程崩溃。"""

    schema_version: Literal["1.0"]
    ruleset_version: Literal["p0-10.v1"]
    status: Literal["valid", "invalid"]
    valid: bool
    error_count: int
    errors: list[ValidationEvidenceOutput]

    @model_validator(mode="after")
    def validate_summary(self) -> "ValidationResponse":
        """拒绝状态、布尔结论和证据数量互相矛盾的 C++ 输出。"""

        if self.error_count != len(self.errors):
            raise ValueError("error_count 必须等于 errors 数量")
        if self.status == "valid":
            if not self.valid or self.errors:
                raise ValueError("status=valid 必须同时满足 valid=true 且 errors 为空")
        elif self.valid or not self.errors:
            raise ValueError("status=invalid 必须同时满足 valid=false 且包含错误证据")
        return self


class ExecutionStateOutput(ToolSchema):
    """运行状态查询输出；snapshot 保留来源系统的完整 JSON 快照。"""

    run_id: str
    source: Literal["run_state", "simulation"]
    status: str
    selected_task_ids: list[str]
    selected_amr_ids: list[str]
    snapshot: dict[str, JsonValue]
    evidence_refs: list[str]


class VerificationCaseOutput(ToolSchema):
    """一个固定验证命令的结果摘要，不把可执行命令暴露成输入契约。"""

    case_id: str
    status: Literal["passed", "failed", "timeout"]
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerificationSuiteOutput(ToolSchema):
    """受控验证套件的汇总。"""

    suite_id: str
    run_id: str | None
    status: Literal["passed", "failed", "timeout"]
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    cases: list[VerificationCaseOutput]

    @model_validator(mode="after")
    def validate_summary(self) -> "VerificationSuiteOutput":
        """汇总计数和 suite 状态必须能由逐 case 结果唯一重算。"""

        passed = sum(item.status == "passed" for item in self.cases)
        failed = sum(item.status == "failed" for item in self.cases)
        timed_out = sum(item.status == "timeout" for item in self.cases)
        if self.case_count != len(self.cases):
            raise ValueError("case_count 必须等于 cases 数量")
        if self.passed_count != passed or self.failed_count != failed:
            raise ValueError("passed_count/failed_count 与 cases 不一致")
        expected_status = "timeout" if timed_out else "failed" if failed else "passed"
        if self.status != expected_status:
            raise ValueError("suite status 与 cases 不一致")
        return self


class ApprovalRequestOutput(ToolSchema):
    """人工审批请求的幂等结果；不会携带自动批准字段。"""

    approval_id: str
    effect_id: str
    run_id: str
    task_id: str
    reason: str
    status: Literal["pending"]
    requested_at: AwareDatetime
    expires_at: AwareDatetime | None


# 让脚本和调用方可以按工具语义发现 P0-11 的两个直接复用输出模型。
DispatchSimulationOutput = SimulationResult
RetrieveKnowledgeOutput = RetrievalResponse


__all__ = [
    "AllocateTasksInput",
    "AllocationAssignmentOutput",
    "AllocationCandidateReason",
    "AllocationCostBreakdown",
    "AllocationPairOutput",
    "AllocationResponse",
    "AllocationUnassignedOrder",
    "ApprovalRequestOutput",
    "DispatchSimulationInput",
    "DispatchSimulationOutput",
    "ExecutionStateOutput",
    "FleetStateOutput",
    "GetFleetStateInput",
    "PlanMultiAMRRoutesInput",
    "QueryExecutionStateInput",
    "RequestApprovalInput",
    "RetrieveKnowledgeInput",
    "RetrieveKnowledgeOutput",
    "RouteAssignmentInput",
    "RouteOutput",
    "RoutePlanResponse",
    "RunVerificationSuiteInput",
    "SimulationPlan",
    "ToolSchema",
    "ValidateFleetPlanInput",
    "ValidationEvidenceOutput",
    "ValidationResponse",
    "VerificationCaseOutput",
    "VerificationSuiteOutput",
    "VerificationSuiteId",
]
