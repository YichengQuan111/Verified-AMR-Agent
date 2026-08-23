"""P0-18 在线闭环：加难地图、额外障碍连通性和配置门禁。"""

from __future__ import annotations

from agent.runtime.graph import PEVRGraphRunner
from agent.tools import ToolName
from domains.amr_warehouse import TransportOrder
from evals.p018.dataset import load_config
from evals.p018.hard_map import (
    EXTRA_OBSTACLES_PER_CASE,
    HARD_ENVIRONMENT_REF,
    HARD_MAP_PATH,
    build_hard_warehouse_map,
    demo_corridors_open,
    extra_obstacles_for_demo,
    extra_obstacles_for_seed,
    path_still_open,
    snapshot_provider_for_case,
)
from evals.p018.online import DEFAULT_ONLINE_CONFIG_PATH, OnlineFastHarness, _as_str
from evals.p018.oracle import POSITIVE_OUTCOMES
from evals.p018.contracts import EvalOutcome, ZeroToleranceMetrics
from evals.p018.dataset import load_dataset
from tests.unit.test_p013_pevr import _contract


def test_order_003_snapshot_marks_seed_dependency_completed() -> None:
    """ORDER-003 单例不得再注入活的 ORDER-001；前置记入 completed_order_ids。"""

    from evals.p018.dataset import load_dataset
    from evals.p018.online import OnlineFastHarness

    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.case_id == "p018-normal-003")
    harness = object.__new__(OnlineFastHarness)
    harness.config = {"extra_obstacles_per_case": EXTRA_OBSTACLES_PER_CASE}
    provider = OnlineFastHarness._snapshot_provider(harness, case)
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    assert [item.order_id for item in snapshot.orders] == ["ORDER-003"]
    assert snapshot.orders[0].dependencies == []
    assert "ORDER-001" in snapshot.completed_order_ids


def test_late_release_order_keeps_explicit_release_time() -> None:
    """normal-004 显式 release_time=5 必须保留，不能被种子 0 覆盖。"""

    from evals.p018.dataset import load_dataset
    from evals.p018.online import OnlineFastHarness

    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.case_id == "p018-normal-004")
    order = OnlineFastHarness._case_order(object.__new__(OnlineFastHarness), case)
    assert order.release_time == 5
    assert order.dependencies == []


def test_hard_map_keeps_pickups_and_is_warehouse_v1_superset() -> None:
    """货架墙不能占工位，并保留原 (15,0)/(15,1) 障碍。"""

    warehouse = build_hard_warehouse_map()
    blocked = {(item.x, item.y) for item in warehouse.obstacles}
    assert (15, 0) in blocked and (15, 1) in blocked
    for group in (warehouse.pickup_points, warehouse.dropoff_points, warehouse.charging_stations):
        for item in group:
            assert (item.x, item.y) not in blocked
    assert HARD_MAP_PATH.is_file()
    from domains.amr_warehouse import WarehouseMap

    dumped = WarehouseMap.model_validate_json(HARD_MAP_PATH.read_text(encoding="utf-8"))
    assert dumped.map_id == "warehouse_v1_hard"
    assert dumped.version == 3
    assert len(dumped.obstacles) > 2


def test_seed_extra_obstacles_keep_order_corridor() -> None:
    """按 seed 叠加障碍后，AMR 起点→取货→交付仍必须连通。"""

    warehouse = build_hard_warehouse_map()
    extras = extra_obstacles_for_seed(
        warehouse,
        seed=18001,
        amr_id="AMR-01",
        order_id="ORDER-001",
        count=EXTRA_OBSTACLES_PER_CASE,
    )
    assert len(extras) == EXTRA_OBSTACLES_PER_CASE
    blocked = {(item.x, item.y) for item in [*warehouse.obstacles, *warehouse.temporary_blocked_cells, *extras]}
    assert path_still_open([(1, 2), (2, 3), (27, 9)], blocked)


