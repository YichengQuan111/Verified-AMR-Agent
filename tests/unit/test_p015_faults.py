"""P0-15 故障分类、预算耗尽和 P0-14 局部恢复契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.context import BudgetUsage
from agent.planning import PlanTaskStatus
from agent.runtime import (
    FaultCategory,
    FaultClassifier,
    FaultRecoveryController,
    RecoveryAction,
    RecoveryUsage,
    RunState,
    RunStatus,
)
from agent.runtime.graph import PEVRExecutionError, PEVRGraphRunner, PEVRStage
from agent.tools import ToolError, ToolErrorCategory, ToolName, ToolResult, ToolResultStatus, UserRole, build_tool_registry
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from tests.unit.test_p013_pevr import _contract, _plan
from tests.unit.test_p014_replanner import _replacement_chain


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("source", "expected_category", "expected_action"),
    [
        ({"code": "amr_battery_below_new_task_threshold", "amr_id": "AMR-01"}, FaultCategory.LOW_BATTERY, RecoveryAction.REPLAN),
        ({"code": "amr_unavailable", "amr_id": "AMR-01"}, FaultCategory.AMR_OFFLINE, RecoveryAction.REPLAN),
        ({"code": "forbidden_edge_traversed", "coordinate": {"x": 4, "y": 5}}, FaultCategory.CHANNEL_CLOSED, RecoveryAction.REPLAN),
        ({"code": "workstation_capacity_exceeded", "workstation_id": "S3"}, FaultCategory.WORKSTATION_OCCUPIED, RecoveryAction.RETRY),
        ({"error": {"category": "timeout", "code": "tool_timeout", "message": "超时", "retryable": True}}, FaultCategory.TOOL_TIMEOUT, RecoveryAction.RETRY),
        ({"error": {"category": "unsafe_plan", "code": "route_infeasible", "message": "无解"}}, FaultCategory.PLAN_INFEASIBLE, RecoveryAction.REPLAN),
        ({"code": "plan_validation_failed", "message": "plan_version_invalid"}, FaultCategory.PLAN_INFEASIBLE, RecoveryAction.REPLAN),
        ({"error": {"category": "conflict", "code": "idempotency_key_reused_with_different_request", "message": "冲突"}}, FaultCategory.STATE_CONFLICT, RecoveryAction.HUMAN),
    ],
)
def test_each_required_fault_has_stable_class_and_default_action(
    source: dict[str, object],
    expected_category: FaultCategory,
    expected_action: RecoveryAction,
) -> None:
    """七类异常从底层错误码进入唯一策略表，不由模型自由选择动作。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 2})}
    )
    controller = FaultRecoveryController(contract)
    decision = controller.handle_failure(source)

    assert decision.fault.category is expected_category
    assert decision.fault.code == expected_category.value
    assert decision.action is expected_action
    assert decision.fault.fault_id.startswith("p015:")


def test_retry_budget_is_global_and_terminates_timeout_without_loop() -> None:
    """超时最多消耗两次重试，第三个不同故障直接 fallback。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_retries": 2})}
    )
    controller = FaultRecoveryController(contract)
    decisions = [
        controller.handle_failure(
            {"error": {"category": "timeout", "code": "tool_timeout", "message": "超时", "retryable": True}},
            task_id=f"TASK-{index}",
            tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        )
        for index in range(1, 4)
    ]

    assert [item.action for item in decisions] == [
        RecoveryAction.RETRY,
        RecoveryAction.RETRY,
        RecoveryAction.FALLBACK,
    ]
    assert decisions[-1].terminal is True
    assert decisions[-1].retry_count == 2
    assert controller.budget_usage.retries == 2

    # 同一故障再次上报只复用结果，不会把计数器推过上限。
    repeated = controller.handle_failure(
        {"error": {"category": "timeout", "code": "tool_timeout", "message": "超时", "retryable": True}},
        task_id="TASK-3",
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    assert repeated.action is RecoveryAction.FALLBACK
    assert controller.budget_usage.retries == 2


def test_repeated_nonterminal_fault_advances_to_terminal_instead_of_reusing_retry_forever() -> None:
    """同一工具同一故障连续失败时，也必须按有限额度最终停止。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_retries": 2})}
    )
    controller = FaultRecoveryController(contract)
    source = {
        "error": {
            "category": "timeout",
            "code": "tool_timeout",
            "message": "同一调用连续超时",
            "retryable": True,
        }
    }
    decisions = [
        controller.handle_failure(
            source,
            task_id="TASK-ROUTE",
            tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        )
        for _ in range(3)
    ]

    assert [item.action for item in decisions] == [
        RecoveryAction.RETRY,
        RecoveryAction.RETRY,
        RecoveryAction.FALLBACK,
    ]
    assert len({item.fault.fault_id for item in decisions}) == 1
    assert decisions[-1].terminal is True


