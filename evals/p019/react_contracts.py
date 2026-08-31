"""P0-19 评测层独立 ReAct 的严格契约。

这些对象只服务对照实验，不进入生产 PEVR 图。模型每轮只输出短
``decision_summary``；完整审计轨迹可持久化，但不得把无限历史重新塞进 Prompt。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.context.contracts import BudgetUsage, ContextEvidence
from agent.planning.contracts import TaskContract
from agent.runtime.hitl import ApprovalGrant, HITLInterrupt
from agent.runtime.state import Observation, RunState
from agent.security.contracts import Principal
from agent.tools.contracts import ToolName, ToolResult, UserRole
from services.amr_simulator.contracts import SimulationPlan


class ReActContract(BaseModel):
    """ReAct 评测对象拒绝未知字段，避免把思维链或任意工具混进状态。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class ReActActionType(str, Enum):
    """模型每轮只能选择一个受控动作。"""

    TOOL = "tool"
    FINISH = "finish"
    STOP = "stop"


class ReActTerminalStatus(str, Enum):
    """由确定性终态检查写入，模型无权直接宣布完成。"""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_STOP = "budget_stop"
    WAITING_APPROVAL = "waiting_approval"


REACT_PROMPT_ID = "amr.eval.p019.react_agent"
REACT_PROMPT_VERSION = "2.1.0"
REACT_RUNNER_VERSION = "p0-19.react.v2"
REACT_DISPATCH_TASK_ID = "react-dispatch"


class ReActRequest(ReActContract):
    """独立 ReAct 入口；字段与共享前置阶段对齐，但不使用 PEVR 图信封。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    raw_request: str = Field(min_length=1, max_length=4000)
    environment_ref: str = Field(min_length=1, max_length=256)
    principal_role: UserRole = UserRole.OPERATOR
    seed: int
    # 与 PEVRRequest 默认 4096 对齐：共享 Understand 的 TaskContract JSON 会被
    # 1024 Token 截断，导致伪失败。decide 循环仍可在 Runner 内单独收紧单次上限。
    requested_output_tokens: int = Field(default=4096, gt=0, le=4096)
    principal: Principal | None = None
    approval_grant: ApprovalGrant | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> "ReActRequest":
        """入口先冻结角色和自然语言，避免循环开始后才发现非法身份。"""

        if not self.raw_request.strip():
            raise ValueError("raw_request 不能只包含空白字符")
        if self.principal is not None and self.principal.role is not self.principal_role:
            raise ValueError("principal.role 必须与 principal_role 一致")
        if isinstance(self.seed, bool):
            raise ValueError("seed 必须是整数而不是布尔值")
        return self


class ReActDecision(ReActContract):
    """模型单轮结构化决定；不保存原始思维链。"""

    action_type: ReActActionType
    tool_name: ToolName | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    decision_summary: str = Field(min_length=1, max_length=300)
    reason_code: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ReActDecision":
        """tool 必须给出白名单工具名；finish/stop 不得夹带工具参数。"""

        if self.action_type is ReActActionType.TOOL:
            if self.tool_name is None:
                raise ValueError("tool 动作必须提供 tool_name")
        elif self.tool_name is not None or self.arguments:
            raise ValueError("finish/stop 不能携带 tool_name 或 arguments")
        return self


class ReActSafetyGateResult(ReActContract):
    """确定性动作门禁结果；模型不能放宽 denied。"""

    allowed: bool
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ReActStep(ReActContract):
    """一轮 decide → guard → act → observe 的可审计记录。"""

    step_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1)
    decision: ReActDecision
    safety_gate: ReActSafetyGateResult
    tool_result: ToolResult | None = None
    observation: Observation | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    prompt_id: str = Field(default=REACT_PROMPT_ID, min_length=1)
    prompt_version: str = Field(default=REACT_PROMPT_VERSION, min_length=1)
    model_version: str | None = None
    tool_version: str | None = None
    raw_chain_of_thought_stored: Literal[False] = False


class ReActRunState(ReActContract):
    """独立 ReAct 运行态；完整轨迹可落盘，Prompt 只读有限摘要。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    request: ReActRequest
    task_contract: TaskContract | None = None
    run_state: RunState | None = None
    rag_evidence: list[ContextEvidence] = Field(default_factory=list)
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retrieve_query: str | None = None
    frozen_facts: dict[str, JsonValue] = Field(default_factory=dict)
    steps: list[ReActStep] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    derived_plan: SimulationPlan | None = None
    validated_plan: SimulationPlan | None = None
    validated_plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validator_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validator_valid: bool = False
    approval_grant: ApprovalGrant | None = None
    hitl_interrupt: HITLInterrupt | None = None
    pending_decision: ReActDecision | None = None
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    loop_iterations: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    dispatch_count: int = Field(default=0, ge=0)
    recovery_action_count: int = Field(default=0, ge=0)
    unknown_side_effect_tools: list[str] = Field(default_factory=list)
    terminal_status: ReActTerminalStatus | None = None
    terminal_code: str | None = Field(default=None, max_length=128)
    terminal_reason: str | None = Field(default=None, max_length=500)
    started_monotonic: float = 0.0


class ReActRunResult(ReActContract):
    """一次独立 ReAct 运行的评测适配结果。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    run_id: str
    trace_id: str
    terminal_status: ReActTerminalStatus
    terminal_code: str | None = None
    terminal_reason: str | None = None
    state: ReActRunState
    trace_events: list[dict[str, Any]]
    tool_results: list[ToolResult]
    model_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    dispatch_count: int = Field(ge=0)
    recovery_action_count: int = Field(ge=0)
    simulation_status: str | None = None
    completed_order_count: int = Field(default=0, ge=0)
    validator_error_count: int = Field(default=0, ge=0)
    charged: bool = False
    side_effect_ids: list[str] = Field(default_factory=list)


class ReActInterrupt(RuntimeError):
    """ReAct 在 dispatch 前等待 HITL；必须由同一 runner 恢复。"""

    def __init__(self, interrupt: HITLInterrupt) -> None:
        super().__init__(
            f"run {interrupt.run_id} 在 task {interrupt.task_id} 等待审批 {interrupt.approval_id}"
        )
        self.code = "hitl_interrupt"
        self.interrupt = interrupt


__all__ = [
    "REACT_DISPATCH_TASK_ID",
    "REACT_PROMPT_ID",
    "REACT_PROMPT_VERSION",
    "REACT_RUNNER_VERSION",
    "ReActActionType",
    "ReActContract",
    "ReActDecision",
    "ReActInterrupt",
    "ReActRequest",
    "ReActRunResult",
    "ReActRunState",
    "ReActSafetyGateResult",
    "ReActStep",
    "ReActTerminalStatus",
]
