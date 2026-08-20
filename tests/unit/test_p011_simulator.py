"""P0-11 仿真器正反例：运输、充电、状态迁移、故障和确定性。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from agent.tools import ToolName
from services.amr_simulator import (
    AMRSimulator,
    ChargingStationSpec,
    FaultInjection,
    FaultType,
    PlanValidationError,
    SimulationOrderStatus,
    SimulationStatus,
    SimulatorConfig,
)


def _path() -> list[dict[str, object]]:
    """构造一条与 P0-09 相同的逐秒直行路径，便于测试阶段性状态。"""

    return [
        {
            "position": {"x": 1, "y": 1},
            "heading": 90,
            "time": 0,
            "action": "start",
            "g_cost": 0.0,
        },
        {
            "position": {"x": 2, "y": 1},
            "heading": 90,
            "time": 1,
            "action": "move",
            "g_cost": 1.0,
        },
        {
            "position": {"x": 3, "y": 1},
            "heading": 90,
            "time": 2,
            "action": "move",
            "g_cost": 2.0,
        },
        {
            "position": {"x": 4, "y": 1},
            "heading": 90,
            "time": 3,
            "action": "move",
            "g_cost": 3.0,
        },
        {
            "position": {"x": 5, "y": 1},
            "heading": 90,
            "time": 4,
            "action": "move",
            "g_cost": 4.0,
        },
        {
            "position": {"x": 6, "y": 1},
            "heading": 90,
            "time": 5,
            "action": "move",
            "g_cost": 5.0,
        },
    ]


def _transport_plan(*, battery: float = 100.0, max_time: int = 7) -> dict[str, object]:
    """返回最小但完整的 P0-10 request；测试直接走真实 C++ Validator。"""

    return {
        "schema_version": "1.0",
        "environment_ref": "warehouse_v1",
        "map_width": 30,
        "map_height": 20,
        "blocked_cells": [],
        "blocked_edges": [],
        "one_way_edges": [],
        "amrs": [
            {
                "amr_id": "AMR-01",
                "position": {"x": 1, "y": 1},
                "heading": 90,
                "battery": battery,
                "load": 0.0,
                "task_status": "IDLE",
                "health_status": "HEALTHY",
                "connection_status": "ONLINE",
            }
        ],
        "orders": [
            {
                "order_id": "ORDER-01",
                "material_id": "MAT-01",
                "pickup": "P1",
                "dropoff": "S1",
                "priority": 3,
                "release_time": 0,
                "deadline": 30,
                "dependencies": [],
            }
        ],
        "location_positions": {
            "P1": {"x": 3, "y": 1},
            "S1": {"x": 6, "y": 1},
        },
        "completed_order_ids": [],
        "routes": [
            {
                "amr_id": "AMR-01",
                "order_id": "ORDER-01",
                "payload_kg": 10.0,
                "pickup_time": 2,
                "dropoff_time": 5,
                "path": _path(),
            }
        ],
        "start_time": 0,
        "max_time": max_time,
        "config": {
            "maximum_load_kg": 100.0,
            "energy_per_cell_percent": 1.0,
            "battery_safety_reserve_percent": 15.0,
            "new_task_battery_threshold_percent": 20.0,
            "critical_battery_threshold_percent": 10.0,
            "minimum_safety_distance_cells": 1,
            "default_workstation_capacity": 1,
        },
        "workstation_capacities": {"P1": 1, "S1": 1},
    }


def _idle_plan(*, battery: float = 20.0, max_time: int = 4) -> dict[str, object]:
    """无运输订单的合法计划，用于隔离充电资源行为。"""

    plan = _transport_plan(battery=battery, max_time=max_time)
    plan["orders"] = []
    plan["location_positions"] = {}
    plan["routes"] = []
    plan["workstation_capacities"] = {}
    return plan


def test_normal_transport_emits_timestamped_states_and_events() -> None:
    result = AMRSimulator().run(
        _transport_plan(),
        simulation_id="normal-transport",
        seed=11,
        until_time=6,
    )

    assert result.status is SimulationStatus.COMPLETED
    assert result.orders[0].status is SimulationOrderStatus.COMPLETED
    assert result.orders[0].pickup_time == 2
    assert result.orders[0].dropoff_time == 5
    assert [item.state_delta["amrs"][0]["task_status"] for item in result.observations] == [
        "IDLE",
        "TO_PICKUP",
        "LOADING",
        "TO_DROPOFF",
        "TO_DROPOFF",
        "UNLOADING",
        "IDLE",
    ]
    assert result.amrs[0].position.model_dump() == {"x": 6, "y": 1}
    assert result.amrs[0].battery == 95.0
    assert result.amrs[0].load == 0.0
    assert {event.event_type for event in result.events} >= {
        "amr.path_step",
        "order.pickup",
        "order.dropoff",
        "simulation.finished",
    }
    assert all(item.source.value == "simulator" for item in result.observations)
    assert all(item.evidence_refs for item in result.observations)
    assert result.workstations[0].service_count == 1
    assert result.workstations[1].service_count == 1


def test_charging_is_capacity_and_tick_deterministic() -> None:
    result = AMRSimulator(
        config=SimulatorConfig(
            charge_threshold_percent=30.0,
            charge_target_percent=40.0,
            charge_rate_percent_per_tick=10.0,
            charging_stations={
                "C1": ChargingStationSpec(
                    position={"x": 1, "y": 1},
                    capacity=1,
                )
            },
        )
    ).run(_idle_plan(battery=20.0), simulation_id="charging", until_time=3)

    assert result.status is SimulationStatus.COMPLETED
    assert [item.state_delta["amrs"][0]["task_status"] for item in result.observations] == [
        "CHARGING",
        "CHARGING",
        "IDLE",
        "IDLE",
    ]
    assert result.amrs[0].battery == 40.0
    assert result.charging_stations[0].status.value == "available"
    assert result.charging_stations[0].total_energy_delivered_percent == 20.0
    assert [event.event_type for event in result.events].count("charging.progress") == 2


def test_low_battery_without_station_enters_to_charge_without_teleporting() -> None:
    result = AMRSimulator().run(
        _transport_plan(battery=25.0, max_time=7),
        simulation_id="needs-charge",
        until_time=6,
    )

    assert result.status is SimulationStatus.COMPLETED
    assert result.amrs[0].task_status.value == "TO_CHARGE"
    assert result.amrs[0].position.model_dump() == {"x": 6, "y": 1}
    assert any(event.event_type == "charging.unavailable" for event in result.events)


def test_offline_fault_blocks_order_and_requests_replan() -> None:
    result = AMRSimulator().run(
        _transport_plan(),
        simulation_id="offline-case",
        until_time=6,
        faults=[
            FaultInjection(
                at_time=2,
                amr_id="AMR-01",
                fault_type=FaultType.OFFLINE,
                reason="网络断开",
            )
        ],
    )

    assert result.status is SimulationStatus.BLOCKED
    assert result.amrs[0].task_status.value == "OFFLINE"
    assert result.amrs[0].connection_status.value == "OFFLINE"
    assert result.orders[0].status is SimulationOrderStatus.BLOCKED
    blocked_observations = [item for item in result.observations if item.status.value == "blocked"]
    assert blocked_observations
    assert all(item.requires_replan for item in blocked_observations)
    assert any(event.event_type == "fault.injected" for event in result.events)
    assert result.amrs[0].position.model_dump() == {"x": 2, "y": 1}


def test_battery_drain_fault_is_terminal_and_preserves_nonnegative_battery() -> None:
    result = AMRSimulator().run(
        _transport_plan(),
        simulation_id="battery-fault",
        until_time=4,
        faults=[
            FaultInjection(
                at_time=1,
                amr_id="AMR-01",
                fault_type=FaultType.BATTERY_DRAIN,
                magnitude=150.0,
            )
        ],
    )

    assert result.status is SimulationStatus.BLOCKED
    assert result.amrs[0].battery == 0.0
    assert result.amrs[0].task_status.value == "OFFLINE"
    assert result.orders[0].status is SimulationOrderStatus.BLOCKED
    assert any(
        event.event_type == "fault.injected"
        and event.payload["fault_type"] == "battery_drain"
        for event in result.events
    )


def test_same_plan_and_seed_are_byte_reproducible() -> None:
    plan = _transport_plan()
    first = AMRSimulator().run(plan, simulation_id="replay", seed=123, until_time=6)
    second = AMRSimulator().run(plan, simulation_id="replay", seed=123, until_time=6)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_invalid_timestamp_plan_is_rejected_before_simulation() -> None:
    plan = _transport_plan()
    plan = deepcopy(plan)
    plan["routes"][0]["dropoff_time"] = 4  # type: ignore[index]

    with pytest.raises(PlanValidationError) as exc_info:
        AMRSimulator().run(plan, simulation_id="invalid-plan", until_time=6)

    assert "dropoff_time_mismatch" in {
        item["code"] for item in exc_info.value.result["errors"]
    }


def test_fault_injection_is_not_a_normal_agent_tool() -> None:
    assert "fault_injection" not in {item.value for item in ToolName}
    assert FaultType.OFFLINE.value not in {item.value for item in ToolName}