@pytest.mark.parametrize(
    ("limit_field", "usage_field", "usage_value"),
    [
        ("max_tool_steps", "tool_steps", 1),
        ("max_total_seconds", "elapsed_seconds", 1.0),
        ("max_input_tokens", "input_tokens", 1),
        ("max_output_tokens", "output_tokens", 1),
    ],
)
def test_total_step_time_and_token_budgets_force_fallback(
    limit_field: str,
    usage_field: str,
    usage_value: int | float,
) -> None:
    """恢复动作开始前，任一总预算达到上限都必须 fallback。"""

    base_contract = _contract()
    contract = base_contract.model_copy(
        update={
            "budgets": base_contract.budgets.model_copy(
                update={limit_field: usage_value, "max_replans": 2, "max_retries": 2}
            )
        }
    )
    usage = RecoveryUsage.model_validate({usage_field: usage_value})
    controller = FaultRecoveryController(contract)
    decision = controller.handle_failure(
        {
            "error": {
                "category": "timeout",
                "code": "tool_timeout",
                "message": "总预算已到上限",
                "retryable": True,
            }
        },
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        usage=usage,
    )

    assert decision.action is RecoveryAction.FALLBACK
    assert decision.terminal is True


def test_replan_budget_stops_after_two_local_replans() -> None:
    """资源故障最多产生两个新计划版本，第三次升级人工而不进入循环。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 2})}
    )
    controller = FaultRecoveryController(contract)
    decisions = [
        controller.handle_failure(
            {"code": "low_battery", "amr_id": "AMR-01"},
            task_id=f"TASK-{index}",
            tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        )
        for index in range(1, 4)
    ]

    assert [item.action for item in decisions] == [
        RecoveryAction.REPLAN,
        RecoveryAction.REPLAN,
        RecoveryAction.HUMAN,
    ]
    assert controller.budget_usage.replans == 2


def test_repeated_replan_fault_escalates_after_two_versions() -> None:
    """同一资源事实在两个局部版本都失败时转人工，不产生第三版。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 2})}
    )
    controller = FaultRecoveryController(contract)
    source = {"code": "channel_closed", "channel_id": "aisle-1"}
    decisions = [
        controller.handle_failure(
            source,
            task_id="TASK-ROUTE",
            tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        )
        for _ in range(3)
    ]

    assert [item.action for item in decisions] == [
        RecoveryAction.REPLAN,
        RecoveryAction.REPLAN,
        RecoveryAction.HUMAN,
    ]
    assert decisions[-1].terminal is True


