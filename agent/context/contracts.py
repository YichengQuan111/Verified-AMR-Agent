"""P0-05 Context Engineering 使用的严格数据契约。

这些对象只描述“允许进入单个 Prompt 的最小上下文”和五个节点的结构化输出，
不会保存聊天历史，也不实现 LangGraph 状态图或业务工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    computed_field,
    field_validator,
    model_validator,
)

from agent.planning.contracts import ExecutionBudgets, PlanTask, PlanTaskStatus
from agent.planning.dag import validate_dag
from agent.runtime.state import ObservationSource, ObservationStatus, RunStatus
from agent.tools.contracts import ToolName, ToolResultStatus
from domains.amr_warehouse.contracts import (
    AMRTaskStatus,
    ConnectionStatus,
    GridPosition,
    HealthStatus,
)


class ContextContract(BaseModel):
    """上下文契约基类：未知字段不能静默进入模型输入。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class PromptNodeName(str, Enum):
    """P0-05 固定的五个独立 Prompt 节点。"""

    UNDERSTAND_GOAL = "understand_goal"
    PLAN_TASKS = "plan_tasks"
    VERIFY_OBSERVATION = "verify_observation"
    REPLAN = "replan"
    COMPOSE_REPORT = "compose_report"


class EvidenceSourceType(str, Enum):
    """允许进入 Prompt 的外部证据来源。"""

    RAG = "rag"
    TOOL = "tool"


class ContextEvidence(ContextContract):
    """带来源、版本和时间的 RAG 文本或必要工具状态。"""

    source_type: EvidenceSourceType
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    observed_at: AwareDatetime
    collected_at: AwareDatetime
    citation: str = Field(min_length=1)
    content: JsonValue

    @model_validator(mode="after")
    def validate_evidence_time(self) -> "ContextEvidence":
        """证据不能声称在采集之后才被观测到。"""

        if self.observed_at > self.collected_at:
            raise ValueError("observed_at 不能晚于 collected_at")
        return self


class DynamicStateSnapshot(ContextContract):
    """尚未形成 RunState 时可提供的动态环境快照摘要。"""

    source_type: Literal["dynamic_state"] = "dynamic_state"
    snapshot_id: str = Field(min_length=1)
    snapshot_version: str = Field(min_length=1)
    environment_ref: str = Field(min_length=1)
    observed_at: AwareDatetime
    payload: dict[str, JsonValue]


