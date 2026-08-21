"""P0-15 与 P0-14 Checkpoint/Effect Ledger 的集成边界测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.planning import PlanTaskStatus
from agent.runtime import (
    FaultCategory,
    FaultRecoveryController,
    RecoveryAction,
    RunState,
    RunStatus,
)
from agent.runtime.checkpoint import InMemoryRuntimeStore, make_effect_idempotency_key
from agent.tools import ToolName, ToolResult, ToolResultStatus, UserRole, build_tool_registry
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from tests.unit.test_p013_pevr import _contract, _plan
from tests.unit.test_p014_replanner import _replacement_chain


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("source", "expected_category", "expected_action"),
    [
        ({"code": "amr_battery_below_new_task_threshold", "amr_id": "AMR-01"}, FaultCategory.LOW_BATTERY, RecoveryAction.REPLAN),
        ({"code": "amr_unavailable", "amr_id": "AMR-01"}, FaultCategory.AMR_OFFLINE, RecoveryAction.REPLAN),
        ({"code": "forbidden_edge_traversed", "edge": {"from": {"x": 4, "y": 5}, "to": {"x": 4, "y": 6}}}, FaultCategory.CHANNEL_CLOSED, RecoveryAction.REPLAN),
        ({"code": "workstation_capacity_exceeded", "workstation_id": "S3"}, FaultCategory.WORKSTATION_OCCUPIED, RecoveryAction.RETRY),
        ({"error": {"category": "timeout", "code": "tool_timeout", "message": "工具超时", "retryable": True}}, FaultCategory.TOOL_TIMEOUT, RecoveryAction.RETRY),
        ({"error": {"category": "unsafe_plan", "code": "route_infeasible", "message": "路线无解"}}, FaultCategory.PLAN_INFEASIBLE, RecoveryAction.REPLAN),
        ({"error": {"category": "conflict", "code": "idempotency_key_reused_with_different_request", "message": "Effect 冲突"}}, FaultCategory.STATE_CONFLICT, RecoveryAction.HUMAN),
    ],
)
def test_all_required_faults_enter_a_checkpointed_recovery_or_terminal_path(
    source: dict[str, object],
    expected_category: FaultCategory,
    expected_action: RecoveryAction,
) -> None:
    """每类 P0-15 故障都写入可重启快照，且动作/终态由策略表唯一决定。"""

    base_contract = _contract()
    contract = base_contract.model_copy(
        update={"budgets": base_contract.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    plan = _plan(contract)
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id=f"run-p015-all-{expected_category.value}",
        status=RunStatus.EXECUTING,
        plan_version=1,
        task_contract=contract,
        plan_tasks=list(plan.tasks),
        amr_states=list(snapshot.amrs),
        orders=list(contract.orders),
        observations=[],
        current_task_id=None,
        completed_task_ids=[],
        failed_task_ids=[],
        created_at=NOW,
        updated_at=NOW,
        replan_count=0,
    )
    controller = FaultRecoveryController(contract, run_state=state, clock=lambda: NOW)
    decision = controller.handle_failure(
        source,
        stage="execute",
        task_id=plan.tasks[1].task_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    recorded = controller.record_on_run_state(state, decision)

    assert decision.fault.category is expected_category
    assert decision.action is expected_action
    assert decision.terminal is (expected_action in {RecoveryAction.FALLBACK, RecoveryAction.HUMAN, RecoveryAction.FATAL})
    assert recorded.fault_history[-1].fault_id == decision.fault.fault_id
    expected_status = RunStatus.REPLANNING if expected_action is RecoveryAction.REPLAN else (
        RunStatus.EXECUTING if expected_action is RecoveryAction.RETRY else RunStatus.FAILED
    )
    assert recorded.status is expected_status

    store = InMemoryRuntimeStore()
    checkpoint = controller.save_replan_checkpoint(
        store,
        request={"run_id": state.run_id},
        state=recorded,
        graph_state={"run_state": recorded.model_dump(mode="json")},
    )
    restarted = InMemoryRuntimeStore()
    restarted.save_checkpoint(checkpoint)
    loaded = restarted.load_checkpoint(state.run_id)
    assert loaded is not None
    assert loaded.status == recorded.status.value
    assert RunState.model_validate(loaded.graph_state["run_state"]).fault_history[-1].category == expected_category.value


def test_checkpointed_local_replan_keeps_effect_ledger_and_is_restart_readable() -> None:
    """新进程读取 P0-15 快照时能看到新版本、故障记录和旧副作用锚点。"""

    base_contract = _contract()
    contract = base_contract.model_copy(
        update={"budgets": base_contract.budgets.model_copy(update={"max_replans": 2})}
    )
    base_plan = _plan(contract)
    allocate = base_plan.tasks[0].model_copy(
        update={"status": PlanTaskStatus.COMPLETED, "effect_id": "effect-p015-db"}
    )
    plan = base_plan.model_copy(update={"tasks": [allocate, *base_plan.tasks[1:]]})
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id="run-p015-checkpoint",
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
    decision = controller.handle_failure(
        {"code": "workstation_capacity_exceeded", "workstation_id": "S3"},
        task_id=plan.tasks[1].task_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    # 工位占用的第一次动作是 bounded retry；第二次仍沿同一 fault_id
    # 消耗下一次有限额度，Effect Ledger 负责保证重试不会重复副作用。
    assert decision.action is RecoveryAction.RETRY
    repeated = controller.handle_failure(
        {"code": "workstation_capacity_exceeded", "workstation_id": "S3"},
        task_id=plan.tasks[1].task_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    assert repeated.fault.fault_id == decision.fault.fault_id
    assert repeated.action is RecoveryAction.RETRY
    state = controller.record_on_run_state(state, decision)

    replan_decision = controller.handle_failure(
        {"code": "channel_closed", "coordinate": {"x": 4, "y": 5}},
        stage="execute",
        task_id=plan.tasks[1].task_id,
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    assert replan_decision.action is RecoveryAction.REPLAN
    recovery = controller.apply_replan(
        state,
        plan,
        replan_decision,
        _replacement_chain(contract, allocate.task_id),
        tool_specs=build_tool_registry().specs(),
        expected_seed=7,
    )

    store = InMemoryRuntimeStore()
    # 先写入一个已完成的 P0-14 Effect Ledger 行；局部重规划只能保留它，
    # 不能为同一 completed 锚点创建新副作用或覆盖结果。
    effect_key = make_effect_idempotency_key(state.run_id, 1, allocate.task_id)
    store.reserve_effect(
        run_id=state.run_id,
        plan_version=1,
        task_id=allocate.task_id,
        tool_name=ToolName.ALLOCATE_TASKS,
        call_id=f"{state.run_id}:allocate",
        input_digest="a" * 64,
        arguments={"order_ids": ["ORDER-001"]},
        now=NOW,
    )
    store.complete_effect(
        effect_key,
        ToolResult(
            tool_name=ToolName.ALLOCATE_TASKS,
            call_id=f"{state.run_id}:allocate",
            status=ToolResultStatus.SUCCESS,
            output={"assignments": []},
            error=None,
            started_at=NOW,
            finished_at=NOW,
            duration_ms=1,
            evidence_refs=["tool://allocate-p015-db"],
            effect_id="effect-p015-db",
            tool_version="1.0.0",
            principal_role=UserRole.OPERATOR,
            input_digest="a" * 64,
            output_digest="b" * 64,
            idempotency_key=effect_key,
            audit_metadata={},
        ),
        external_effect_id="effect-p015-db",
    )
    checkpoint = controller.save_replan_checkpoint(
        store,
        request={"run_id": state.run_id},
        state=recovery.state,
        graph_state={"run_state": recovery.state.model_dump(mode="json")},
    )
    restarted = InMemoryRuntimeStore()
    # 将快照转交给全新 Store，模拟持久化读取而不是复用控制器内存。
    restarted.save_checkpoint(checkpoint)
    loaded = restarted.load_checkpoint(state.run_id)

    assert loaded is not None
    assert loaded.plan_version == 2
    assert loaded.status == RunStatus.REPLANNING.value
    assert len(store.list_effects(state.run_id)) == 1
    assert store.list_effects(state.run_id)[0].status.value == "completed"
    loaded_state = RunState.model_validate(loaded.graph_state["run_state"])
    assert [item.category for item in loaded_state.fault_history] == [
        "workstation_occupied",
        "channel_closed",
    ]
    assert next(item for item in loaded_state.plan_tasks if item.task_id == allocate.task_id).effect_id == "effect-p015-db"