def test_side_effect_timeout_requires_external_not_found_before_retry() -> None:
    """dispatch 超时没有 P0-14 外部 not_found 证据时必须人工接管。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_retries": 2})}
    )
    controller = FaultRecoveryController(contract)
    decision = controller.handle_failure(
        {"error": {"category": "timeout", "code": "tool_timeout", "message": "派发超时", "retryable": True}},
        tool_name=ToolName.DISPATCH_SIMULATION,
        has_side_effects=True,
    )

    assert decision.fault.category is FaultCategory.TOOL_TIMEOUT
    assert decision.action is RecoveryAction.HUMAN
    assert decision.terminal is True
    assert controller.budget_usage.retries == 0


def test_local_replan_preserves_completed_effect_and_records_fault() -> None:
    """封闭通道只替换未完成后继，并把故障记录带入新的 RunState。"""

    base_contract = _contract()
    contract = base_contract.model_copy(
        update={"budgets": base_contract.budgets.model_copy(update={"max_replans": 2})}
    )
    base_plan = _plan(contract)
    allocate = base_plan.tasks[0].model_copy(
        update={
            "status": PlanTaskStatus.COMPLETED,
            "effect_id": "effect-allocate-p015",
            "evidence_refs": ["tool://allocate-p015"],
        }
    )
    plan = base_plan.model_copy(update={"tasks": [allocate, *base_plan.tasks[1:]]})
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id="run-p015-replan",
        status=RunStatus.EXECUTING,
        plan_version=1,
        task_contract=contract,
        plan_tasks=list(plan.tasks),
        amr_states=list(snapshot.amrs),
        orders=list(contract.orders),
        observations=[],
        current_task_id=None,
        completed_task_ids=[allocate.task_id],
        failed_task_ids=[],
        created_at=NOW,
        updated_at=NOW,
        replan_count=0,
    )
    controller = FaultRecoveryController(contract, run_state=state, clock=lambda: NOW)
    route_id = plan.tasks[1].task_id
    decision = controller.handle_failure(
        {"code": "channel_closed", "message": "通道封闭", "coordinate": {"x": 4, "y": 5}},
        stage="execute",
        task_id=route_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    result = controller.apply_replan(
        state,
        plan,
        decision,
        _replacement_chain(contract, allocate.task_id),
        tool_specs=build_tool_registry().specs(),
        expected_seed=7,
    )

    assert result.state.plan_version == 2
    assert result.state.replan_count == 1
    assert result.state.status is RunStatus.REPLANNING
    assert result.state.fault_history[0].category == FaultCategory.CHANNEL_CLOSED.value
    retained = next(item for item in result.state.plan_tasks if item.task_id == allocate.task_id)
    assert retained.status is PlanTaskStatus.COMPLETED
    assert retained.effect_id == "effect-allocate-p015"
    assert set(result.replan_result.invalidated_task_ids) == {
        plan.tasks[1].task_id,
        plan.tasks[2].task_id,
        plan.tasks[3].task_id,
    }


def test_run_state_rejects_retry_or_fault_history_over_contract_budget() -> None:
    """恢复载荷即使被手工篡改，也不能绕过 RunState 的硬预算校验。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_retries": 1})}
    )
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    with pytest.raises(ValueError, match="retry_count"):
        RunState(
            run_id="run-p015-budget",
            status=RunStatus.PLANNING,
            plan_version=1,
            task_contract=contract,
            plan_tasks=[],
            amr_states=list(snapshot.amrs),
            orders=list(contract.orders),
            observations=[],
            current_task_id=None,
            completed_task_ids=[],
            failed_task_ids=[],
            created_at=NOW,
            updated_at=NOW,
            replan_count=0,
            retry_count=2,
        )


def test_tool_result_timeout_is_classified_with_original_error_evidence() -> None:
    """真实 ToolResult 的 timeout 状态和底层 ToolError 共同进入 P0-15。"""

    result = ToolResult(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        call_id="run-p015:route",
        status=ToolResultStatus.TIMEOUT,
        output=None,
        error=ToolError(
            category=ToolErrorCategory.TIMEOUT,
            code="tool_timeout",
            message="路线工具超时",
            retryable=True,
            details={"timeout_seconds": 20},
        ),
        started_at=NOW,
        finished_at=NOW,
        duration_ms=20_000,
        evidence_refs=["tool://run-p015:route"],
        effect_id=None,
        tool_version="1.0.0",
        principal_role=UserRole.OPERATOR,
        input_digest="a" * 64,
        output_digest=None,
        idempotency_key=None,
        audit_metadata={},
    )
    signal = FaultClassifier.classify(result, stage="execute", task_id="TASK-ROUTE")

    assert signal.category is FaultCategory.TOOL_TIMEOUT
    assert signal.raw_code == "tool_timeout"
    assert signal.evidence_refs == ["tool://run-p015:route"]
    status_only = result.model_copy(update={"error": None})
    assert FaultClassifier.classify(status_only).category is FaultCategory.TOOL_TIMEOUT


