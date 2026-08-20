"""构造严格、有限且可审计的单节点上下文。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from pydantic import JsonValue

from agent.context.contracts import (
    BudgetSnapshot,
    BudgetUsage,
    ContextEvidence,
    DynamicStateSnapshot,
    EvidenceSourceType,
    NodeContext,
    PromptNodeName,
)
from agent.context.summarizer import summarize_run_state
from agent.planning.contracts import ExecutionBudgets
from agent.runtime.state import RunState


def build_budget_snapshot(
    limits: ExecutionBudgets,
    *,
    usage: BudgetUsage | None = None,
    captured_at: datetime | None = None,
) -> BudgetSnapshot:
    """用任务合同上限和已用量生成不可含糊的预算快照。"""

    return BudgetSnapshot(
        limits=limits,
        usage=usage or BudgetUsage(),
        captured_at=captured_at or datetime.now(timezone.utc),
    )


def build_node_context(
    *,
    node_name: PromptNodeName,
    request_id: str,
    node_input: Mapping[str, JsonValue],
    budget_limits: ExecutionBudgets,
    budget_usage: BudgetUsage | None = None,
    requested_output_tokens: int,
    run_state: RunState | None = None,
    dynamic_state: DynamicStateSnapshot | None = None,
    rag_evidence: Sequence[ContextEvidence] = (),
    tool_evidence: Sequence[ContextEvidence] = (),
    generated_at: datetime | None = None,
) -> NodeContext:
    """生成五个 Prompt 共用的上下文信封。

    ``run_state`` 只在 Python 内存中交给摘要器，绝不会直接写入 NodeContext。
    RAG 与工具证据必须由调用方显式标注来源类型、版本和观测/采集时间。
    """

    now = generated_at or datetime.now(timezone.utc)
    summary = (
        summarize_run_state(run_state, summarized_at=now)
        if run_state is not None
        else None
    )

    # 构造器先做快速、易懂的来源分区检查；Pydantic validator 仍是最终防线。
    if any(item.source_type is not EvidenceSourceType.RAG for item in rag_evidence):
        raise ValueError("rag_evidence 中存在非 RAG 来源")
    if any(item.source_type is not EvidenceSourceType.TOOL for item in tool_evidence):
        raise ValueError("tool_evidence 中存在非 tool 来源")

    return NodeContext(
        node_name=node_name,
        request_id=request_id,
        generated_at=now,
        node_input=dict(node_input),
        state_summary=summary,
        dynamic_state=dynamic_state,
        current_task=summary.current_task if summary is not None else None,
        rag_evidence=list(rag_evidence),
        tool_evidence=list(tool_evidence),
        budget=build_budget_snapshot(
            budget_limits,
            usage=budget_usage,
            captured_at=now,
        ),
        requested_output_tokens=requested_output_tokens,
    )


__all__ = ["build_budget_snapshot", "build_node_context"]