class BudgetUsage(ContextContract):
    """调用节点前已经实际消耗的五类预算。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_steps: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)


class BudgetSnapshot(ContextContract):
    """某个确定时刻的预算上限、用量和可计算剩余额度。"""

    limits: ExecutionBudgets
    usage: BudgetUsage
    captured_at: AwareDatetime

    @computed_field
    @property
    def remaining_input_tokens(self) -> int:
        return max(0, self.limits.max_input_tokens - self.usage.input_tokens)

    @computed_field
    @property
    def remaining_output_tokens(self) -> int:
        return max(0, self.limits.max_output_tokens - self.usage.output_tokens)

    @computed_field
    @property
    def remaining_tool_steps(self) -> int:
        return max(0, self.limits.max_tool_steps - self.usage.tool_steps)

    @computed_field
    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_total_seconds - self.usage.elapsed_seconds)

    @computed_field
    @property
    def remaining_replans(self) -> int:
        return max(0, self.limits.max_replans - self.usage.replans)

    @computed_field
    @property
    def already_exceeded(self) -> bool:
        """识别调用前已经超过任一总预算的异常状态。"""

        return any(
            (
                self.usage.input_tokens > self.limits.max_input_tokens,
                self.usage.output_tokens > self.limits.max_output_tokens,
                self.usage.tool_steps > self.limits.max_tool_steps,
                self.usage.elapsed_seconds > self.limits.max_total_seconds,
                self.usage.replans > self.limits.max_replans,
            )
        )


class AMRStateSummary(ContextContract):
    """状态摘要中保留的单车关键字段，不包含完整事件轨迹。"""

    amr_id: str = Field(min_length=1)
    position: GridPosition
    battery: float = Field(ge=0, le=100)
    load: float = Field(ge=0)
    task_status: AMRTaskStatus
    health_status: HealthStatus
    connection_status: ConnectionStatus


class CurrentTaskSummary(ContextContract):
    """当前子任务的执行所需字段。"""

    task_id: str = Field(min_length=1)
    dependencies: list[str]
    tool_name: ToolName
    tool_arguments: dict[str, JsonValue]
    target_amr: str | None
    completion_criteria: list[str]
    status: PlanTaskStatus
    effect_id: str | None


class ObservationDigest(ContextContract):
    """最近观测的短摘要；有来源标记，但不复制完整工具载荷。"""

    observation_id: str = Field(min_length=1)
    source: ObservationSource
    status: ObservationStatus
    observed_at: AwareDatetime
    summary: str = Field(min_length=1)
    evidence_refs: list[str]
    tool_status: ToolResultStatus | None
    requires_replan: bool
    requires_human: bool


class StateSummary(ContextContract):
    """从 RunState 派生的有版本、时间和长度上限的状态摘要。"""

    summary_version: Literal["1.0"] = "1.0"
    source_type: Literal["dynamic_state"] = "dynamic_state"
    run_id: str
    run_status: RunStatus
    plan_version: int = Field(ge=1)
    contract_id: str
    environment_ref: str
    state_updated_at: AwareDatetime
    summarized_at: AwareDatetime
    current_task: CurrentTaskSummary | None
    task_status_counts: dict[str, int]
    completed_task_ids: list[str]
    failed_task_ids: list[str]
    amrs: list[AMRStateSummary] = Field(max_length=4)
    order_ids: list[str]
    recent_observations: list[ObservationDigest] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_summary_time(self) -> "StateSummary":
        if self.summarized_at < self.state_updated_at:
            raise ValueError("summarized_at 不能早于 state_updated_at")
        return self


# 这些键表示完整对话或完整运行轨迹，不能藏在 node_input 中绕过摘要器。
FORBIDDEN_HISTORY_KEYS = frozenset(
    {
        "history",
        "chathistory",
        "messages",
        "fullhistory",
        "trajectory",
        "trace",
        "observations",
        "runstate",
        "statedelta",
        "toolresult",
    }
)


def _find_forbidden_history_key(value: JsonValue, path: str = "node_input") -> str | None:
    """递归查找可能承载完整历史的键，并返回可定位路径。"""

    if isinstance(value, dict):
        for key, nested in value.items():
            # 去掉下划线、连字符等分隔符，避免 fullHistory / full_history 等写法绕过。
            normalized_key = "".join(
                character for character in key.lower() if character.isalnum()
            )
            if normalized_key in FORBIDDEN_HISTORY_KEYS:
                return f"{path}.{key}"
            found = _find_forbidden_history_key(nested, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found = _find_forbidden_history_key(nested, f"{path}[{index}]")
            if found:
                return found
    return None


class NodeContext(ContextContract):
    """一次独立 Prompt 调用允许看到的完整上下文信封。"""

    context_version: Literal["1.0"] = "1.0"
    node_name: PromptNodeName
    request_id: str = Field(min_length=1)
    generated_at: AwareDatetime
    node_input: dict[str, JsonValue]
    state_summary: StateSummary | None
    dynamic_state: DynamicStateSnapshot | None
    current_task: CurrentTaskSummary | None
    rag_evidence: list[ContextEvidence]
    tool_evidence: list[ContextEvidence]
    budget: BudgetSnapshot
    requested_output_tokens: int = Field(gt=0)

    @field_validator("node_input")
    @classmethod
    def reject_full_history(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """从结构上禁止把完整消息、轨迹、RunState 或 observations 传给模型。"""

        forbidden_path = _find_forbidden_history_key(value)
        if forbidden_path:
            raise ValueError(f"上下文禁止包含完整历史字段: {forbidden_path}")
        return value

    @model_validator(mode="after")
    def validate_sources(self) -> "NodeContext":
        """确保来源分区、版本与上下文生成时间一致。"""

        if any(item.source_type is not EvidenceSourceType.RAG for item in self.rag_evidence):
            raise ValueError("rag_evidence 只能包含 source_type=rag")
        if any(item.source_type is not EvidenceSourceType.TOOL for item in self.tool_evidence):
            raise ValueError("tool_evidence 只能包含 source_type=tool")
        identities = [
            (item.source_type, item.source_id, item.source_version)
            for item in [*self.rag_evidence, *self.tool_evidence]
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("上下文不能包含重复的来源版本")
        if self.budget.captured_at > self.generated_at:
            raise ValueError("预算快照时间不能晚于上下文生成时间")
        if self.state_summary is not None:
            if self.state_summary.summarized_at > self.generated_at:
                raise ValueError("状态摘要时间不能晚于上下文生成时间")
            if self.current_task != self.state_summary.current_task:
                raise ValueError("current_task 必须与状态摘要中的当前任务一致")
        elif self.current_task is not None:
            raise ValueError("没有 state_summary 时不能单独提供 current_task")
        if (
            self.dynamic_state is not None
            and self.dynamic_state.observed_at > self.generated_at
        ):
            raise ValueError("动态状态观测时间不能晚于上下文生成时间")
        if any(
            item.collected_at > self.generated_at
            for item in [*self.rag_evidence, *self.tool_evidence]
        ):
            raise ValueError("证据采集时间不能晚于上下文生成时间")
        return self


class PlanTasksOutput(ContextContract):
    """plan_tasks Prompt 的结构化输出。"""

    plan_version: int = Field(ge=1)
    tasks: list[PlanTask] = Field(min_length=1)
    planning_assumptions: list[str]
    unresolved_risks: list[str]

    @model_validator(mode="after")
    def validate_plan_dag(self) -> "PlanTasksOutput":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("tasks 不能包含重复 task_id")
        validate_dag({task.task_id: task.dependencies for task in self.tasks})
        return self


class VerificationDecision(str, Enum):
    """verify_observation 节点允许返回的下一步建议。"""

    CONTINUE = "continue"
    REPLAN = "replan"
    HUMAN = "human"
    FINISH = "finish"
    FALLBACK = "fallback"


class ObservationVerification(ContextContract):
    """verify_observation Prompt 的结构化输出。"""

    observation_id: str = Field(min_length=1)
    verified: bool
    decision: VerificationDecision
    reason: str = Field(min_length=1)
    evidence_refs: list[str]
    affected_entities: list[str]
    next_task_id: str | None

    @model_validator(mode="after")
    def validate_decision(self) -> "ObservationVerification":
        if self.decision in {VerificationDecision.CONTINUE, VerificationDecision.FINISH} and not self.verified:
            raise ValueError("continue/finish 决策必须建立在 verified=true 上")
        if self.decision is VerificationDecision.REPLAN and not self.affected_entities:
            raise ValueError("replan 决策必须指出 affected_entities")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs 不能重复")
        return self


class ReplanOutput(ContextContract):
    """replan Prompt 的局部重规划输出。"""

    previous_plan_version: int = Field(ge=1)
    new_plan_version: int = Field(ge=2)
    trigger_observation_id: str = Field(min_length=1)
    retained_task_ids: list[str]
    invalidated_task_ids: list[str]
    replacement_tasks: list[PlanTask]
    reason: str = Field(min_length=1)
    requires_human: bool

    @model_validator(mode="after")
    def validate_replan(self) -> "ReplanOutput":
        if self.new_plan_version != self.previous_plan_version + 1:
            raise ValueError("new_plan_version 必须恰好增加 1")
        retained = set(self.retained_task_ids)
        invalidated = set(self.invalidated_task_ids)
        if len(retained) != len(self.retained_task_ids):
            raise ValueError("retained_task_ids 不能重复")
        if len(invalidated) != len(self.invalidated_task_ids):
            raise ValueError("invalidated_task_ids 不能重复")
        if retained & invalidated:
            raise ValueError("保留任务和失效任务不能重叠")

        replacement_ids = [task.task_id for task in self.replacement_tasks]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise ValueError("replacement_tasks 不能包含重复 task_id")
        if set(replacement_ids) & (retained | invalidated):
            raise ValueError("替换任务必须使用新的 task_id")
        if self.requires_human and self.replacement_tasks:
            raise ValueError("requires_human=true 时不能同时给出替换任务")

        # 已保留任务视为已经存在的零入度锚点；替换任务可以依赖它们，不能依赖
        # 已失效或上下文中不存在的任务。这样也能检测替换子图内部的循环。
        dependencies = {task_id: [] for task_id in retained}
        dependencies.update(
            {task.task_id: task.dependencies for task in self.replacement_tasks}
        )
        validate_dag(dependencies)
        return self


class FinalReportStatus(str, Enum):
    """compose_report 可以声明的最终报告状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_HUMAN = "needs_human"


