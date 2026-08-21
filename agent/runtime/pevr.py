"""P0-13 PEVR 主闭环的公共报告与图状态契约。

``RunState`` 仍是运行事实的唯一聚合状态；这里的 ``PEVRGraphState`` 只是
LangGraph 节点之间的受控信封，保存当前节点需要的引用、工具证据和模型结果，
不会另造一套订单、AMR 或任务状态。最终 ``PEVRRunReport`` 将 LLM 报告、工具
审计和确定性指标合并，方便 CLI、后续 Checkpoint 和人工复核直接消费。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.runtime.hitl import ApprovalGrant, HITLInterrupt
from agent.security.contracts import Principal
from agent.context.contracts import (
    BudgetUsage,
    ContextEvidence,
    FinalReportStatus,
    ObservationVerification,
    PlanTasksOutput,
)
from agent.planning.contracts import TaskContract
from agent.planning.replanner import TaskResourceProvenance
from agent.planning.validator import PlanValidationResult
from agent.runtime.state import Observation, RunState
from agent.runtime.trace import TraceEvent
from agent.tools.contracts import ToolName, ToolResult, ToolResultStatus, UserRole
from services.amr_simulator.contracts import SimulationPlan
from services.model_gateway.contracts import ModelVersionRecord


class PEVRContract(BaseModel):
    """PEVR 对外契约基类；未知字段必须在边界处失败。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class PEVRStage(str, Enum):
    """P0-13 正常链路的固定状态图节点。"""

    GUARD = "guard"
    UNDERSTAND = "understand"
    RETRIEVE = "retrieve"
    PLAN = "plan"
    VALIDATE = "validate"
    EXECUTE = "execute"
    VERIFY = "verify"
    FINISH = "finish"


PEVR_STAGE_ORDER: tuple[PEVRStage, ...] = (
    PEVRStage.GUARD,
    PEVRStage.UNDERSTAND,
    PEVRStage.RETRIEVE,
    PEVRStage.PLAN,
    PEVRStage.VALIDATE,
    PEVRStage.EXECUTE,
    PEVRStage.VERIFY,
    PEVRStage.FINISH,
)


class PEVRRequest(PEVRContract):
    """一次主闭环调用的入口参数。

    ``approval_granted`` 仅为 P0-13 离线兼容夹具保留；一旦绑定 Principal，入口会拒绝
    这个 legacy 布尔开关。安全生产调用必须使用验签 Principal 和由 HITL Store 核验的
    ApprovalGrant，不能由自然语言或 LLM 自己设置。
    """

    schema_version: Literal["1.0"] = "1.0"
    # PostgreSQL runs.run_id 是 String(64)；公共入口先拒绝超长值，避免运行到
    # Checkpoint 才由数据库驱动报错，形成 Pydantic/DB 字段漂移。
    run_id: str = Field(min_length=1, max_length=64)
    # Trace ID 可由上游链路传入；缺省时由 PEVR runner 在运行开始处生成，恢复时
    # 则优先复用 Checkpoint 中的既有值，保证同一 run 不会被切成多条不可关联轨迹。
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    raw_request: str = Field(min_length=1, max_length=4000)
    environment_ref: str = Field(default="warehouse_v1@seed-v1", min_length=1, max_length=256)
    principal_role: UserRole = UserRole.OPERATOR
    seed: int = 7
    # 四步正常 DAG 的结构化 JSON 明显长于 P0-05 的两步示例；4096 是网关
    # 的安全上限，实际请求还会受 Fast 单次 8192 上下文和累计预算约束，避免
    # 把合法 Planner 输出在 JSON 字符串中途截断。更大的需求需由上层显式承担。
    requested_output_tokens: int = Field(default=4096, gt=0)
    # 真实安全入口使用验签 Principal 和审批票据；legacy bool 只为 P0-13
    # 本地兼容测试保留，且不能与真实身份同时出现。
    principal: Principal | None = None
    approval_grant: ApprovalGrant | None = None
    approval_granted: bool = False

    @model_validator(mode="after")
    def validate_entry_scope(self) -> "PEVRRequest":
        """入口先约束本地 seed、角色和自然语言长度，避免进入图后才失败。"""

        if not self.raw_request.strip():
            raise ValueError("raw_request 不能只包含空白字符")
        if not self.environment_ref.startswith("warehouse_v1@"):
            raise ValueError("P0-13 只允许受控 warehouse_v1 环境快照")
        if self.principal_role is not UserRole.OPERATOR:
            raise ValueError("P0-13 正常执行链必须由 operator 调用")
        if self.principal is not None and self.principal.role is not self.principal_role:
            raise ValueError("principal.role 必须与 principal_role 一致")
        if self.principal is not None and self.approval_granted:
            raise ValueError("安全身份不能使用 legacy approval_granted")
        if isinstance(self.seed, bool):
            raise ValueError("seed 必须是整数而不是布尔值")
        return self


class PEVRTraceEvent(PEVRContract):
    """一个状态图节点的进入/完成审计摘要。"""

    sequence: int = Field(ge=1)
    stage: PEVRStage
    status: Literal["completed"] = "completed"
    started_at: AwareDatetime
    finished_at: AwareDatetime
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_time(self) -> "PEVRTraceEvent":
        """图节点时间不能倒流，保证后续指标可直接累计。"""

        if self.finished_at < self.started_at:
            raise ValueError("finished_at 不能早于 started_at")
        return self


