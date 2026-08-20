"""P0-05 的 Prompt、有限上下文、状态摘要和独立节点入口。"""

from agent.context.builder import build_budget_snapshot, build_node_context
from agent.context.contracts import (
    BudgetSnapshot,
    BudgetUsage,
    ContextEvidence,
    DynamicStateSnapshot,
    EvidenceSourceType,
    FinalReport,
    NodeContext,
    NodeExecutionResult,
    NodeRoute,
    ObservationVerification,
    PlanTasksOutput,
    PromptNodeName,
    ReplanOutput,
    StateSummary,
)
from agent.context.nodes import (
    StandalonePromptNode,
    compose_report,
    plan_tasks,
    replan,
    understand_goal,
    verify_observation,
)
from agent.context.prompt_registry import (
    PROMPT_DEFINITIONS,
    PromptDefinition,
    get_prompt_definition,
)
from agent.context.summarizer import summarize_run_state

__all__ = [
    "BudgetSnapshot",
    "BudgetUsage",
    "ContextEvidence",
    "DynamicStateSnapshot",
    "EvidenceSourceType",
    "FinalReport",
    "NodeContext",
    "NodeExecutionResult",
    "NodeRoute",
    "ObservationVerification",
    "PROMPT_DEFINITIONS",
    "PlanTasksOutput",
    "PromptDefinition",
    "PromptNodeName",
    "ReplanOutput",
    "StandalonePromptNode",
    "StateSummary",
    "build_budget_snapshot",
    "build_node_context",
    "compose_report",
    "get_prompt_definition",
    "plan_tasks",
    "replan",
    "summarize_run_state",
    "understand_goal",
    "verify_observation",
]
