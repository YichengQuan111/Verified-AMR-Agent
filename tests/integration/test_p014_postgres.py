"""P0-14 PostgreSQL Checkpoint 与 Effect Ledger 集成测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import delete, inspect, select

from agent.context import PlanTasksOutput
from agent.planning import FallbackStrategy, PlanTask, PlanTaskStatus, RiskLevel
from agent.runtime import RunState, RunStatus
from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    ExternalExecutionSnapshot,
    ExternalExecutionStatus,
    InMemoryExternalStateReconciler,
    make_effect_idempotency_key,
    to_jsonable,
)
from agent.runtime.graph import PEVRGraphRunner
from agent.runtime.pevr import PEVRRequest
from agent.tools import ToolName, ToolResult, ToolResultStatus
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from services.application import PostgresRuntimeStore
from services.config.settings import AppSettings
from services.persistence import (
    Base,
    DatabaseRuntime,
    EffectRecord,
    EventRecord,
    PlanRecord,
    RunRecord,
    TaskRecord,
    ToolCallRecord,
    create_database_runtime,
)
from tests.unit.test_p004_contracts import task_contract_payload
from tests.unit.test_p013_pevr import _FakeProvider, _FakeRegistry, _contract as pevr_contract, _plan as pevr_plan
from tests.helpers.p014_process_worker import KILL_EXIT_CODE, ProcessRecoveryRegistry
from agent.planning import TaskContract


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def database_runtime() -> DatabaseRuntime:
    """真实 PostgreSQL 缺表时直接失败，不能用 SQLite 或 fake 冒充持久化。"""

    runtime = create_database_runtime(AppSettings().database)
    existing = set(inspect(runtime.engine).get_table_names(schema="public"))
    assert set(Base.metadata.tables).issubset(existing), "请先执行 P0-06 Alembic upgrade"
    yield runtime
    runtime.dispose()


def _contract() -> TaskContract:
    """为本测试生成独立合同，清理时只按精确 run_id 回收。"""

    payload = task_contract_payload()
    suffix = uuid4().hex[:12]
    payload["contract_id"] = f"CONTRACT-P014-{suffix}"
    payload["orders"][0]["order_id"] = f"ORDER-P014-{suffix}"
    payload["orders"][0]["material_id"] = f"MAT-P014-{suffix}"
    return TaskContract.model_validate(payload)


def _cleanup(runtime: DatabaseRuntime, run_id: str) -> None:
    """按外键逆序删除本测试唯一运行，不触碰其他用户数据。"""

    with runtime.session_factory() as session:
        with session.begin():
            session.execute(delete(EffectRecord).where(EffectRecord.run_id == run_id))
            session.execute(delete(EventRecord).where(EventRecord.run_id == run_id))
            session.execute(delete(ToolCallRecord).where(ToolCallRecord.run_id == run_id))
            session.execute(delete(TaskRecord).where(TaskRecord.run_id == run_id))
            session.execute(delete(PlanRecord).where(PlanRecord.run_id == run_id))
            session.execute(delete(RunRecord).where(RunRecord.run_id == run_id))


def test_postgres_checkpoint_and_effect_ledger_are_restart_readable(
    database_runtime: DatabaseRuntime,
) -> None:
    """新 Store 实例可读 Checkpoint，重复预留只返回一条副作用记录。"""

    runtime = database_runtime
    run_id = f"run_p014_{uuid4().hex[:20]}"
    contract = _contract()
    store = PostgresRuntimeStore(runtime.session_factory, clock=lambda: NOW)
    key = make_effect_idempotency_key(run_id, 1, "TASK-DISPATCH")
    try:
        store.ensure_run(run_id, contract)
        checkpoint = CheckpointSnapshot(
            checkpoint_id="cp-p014-db",
            run_id=run_id,
            stage="execute",
            status="executing",
            plan_version=1,
            current_task_id="TASK-DISPATCH",
            graph_state={
                "request": {"raw_request": "恢复测试", "environment_ref": contract.environment_ref},
                "completed_task_ids": [],
            },
            saved_at=NOW,
        )
        store.save_checkpoint(checkpoint)

        # 计划任务行和 RunState 的可变状态必须在同一 Checkpoint 事务中同步，
        # 否则恢复查询会看到 JSONB 已完成而 tasks 索引仍 pending 的两套真相。
        plan = PlanTasksOutput(
            plan_version=1,
            tasks=[
                PlanTask(
                    task_id="TASK-P014-DB",
                    dependencies=[],
                    tool_name=ToolName.ALLOCATE_TASKS,
                    tool_arguments={
                        "order_ids": [contract.orders[0].order_id],
                        "environment_ref": contract.environment_ref,
                    },
                    target_amr=None,
                    pickup=None,
                    dropoff=None,
                    workstation=None,
                    preconditions=[],
                    completion_criteria=["allocation completed"],
                    time_budget=30,
                    energy_budget=0,
                    risk_level=RiskLevel.LOW,
                    approval_required=False,
                    fallback_strategy=FallbackStrategy.FATAL,
                    status=PlanTaskStatus.PENDING,
                    evidence_refs=[],
                    effect_id=None,
                )
            ],
            planning_assumptions=[],
            unresolved_risks=[],
        )
        snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(contract.environment_ref)
        run_state = RunState(
            run_id=run_id,
            status=RunStatus.PLANNING,
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
        plan_checkpoint = CheckpointSnapshot(
            checkpoint_id="cp-p014-db-plan",
            run_id=run_id,
            stage="plan",
            status="planning",
            plan_version=1,
            current_task_id=None,
            graph_state={
                "plan": to_jsonable(plan),
                "run_state": to_jsonable(run_state),
            },
            saved_at=NOW,
        )
        store.save_checkpoint(plan_checkpoint)
        completed_task = plan.tasks[0].model_copy(
            update={"status": PlanTaskStatus.COMPLETED, "effect_id": "effect-task-p014"}
        )
        completed_state = run_state.model_copy(
            update={
                "status": RunStatus.EXECUTING,
                "plan_tasks": [completed_task],
                "completed_task_ids": [completed_task.task_id],
            }
        )
        store.save_checkpoint(
            plan_checkpoint.model_copy(
                update={
                    "checkpoint_id": "cp-p014-db-task",
                    "stage": "execute",
                    "status": "executing",
                    "current_task_id": None,
                    "graph_state": {
                        "plan": to_jsonable(plan),
                        "run_state": to_jsonable(completed_state),
                    },
                }
            )
        )
        with runtime.session_factory() as session:
            task_row = session.scalar(
                select(TaskRecord).where(
                    TaskRecord.run_id == run_id,
                    TaskRecord.task_id == "TASK-P014-DB",
                )
            )
            assert task_row is not None
            assert task_row.status == PlanTaskStatus.COMPLETED.value
            assert task_row.effect_id == "effect-task-p014"

        recovered_store = PostgresRuntimeStore(runtime.session_factory, clock=lambda: NOW)
        recovered = recovered_store.load_checkpoint(run_id)
        assert recovered is not None
        assert recovered.stage == "execute"
        assert recovered.current_task_id is None

        first = recovered_store.reserve_effect(
            run_id=run_id,
            plan_version=1,
            task_id="TASK-DISPATCH",
            tool_name=ToolName.DISPATCH_SIMULATION,
            call_id="call-p014-db",
            input_digest="a" * 64,
            arguments={"seed": 7},
            now=NOW,
        )
        second = recovered_store.reserve_effect(
            run_id=run_id,
            plan_version=1,
            task_id="TASK-DISPATCH",
            tool_name=ToolName.DISPATCH_SIMULATION,
            call_id="call-p014-db-retry",
            input_digest="a" * 64,
            arguments={"seed": 7},
            now=NOW,
        )
        result = ToolResult(
            tool_name=ToolName.DISPATCH_SIMULATION,
            call_id="call-p014-db",
            status=ToolResultStatus.SUCCESS,
            output={"status": "completed", "simulation_id": "simulation-p014"},
            error=None,
            started_at=NOW,
            finished_at=NOW,
            duration_ms=1,
            evidence_refs=["simulation://simulation-p014"],
            effect_id="simulation-p014",
            tool_version="1.0.0",
            principal_role="operator",
            input_digest="a" * 64,
            output_digest="b" * 64,
            idempotency_key=key,
            audit_metadata={},
        )
        recovered_store.complete_effect(key, result, external_effect_id="simulation-p014")

        assert first.owner is True
        assert second.owner is False
        entries = recovered_store.list_effects(run_id)
        assert len(entries) == 1
        assert entries[0].status.value == "completed"
        assert entries[0].result is not None
    finally:
        _cleanup(runtime, run_id)


def test_postgres_pevr_restart_reuses_external_result_without_redispatch(
    database_runtime: DatabaseRuntime,
) -> None:
    """新 Store/Runner 实例从 PostgreSQL 终态恢复，已完成 dispatch 不再调用 handler。"""

    runtime = database_runtime
    run_id = f"run_p014_runner_{uuid4().hex[:16]}"
    contract = pevr_contract()
    registry = _FakeRegistry(run_id)
    request = PEVRRequest(
        run_id=run_id,
        raw_request="把 MAT-001 从 P1 运到 S3",
        environment_ref=contract.environment_ref,
        seed=7,
        approval_granted=True,
    )
    store = PostgresRuntimeStore(runtime.session_factory, clock=lambda: NOW)
    try:
        first = PEVRGraphRunner(
            _FakeProvider(contract, pevr_plan(contract), run_id),
            registry=registry,
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(request)
        calls_after_first = len(registry.calls)
        key = make_effect_idempotency_key(run_id, 1, "TASK-DISPATCH")
        effect = store.get_effect(key)
        assert effect is not None and effect.result is not None

        external = InMemoryExternalStateReconciler()
        external.put(
            key,
            ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.COMPLETED,
                source="postgres-restart-test",
                observed_at=NOW,
                external_effect_id=effect.result.effect_id,
                result=effect.result,
            ),
        )
        second = PEVRGraphRunner(
            _FakeProvider(contract, pevr_plan(contract), run_id),
            registry=registry,
            checkpoint_store=PostgresRuntimeStore(runtime.session_factory, clock=lambda: NOW),
            external_state_reconciler=external,
            clock=lambda: NOW,
        ).run(request)

        assert first.report.final_status == second.report.final_status
        assert len(registry.calls) == calls_after_first
        assert len(store.list_effects(run_id)) == 1
    finally:
        _cleanup(runtime, run_id)


def test_actual_process_kill_recovers_external_truth_without_redispatch(
    database_runtime: DatabaseRuntime,
) -> None:
    """真实杀进程命中 handler/ledger 窗口，新进程恢复时派发增量必须为零。"""

    runtime = database_runtime
    run_id = f"run_p014_kill_{uuid4().hex[:20]}"
    worker = Path(__file__).resolve().parents[1] / "helpers" / "p014_process_worker.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(worker), "--run-id", run_id],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
            timeout=60,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == KILL_EXIT_CODE, completed.stderr

        store = PostgresRuntimeStore(runtime.session_factory, clock=lambda: NOW)
        effects_before = store.list_effects(run_id)
        assert len(effects_before) == 1
        assert effects_before[0].status.value == "reserved"
        registry = ProcessRecoveryRegistry(store, run_id)
        contract = pevr_contract()
        result = PEVRGraphRunner(
            _FakeProvider(contract, pevr_plan(contract), run_id),
            registry=registry,
            checkpoint_store=store,
            clock=lambda: NOW,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=contract.environment_ref,
                seed=7,
                approval_granted=True,
            )
        )

        assert result.report.final_status.value == "completed"
        assert registry.dispatch_calls == 0
        effects_after = store.list_effects(run_id)
        assert len(effects_after) == 1
        assert effects_after[0].status.value == "reconciled"
        with runtime.session_factory() as session:
            calls = list(
                session.scalars(
                    select(ToolCallRecord).where(ToolCallRecord.run_id == run_id)
                )
            )
        assert len(calls) == 1
    finally:
        _cleanup(runtime, run_id)