def test_demo_extra_obstacles_keep_all_station_corridors() -> None:
    """演示额外障碍必须保持全部 AMR 起点、取货、交付、充电走廊连通。"""

    warehouse = build_hard_warehouse_map()
    extras = extra_obstacles_for_demo(warehouse)
    assert len(extras) == EXTRA_OBSTACLES_PER_CASE
    blocked = {
        (item.x, item.y)
        for item in [*warehouse.obstacles, *warehouse.temporary_blocked_cells, *extras]
    }
    assert demo_corridors_open(warehouse, blocked)


def test_online_config_requires_live_fast_and_hard_map() -> None:
    """在线配置不能回落到 offline oracle 或生产空旷地图。"""

    config = load_config(DEFAULT_ONLINE_CONFIG_PATH)
    assert config["execution_mode"] == "online_fast_closed_loop"
    assert config["model"]["online_service_required"] is True
    assert str(config["map_path"]).endswith("warehouse_v1_hard.json")
    assert int(config["extra_obstacles_per_case"]) == EXTRA_OBSTACLES_PER_CASE
    offline = load_config()
    assert offline["execution_mode"] == "offline_deterministic_oracle"
    assert offline["model"]["online_service_required"] is False


def test_online_scoring_rejects_collisions_even_if_completed() -> None:
    """零容忍非 0 时完成态也不能算通过。"""

    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.case_id == "p018-normal-001")
    harness = object.__new__(OnlineFastHarness)
    assert harness._score(case, EvalOutcome.COMPLETED, ZeroToleranceMetrics(), {}) is True
    assert (
        harness._score(
            case,
            EvalOutcome.COMPLETED,
            ZeroToleranceMetrics(vertex_collision_count=1),
            {},
        )
        is False
    )
    denied = next(item for item in dataset.cases if item.expected_outcome is EvalOutcome.DENIED)
    assert harness._score(denied, EvalOutcome.DENIED, ZeroToleranceMetrics(), {}) is True
    assert harness._score(denied, EvalOutcome.COMPLETED, ZeroToleranceMetrics(), {}) is False
    assert case.expected_outcome in POSITIVE_OUTCOMES


def test_hard_snapshot_merges_extra_obstacles() -> None:
    """Provider 必须把额外障碍并进 blocked_cells，供 C++ 路径器看到。"""

    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18007,
        pickup="P4",
        dropoff="S5",
    )
    snapshot = provider.get_snapshot("warehouse_v1@eval-hard")
    assert snapshot.map_width == 30
    assert len(snapshot.blocked_cells) > 2
    extras = extra_obstacles_for_seed(
        build_hard_warehouse_map(),
        seed=18007,
        amr_id="AMR-01",
        order_id="ORDER-001",
        pickup="P4",
        dropoff="S5",
    )
    extra_xy = {(item.x, item.y) for item in extras}
    blocked_xy = {(item.x, item.y) for item in snapshot.blocked_cells}
    assert extra_xy <= blocked_xy


def test_as_str_does_not_call_value_on_literal() -> None:
    """Trace 的 event_type/status 是普通字符串，序列化不能再取 .value。"""

    from agent.context.contracts import FinalReportStatus

    assert _as_str("node") == "node"
    assert _as_str("completed") == "completed"
    assert _as_str(FinalReportStatus.COMPLETED) == "completed"
    assert _as_str(EvalOutcome.FAILED) == "failed"


def _injected_transport_order() -> TransportOrder:
    """在线 harness 注入形态的真实运输订单，与充电占位单同一 Schema。"""

    return TransportOrder(
        order_id="ORDER-001",
        material_id="MAT-001",
        pickup="P4",
        dropoff="S5",
        priority=3,
        release_time=0,
        deadline=120,
        dependencies=[],
    )


def test_hard_snapshot_exposes_injected_orders_when_orders_passed() -> None:
    """非空 orders 必须暴露 injected_orders，PEVR understand 才能 duck-type 到 canonicalize。"""

    order = _injected_transport_order()
    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18007,
        pickup="P4",
        dropoff="S5",
        orders=[order],
    )
    injected = getattr(provider, "injected_orders", None)
    assert injected
    assert list(injected) == [order]