class PEVRToolEvidence(PEVRContract):
    """报告中的单次工具证据索引；正文仍由 ToolResult 的 output_digest 追溯。"""

    task_id: str | None
    tool_name: ToolName
    call_id: str
    status: ToolResultStatus
    tool_version: str | None
    principal_role: UserRole | None
    principal_subject: str | None = None
    input_digest: str | None
    output_digest: str | None
    evidence_refs: list[str]
    effect_id: str | None
    error_code: str | None

    @classmethod
    def from_result(cls, result: ToolResult, *, task_id: str | None) -> "PEVRToolEvidence":
        """把完整 ToolResult 收缩为报告索引，同时保留安全审计定位字段。"""

        return cls(
            task_id=task_id,
            tool_name=result.tool_name,
            call_id=result.call_id,
            status=result.status,
            tool_version=result.tool_version,
            principal_role=result.principal_role,
            principal_subject=result.principal_subject,
            input_digest=result.input_digest,
            output_digest=result.output_digest,
            evidence_refs=list(result.evidence_refs),
            effect_id=result.effect_id,
            error_code=result.error.code if result.error is not None else None,
        )


class PEVRMetrics(PEVRContract):
    """正常闭环可由实际轨迹重算的关键指标。"""

    graph_stage_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    successful_tool_call_count: int = Field(ge=0)
    plan_task_count: int = Field(ge=0)
    validator_error_count: int = Field(ge=0)
    retrieval_result_count: int = Field(ge=0)
    completed_order_count: int = Field(ge=0)
    route_count: int = Field(ge=0)
    simulation_status: str
    simulation_end_time: int = Field(ge=0)
    total_tool_duration_ms: int = Field(ge=0)


class PEVRRunReport(PEVRContract):
    """P0-13 最终运行报告，包含引用、计划、工具证据、指标和风险。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=64)
    # 报告与 PEVRRunResult/验证报告共享同一 Trace 关联键；旧 Checkpoint 缺少该
    # 字段时仍允许读取 None，由新运行在入口处补齐。
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    # 安全发布运行必须能从报告直接定位“谁批准、批准哪条 waiting checkpoint”；
    # legacy 离线 fixture 没有真实 HITL，因此保持可空以兼容历史报告。
    principal_subject: str | None = Field(default=None, min_length=1, max_length=128)
    approval_id: str | None = Field(default=None, min_length=1, max_length=128)
    approval_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128)
    final_status: FinalReportStatus
    state_version: str
    plan_version: int = Field(ge=1)
    generated_at: AwareDatetime
    summary: str = Field(min_length=1)
    completed_order_ids: list[str]
    incomplete_order_ids: list[str]
    evidence_refs: list[str]
    citations: list[str]
    tool_evidence: list[PEVRToolEvidence]
    metrics: PEVRMetrics
    unresolved_risks: list[str]
    budget_usage: BudgetUsage
    model: ModelVersionRecord | None

    @model_validator(mode="after")
    def validate_report_sets(self) -> "PEVRRunReport":
        """报告中的集合、引用和计划版本必须没有重复或互相矛盾。"""

        completed = set(self.completed_order_ids)
        incomplete = set(self.incomplete_order_ids)
        if len(completed) != len(self.completed_order_ids):
            raise ValueError("completed_order_ids 不能重复")
        if len(incomplete) != len(self.incomplete_order_ids):
            raise ValueError("incomplete_order_ids 不能重复")
        if completed & incomplete:
            raise ValueError("订单不能同时标记为完成和未完成")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs 不能重复")
        if len(self.citations) != len(set(self.citations)):
            raise ValueError("citations 不能重复")
        approval_fields = (self.approval_id, self.approval_checkpoint_id)
        if any(approval_fields) and (not all(approval_fields) or self.principal_subject is None):
            raise ValueError("HITL 报告必须同时记录 principal、approval_id 和 checkpoint_id")
        return self


class PEVRRunResult(PEVRContract):
    """主图返回值；既给调用方最终报告，也保留可复核的 RunState 和证据。"""

    request: PEVRRequest
    trace_id: str = Field(min_length=1, max_length=128)
    report: PEVRRunReport
    run_state: RunState
    stage_trace: list[PEVRTraceEvent]
    tool_results: list[ToolResult]
    observations: list[Observation]
    verification: ObservationVerification
    resource_provenance: list[TaskResourceProvenance]
    trace_events: list[TraceEvent]

    @property
    def trace(self) -> list[TraceEvent]:
        """为报告/调用方提供更短的 Trace 读取别名，不复制事件数据。"""

        return self.trace_events


class PEVRGraphState(TypedDict, total=False):
    """LangGraph 的受控信封；业务事实始终放在 ``run_state`` 中。"""

    request: PEVRRequest
    trace_id: str
    trace_events: list[TraceEvent]
    stage: PEVRStage
    stage_trace: list[PEVRTraceEvent]
    run_state: RunState | None
    contract: TaskContract | None
    retrieval_result: ToolResult | None
    rag_evidence: list[ContextEvidence]
    plan: PlanTasksOutput | None
    plan_normalization_notes: list[str]
    plan_validation: PlanValidationResult | None
    derived_plan: SimulationPlan | None
    tool_results: list[ToolResult]
    tool_task_ids: list[str | None]
    resource_provenance: list[TaskResourceProvenance]
    observations: list[Observation]
    verification: ObservationVerification | None
    final_report: PEVRRunReport | None
    model_version: ModelVersionRecord | None
    budget_usage: BudgetUsage
    model_call_count: int
    hitl_interrupt: HITLInterrupt | None
    approval_grant: ApprovalGrant | None
    approval_id: str | None
    approval_checkpoint_id: str | None


__all__ = [
    "PEVRGraphState",
    "PEVRMetrics",
    "PEVRRequest",
    "PEVRRunReport",
    "PEVRRunResult",
    "PEVRStage",
    "PEVRToolEvidence",
    "PEVRTraceEvent",
    "PEVR_STAGE_ORDER",
    "TraceEvent",
    "ApprovalGrant",
    "HITLInterrupt",
]
