"""P0-04 核心数据契约及 Schema 导出的正反例测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agent.planning import PlanTask, TaskContract, topological_sort
from agent.runtime import Observation, RunState
from agent.tools import ToolResult, ToolSpec
from domains.amr_warehouse import AMRState, GridPosition, TransportOrder, WarehouseMap
from scripts.export_schemas import SCHEMA_MODELS, export_schemas


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)


def order_payload(
    order_id: str = "ORDER-001", *, dependencies: list[str] | None = None
) -> dict[str, Any]:
    """生成最小合法订单；测试只覆盖时再修改目标字段。"""

    return {
        "order_id": order_id,
        "material_id": f"MAT-{order_id[-3:]}",
        "pickup": "P1",
        "dropoff": "S1",
        "priority": 3,
        "release_time": 0,
        "deadline": 120,
        "dependencies": dependencies or [],
    }


def amr_payload(amr_id: str = "AMR-01") -> dict[str, Any]:
    """生成最小合法 AMR 状态。"""

    return {
        "amr_id": amr_id,
        "position": {"x": 1, "y": 2},
        "heading": 0,
        "battery": 90,
        "load": 0,
        "task_status": "IDLE",
        "health_status": "HEALTHY",
        "connection_status": "ONLINE",
    }


def task_contract_payload(
    *, orders: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """生成带硬预算和审批信息的合法任务合同。"""

    return {
        "contract_id": "CONTRACT-001",
        "schema_version": "1.0",
        "goal": "完成指定运输订单",
        "orders": orders or [order_payload()],
        "environment_ref": "warehouse_v1@state-001",
        "constraints": {
            "map_width": 30,
            "map_height": 20,
            "blocked_cells": [],
            "minimum_battery_percent": 20,
            "maximum_load_kg": 100,
            "enforce_time_windows": True,
        },
        "completion_criteria": ["全部订单在截止时间前交付"],
        "risk_level": "low",
        "approval": {
            "required": False,
            "reason": None,
            "required_role": None,
        },
        "budgets": {
            "max_total_seconds": 300,
            "max_input_tokens": 4096,
            "max_output_tokens": 2048,
            "max_tool_steps": 20,
            "max_replans": 2,
        },
        "missing_information": [],
    }


def plan_task_payload(
    task_id: str = "TASK-001",
    *,
    dependencies: list[str] | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    """生成使用只读车队状态工具的合法计划步骤。"""

    return {
        "task_id": task_id,
        "dependencies": dependencies or [],
        "tool_name": "get_fleet_state",
        "tool_arguments": {"environment_ref": "warehouse_v1@state-001"},
        "target_amr": None,
        "pickup": None,
        "dropoff": None,
        "workstation": None,
        "preconditions": ["环境快照存在"],
        "completion_criteria": ["返回车队状态"],
        "time_budget": 10,
        "energy_budget": 0,
        "risk_level": "low",
        "approval_required": False,
        "fallback_strategy": "retry",
        "status": status,
        "evidence_refs": [],
        "effect_id": None,
    }


def tool_spec_payload() -> dict[str, Any]:
    """生成与 get_fleet_state 顶层参数白名单一致的工具说明。"""

    return {
        "tool_name": "get_fleet_state",
        "version": "1.0.0",
        "description": "读取指定环境快照中的车队状态",
        "input_schema": {
            "type": "object",
            "properties": {
                "environment_ref": {"type": "string"},
                "amr_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["environment_ref"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"amrs": {"type": "array"}},
            "additionalProperties": False,
        },
        "allowed_roles": ["viewer", "operator"],
        "timeout_seconds": 5,
        "idempotent": True,
        "has_side_effects": False,
        "requires_approval": False,
        "error_categories": ["not_found", "unavailable"],
    }


def tool_result_payload() -> dict[str, Any]:
    """生成一次成功工具调用的审计结果。"""

    return {
        "tool_name": "get_fleet_state",
        "call_id": "CALL-001",
        "status": "success",
        "output": {"amrs": [amr_payload()]},
        "error": None,
        "started_at": OBSERVED_AT,
        "finished_at": OBSERVED_AT + timedelta(milliseconds=50),
        "duration_ms": 50,
        "evidence_refs": ["fleet://state-001"],
        "effect_id": None,
    }


def observation_payload() -> dict[str, Any]:
    """生成不依赖工具结果的系统观测。"""

    return {
        "observation_id": "OBS-001",
        "run_id": "RUN-001",
        "task_id": None,
        "source": "system",
        "observed_at": OBSERVED_AT,
        "status": "ok",
        "summary": "运行已创建",
        "state_delta": {"status": "created"},
        "evidence_refs": [],
        "tool_result": None,
        "violations": [],
        "requires_replan": False,
        "requires_human": False,
    }


def run_state_payload(
    *, plan_tasks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """生成尚未开始执行的合法运行状态。"""

    return {
        "run_id": "RUN-001",
        "status": "planning",
        "plan_version": 1,
        "task_contract": task_contract_payload(),
        "plan_tasks": plan_tasks or [plan_task_payload()],
        "amr_states": [amr_payload()],
        "orders": [order_payload()],
        "observations": [observation_payload()],
        "current_task_id": None,
        "completed_task_ids": [],
        "failed_task_ids": [],
        "created_at": OBSERVED_AT,
        "updated_at": OBSERVED_AT,
        "replan_count": 0,
    }


def core_contract_cases() -> list[tuple[type[BaseModel], dict[str, Any], str]]:
    """返回核心模型、合法输入和一个必填字段名。"""

    return [
        (TaskContract, task_contract_payload(), "goal"),
        (AMRState, amr_payload(), "position"),
        (TransportOrder, order_payload(), "deadline"),
        (PlanTask, plan_task_payload(), "tool_name"),
        (ToolSpec, tool_spec_payload(), "input_schema"),
        (ToolResult, tool_result_payload(), "status"),
        (Observation, observation_payload(), "source"),
        (RunState, run_state_payload(), "task_contract"),
        (
            WarehouseMap,
            json.loads(
                (
                    PROJECT_ROOT
                    / "domains"
                    / "amr_warehouse"
                    / "data"
                    / "warehouse_v1.json"
                ).read_text(encoding="utf-8")
            ),
            "map_id",
        ),
    ]


@pytest.mark.parametrize(
    ("model", "payload", "required_field"),
    core_contract_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_core_contracts_reject_undeclared_fields(
    model: type[BaseModel], payload: dict[str, Any], required_field: str
) -> None:
    """核心契约都必须执行 extra=forbid。"""

    del required_field
    invalid = deepcopy(payload)
    invalid["undeclared_field"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(invalid)


@pytest.mark.parametrize(
    ("model", "payload", "required_field"),
    core_contract_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_core_contracts_reject_missing_required_fields(
    model: type[BaseModel], payload: dict[str, Any], required_field: str
) -> None:
    """每个核心契约至少抽查一个必填字段。"""

    invalid = deepcopy(payload)
    del invalid[required_field]

    with pytest.raises(ValidationError, match="Field required"):
        model.model_validate(invalid)


def test_seed_data_matches_domain_contracts() -> None:
    """仓库地图、AMR 与订单种子必须全部通过同一套公共契约。"""

    data_directory = PROJECT_ROOT / "domains" / "amr_warehouse" / "data"
    amrs = json.loads((data_directory / "amrs_v1.json").read_text(encoding="utf-8"))
    orders = json.loads(
        (data_directory / "orders_seed_v1.json").read_text(encoding="utf-8")
    )
    warehouse = json.loads(
        (data_directory / "warehouse_v1.json").read_text(encoding="utf-8")
    )

    parsed_map = WarehouseMap.model_validate(warehouse)
    assert parsed_map.obstacles
    assert parsed_map.narrow_aisles
    assert parsed_map.one_way_edges
    assert parsed_map.temporary_blocked_cells
    assert len([AMRState.model_validate(item) for item in amrs["amrs"]]) == 4
    assert len([TransportOrder.model_validate(item) for item in orders["orders"]]) == 3


@pytest.mark.parametrize("value", [True, 1.0])
def test_grid_position_requires_actual_integers(value: object) -> None:
    """JSON 布尔值和浮点值不能借 Python 的 int 兼容语义进入 C++ 坐标。"""

    with pytest.raises(ValidationError):
        GridPosition.model_validate({"x": value, "y": 1})


@pytest.mark.parametrize(
    ("axis", "value"),
    [("x", -1), ("x", 30), ("y", -1), ("y", 20)],
)
def test_amr_state_rejects_out_of_bounds_coordinates(axis: str, value: int) -> None:
    invalid = amr_payload()
    invalid["position"][axis] = value

    with pytest.raises(ValidationError):
        AMRState.model_validate(invalid)


@pytest.mark.parametrize(
    "changes",
    [
        {"pickup": "S1"},
        {"release_time": 120},
        {"dependencies": ["ORDER-001"]},
        {"dependencies": ["ORDER-002", "ORDER-002"]},
    ],
)
def test_transport_order_rejects_invalid_semantics(changes: dict[str, Any]) -> None:
    invalid = order_payload()
    invalid.update(changes)

    with pytest.raises(ValidationError):
        TransportOrder.model_validate(invalid)


def test_task_contract_rejects_unknown_order_dependency() -> None:
    payload = task_contract_payload(
        orders=[order_payload(dependencies=["ORDER-404"])]
    )

    with pytest.raises(ValidationError, match="未知依赖"):
        TaskContract.model_validate(payload)


def test_task_contract_rejects_cyclic_order_dependencies() -> None:
    payload = task_contract_payload(
        orders=[
            order_payload("ORDER-001", dependencies=["ORDER-002"]),
            order_payload("ORDER-002", dependencies=["ORDER-001"]),
        ]
    )

    with pytest.raises(ValidationError, match="存在循环"):
        TaskContract.model_validate(payload)


def test_topological_sort_is_deterministic() -> None:
    dependencies = {"task-b": ["task-a"], "task-a": [], "task-c": []}

    assert topological_sort(dependencies) == ["task-a", "task-b", "task-c"]


def test_plan_task_rejects_unknown_tool() -> None:
    invalid = plan_task_payload()
    invalid["tool_name"] = "execute_arbitrary_shell"

    with pytest.raises(ValidationError):
        PlanTask.model_validate(invalid)


def test_plan_task_rejects_unauthorized_tool_argument() -> None:
    invalid = plan_task_payload()
    invalid["tool_arguments"]["shell_command"] = "do-not-run"

    with pytest.raises(ValidationError, match="未授权参数"):
        PlanTask.model_validate(invalid)


def test_plan_task_rejects_missing_required_tool_argument() -> None:
    invalid = plan_task_payload()
    invalid["tool_arguments"] = {}

    with pytest.raises(ValidationError, match="缺少必填参数"):
        PlanTask.model_validate(invalid)


def test_high_risk_plan_task_requires_approval() -> None:
    invalid = plan_task_payload()
    invalid["risk_level"] = "high"

    with pytest.raises(ValidationError, match="必须要求人工审批"):
        PlanTask.model_validate(invalid)


def test_tool_spec_requires_closed_json_schemas() -> None:
    invalid = tool_spec_payload()
    invalid["input_schema"]["additionalProperties"] = True

    with pytest.raises(ValidationError, match="additionalProperties 必须为 false"):
        ToolSpec.model_validate(invalid)


def test_tool_spec_rejects_unauthorized_property_definition() -> None:
    invalid = tool_spec_payload()
    invalid["input_schema"]["properties"]["shell_command"] = {"type": "string"}

    with pytest.raises(ValidationError, match="未授权参数"):
        ToolSpec.model_validate(invalid)


def test_tool_result_rejects_failure_without_error() -> None:
    invalid = tool_result_payload()
    invalid["status"] = "timeout"

    with pytest.raises(ValidationError, match="必须携带 error"):
        ToolResult.model_validate(invalid)


def test_tool_result_rejects_reversed_timestamps() -> None:
    invalid = tool_result_payload()
    invalid["finished_at"] = invalid["started_at"] - timedelta(seconds=1)

    with pytest.raises(ValidationError, match="不能早于"):
        ToolResult.model_validate(invalid)


def test_observation_requires_tool_result_for_tool_source() -> None:
    invalid = observation_payload()
    invalid["source"] = "tool"

    with pytest.raises(ValidationError, match="必须携带 tool_result"):
        Observation.model_validate(invalid)


def test_blocked_observation_requires_follow_up_action() -> None:
    invalid = observation_payload()
    invalid["status"] = "blocked"

    with pytest.raises(ValidationError, match="必须触发"):
        Observation.model_validate(invalid)


def test_run_state_rejects_cyclic_plan_dependencies() -> None:
    tasks = [
        plan_task_payload("TASK-001", dependencies=["TASK-002"]),
        plan_task_payload("TASK-002", dependencies=["TASK-001"]),
    ]

    with pytest.raises(ValidationError, match="存在循环"):
        RunState.model_validate(run_state_payload(plan_tasks=tasks))


def test_run_state_rejects_unknown_plan_dependency() -> None:
    tasks = [plan_task_payload(dependencies=["TASK-404"])]

    with pytest.raises(ValidationError, match="未知依赖"):
        RunState.model_validate(run_state_payload(plan_tasks=tasks))


def test_run_state_rejects_unknown_target_amr() -> None:
    payload = run_state_payload()
    payload["plan_tasks"][0]["target_amr"] = "AMR-404"

    with pytest.raises(ValidationError, match="未知 AMR"):
        RunState.model_validate(payload)


def test_run_state_rejects_status_list_mismatch() -> None:
    payload = run_state_payload()
    payload["completed_task_ids"] = ["TASK-001"]

    with pytest.raises(ValidationError, match="PlanTask.status 不一致"):
        RunState.model_validate(payload)


def test_run_state_enforces_replan_budget() -> None:
    payload = run_state_payload()
    payload["replan_count"] = 3

    with pytest.raises(ValidationError, match="重规划次数"):
        RunState.model_validate(payload)


def test_completed_run_state_requires_all_tasks_completed() -> None:
    payload = run_state_payload(
        plan_tasks=[plan_task_payload(status="completed")]
    )
    payload["status"] = "completed"
    payload["completed_task_ids"] = ["TASK-001"]

    state = RunState.model_validate(payload)

    assert state.status.value == "completed"


def test_export_schemas_writes_exact_utf8_json(tmp_path: Path) -> None:
    exported = export_schemas(tmp_path)

    assert {path.name for path in exported} == set(SCHEMA_MODELS)
    for path in exported:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        schema = json.loads(text)
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert text.startswith("{\n  ")
        assert any(ord(character) > 127 for character in text)
        assert schema["additionalProperties"] is False
        assert schema == SCHEMA_MODELS[path.name].model_json_schema()


def test_checked_in_schemas_are_current() -> None:
    schema_directory = PROJECT_ROOT / "docs" / "schemas"

    assert {path.name for path in schema_directory.glob("*.schema.json")} == set(
        SCHEMA_MODELS
    )
    for filename, model in SCHEMA_MODELS.items():
        checked_in = json.loads(
            (schema_directory / filename).read_text(encoding="utf-8")
        )
        assert checked_in == model.model_json_schema()