class FinalReport(ContextContract):
    """compose_report Prompt 的结构化、可审计输出。"""

    run_id: str = Field(min_length=1)
    final_status: FinalReportStatus
    state_version: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    generated_at: AwareDatetime
    summary: str = Field(min_length=1)
    completed_order_ids: list[str]
    incomplete_order_ids: list[str]
    evidence_refs: list[str]
    unresolved_risks: list[str]
    budget_usage: BudgetUsage

    @model_validator(mode="after")
    def validate_report_sets(self) -> "FinalReport":
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
        return self


class NodeRoute(str, Enum):
    """独立节点执行后的确定性路由。"""

    SUCCESS = "success"
    FALLBACK = "fallback"
    HUMAN = "human"


OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class NodeExecutionResult(Generic[OutputT]):
    """独立节点的执行结果；预算门禁可在不调用模型时直接返回。"""

    node_name: PromptNodeName
    prompt_id: str
    prompt_version: str
    route: NodeRoute
    output: OutputT | None
    reason_code: str | None
    reason: str | None
    context_digest: str
    estimated_input_tokens: int
    usage_before: BudgetUsage
    usage_after: BudgetUsage
    started_at: datetime
    finished_at: datetime
    model_alias: str | None


__all__ = [
    "AMRStateSummary",
    "BudgetSnapshot",
    "BudgetUsage",
    "ContextEvidence",
    "CurrentTaskSummary",
    "DynamicStateSnapshot",
    "EvidenceSourceType",
    "FinalReport",
    "FinalReportStatus",
    "NodeContext",
    "NodeExecutionResult",
    "NodeRoute",
    "ObservationDigest",
    "ObservationVerification",
    "PlanTasksOutput",
    "PromptNodeName",
    "ReplanOutput",
    "StateSummary",
    "VerificationDecision",
]
