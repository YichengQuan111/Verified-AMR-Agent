"""P0-14 局部 Replanner 的影响传播与版本化替换测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.planning import (
    AffectedEntitySet,
    LocalReplanner,
    PlanTaskStatus,
    build_task_resource_provenance,
)
from agent.runtime import RunState, RunStatus
from agent.tools import ToolName, UserRole, build_tool_registry
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from tests.unit.test_p013_pevr import _contract, _plan, _task
from tests.unit.test_p013_pevr import _FakeRegistry


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _replacement_chain(contract, allocate_id: str):
    """构造会再次经过 route→Validator→dispatch 的完整局部替换链。"""

    route = _task(
        "TASK-ROUTE-REPLACED",
        ToolName.PLAN_MULTI_AMR_ROUTES,
        [allocate_id],
        {
            "assignments": {"$ref": f"task:{allocate_id}/output/assignments"},
            "environment_ref": contract.environment_ref,
            "blocked_cells": [
                item.model_dump(mode="json") for item in contract.constraints.blocked_cells
            ],
            "max_time": 120,
        },
    )
    validate = _task(
        "TASK-VALIDATE-REPLACED",
        ToolName.VALIDATE_FLEET_PLAN,
        [route.task_id],
        {
            "plan": {"$ref": "derived:simulation_plan"},
            "environment_ref": contract.environment_ref,
            "ruleset_version": "p0-10.v1",
        },
    )
    dispatch = _task(
        "TASK-DISPATCH-REPLACED",
        ToolName.DISPATCH_SIMULATION,
        [validate.task_id],
        {"plan": {"$ref": "derived:simulation_plan"}, "seed": 7},
        approval_required=True,
    )
    return [route, validate, dispatch]


def _apply(replanner, plan, analysis, replacements, contract, *, reason="channel fault"):
    """所有测试都显式提供正式 ToolSpec 和 seed，不能退化成只验 DAG。"""

    return replanner.apply(
        plan,
        analysis,
        replacements,
        reason=reason,
        contract=contract,
        tool_specs=build_tool_registry().specs(),
        expected_seed=7,
    )


def test_failed_route_invalidates_only_unfinished_downstream_subgraph() -> None:
    """已完成 allocate 保留，route 及其 Validator/dispatch 后继失效。"""

    plan = _plan(_contract())
    route_id = plan.tasks[1].task_id
    replanner = LocalReplanner()

    analysis = replanner.analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id],
        affected_entities=[f"tool:{ToolName.PLAN_MULTI_AMR_ROUTES.value}"],
    )

    assert analysis.completed_task_ids == [plan.tasks[0].task_id]
    assert set(analysis.invalidated_task_ids) == {
        route_id,
        plan.tasks[2].task_id,
        plan.tasks[3].task_id,
    }
    assert analysis.retained_task_ids == [plan.tasks[0].task_id]


def test_blocked_cell_matches_route_arguments_and_propagates_downstream() -> None:
    """通道封闭只通过任务参数引用匹配，不依赖自然语言猜测。"""

    contract = _contract()
    plan = _plan(contract)
    route = plan.tasks[1].model_copy(
        update={"tool_arguments": {**plan.tasks[1].tool_arguments, "blocked_cells": [{"x": 4, "y": 5}]}}
    )
    plan = plan.model_copy(update={"tasks": [plan.tasks[0], route, *plan.tasks[2:]]})

    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id],
        affected_entities=["cell:4,5"],
    )

    assert route.task_id in analysis.invalidated_task_ids
    assert plan.tasks[2].task_id in analysis.invalidated_task_ids
    assert plan.tasks[3].task_id in analysis.invalidated_task_ids


def test_apply_replan_increments_version_and_preserves_completed_effect() -> None:
    """新任务只能依赖保留锚点；旧完成节点的 effect_id 不被清空。"""

    contract = _contract()
    plan = _plan(contract)
    allocate = plan.tasks[0].model_copy(
        update={
            "status": PlanTaskStatus.COMPLETED,
            "effect_id": "effect-allocate-existing",
            "evidence_refs": ["tool://allocate-existing"],
        }
    )
    plan = plan.model_copy(update={"tasks": [allocate, *plan.tasks[1:]]})
    replanner = LocalReplanner()
    analysis = replanner.analyze(
        plan,
        completed_task_ids=[allocate.task_id],
        affected_entities=["tool:plan_multi_amr_routes"],
    )
    replacements = _replacement_chain(contract, allocate.task_id)

    result = _apply(
        replanner,
        plan,
        analysis,
        replacements,
        contract,
        reason="原路线引用的通道已封闭",
    )

    assert result.new_plan_version == 2
    assert result.plan.plan_version == 2
    retained_allocate = next(task for task in result.plan.tasks if task.task_id == allocate.task_id)
    assert retained_allocate.status is PlanTaskStatus.COMPLETED
    assert retained_allocate.effect_id == "effect-allocate-existing"
    assert {task.task_id for task in result.plan.tasks} == {
        allocate.task_id,
        *(task.task_id for task in replacements),
    }
    assert result.plan_validation.valid is True


def test_apply_rewrites_dirty_replacement_dataflow_before_pevr_gate() -> None:
    """替换子图写错环境引用/内联 plan/缺 seed 时，apply 必须覆盖成真值并落地 v2。"""

    contract = _contract()
    plan = _plan(contract)
    allocate = plan.tasks[0].model_copy(
        update={"status": PlanTaskStatus.COMPLETED, "effect_id": "effect-allocate-dirty"}
    )
    plan = plan.model_copy(update={"tasks": [allocate, *plan.tasks[1:]]})
    replanner = LocalReplanner()
    analysis = replanner.analyze(
        plan,
        completed_task_ids=[allocate.task_id],
        affected_entities=["tool:plan_multi_amr_routes"],
    )
    route, validate, dispatch = _replacement_chain(contract, allocate.task_id)
    dirty = [
        route.model_copy(
            update={"tool_arguments": {**route.tool_arguments, "environment_ref": "warehouse_v1@wrong"}}
        ),
        validate.model_copy(
            update={
                "tool_arguments": {
                    "plan": {"schema_version": "1.0", "blocked_cells": [{"x": 1, "y": 1}] * 20},
                    "environment_ref": "warehouse_v1@wrong",
                    "ruleset_version": "not-p0-10",
                }
            }
        ),
        dispatch.model_copy(
            update={
                "tool_arguments": {"plan": {"schema_version": "1.0"}, "seed": 99},
                "evidence_refs": ["tool://stale-validate"],
            }
        ),
    ]

    result = _apply(replanner, plan, analysis, dirty, contract)

    assert result.new_plan_version == 2
    assert result.plan_validation.valid is True
    new_validate = next(
        task for task in result.plan.tasks if task.tool_name is ToolName.VALIDATE_FLEET_PLAN
    )
    new_dispatch = next(
        task for task in result.plan.tasks if task.tool_name is ToolName.DISPATCH_SIMULATION
    )
    assert new_validate.tool_arguments["plan"] == {"$ref": "derived:simulation_plan"}
    assert new_validate.tool_arguments["environment_ref"] == contract.environment_ref
    assert new_validate.tool_arguments["ruleset_version"] == "p0-10.v1"
    assert new_dispatch.tool_arguments["seed"] == 7
    assert new_dispatch.tool_arguments["plan"] == {"$ref": "derived:simulation_plan"}
    assert new_dispatch.evidence_refs == []


def test_route_only_replacement_is_rejected_by_full_pevr_validation() -> None:
    """DAG 结构合法但缺 Validator/dispatch 的候选不能成为新计划版本。"""

    contract = _contract()
    plan = _plan(contract)
    allocate = plan.tasks[0].model_copy(update={"status": PlanTaskStatus.COMPLETED})
    plan = plan.model_copy(update={"tasks": [allocate, *plan.tasks[1:]]})
    replanner = LocalReplanner()
    analysis = replanner.analyze(
        plan,
        completed_task_ids=[allocate.task_id],
        affected_entities=["tool:plan_multi_amr_routes"],
    )
    route_only = _replacement_chain(contract, allocate.task_id)[:1]

    with pytest.raises(ValueError, match="确定性 PEVR 门禁"):
        _apply(replanner, plan, analysis, route_only, contract)


def test_workstation_fault_can_be_targeted_without_invalidating_completed_task() -> None:
    """工位故障对已完成节点只保留审计事实，不会删除它。"""

    contract = _contract()
    plan = _plan(contract)
    route = plan.tasks[1].model_copy(update={"workstation": "S3"})
    plan = plan.model_copy(update={"tasks": [plan.tasks[0], route, *plan.tasks[2:]]})

    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id, route.task_id],
        affected_entities=["workstation:S3"],
    )

    assert route.task_id not in analysis.invalidated_task_ids
    assert plan.tasks[2].task_id in analysis.invalidated_task_ids


def test_channel_fault_matches_channel_reference_in_unfinished_task() -> None:
    """通道标签只影响引用该通道的未完成任务，不会误伤已完成锚点。"""

    contract = _contract()
    plan = _plan(contract)
    route = plan.tasks[1].model_copy(
        update={"tool_arguments": {**plan.tasks[1].tool_arguments, "blocked_cells": [{"x": 4, "y": 5}]}}
    )
    plan = plan.model_copy(update={"tasks": [plan.tasks[0], route, *plan.tasks[2:]]})

    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id],
        affected_entities=["channel:4,5"],
    )

    assert set(analysis.invalidated_task_ids) == {
        plan.tasks[1].task_id,
        plan.tasks[2].task_id,
        plan.tasks[3].task_id,
    }
    assert analysis.retained_task_ids == [plan.tasks[0].task_id]


def test_apply_to_run_state_advances_version_and_keeps_effect_anchor() -> None:
    """重规划写回 RunState 后，完成任务及旧 effect 仍是只读锚点。"""

    contract = _contract().model_copy(
        update={"budgets": _contract().budgets.model_copy(update={"max_replans": 1})}
    )
    base_plan = _plan(contract)
    allocate = base_plan.tasks[0].model_copy(
        update={"status": PlanTaskStatus.COMPLETED, "effect_id": "effect-existing"}
    )
    plan = base_plan.model_copy(update={"tasks": [allocate, *base_plan.tasks[1:]]})
    replanner = LocalReplanner()
    analysis = replanner.analyze(
        plan,
        completed_task_ids=[allocate.task_id],
        affected_entities=["tool:plan_multi_amr_routes"],
    )
    replacements = _replacement_chain(contract, allocate.task_id)
    result = _apply(replanner, plan, analysis, replacements, contract)
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    state = RunState(
        run_id="run-replan-state",
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

    updated = replanner.apply_to_run_state(state, result, updated_at=NOW)

    assert updated.status is RunStatus.REPLANNING
    assert updated.plan_version == 2
    assert updated.completed_task_ids == [allocate.task_id]
    assert next(task for task in updated.plan_tasks if task.task_id == allocate.task_id).effect_id == "effect-existing"


def test_runtime_route_provenance_matches_actual_amr_and_path_cell() -> None:
    """静态 PlanTask 没写 target_amr/cell 时，真实 route 输出仍能定位受影响子图。"""

    contract = _contract()
    plan = _plan(contract)
    registry = _FakeRegistry("run-provenance")
    allocation = registry.execute(
        ToolName.ALLOCATE_TASKS,
        {"order_ids": ["ORDER-001"], "environment_ref": contract.environment_ref},
        role=UserRole.OPERATOR,
        call_id="allocation-provenance",
    )
    route = registry.execute(
        ToolName.PLAN_MULTI_AMR_ROUTES,
        {
            "assignments": allocation.output["assignments"],
            "environment_ref": contract.environment_ref,
            "blocked_cells": [
                item.model_dump(mode="json") for item in contract.constraints.blocked_cells
            ],
            "max_time": 120,
        },
        role=UserRole.OPERATOR,
        call_id="route-provenance",
    )
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
    provenance = build_task_resource_provenance(
        plan,
        tool_results=[allocation, route],
        tool_task_ids=[plan.tasks[0].task_id, plan.tasks[1].task_id],
        contract=contract,
        snapshot=snapshot,
    )

    by_amr = LocalReplanner().analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id, plan.tasks[1].task_id],
        affected_entities=["amr:AMR-01"],
        runtime_resources=provenance,
    )
    by_cell = LocalReplanner().analyze(
        plan,
        completed_task_ids=[plan.tasks[0].task_id, plan.tasks[1].task_id],
        affected_entities=["cell:27,9"],
        runtime_resources=provenance,
    )

    expected = {plan.tasks[2].task_id, plan.tasks[3].task_id}
    assert set(by_amr.invalidated_task_ids) == expected
    assert set(by_cell.invalidated_task_ids) == expected


def test_apply_model_output_rejects_mismatched_invalidated_ids() -> None:
    """LLM 给出的失效集合必须与确定性 analyze 一致，否则拒绝落地。"""

    from agent.context.contracts import ReplanOutput

    contract = _contract()
    plan = _plan(contract)
    specs = build_tool_registry().specs()
    analysis = LocalReplanner().analyze(
        plan,
        completed_task_ids=[],
        affected_entities=["tool:plan_multi_amr_routes"],
        failed_tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
    )
    wrong = ReplanOutput(
        previous_plan_version=1,
        new_plan_version=2,
        trigger_observation_id="observation://mismatch",
        retained_task_ids=list(analysis.retained_task_ids),
        invalidated_task_ids=["TASK-NOT-IN-ANALYSIS"],
        replacement_tasks=_replacement_chain(contract, plan.tasks[0].task_id),
        reason="故意提交不一致的失效集合",
        requires_human=False,
    )
    with pytest.raises(ValueError, match="LLM invalidated_task_ids 与确定性影响集合不一致"):
        LocalReplanner().apply_model_output(
            plan,
            wrong,
            completed_task_ids=[],
            affected_entities=["tool:plan_multi_amr_routes"],
            failed_tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
            contract=contract,
            tool_specs=specs,
            expected_seed=7,
        )
