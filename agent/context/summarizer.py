"""把完整 RunState 压缩为 Prompt 可用的有限状态摘要。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from agent.context.contracts import (
    AMRStateSummary,
    CurrentTaskSummary,
    ObservationDigest,
    StateSummary,
)
from agent.planning.contracts import PlanTask
from agent.runtime.state import Observation, RunState


MAX_RECENT_OBSERVATIONS = 3


def _summarize_task(task: PlanTask | None) -> CurrentTaskSummary | None:
    """只保留执行当前子任务真正需要的字段。"""

    if task is None:
        return None
    return CurrentTaskSummary(
        task_id=task.task_id,
        dependencies=list(task.dependencies),
        tool_name=task.tool_name,
        tool_arguments=dict(task.tool_arguments),
        target_amr=task.target_amr,
        completion_criteria=list(task.completion_criteria),
        status=task.status,
        effect_id=task.effect_id,
    )


def _summarize_observation(observation: Observation) -> ObservationDigest:
    """保留结论和引用，不复制可能很大的 state_delta 或工具输出。"""

    tool_status = (
        observation.tool_result.status if observation.tool_result is not None else None
    )
    return ObservationDigest(
        observation_id=observation.observation_id,
        source=observation.source,
        status=observation.status,
        observed_at=observation.observed_at,
        summary=observation.summary,
        evidence_refs=list(observation.evidence_refs),
        tool_status=tool_status,
        requires_replan=observation.requires_replan,
        requires_human=observation.requires_human,
    )


def summarize_run_state(
    state: RunState,
    *,
    summarized_at: datetime | None = None,
    max_recent_observations: int = MAX_RECENT_OBSERVATIONS,
) -> StateSummary:
    """生成稳定、带版本和时间的状态摘要。

    最多保留三条最新观测，且不携带完整历史、state_delta 或 ToolResult.output。
    调用方若需要某个工具结果，必须单独作为带来源的 ``tool_evidence`` 传入。
    """

    if not 0 <= max_recent_observations <= MAX_RECENT_OBSERVATIONS:
        raise ValueError(
            f"max_recent_observations 必须在 0～{MAX_RECENT_OBSERVATIONS} 之间"
        )
    now = summarized_at or datetime.now(timezone.utc)

    task_by_id = {task.task_id: task for task in state.plan_tasks}
    current_task = (
        task_by_id.get(state.current_task_id)
        if state.current_task_id is not None
        else None
    )
    counts = Counter(task.status.value for task in state.plan_tasks)

    # 先按观测时间从新到旧排序，限制条数后不再保留更早轨迹。
    recent = sorted(
        state.observations,
        key=lambda observation: (observation.observed_at, observation.observation_id),
        reverse=True,
    )[:max_recent_observations]

    return StateSummary(
        run_id=state.run_id,
        run_status=state.status,
        plan_version=state.plan_version,
        contract_id=state.task_contract.contract_id,
        environment_ref=state.task_contract.environment_ref,
        state_updated_at=state.updated_at,
        summarized_at=now,
        current_task=_summarize_task(current_task),
        task_status_counts=dict(sorted(counts.items())),
        completed_task_ids=sorted(state.completed_task_ids),
        failed_task_ids=sorted(state.failed_task_ids),
        amrs=[
            AMRStateSummary(
                amr_id=amr.amr_id,
                position=amr.position,
                battery=amr.battery,
                load=amr.load,
                task_status=amr.task_status,
                health_status=amr.health_status,
                connection_status=amr.connection_status,
            )
            for amr in sorted(state.amr_states, key=lambda item: item.amr_id)
        ],
        order_ids=sorted(order.order_id for order in state.orders),
        recent_observations=[_summarize_observation(item) for item in recent],
    )


__all__ = ["MAX_RECENT_OBSERVATIONS", "summarize_run_state"]
