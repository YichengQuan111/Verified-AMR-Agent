"""P0-14 Checkpoint、Effect Ledger 和恢复核对的离线契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    EffectLedgerStatus,
    ExternalExecutionSnapshot,
    ExternalExecutionStatus,
    InMemoryExternalStateReconciler,
    InMemoryRuntimeStore,
    RecoveryCoordinator,
    RecoveryDecision,
    make_effect_idempotency_key,
)
from agent.runtime.graph import PEVRGraphRunner
from agent.runtime.graph import PEVRExecutionError
from agent.runtime.pevr import PEVRRequest
from agent.tools import ToolName, ToolResult, ToolResultStatus
from tests.unit.test_p013_pevr import _FakeProvider, _FakeRegistry, _contract, _plan


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _result(*, key: str, call_id: str = "call-1") -> ToolResult:
    """构造最小成功副作用结果，保证测试关注账本而不是工具业务载荷。"""

    return ToolResult(
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id=call_id,
        status=ToolResultStatus.SUCCESS,
        output={"status": "completed", "simulation_id": "simulation-test"},
        error=None,
        started_at=NOW,
        finished_at=NOW,
        duration_ms=1,
        evidence_refs=["simulation://simulation-test"],
        effect_id="simulation-test",
        tool_version="1.0.0",
        principal_role="operator",
        input_digest="a" * 64,
        output_digest="b" * 64,
        idempotency_key=key,
        audit_metadata={},
    )


def test_effect_key_is_only_run_plan_task() -> None:
    """key 只由业务三元组推导，分隔符内容也不能制造碰撞。"""

    key = make_effect_idempotency_key("run-1", 3, "task-7")
    assert key.startswith("p014:")
    assert key == make_effect_idempotency_key("run-1", 3, "task-7")
    assert make_effect_idempotency_key("a:1", 2, "c") != make_effect_idempotency_key(
        "a", 1, "2:c"
    )
    with pytest.raises(ValueError, match="正整数"):
        make_effect_idempotency_key("run-1", 0, "task-7")


def test_persisted_identifier_lengths_match_postgres_columns() -> None:
    """公共入口必须在触碰数据库前拒绝超出 runs/tasks 列宽的 ID。"""

    with pytest.raises(ValueError):
        PEVRRequest(
            run_id="r" * 65,
            raw_request="执行订单",
            approval_granted=True,
        )


def test_duplicate_reservation_does_not_create_second_effect() -> None:
    """重复恢复/调用只能拿到原账本行，首次 owner 才能触发 handler。"""

    store = InMemoryRuntimeStore()
    first = store.reserve_effect(
        run_id="run-1",
        plan_version=1,
        task_id="task-7",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-1",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )
    second = store.reserve_effect(
        run_id="run-1",
        plan_version=1,
        task_id="task-7",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-2",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )

    assert first.owner is True
    assert second.owner is False
    assert len(store.list_effects("run-1")) == 1

    completed = store.complete_effect(
        first.entry.idempotency_key,
        _result(key=first.entry.idempotency_key),
    )
    assert completed.status is EffectLedgerStatus.COMPLETED
    assert completed.result is not None


def test_recovery_queries_external_state_before_skipping_completed_effect() -> None:
    """外部已完成且带结果时才允许恢复跳过副作用。"""

    store = InMemoryRuntimeStore()
    reservation = store.reserve_effect(
        run_id="run-2",
        plan_version=1,
        task_id="task-8",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-8",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )
    external = InMemoryExternalStateReconciler()
    external.put(
        reservation.entry.idempotency_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.COMPLETED,
            source="simulation-test",
            observed_at=NOW,
            external_effect_id="simulation-test",
            result=_result(key=reservation.entry.idempotency_key, call_id="call-8"),
        ),
    )

    assessment = RecoveryCoordinator(external).assess(reservation.entry)
    assert assessment.decision is RecoveryDecision.SKIP_COMPLETED
    assert assessment.external.status is ExternalExecutionStatus.COMPLETED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_effect_id", "simulation-other"),
        ("input_digest", "c" * 64),
        ("output_digest", "d" * 64),
    ],
)
def test_recovery_refuses_mismatched_external_completion(field: str, value: str) -> None:
    """外部 completed 必须能证明是同一 effect、同一输入且结果未漂移。"""

    store = InMemoryRuntimeStore()
    reservation = store.reserve_effect(
        run_id="run-mismatch",
        plan_version=1,
        task_id="task-dispatch",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-dispatch",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )
    completed = store.complete_effect(
        reservation.entry.idempotency_key,
        _result(key=reservation.entry.idempotency_key, call_id="call-dispatch"),
        external_effect_id="simulation-test",
    )
    external_result = completed.result
    assert external_result is not None
    external_id = "simulation-test"
    if field == "external_effect_id":
        external_id = value
    else:
        external_result = external_result.model_copy(update={field: value})
    external = InMemoryExternalStateReconciler()
    external.put(
        completed.idempotency_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.COMPLETED,
            source="simulation-test",
            observed_at=NOW,
            external_effect_id=external_id,
            result=external_result,
        ),
    )

    assessment = RecoveryCoordinator(external).assess(completed)

    assert assessment.decision is RecoveryDecision.REPLAN


def test_unknown_external_state_never_replays_side_effect_automatically() -> None:
    """仅有 reserved Checkpoint 而没有真实状态时必须转重规划。"""

    store = InMemoryRuntimeStore()
    reservation = store.reserve_effect(
        run_id="run-3",
        plan_version=1,
        task_id="task-9",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-9",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )

    assessment = RecoveryCoordinator().assess(reservation.entry)
    assert assessment.decision is RecoveryDecision.REPLAN
    assert "不能仅凭旧 Checkpoint" in assessment.reason


def test_failed_external_state_requires_compensation_before_replanning() -> None:
    """外部失败只能进入补偿账本，不允许恢复器直接再次派发。"""

    store = InMemoryRuntimeStore()
    reservation = store.reserve_effect(
        run_id="run-compensate",
        plan_version=1,
        task_id="task-10",
        tool_name=ToolName.DISPATCH_SIMULATION,
        call_id="call-10",
        input_digest="a" * 64,
        arguments={"seed": 7},
        now=NOW,
    )
    external = InMemoryExternalStateReconciler()
    external.put(
        reservation.entry.idempotency_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.FAILED,
            source="simulation-test",
            observed_at=NOW,
            external_effect_id="simulation-failed",
        ),
    )

    assessment = RecoveryCoordinator(external).assess(reservation.entry)

    assert assessment.decision is RecoveryDecision.COMPENSATE
    marked = store.fail_effect(
        reservation.entry.idempotency_key,
        note="external failed",
        compensation_required=True,
    )
    assert RecoveryCoordinator(InMemoryExternalStateReconciler()).assess(marked).decision is RecoveryDecision.COMPENSATE
    with pytest.raises(ValueError, match="不能直接标记完成"):
        store.complete_effect(marked.idempotency_key, _result(key=marked.idempotency_key))


def test_checkpoint_round_trip_is_deep_copied_and_monotonic() -> None:
    """快照恢复不能被调用方修改，连续写入同一运行必须递增序号。"""

    store = InMemoryRuntimeStore()
    first = store.save_checkpoint(
        CheckpointSnapshot(
            checkpoint_id="cp-1",
            run_id="run-4",
            stage="execute",
            status="executing",
            plan_version=1,
            current_task_id="task-1",
            graph_state={"completed_task_ids": ["task-1"]},
            saved_at=NOW,
        )
    )
    second = store.save_checkpoint(
        first.model_copy(
            update={
                "checkpoint_id": "cp-2",
                "graph_state": {"completed_task_ids": ["task-1", "task-2"]},
            }
        )
    )
    loaded = store.load_checkpoint("run-4")

    assert first.checkpoint_sequence == 1
    assert second.checkpoint_sequence == 2
    assert loaded is not None
    loaded.graph_state["completed_task_ids"] = []
    assert store.load_checkpoint("run-4").graph_state["completed_task_ids"] == ["task-1", "task-2"]


def test_process_restart_reuses_checkpoint_and_does_not_dispatch_completed_effect() -> None:
    """模拟新 Runner 进程：先查外部仿真，再直接返回终态，不新增工具调用。"""

    run_id = "run-p014-restart"
    contract = _contract()
    registry = _FakeRegistry(run_id)
    store = InMemoryRuntimeStore()
    runner = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        clock=lambda: NOW,
    )
    request = PEVRRequest(
        run_id=run_id,
        raw_request="把 MAT-001 从 P1 运到 S3",
        environment_ref="warehouse_v1@seed-v1",
        approval_granted=True,
    )
    first = runner.run(request)
    calls_after_first = len(registry.calls)
    dispatch_key = make_effect_idempotency_key(run_id, 1, "TASK-DISPATCH")
    effect = store.get_effect(dispatch_key)
    assert effect is not None and effect.result is not None

    external = InMemoryExternalStateReconciler()
    external.put(
        dispatch_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.COMPLETED,
            source="fake-simulation",
            observed_at=NOW,
            external_effect_id=effect.result.effect_id,
            result=effect.result,
        ),
    )
    restarted = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        checkpoint_store=store,
        external_state_reconciler=external,
        clock=lambda: NOW,
    )
    second = restarted.run(request)

    assert first.report.final_status == second.report.final_status
    assert len(registry.calls) == calls_after_first
    assert len(store.list_effects(run_id)) == 1


def test_resume_rejects_seed_drift_for_same_run_id() -> None:
    """seed 会改变仿真副作用，同一 run_id 不能在恢复时悄悄替换它。"""

    run_id = "run-p014-seed-drift"
    contract = _contract()
    store = InMemoryRuntimeStore()
    request = PEVRRequest(
        run_id=run_id,
        raw_request="把 MAT-001 从 P1 运到 S3",
        environment_ref="warehouse_v1@seed-v1",
        seed=7,
        approval_granted=True,
    )
    PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=_FakeRegistry(run_id),
        checkpoint_store=store,
        clock=lambda: NOW,
    ).run(request)

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=_FakeRegistry(run_id),
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(request.model_copy(update={"seed": 8}))

    assert error.value.code == "checkpoint_request_mismatch"


def test_corrupt_checkpoint_list_entry_is_rejected_not_silently_dropped() -> None:
    """JSONB 列表出现错误类型时恢复必须失败，不能删掉坏项继续执行。"""

    run_id = "run-p014-corrupt"
    contract = _contract()
    store = InMemoryRuntimeStore()
    request = PEVRRequest(
        run_id=run_id,
        raw_request="把 MAT-001 从 P1 运到 S3",
        environment_ref="warehouse_v1@seed-v1",
        approval_granted=True,
    )
    PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=_FakeRegistry(run_id),
        checkpoint_store=store,
        clock=lambda: NOW,
    ).run(request)
    checkpoint = store.load_checkpoint(run_id)
    assert checkpoint is not None
    graph_state = dict(checkpoint.graph_state)
    graph_state["tool_results"] = [*graph_state["tool_results"], "corrupt-entry"]
    store.save_checkpoint(checkpoint.model_copy(update={"graph_state": graph_state}))
    effect = store.list_effects(run_id)[0]
    assert effect.result is not None
    external = InMemoryExternalStateReconciler()
    external.put(
        effect.idempotency_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.COMPLETED,
            source="corrupt-checkpoint-test",
            observed_at=NOW,
            external_effect_id=effect.external_effect_id,
            result=effect.result,
        ),
    )

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=_FakeRegistry(run_id),
            checkpoint_store=store,
            external_state_reconciler=external,
            clock=lambda: NOW,
        ).run(request)

    assert error.value.code == "checkpoint_corrupt"
