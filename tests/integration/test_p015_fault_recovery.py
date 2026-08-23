"""P0-15 与 P0-14 Checkpoint/Effect Ledger 的集成边界测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from agent.context import FinalReport
from agent.planning import PlanTaskStatus
from agent.runtime import (
    FaultCategory,
    FaultClassifier,
    FaultRecoveryController,
    InMemoryHITLStore,
    RecoveryAction,
    RunState,
    RunStatus,
    PEVRExecutionError,
    PEVRGraphRunner,
    PEVRInterrupt,
)
from agent.runtime.checkpoint import InMemoryRuntimeStore, make_effect_idempotency_key
from agent.runtime.pevr import PEVRRequest, PEVRStage
from agent.security import Principal
from agent.tools import (
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
    UserRole,
    build_tool_registry,
)
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from tests.unit.test_p013_pevr import (
    ENVIRONMENT_REF,
    _FakeProvider,
    _FakeRegistry,
    _contract,
    _plan,
)
from tests.unit.test_p014_replanner import _replacement_chain


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


class _ProductionFaultRegistry(_FakeRegistry):
    """让固定 PEVR execute 节点收到真实 ToolResult 故障信封。"""

    def __init__(
        self,
        run_id: str,
        *,
        category: ToolErrorCategory,
        code: str,
        retryable: bool,
        failures: int | None = None,
    ) -> None:
        super().__init__(run_id)
        self.category = category
        self.code = code
        self.retryable = retryable
        self.failures = failures

    def execute(self, tool_name, arguments, *, role, call_id):
        result = super().execute(tool_name, arguments, role=role, call_id=call_id)
        if result.tool_name is not ToolName.PLAN_MULTI_AMR_ROUTES:
            return result
        if self.failures is not None and self.failures <= 0:
            return result
        if self.failures is not None:
            self.failures -= 1
        return result.model_copy(
            update={
                "status": (
                    ToolResultStatus.TIMEOUT
                    if self.category is ToolErrorCategory.TIMEOUT
                    else ToolResultStatus.FAILED
                ),
                "output": None,
                "output_digest": None,
                "error": ToolError(
                    category=self.category,
                    code=self.code,
                    message=f"生产图故障注入: {self.code}",
                    retryable=self.retryable,
                    details={},
                ),
            }
        )


class _ReplanAwareProvider(_FakeProvider):
    """报告节点采用当前计划版本，而不是写死 v2。"""

    def generate_structured(self, messages, response_model, **kwargs):
        generated = super().generate_structured(messages, response_model, **kwargs)
        if response_model is not FinalReport:
            return generated
        content = ""
        for message in reversed(list(messages)):
            payload = message.content if hasattr(message, "content") else str(message)
            if "plan_version" in payload:
                content = payload
                break
        start = content.find("{")
        plan_version = 2
        if start >= 0:
            envelope = json.loads(content[start:])
            node_input = envelope.get("node_input") or envelope
            plan_version = int(node_input.get("plan_version") or plan_version)
        report = generated.value.model_copy(
            update={
                "plan_version": plan_version,
                "state_version": f"run:{self.run_id}/plan:{plan_version}",
            }
        )
        return generated.model_copy(update={"value": report})


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


@pytest.mark.parametrize(
    ("category", "code", "retryable", "expected_actions", "expected_plan_version"),
    [
        (ToolErrorCategory.UNSAFE_PLAN, "low_battery", False, ["replan", "replan", "human"], 3),
        (ToolErrorCategory.UNAVAILABLE, "amr_offline", False, ["replan", "replan", "human"], 3),
        (ToolErrorCategory.UNSAFE_PLAN, "channel_closed", False, ["replan", "replan", "human"], 3),
        (
            ToolErrorCategory.UNAVAILABLE,
            "workstation_occupied",
            True,
            ["retry", "retry", "replan", "replan", "human"],
            3,
        ),
        (ToolErrorCategory.TIMEOUT, "tool_timeout", True, ["retry", "retry", "fallback"], 1),
        (ToolErrorCategory.UNSAFE_PLAN, "route_infeasible", False, ["replan", "replan", "human"], 3),
        (ToolErrorCategory.CONFLICT, "state_conflict", False, ["human"], 1),
    ],
)
def test_seven_faults_run_through_production_pevr_and_end_bounded(
    category: ToolErrorCategory,
    code: str,
    retryable: bool,
    expected_actions: list[str],
    expected_plan_version: int,
) -> None:
    """七类故障必须由真实 PEVR execute→controller 路径落到有界终态。"""

    run_id = f"run-p015-production-{code}"
    base = _contract()
    contract = base.model_copy(
        update={
            "budgets": base.budgets.model_copy(
                update={"max_replans": 2, "max_retries": 2}
            )
        }
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=category,
        code=code,
        retryable=retryable,
    )
    runner = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        clock=lambda: NOW,
    )

    with pytest.raises(PEVRExecutionError):
        runner.run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                approval_granted=True,
            )
        )

    checkpoint = store.load_checkpoint(run_id)
    assert checkpoint is not None
    recovered_state = RunState.model_validate(checkpoint.graph_state["run_state"])
    assert recovered_state.status is RunStatus.FAILED
    assert recovered_state.current_task_id is None
    assert recovered_state.plan_version == expected_plan_version
    assert recovered_state.replan_count <= 2
    assert recovered_state.retry_count <= 2
    recovery_events = [
        event
        for event in store.list_trace_events(run_id)
        if event.node == "recovery"
    ]
    assert [event.metadata["recovery_action"] for event in recovery_events] == expected_actions
    assert recovery_events[-1].status == "failed"
    assert [name for name, _ in registry.calls].count(ToolName.ALLOCATE_TASKS) == 1


def test_production_pevr_retry_can_continue_to_verified_completion() -> None:
    """一次无副作用 timeout 只重试故障任务，成功后继续 Validator/dispatch。"""

    run_id = "run-p015-production-retry-success"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.TIMEOUT,
        code="tool_timeout",
        retryable=True,
        failures=1,
    )
    result = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        clock=lambda: NOW,
    ).run(
        PEVRRequest(
            run_id=run_id,
            raw_request="把 MAT-001 从 P1 运到 S3",
            environment_ref=ENVIRONMENT_REF,
            approval_granted=True,
        )
    )

    assert result.run_state.status is RunStatus.COMPLETED
    assert result.run_state.retry_count == 1
    assert [name for name, _ in registry.calls].count(ToolName.ALLOCATE_TASKS) == 1
    assert [name for name, _ in registry.calls].count(ToolName.PLAN_MULTI_AMR_ROUTES) == 2


def test_production_pevr_local_replan_replaces_only_unfinished_subgraph() -> None:
    """一次不可行路线生成 v2 子图，已完成 allocation 保留且不会重做。"""

    run_id = "run-p015-production-replan-success"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
        failures=1,
    )
    result = PEVRGraphRunner(
        _ReplanAwareProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        clock=lambda: NOW,
    ).run(
        PEVRRequest(
            run_id=run_id,
            raw_request="把 MAT-001 从 P1 运到 S3",
            environment_ref=ENVIRONMENT_REF,
            approval_granted=True,
        )
    )

    assert result.run_state.status is RunStatus.COMPLETED
    assert result.run_state.plan_version == 2
    assert result.run_state.replan_count == 1
    assert [name for name, _ in registry.calls].count(ToolName.ALLOCATE_TASKS) == 1
    replacement_ids = {task.task_id for task in result.run_state.plan_tasks}
    assert "TASK-ALLOCATE" in replacement_ids
    assert {"TASK-ROUTE", "TASK-VALIDATE", "TASK-DISPATCH"}.isdisjoint(replacement_ids)


def test_secure_replan_invalidates_old_approval_and_waits_on_new_plan() -> None:
    """重规划后的 dispatch 必须为 v2 新建 waiting checkpoint，再签 grant 恢复。"""

    run_id = "run-p015-secure-replan-hitl"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    hitl = InMemoryHITLStore(signing_secret="p015-secure-hitl-secret-more-than-32")
    principal = Principal(subject="operator-p015", role=UserRole.OPERATOR)
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
        failures=1,
    )
    runner = PEVRGraphRunner(
        _ReplanAwareProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        hitl_store=hitl,
        security_required=True,
        clock=lambda: NOW,
    )
    request = PEVRRequest(
        run_id=run_id,
        raw_request="把 MAT-001 从 P1 运到 S3",
        environment_ref=ENVIRONMENT_REF,
        principal=principal,
    )

    with pytest.raises(PEVRInterrupt) as paused:
        runner.run(request)
    pending = hitl.get_request(paused.value.interrupt.approval_id)
    assert pending is not None
    assert pending.plan_version == 2
    grant = hitl.approve(pending.approval_id, principal=principal, now=NOW)
    result = runner.run(request.model_copy(update={"approval_grant": grant}))

    assert result.run_state.status is RunStatus.COMPLETED
    assert result.report.plan_version == 2
    assert result.report.approval_id == pending.approval_id
    assert result.report.approval_checkpoint_id == pending.checkpoint_id


def test_first_apply_failure_retries_production_replan_instead_of_fake_fatal() -> None:
    """第一次 apply 抛错后若策略仍是 REPLAN，必须再 apply，不能用 recovery_fatal 冒充额度。"""

    run_id = "run-p015-apply-retry-after-reject"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
        failures=1,
    )
    apply_calls = {"n": 0}
    original = PEVRGraphRunner._apply_production_replan

    def fail_once_then_apply(self, *args, **kwargs):
        apply_calls["n"] += 1
        if apply_calls["n"] == 1:
            raise ValueError("故障没有定位到可替换的未完成任务")
        return original(self, *args, **kwargs)

    with patch.object(PEVRGraphRunner, "_apply_production_replan", fail_once_then_apply):
        result = PEVRGraphRunner(
            _ReplanAwareProvider(contract, _plan(contract), run_id),
            registry=registry,
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                approval_granted=True,
            )
        )

    assert apply_calls["n"] == 2
    assert result.run_state.status is RunStatus.COMPLETED
    assert result.run_state.plan_version == 2
    assert result.run_state.replan_count == 1


def test_local_replan_apply_exhausts_budget_with_real_error_not_fake_fatal() -> None:
    """max_replans 用尽后应 HUMAN/FATAL，reason 含真实异常，且 apply 次数受额度约束。"""

    run_id = "run-p015-apply-exhausted"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
    )
    apply_calls = {"n": 0}

    def always_reject(self, *args, **kwargs):
        apply_calls["n"] += 1
        raise ValueError("故障没有定位到可替换的未完成任务")

    with patch.object(PEVRGraphRunner, "_apply_production_replan", always_reject):
        with pytest.raises(PEVRExecutionError) as raised:
            PEVRGraphRunner(
                _FakeProvider(contract, _plan(contract), run_id),
                registry=registry,
                checkpoint_store=store,
                clock=lambda: NOW,
            ).run(
                PEVRRequest(
                    run_id=run_id,
                    raw_request="把 MAT-001 从 P1 运到 S3",
                    environment_ref=ENVIRONMENT_REF,
                    approval_granted=True,
                )
            )

    error = raised.value
    assert error.code == "recovery_human"
    assert "故障没有定位到可替换的未完成任务" in str(error)
    assert "原始错误" in str(error)
    assert "route_infeasible" in str(error)
    assert "允许第" not in str(error)
    assert apply_calls["n"] == 2
    checkpoint = store.load_checkpoint(run_id)
    assert checkpoint is not None
    recovered_state = RunState.model_validate(checkpoint.graph_state["run_state"])
    assert recovered_state.status is RunStatus.FAILED
    assert recovered_state.replan_count == 0


def test_human_after_successful_v2_keeps_original_validator_error() -> None:
    """v2 落地后额度用尽转 HUMAN 时，终态仍必须带上 C++/工具原文。"""

    run_id = "run-p015-v2-then-human"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 1, "max_retries": 0})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
    )
    with pytest.raises(PEVRExecutionError) as raised:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=registry,
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                approval_granted=True,
            )
        )

    error = raised.value
    assert error.code == "recovery_human"
    assert "原始错误" in str(error)
    assert "route_infeasible" in str(error)
    checkpoint = store.load_checkpoint(run_id)
    assert checkpoint is not None
    recovered_state = RunState.model_validate(checkpoint.graph_state["run_state"])
    assert recovered_state.plan_version == 2
    assert recovered_state.replan_count == 1


def test_empty_infeasible_impact_generates_v2_and_completes() -> None:
    """validate 节点无 task_id 的 PLAN_INFEASIBLE 也必须真正写出 v2。"""

    run_id = "run-p015-empty-impact-v2"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    original_validate = PEVRGraphRunner._validate_node

    def fail_first_validate(self, state):
        plan = state.get("plan")
        run_state = state.get("run_state")
        if getattr(plan, "plan_version", 1) == 1 and not getattr(run_state, "completed_task_ids", []):
            raise PEVRExecutionError(
                PEVRStage.VALIDATE,
                "plan_validation_failed",
                "测试空影响集合",
                fault=FaultClassifier.classify(
                    {"code": "plan_validation_failed", "message": "测试空影响集合"},
                    stage=PEVRStage.VALIDATE.value,
                ),
            )
        return original_validate(self, state)

    with patch.object(PEVRGraphRunner, "_validate_node", fail_first_validate):
        result = PEVRGraphRunner(
            _ReplanAwareProvider(contract, _plan(contract), run_id),
            registry=_FakeRegistry(run_id),
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                approval_granted=True,
            )
        )

    assert result.run_state.status is RunStatus.COMPLETED
    assert result.run_state.plan_version == 2
    assert result.run_state.replan_count == 1
    tools = [task.tool_name for task in result.run_state.plan_tasks]
    assert ToolName.ALLOCATE_TASKS in tools
    assert any(task.task_id.endswith("-R2") or "REPLAN" in task.task_id for task in result.run_state.plan_tasks)


def test_second_apply_uses_model_replan_node() -> None:
    """计划已是 v2 后的下一次 apply 必须走 Fast replan 节点。"""

    run_id = "run-p015-model-replan"
    base = _contract()
    contract = base.model_copy(
        update={"budgets": base.budgets.model_copy(update={"max_replans": 2, "max_retries": 2})}
    )
    store = InMemoryRuntimeStore()
    registry = _ProductionFaultRegistry(
        run_id,
        category=ToolErrorCategory.UNSAFE_PLAN,
        code="route_infeasible",
        retryable=False,
        failures=2,
    )
    result = PEVRGraphRunner(
        _ReplanAwareProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        clock=lambda: NOW,
    ).run(
        PEVRRequest(
            run_id=run_id,
            raw_request="把 MAT-001 从 P1 运到 S3",
            environment_ref=ENVIRONMENT_REF,
            approval_granted=True,
        )
    )

    assert result.run_state.status is RunStatus.COMPLETED
    assert result.run_state.plan_version == 3
    assert result.run_state.replan_count == 2
    assert any(event.node == "replan" for event in store.list_trace_events(run_id))