def test_hard_snapshot_canonicalize_clears_missing_information() -> None:
    """硬地图注入订单后，带 missing_information 的合同应对齐快照真值并可通过校验。"""

    order = _injected_transport_order()
    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18007,
        pickup="P4",
        dropoff="S5",
        orders=[order],
    )
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    mismatched = _contract().model_copy(update={"missing_information": ["请求缺少运输必填项"]})
    aligned = PEVRGraphRunner._canonicalize_contract_against_snapshot(mismatched, snapshot)
    assert aligned.missing_information == []
    assert aligned.orders == snapshot.orders
    assert aligned.environment_ref == snapshot.environment_ref
    PEVRGraphRunner._validate_contract_against_snapshot(aligned, snapshot)


def test_hard_snapshot_seed_fallback_has_no_injected_orders() -> None:
    """orders=None 回退种子订单，不能假装有 injected_orders。"""

    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18001,
    )
    assert not getattr(provider, "injected_orders", None)


def test_charging_snapshot_has_empty_orders_and_injected_charging() -> None:
    """充电例不得再注入占位 TransportOrder，AMR 必须停在充电站上。"""

    from agent.planning import ChargingGoal

    charging = ChargingGoal(amr_id="AMR-01", charge_station="C1", target_percent=90)
    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18021,
        orders=[],
        charging=charging,
        amr_batteries={"AMR-01": 20.0},
    )
    assert getattr(provider, "injected_charging", None) == charging
    assert getattr(provider, "injected_orders", None) == []
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    assert snapshot.orders == []
    amr = next(item for item in snapshot.amrs if item.amr_id == "AMR-01")
    station = snapshot.location_positions["C1"]
    assert amr.position == station
    assert amr.battery == 20.0


def test_exception_snapshot_writes_fault_code() -> None:
    """8 个期望完成的异常例必须把 fault_code 写进快照。"""

    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18201,
        fault_code="battery_below_new_task_threshold",
    )
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    assert snapshot.fault_code == "battery_below_new_task_threshold"


def test_online_recovery_scoring_ignores_hard_map_luck() -> None:
    """replan_count=0 的硬地图完成不能算异常恢复成功。"""

    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.case_id == "p018-exception-001")
    harness = object.__new__(OnlineFastHarness)
    assert (
        harness._exception_recovery_ok(
            case,
            observed=EvalOutcome.COMPLETED,
            replans=0,
            retries=0,
            plan_version=1,
            zero=ZeroToleranceMetrics(),
            evaluation_passed=True,
        )
        is False
    )
    assert (
        harness._exception_recovery_ok(
            case,
            observed=EvalOutcome.FAILED,
            replans=1,
            retries=0,
            plan_version=2,
            zero=ZeroToleranceMetrics(),
            evaluation_passed=False,
        )
        is True
    )
    timeout = next(item for item in dataset.cases if item.case_id == "p018-exception-005")
    assert harness._exception_recovery_ok(
        timeout,
        observed=EvalOutcome.FAILED,
        replans=0,
        retries=1,
        plan_version=1,
        zero=ZeroToleranceMetrics(),
        evaluation_passed=False,
    )
    charged_case = next(item for item in dataset.cases if item.expected_outcome is EvalOutcome.CHARGED)
    assert harness._score(charged_case, EvalOutcome.COMPLETED, ZeroToleranceMetrics(), {}) is False
    assert harness._score(charged_case, EvalOutcome.CHARGED, ZeroToleranceMetrics(), {}) is True