def test_pevr_execution_error_keeps_structured_fault_for_recovery_controller() -> None:
    """固定 PEVR 图抛出的旧异常契约可直接进入 P0-15，而不会丢失分类。"""

    error = PEVRExecutionError(
        PEVRStage.EXECUTE,
        "tool_timeout",
        "路线工具超时",
    )
    signal = PEVRGraphRunner.classify_failure(error, task_id="TASK-ROUTE")

    assert signal.category is FaultCategory.TOOL_TIMEOUT
    assert signal.stage == PEVRStage.EXECUTE.value
    controller = FaultRecoveryController(_contract())
    decision = controller.handle_failure(error)
    assert decision.action is RecoveryAction.RETRY
    assert decision.fault.fault_id == signal.fault_id


def test_terminal_run_state_cannot_be_reopened_by_a_late_fault() -> None:
    """迟到异常不能把已完成 Checkpoint 重新打开成执行态。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 2})}
    )
    plan = _plan(contract)
    completed_tasks = [task.model_copy(update={"status": PlanTaskStatus.COMPLETED}) for task in plan.tasks]
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id="run-p015-terminal",
        status=RunStatus.COMPLETED,
        plan_version=1,
        task_contract=contract,
        plan_tasks=completed_tasks,
        amr_states=list(snapshot.amrs),
        orders=list(contract.orders),
        observations=[],
        current_task_id=None,
        completed_task_ids=[task.task_id for task in completed_tasks],
        failed_task_ids=[],
        created_at=NOW,
        updated_at=NOW,
        replan_count=0,
    )
    controller = FaultRecoveryController(contract, run_state=state)
    decision = controller.handle_failure(
        {"error": {"category": "timeout", "code": "tool_timeout", "message": "迟到超时"}},
        task_id=completed_tasks[0].task_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )

    with pytest.raises(ValueError, match="终态 RunState"):
        controller.record_on_run_state(state, decision)


def test_local_replan_rejected_is_plan_infeasible_not_unknown_or_fatal() -> None:
    """apply 失败稳定码必须再走 REPLAN，不能靠 plan_invalid 子串，也不能落入 UNKNOWN。"""

    signal = FaultClassifier.classify(
        {
            "code": "local_replan_rejected",
            "message": "局部重规划未通过完整门禁: ValueError: 故障没有定位到可替换的未完成任务",
        }
    )
    assert signal.category is FaultCategory.PLAN_INFEASIBLE
    assert signal.raw_code == "local_replan_rejected"

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 2})}
    )
    controller = FaultRecoveryController(contract)
    source = {
        "code": "local_replan_rejected",
        "message": "ValueError: 故障没有定位到可替换的未完成任务",
    }
    decisions = [controller.handle_failure(source) for _ in range(3)]
    assert [item.action for item in decisions] == [
        RecoveryAction.REPLAN,
        RecoveryAction.REPLAN,
        RecoveryAction.HUMAN,
    ]
    assert controller.budget_usage.replans == 2


def test_empty_plan_infeasible_impact_invalidates_unfinished_route_chain() -> None:
    """PLAN_INFEASIBLE 且空影响集合时，未完成 route/validate/dispatch 必须被标失效。"""

    from agent.planning.replanner import LocalReplanner

    plan = _plan(_contract())
    controller = FaultRecoveryController(_contract())
    decision = controller.handle_failure({"code": "plan_validation_failed", "message": "测试空影响"})
    assert decision.fault.category is FaultCategory.PLAN_INFEASIBLE
    assert decision.fault.task_id is None
    expanded = PEVRGraphRunner._expand_empty_plan_infeasible_impact(decision, plan)
    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[],
        affected_entities=expanded.fault.affected_entities,
    )
    names = {task.tool_name for task in plan.tasks if task.task_id in analysis.invalidated_task_ids}
    assert names == {
        ToolName.PLAN_MULTI_AMR_ROUTES,
        ToolName.VALIDATE_FLEET_PLAN,
        ToolName.DISPATCH_SIMULATION,
    }
    assert plan.tasks[0].task_id in analysis.retained_task_ids


def test_empty_impact_non_infeasible_stays_unlocated() -> None:
    """非 PLAN_INFEASIBLE 的空影响集合不能偷偷改成换子图。"""

    from agent.planning.replanner import LocalReplanner

    plan = _plan(_contract())
    controller = FaultRecoveryController(_contract())
    decision = controller.handle_failure({"code": "state_conflict", "message": "身份冲突"})
    expanded = PEVRGraphRunner._expand_empty_plan_infeasible_impact(decision, plan)
    assert expanded.fault.affected_entities.tool_names == decision.fault.affected_entities.tool_names
    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[],
        affected_entities=expanded.fault.affected_entities,
        failed_task_id=expanded.fault.task_id,
        failed_tool_name=expanded.fault.tool_name,
    )
    assert analysis.invalidated_task_ids == []


def test_empty_impact_unfinishes_completed_route_before_analyze() -> None:
    """C++ 拒绝时 route 虽已 completed，空影响 fallback 也必须把它移出完成集。"""

    from agent.planning.replanner import LocalReplanner

    contract = _contract()
    base = _plan(contract)
    allocate = base.tasks[0].model_copy(
        update={"status": PlanTaskStatus.COMPLETED, "effect_id": "effect-allocate"}
    )
    route = base.tasks[1].model_copy(update={"status": PlanTaskStatus.COMPLETED})
    plan = base.model_copy(update={"tasks": [allocate, route, *base.tasks[2:]]})
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id="run-p015-unfinish",
        status=RunStatus.EXECUTING,
        plan_version=1,
        task_contract=contract,
        plan_tasks=list(plan.tasks),
        amr_states=list(snapshot.amrs),
        orders=list(contract.orders),
        observations=[],
        current_task_id=None,
        completed_task_ids=[allocate.task_id, route.task_id],
        failed_task_ids=[],
        created_at=NOW,
        updated_at=NOW,
        replan_count=0,
    )
    controller = FaultRecoveryController(contract)
    decision = controller.handle_failure({"code": "plan_validation_failed", "message": "fleet_plan_invalid"})
    expanded = PEVRGraphRunner._expand_empty_plan_infeasible_impact(decision, plan)
    updated_state, updated_plan = PEVRGraphRunner._unfinish_empty_impact_tasks(state, plan, expanded)
    assert allocate.task_id in updated_state.completed_task_ids
    assert route.task_id not in updated_state.completed_task_ids
    analysis = LocalReplanner().analyze(
        updated_plan,
        completed_task_ids=updated_state.completed_task_ids,
        affected_entities=expanded.fault.affected_entities,
    )
    names = {
        task.tool_name
        for task in updated_plan.tasks
        if task.task_id in analysis.invalidated_task_ids
    }
    assert ToolName.PLAN_MULTI_AMR_ROUTES in names
    assert ToolName.VALIDATE_FLEET_PLAN in names
    assert ToolName.DISPATCH_SIMULATION in names
    assert allocate.task_id in analysis.retained_task_ids


def test_compact_plan_for_replan_strips_blocked_cells() -> None:
    """Fast replan 的 current_plan 不能把硬地图障碍数组再抄进输出。"""

    contract = _contract()
    plan = _plan(contract)
    fat = plan.tasks[1].model_copy(
        update={"tool_arguments": {**plan.tasks[1].tool_arguments, "blocked_cells": [{"x": 1, "y": 1}] * 80}}
    )
    bulky = plan.model_copy(update={"tasks": [plan.tasks[0], fat, *plan.tasks[2:]]})
    compact = PEVRGraphRunner._compact_plan_for_replan(bulky)
    assert "blocked_cells" not in compact.tasks[1].tool_arguments
    assert bulky.tasks[1].tool_arguments["blocked_cells"]
    inline = plan.tasks[2].model_copy(
        update={"tool_arguments": {**plan.tasks[2].tool_arguments, "plan": {"schema_version": "1.0", "x": 1}}}
    )
    compacted_validate = PEVRGraphRunner._compact_plan_for_replan(
        plan.model_copy(update={"tasks": [*plan.tasks[:2], inline, plan.tasks[3]]})
    )
    assert compacted_validate.tasks[2].tool_arguments["plan"] == {"$ref": "derived:simulation_plan"}