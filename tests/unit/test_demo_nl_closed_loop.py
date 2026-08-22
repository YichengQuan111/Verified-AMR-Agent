"""动态订单注入 PEVR 闭环：快照包装、合同规范化、匿名 HITL 与 Ledger 幂等。

2026-08-22 用户指令：演示闭环完全不考虑安全；本文件覆盖闭环正确性，
不把 JWT 门禁当成验收项。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.runtime import (
    ExternalExecutionSnapshot,
    ExternalExecutionStatus,
    InMemoryExternalStateReconciler,
    InMemoryHITLStore,
    InMemoryRuntimeStore,
    PEVRGraphRunner,
    PEVRInterrupt,
)
from agent.runtime.graph import PEVRExecutionError
from agent.runtime.hitl import HITLReason, build_hitl_request
from agent.runtime.pevr import PEVRRequest, PEVRStage
from agent.security import Principal
from agent.tools import ToolName, UserRole
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider, DynamicOrderSnapshotProvider
from apps.api.dependencies import get_hitl_store, get_model_provider
from apps.api.main import create_app
from domains.amr_warehouse import TransportOrder
from services.config.settings import AppSettings
from services.demo.contracts import DemoOrderExtraction
from services.demo.order_json import (
    dump_demo_nl_order_json,
    load_demo_nl_order_json,
    resolve_demo_nl_order_json_path,
)
from services.demo.service import WarehouseDemoService
from services.model_gateway.exceptions import StructuredOutputError
from tests.unit.test_demo_order import _FakeProvider
from tests.unit.test_p013_pevr import (
    ENVIRONMENT_REF,
    _FakeProvider as _PevrFakeProvider,
    _FakeRegistry,
    _contract,
    _now,
    _plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _nl_order() -> TransportOrder:
    """服务端重建形态的动态订单（P3→S3，不在种子 ORDER-001 路径上）。"""

    return TransportOrder(
        order_id="NL-TEST01",
        material_id="MAT-001",
        pickup="P3",
        dropoff="S3",
        priority=3,
        release_time=0,
        deadline=120,
        dependencies=[],
    )


def _mismatched_contract():
    """模拟 Fast understand：填了种子订单 + missing_information。"""

    return _contract().model_copy(update={"missing_information": ["请求订单不在种子快照中"]})


def test_dynamic_snapshot_replaces_seed_orders_without_touching_seed_file() -> None:
    """包装器把动态订单写入快照 orders；种子文件内容不变。"""

    order = _nl_order()
    seed_path = REPOSITORY_ROOT / "domains" / "amr_warehouse" / "data" / "orders_seed_v1.json"
    before = seed_path.read_text(encoding="utf-8")
    snapshot = DynamicOrderSnapshotProvider([order]).get_snapshot(ENVIRONMENT_REF)
    assert [item.order_id for item in snapshot.orders] == ["NL-TEST01"]
    assert snapshot.orders[0] == order
    assert snapshot.map_width == 30
    assert "P3" in snapshot.location_positions
    seed_only = DefaultWarehouseSnapshotProvider().get_snapshot(ENVIRONMENT_REF)
    assert [item.order_id for item in seed_only.orders] == ["ORDER-001", "ORDER-002", "ORDER-003"]
    assert seed_path.read_text(encoding="utf-8") == before


def test_canonicalize_aligns_contract_and_clears_missing_information() -> None:
    """规范化把合同订单覆盖为快照真值并清零 missing_information，随后逐字段校验通过。"""

    order = _nl_order()
    snapshot = DynamicOrderSnapshotProvider([order]).get_snapshot(ENVIRONMENT_REF)
    aligned = PEVRGraphRunner._canonicalize_contract_against_snapshot(_mismatched_contract(), snapshot)
    assert aligned.orders == snapshot.orders
    assert aligned.missing_information == []
    assert aligned.environment_ref == snapshot.environment_ref
    PEVRGraphRunner._validate_contract_against_snapshot(aligned, snapshot)


def test_validate_without_canonicalize_still_rejects_mismatch() -> None:
    """不规范化时，动态快照与种子合同仍然 order_snapshot_mismatch。"""

    snapshot = DynamicOrderSnapshotProvider([_nl_order()]).get_snapshot(ENVIRONMENT_REF)
    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner._validate_contract_against_snapshot(_contract(), snapshot)
    assert error.value.stage is PEVRStage.UNDERSTAND
    assert error.value.code == "order_snapshot_mismatch"


def test_pevr_dynamic_order_reaches_waiting_approval() -> None:
    """LLM 合同与快照不一致时，规范化后闭环仍停在 waiting_approval。"""

    order = _nl_order()
    run_id = "run-dyn-wait"
    registry = _FakeRegistry(run_id)
    checkpoints = InMemoryRuntimeStore()
    hitl = InMemoryHITLStore(signing_secret="h" * 40)
    principal = Principal(subject="operator-1", role=UserRole.OPERATOR)
    aligned_for_plan = PEVRGraphRunner._canonicalize_contract_against_snapshot(
        _mismatched_contract(),
        DynamicOrderSnapshotProvider([order]).get_snapshot(ENVIRONMENT_REF),
    )
    runner = PEVRGraphRunner(
        _PevrFakeProvider(_mismatched_contract(), _plan(aligned_for_plan), run_id),
        registry=registry,
        snapshot_provider=DynamicOrderSnapshotProvider([order]),
        checkpoint_store=checkpoints,
        hitl_store=hitl,
        security_required=True,
        clock=_now,
    )
    with pytest.raises(PEVRInterrupt) as interrupted:
        runner.run(
            PEVRRequest(
                run_id=run_id,
                raw_request="请把 MAT-001 从 P3 运到 S3。",
                environment_ref=ENVIRONMENT_REF,
                seed=7,
                principal=principal,
            )
        )
    assert interrupted.value.interrupt.reason_code is HITLReason.HIGH_RISK_WRITE
    checkpoint = checkpoints.load_checkpoint(run_id)
    assert checkpoint is not None
    assert checkpoint.status == "waiting_approval"
    assert checkpoint.current_task_id == "TASK-DISPATCH"


def test_pevr_anonymous_approve_completes_and_ledger_is_idempotent() -> None:
    """匿名 operator 批准后 completed；重复恢复不第二次调用 dispatch_simulation。

    Ledger 幂等与订单是否动态无关，这里用种子合同避免 FakeRegistry 写死 ORDER-001
    与动态订单 ID 在 verify 阶段冲突；动态订单走到 waiting_approval 由上一条覆盖。
    """

    run_id = "run-dyn-ledger"
    registry = _FakeRegistry(run_id)
    checkpoints = InMemoryRuntimeStore()
    hitl = InMemoryHITLStore(signing_secret="h" * 40)
    principal = Principal(subject="demo-anonymous-approver", role=UserRole.OPERATOR, auth_method="internal")
    clock_time = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    runner = PEVRGraphRunner(
        _PevrFakeProvider(_contract(), _plan(_contract()), run_id),
        registry=registry,
        snapshot_provider=DefaultWarehouseSnapshotProvider(),
        checkpoint_store=checkpoints,
        hitl_store=hitl,
        security_required=True,
        clock=lambda: clock_time,
    )
    request = PEVRRequest(
        run_id=run_id,
        raw_request="请把 MAT-001 从 P1 运到 S3。",
        environment_ref=ENVIRONMENT_REF,
        seed=7,
        principal=principal,
    )
    with pytest.raises(PEVRInterrupt) as interrupted:
        runner.run(request)
    grant = hitl.approve(
        interrupted.value.interrupt.approval_id,
        principal=principal,
        now=clock_time + timedelta(seconds=1),
    )
    result = runner.run(request.model_copy(update={"approval_grant": grant}))
    assert result.run_state.status.value == "completed"
    assert [name for name, _ in registry.calls].count(ToolName.DISPATCH_SIMULATION) == 1

    effect = checkpoints.list_effects(run_id)[0]
    external = InMemoryExternalStateReconciler()
    external.put(
        effect.idempotency_key,
        ExternalExecutionSnapshot(
            status=ExternalExecutionStatus.COMPLETED,
            source="dynamic-order-replay",
            observed_at=clock_time,
            external_effect_id=effect.external_effect_id,
            result=effect.result,
        ),
    )
    replayed = PEVRGraphRunner(
        _PevrFakeProvider(_contract(), _plan(_contract()), run_id),
        registry=registry,
        snapshot_provider=DefaultWarehouseSnapshotProvider(),
        checkpoint_store=checkpoints,
        external_state_reconciler=external,
        hitl_store=hitl,
        security_required=True,
        clock=lambda: clock_time,
    ).run(request.model_copy(update={"approval_grant": grant}))
    assert replayed.report.approval_id == interrupted.value.interrupt.approval_id
    assert [name for name, _ in registry.calls].count(ToolName.DISPATCH_SIMULATION) == 1


def test_order_json_rejects_escape_and_accepts_tmp_pattern(tmp_path: Path) -> None:
    """CLI 只接受仓库 tmp/ 下 demo_nl_order_*.json，拒绝路径穿越。"""

    order = _nl_order()
    outsider = tmp_path / "demo_nl_order_escape.json"
    dump_demo_nl_order_json(outsider, order)
    with pytest.raises(ValueError, match="tmp"):
        resolve_demo_nl_order_json_path(outsider, repository_root=REPOSITORY_ROOT)

    legal = REPOSITORY_ROOT / "tmp" / "demo_nl_order_unit-test.json"
    legal.parent.mkdir(parents=True, exist_ok=True)
    dump_demo_nl_order_json(legal, order)
    try:
        resolved = resolve_demo_nl_order_json_path(legal, repository_root=REPOSITORY_ROOT)
        assert load_demo_nl_order_json(resolved) == order
    finally:
        legal.unlink(missing_ok=True)


def test_prepare_dynamic_order_unknown_location_and_extract_failure() -> None:
    """非种子地点 422 unknown_location；抽取失败 422 nl_extract_failed。"""

    service = WarehouseDemoService()
    bad_location = _FakeProvider(
        value=DemoOrderExtraction(material_id="MAT-001", pickup="PX", dropoff="S3", deadline=120)
    )
    with pytest.raises(Exception) as unknown:
        service.prepare_dynamic_order("从 PX 运到 S3", model_provider=bad_location)
    assert unknown.value.status_code == 422
    assert unknown.value.code == "unknown_location"

    failed = _FakeProvider(error=StructuredOutputError(attempts=2, last_error="schema"))
    with pytest.raises(Exception) as extract:
        service.prepare_dynamic_order("随便一单", model_provider=failed)
    assert extract.value.status_code == 422
    assert extract.value.code == "nl_extract_failed"


def test_http_nl_run_anonymous_extract_failures_do_not_occupy_slot() -> None:
    """匿名 /demo/nl/run：地点非法与抽取失败均 422，且不启动槽位。"""

    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)

    with TestClient(app) as client:
        app.dependency_overrides[get_model_provider] = lambda: _FakeProvider(
            value=DemoOrderExtraction(material_id="MAT-001", pickup="PX", dropoff="S3", deadline=120)
        )
        unknown = client.post("/demo/nl/run", json={"request": "从 PX 送到 S3"})
        assert unknown.status_code == 422
        assert unknown.json()["detail"]["code"] == "unknown_location"

        app.dependency_overrides[get_model_provider] = lambda: _FakeProvider(
            error=StructuredOutputError(attempts=2, last_error="schema")
        )
        failed = client.post("/demo/nl/run", json={"request": "抽不出来"})
        assert failed.status_code == 422
        assert failed.json()["detail"]["code"] == "nl_extract_failed"

        active = client.get("/demo/nl/active")
        assert active.status_code == 200
        assert active.json() is None


def test_http_hitl_approve_reject_are_anonymous_and_run_scoped() -> None:
    """2026-08-22 用户指令豁免：HITL 审批/拒绝匿名开放，仍禁止跨 run 复用。"""

    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)
    hitl_store = InMemoryHITLStore(signing_secret="h" * 40)
    app.dependency_overrides[get_hitl_store] = lambda: hitl_store
    now = datetime.now(timezone.utc)
    pending = build_hitl_request(
        run_id="run-http-hitl",
        task_id="TASK-HTTP-HITL",
        plan_version=1,
        requested_by="operator-http",
        reason_code=HITLReason.HIGH_RISK_WRITE,
        reason="HTTP approval",
        checkpoint_id="cp-http-hitl",
        plan_digest="a" * 64,
        validator_digest="b" * 64,
        now=now,
    )
    hitl_store.request_approval(pending)

    with TestClient(app) as client:
        assert client.post(
            f"/agent/runs/another-run/hitl/{pending.approval_id}/approve",
        ).status_code == 404
        approved = client.post(
            f"/agent/runs/run-http-hitl/hitl/{pending.approval_id}/approve",
        )
        assert approved.status_code == 200
        assert approved.json()["approved_by"] == "demo-anonymous-approver"

        rejected = build_hitl_request(
            run_id="run-http-hitl",
            task_id="TASK-HTTP-REJECT",
            plan_version=1,
            requested_by="operator-http",
            reason_code=HITLReason.HUMAN_TAKEOVER,
            reason="HTTP rejection",
            checkpoint_id="cp-http-reject",
            plan_digest="c" * 64,
            validator_digest="d" * 64,
            now=now,
        )
        hitl_store.request_approval(rejected)
        rejected_response = client.post(
            f"/agent/runs/run-http-hitl/hitl/{rejected.approval_id}/reject",
        )
    assert rejected_response.status_code == 200
    assert rejected_response.json()["status"] == "rejected"
