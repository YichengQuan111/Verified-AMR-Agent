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
    P005_PROMPT_VERSION,
    PROMPT_DEFINITIONS,
    PromptDefinition,
    get_prompt_definition,
)
from agent.context.shared_prefix import (
    SHARED_PREFIX_ID,
    SHARED_PREFIX_VERSION,
    prepend_shared_system_prefix,
    render_shared_system_prefix,
    shared_system_prefix_digest,
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
    "P005_PROMPT_VERSION",
    "PROMPT_DEFINITIONS",
    "PlanTasksOutput",
    "PromptDefinition",
    "PromptNodeName",
    "ReplanOutput",
    "SHARED_PREFIX_ID",
    "SHARED_PREFIX_VERSION",
    "StandalonePromptNode",
    "StateSummary",
    "build_budget_snapshot",
    "build_node_context",
    "compose_report",
    "get_prompt_definition",
    "plan_tasks",
    "prepend_shared_system_prefix",
    "render_shared_system_prefix",
    "replan",
    "shared_system_prefix_digest",
    "summarize_run_state",
    "understand_goal",
    "verify_observation",
]