def test_charging_contract_canonicalize_and_validate_plan() -> None:
    """充电 canonicalize 清空订单，合成计划只含 dispatch。"""

    from agent.planning import ChargingGoal
    from agent.planning.validator import validate_charging_pevr_plan

    charging = ChargingGoal(amr_id="AMR-01", charge_station="C1", target_percent=90)
    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18021,
        orders=[],
        charging=charging,
        amr_batteries={"AMR-01": 20.0},
    )
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    transport = _contract().model_copy(update={"missing_information": ["缺充电站"]})
    runner = PEVRGraphRunner.__new__(PEVRGraphRunner)
    runner.snapshot_provider = provider
    aligned = runner._canonicalize_charging_contract(transport, snapshot)
    assert aligned.is_charging_contract()
    assert aligned.orders == []
    assert aligned.charging == charging
    assert aligned.missing_information == []
    PEVRGraphRunner._validate_contract_against_snapshot(aligned, snapshot)
    plan = runner._synthetic_charging_plan(aligned, expected_seed=18021)
    result = validate_charging_pevr_plan(aligned, plan, expected_seed=18021)
    assert result.valid
    assert result.required_tool_names == [ToolName.DISPATCH_SIMULATION]


def test_charging_pevr_observes_charged_from_charging_event() -> None:
    """充电闭环走完后观察必须是 charged，不能把运输 completed 当 charged。"""

    from agent.planning import ChargingGoal
    from agent.runtime.pevr import PEVRRequest
    from tests.unit.test_p013_pevr import _FakeProvider, _FakeRegistry, _contract, _plan

    charging = ChargingGoal(amr_id="AMR-01", charge_station="C1", target_percent=90)
    provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18021,
        orders=[],
        charging=charging,
        amr_batteries={"AMR-01": 20.0},
    )
    run_id = "run-p018-charging"
    dummy = _contract()
    result = PEVRGraphRunner(
        _FakeProvider(dummy, _plan(dummy), run_id),
        registry=_FakeRegistry(run_id),
        snapshot_provider=provider,
    ).run(
        PEVRRequest(
            run_id=run_id,
            raw_request="AMR-01 请到 C1 充电到 90%",
            environment_ref=HARD_ENVIRONMENT_REF,
            seed=18021,
            approval_granted=True,
        )
    )
    assert result.run_state.task_contract.is_charging_contract()
    assert result.report.completed_order_ids == []
    assert result.report.final_status.value == "completed"
    assert [task.tool_name for task in result.run_state.plan_tasks] == [ToolName.DISPATCH_SIMULATION]
    harness = object.__new__(OnlineFastHarness)
    assert harness._observed_charged(result) is True
    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.expected_outcome is EvalOutcome.CHARGED)
    assert harness._score(case, EvalOutcome.CHARGED, ZeroToleranceMetrics(), {}) is True
    assert harness._score(case, EvalOutcome.COMPLETED, ZeroToleranceMetrics(), {}) is False


def test_hard_map_idle_charging_passes_cpp_and_emits_completed() -> None:
    """充电 idle plan 必须过真实 C++ Validator，并在站内发出 charging.completed。"""

    from agent.planning import ChargingGoal
    from evals.p018.dataset import load_dataset
    from evals.p018.online import OnlineFastHarness

    charging = ChargingGoal(amr_id="AMR-01", charge_station="C1", target_percent=90)
    snapshot_provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18021,
        orders=[],
        charging=charging,
        amr_batteries={"AMR-01": 20.0},
    )
    snapshot = snapshot_provider.get_snapshot(HARD_ENVIRONMENT_REF)
    transport = _contract().model_copy(update={"missing_information": ["缺充电站"]})
    runner = PEVRGraphRunner.__new__(PEVRGraphRunner)
    runner.snapshot_provider = snapshot_provider
    contract = runner._canonicalize_charging_contract(transport, snapshot)
    plan = runner._idle_charging_simulation_plan(contract)
    dataset = load_dataset()
    case = next(item for item in dataset.cases if item.scenario == "charging")
    harness = object.__new__(OnlineFastHarness)
    simulator = harness._charging_simulator(case)
    result = simulator.run(plan, simulation_id="eval-charge-idle", seed=case.seed)
    assert any(event.event_type == "charging.completed" for event in result.events)
    amr = next(item for item in result.amrs if item.amr_id == "AMR-01")
    assert float(amr.battery) + 1e-9 >= 90.0


def test_fault_inject_skips_blocked_and_duplicate_then_fails_once() -> None:
    """007/008 sidecar 与 duplicate 例不注入失败；其余期望完成例前 N 次失败后放行。"""

    from evals.p018.dataset import load_dataset
    from evals.p018.fault_inject import FaultInjectingRegistry, inject_spec_for_case

    dataset = load_dataset()
    blocked = next(item for item in dataset.cases if item.case_id == "p018-exception-007")
    duplicate = next(item for item in dataset.cases if item.case_id == "p018-exception-009")
    low_battery = next(item for item in dataset.cases if item.case_id == "p018-exception-001")
    assert inject_spec_for_case(blocked) is None
    assert inject_spec_for_case(duplicate) is None
    spec = inject_spec_for_case(low_battery)
    assert spec is not None
    assert spec.fail_times == 1

    class _Inner:
        def execute(self, tool_name, arguments, **kwargs):
            return {"ok": True, "tool": tool_name}

    registry = FaultInjectingRegistry(_Inner(), spec)
    first = registry.execute(spec.tool_name, {})
    assert first.status is not None
    assert first.error is not None
    assert first.error.code == spec.code
    second = registry.execute(spec.tool_name, {})
    assert second == {"ok": True, "tool": spec.tool_name}


def test_fault_inject_registry_forwards_hitl_grant() -> None:
    """包装 Registry 必须把 approval_grant 交给内层，dispatch 才能过 HITL。"""

    import inspect

    from agent.runtime.graph import PEVRGraphRunner
    from agent.tools import UserRole
    from evals.p018.dataset import load_dataset
    from evals.p018.fault_inject import FaultInjectingRegistry, inject_spec_for_case

    dataset = load_dataset()
    low_battery = next(item for item in dataset.cases if item.case_id == "p018-exception-001")
    spec = inject_spec_for_case(low_battery)
    assert spec is not None
    parameters = inspect.signature(FaultInjectingRegistry.execute).parameters
    assert "approval_grant" in parameters

    captured: dict[str, object] = {}

    class _Inner:
        def execute(self, tool_name, arguments, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"idempotency_key": "inner"})()

    wrapped = FaultInjectingRegistry(_Inner(), spec)
    wrapped.execute(spec.tool_name, {})  # 消耗一次注入失败
    runner = PEVRGraphRunner.__new__(PEVRGraphRunner)
    runner.registry = wrapped
    grant = object()
    runner._registry_execute(
        ToolName.DISPATCH_SIMULATION,
        {},
        role=UserRole.OPERATOR,
        call_id="grant-forward",
        approval_grant=grant,  # type: ignore[arg-type]
    )
    assert captured.get("approval_grant") is grant


def test_fault_inject_wrapper_forwards_approval_verifier() -> None:
    """PEVR 把 approval_verifier 写在 Registry 上时必须落到内层生产表。"""

    from agent.runtime.checkpoint import InMemoryRuntimeStore
    from agent.runtime.graph import PEVRGraphRunner
    from agent.runtime.hitl import InMemoryHITLStore
    from agent.tools import build_tool_registry
    from evals.p018.dataset import load_dataset
    from evals.p018.fault_inject import FaultInjectingRegistry, inject_spec_for_case
    from tests.unit.test_p013_pevr import _FakeProvider, _contract, _plan

    dataset = load_dataset()
    low_battery = next(item for item in dataset.cases if item.case_id == "p018-exception-001")
    spec = inject_spec_for_case(low_battery)
    assert spec is not None
    inner = build_tool_registry(security_required=True)
    wrapped = FaultInjectingRegistry(inner, spec)
    dummy = _contract()
    PEVRGraphRunner(
        _FakeProvider(dummy, _plan(dummy), "run-wrap-verifier"),
        registry=wrapped,
        checkpoint_store=InMemoryRuntimeStore(),
        hitl_store=InMemoryHITLStore(),
        security_required=True,
    )
    assert inner.security_required is True
    assert inner.approval_verifier is not None
