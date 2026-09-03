"""P1-1：STL 第二判定层与规则验证器的布尔一致性核对 harness。

设计原因：STL 监控器的价值取决于“它和规则层对同一计划给出同样的布尔结论”。
本 harness 不依赖 LLM：它复用生产 Hungarian/A*（通过正式 ToolRegistry）为
P0-18 的运输类用例生成真实计划（与在线 60 例相同的加难地图、seed 障碍、电量
和 release_time 覆盖），再对每个基础计划施加确定性变异（超载、低电、封路、
截断路径、闯入空闲 AMR……）并加入合成冲突场景（顶点/交换边/工位容量/依赖），
最后对每个计划分别调用 CLI 的“仅规则层”和“规则层 + STL”模式，逐公式比对：

    公式实例被违反  ⟺  该公式 rule_codes 中至少一个错误码出现在规则层结果里

不一致就是两层之一的 Bug，harness 以非零退出码失败并列出计划 id 与公式 id。
同时统计每例最小鲁棒度分布、险胜例数和单次验证开销（ms）。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from agent.tools.contracts import ToolName, ToolResultStatus, UserRole
from agent.tools.cpp_client import CppProgram, FixedCppJsonClient
from agent.tools.registry import build_tool_registry
from domains.amr_warehouse import TransportOrder
from evals.p018.contracts import EvalCase
from evals.p018.dataset import PROJECT_ROOT, load_dataset
from evals.p018.hard_map import (
    EXTRA_OBSTACLES_PER_CASE,
    HARD_ENVIRONMENT_REF,
    snapshot_provider_for_case,
)
from services.amr_simulator.contracts import SimulationPlan

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tmp" / "stl_consistency"
SEED_DATA_ROOT = PROJECT_ROOT / "domains" / "amr_warehouse" / "data"
# 与 evals.p018.hard_map 的私有映射保持一致；只用于缺省 pickup/dropoff。
ORDER_LOCATIONS = {
    "ORDER-001": ("P1", "S3"),
    "ORDER-002": ("P2", "S1"),
    "ORDER-003": ("P3", "S4"),
}
TRANSPORT_SCENARIOS = {"normal_order", "approval_required_waiting_resume", "approval_rejected"}
DEFAULT_CONFIG = {
    "maximum_load_kg": 100.0,
    "energy_per_cell_percent": 1.0,
    "battery_safety_reserve_percent": 15.0,
    "new_task_battery_threshold_percent": 20.0,
    "critical_battery_threshold_percent": 10.0,
    "minimum_safety_distance_cells": 1,
    "default_workstation_capacity": 1,
}


@dataclass
class ValidatorRun:
    """一次 CLI 调用的结构化结果。"""

    exit_code: int
    response: dict[str, Any]
    elapsed_ms: float

    @property
    def rule_codes(self) -> list[str]:
        """规则层错误码（剔除 gate 模式追加的 STL 错误）。"""

        return sorted(
            {
                str(item["code"])
                for item in self.response.get("errors", [])
                if item.get("code") != "stl_specification_violated"
            }
        )


@dataclass
class PlanRecord:
    """一个计划的核对记录。"""

    plan_id: str
    kind: str
    source_case_id: str | None
    mutation: str | None
    rules_valid: bool
    rule_codes: list[str]
    stl_status: str
    stl_satisfied: bool
    violated_formulas: list[str]
    narrow_pass_formulas: list[str]
    min_robustness: float | None
    min_robustness_formula_id: str | None
    formula_robustness: dict[str, float | None]
    formula_consistency: dict[str, bool]
    plan_consistent: bool
    rules_only_ms: float
    with_stl_ms: float
    gate_valid: bool
    notes: list[str] = field(default_factory=list)


class ValidatorCli:
    """固定 fleet_plan_validator_cli 调用；argv 与生产 cpp_client 相同。"""

    def __init__(self) -> None:
        client = FixedCppJsonClient()
        self.executable = client.executable_path(CppProgram.FLEET_PLAN_VALIDATOR)
        self.spec_path = client.stl_specification_path
        self.repository_root = client.repository_root
        if not self.executable.is_file():
            raise FileNotFoundError(f"缺少 Validator 可执行文件: {self.executable}")
        if not self.spec_path.is_file():
            raise FileNotFoundError(f"缺少 STL 规约文件: {self.spec_path}")

    def run(self, plan: Mapping[str, Any], *, with_stl: bool) -> ValidatorRun:
        arguments = [str(self.executable), "--validate"]
        if with_stl:
            arguments.extend(["--stl-spec", str(self.spec_path)])
        request = json.dumps(dict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        started = time.perf_counter()
        completed = subprocess.run(
            arguments,
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30.0,
            check=False,
            shell=False,
            cwd=str(self.repository_root),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Validator 输出不是 JSON (exit={completed.returncode}): {completed.stderr[-500:]}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"Validator 契约错误 (exit={completed.returncode}): {response}")
        return ValidatorRun(exit_code=completed.returncode, response=response, elapsed_ms=elapsed_ms)

    def describe_spec(self) -> dict[str, Any]:
        completed = subprocess.run(
            [str(self.executable), "--describe-stl-spec", str(self.spec_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30.0,
            check=False,
            shell=False,
            cwd=str(self.repository_root),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"规约描述失败: {completed.stdout}")
        return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# 基础计划：P0-18 运输类用例 × 生产 C++ 链路
# ---------------------------------------------------------------------------


def _seed_order(order_id: str) -> dict[str, Any]:
    payload = json.loads((SEED_DATA_ROOT / "orders_seed_v1.json").read_text(encoding="utf-8"))
    return next((item for item in payload["orders"] if item["order_id"] == order_id), payload["orders"][0])


def case_has_transport_plan(case: EvalCase) -> bool:
    """只有会经过 allocate → route → validate 主链的用例才有可核对的计划。"""

    return case.scenario in TRANSPORT_SCENARIOS or case.category.value == "exception_local_replan"


def case_order(case: EvalCase) -> TransportOrder:
    """与 evals.p018.online._case_order 相同的派生规则。"""

    order_id = str(case.input_data.get("order_id") or (case.order_refs[0] if case.order_refs else "ORDER-001"))
    source = _seed_order(order_id)
    pickup_default, dropoff_default = ORDER_LOCATIONS.get(order_id, ("P1", "S3"))
    release_time = int(case.input_data["release_time"]) if "release_time" in case.input_data else int(source["release_time"])
    return TransportOrder.model_validate(
        {
            **source,
            "pickup": str(case.input_data.get("pickup") or pickup_default),
            "dropoff": str(case.input_data.get("dropoff") or dropoff_default),
            "priority": int(case.input_data.get("priority") or source["priority"]),
            "release_time": release_time,
            "dependencies": [],
        }
    )


def build_case_plan(case: EvalCase, cli: ValidatorCli) -> tuple[dict[str, Any] | None, str | None]:
    """用正式 ToolRegistry 跑 Hungarian + A*，返回 P0-10 计划 envelope。"""

    amr_id = str(case.input_data.get("amr_id") or (case.amr_refs[0] if case.amr_refs else "AMR-01"))
    primary = case_order(case)
    batteries: dict[str, float] = {}
    if "start_battery" in case.input_data:
        batteries[amr_id] = float(case.input_data["start_battery"])
    source = _seed_order(primary.order_id)
    completed = [str(item) for item in case.input_data.get("completed_before") or []]
    completed.extend(str(item) for item in source.get("dependencies") or [])
    completed_order_ids = sorted({item for item in completed if item and item != primary.order_id})
    provider = snapshot_provider_for_case(
        amr_id=amr_id,
        order_id=primary.order_id,
        seed=case.seed,
        pickup=primary.pickup,
        dropoff=primary.dropoff,
        orders=[primary],
        amr_batteries=batteries or None,
        completed_order_ids=completed_order_ids,
        extra_count=EXTRA_OBSTACLES_PER_CASE,
    )
    registry = build_tool_registry(snapshot_provider=provider, cpp_client=FixedCppJsonClient())
    allocation = registry.execute(
        ToolName.ALLOCATE_TASKS,
        {"environment_ref": HARD_ENVIRONMENT_REF, "order_ids": [primary.order_id]},
        role=UserRole.OPERATOR,
        call_id=f"stl-alloc-{case.case_id}",
    )
    if allocation.status is not ToolResultStatus.SUCCESS or not allocation.output or not allocation.output.get("assignments"):
        return None, f"allocation_unavailable: {allocation.error.code if allocation.error else 'no_assignment'}"
    assignment = allocation.output["assignments"][0]
    route = registry.execute(
        ToolName.PLAN_MULTI_AMR_ROUTES,
        {
            "environment_ref": HARD_ENVIRONMENT_REF,
            "assignments": [{"amr_id": assignment["amr_id"], "order_id": assignment["order_id"]}],
            "max_time": 120,
        },
        role=UserRole.OPERATOR,
        call_id=f"stl-route-{case.case_id}",
    )
    if route.status is not ToolResultStatus.SUCCESS or not route.output or route.output.get("status") != "complete":
        return None, f"route_unavailable: {route.error.code if route.error else route.output and route.output.get('status')}"
    snapshot = provider.get_snapshot(HARD_ENVIRONMENT_REF)
    plan = {
        "schema_version": "1.0",
        "environment_ref": snapshot.environment_ref,
        "map_width": snapshot.map_width,
        "map_height": snapshot.map_height,
        "blocked_cells": [item.model_dump(mode="json") for item in snapshot.blocked_cells],
        "blocked_edges": [
            {"from": edge["from"].model_dump(mode="json"), "to": edge["to"].model_dump(mode="json")}
            for edge in snapshot.blocked_edges
        ],
        "one_way_edges": [
            {"from": edge["from"].model_dump(mode="json"), "to": edge["to"].model_dump(mode="json")}
            for edge in snapshot.one_way_edges
        ],
        "amrs": [item.model_dump(mode="json") for item in snapshot.amrs],
        "orders": [primary.model_dump(mode="json")],
        "location_positions": {key: value.model_dump(mode="json") for key, value in snapshot.location_positions.items()},
        "completed_order_ids": list(snapshot.completed_order_ids),
        "routes": [{**item, "payload_kg": 1.0} for item in route.output["routes"]],
        "start_time": snapshot.start_time,
        "max_time": 120,
        "config": dict(DEFAULT_CONFIG),
        "workstation_capacities": dict(snapshot.workstation_capacities),
        "ruleset_version": "p0-10.v1",
    }
    return canonical_plan(plan), None


def canonical_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """经过 SimulationPlan 契约后再 dump，保证与工具层发给 C++ 的 envelope 相同。"""

    return SimulationPlan.model_validate(plan).model_dump(mode="json", by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
# 变异：每个变异都应触发某条被 STL 覆盖的规则错误码
# ---------------------------------------------------------------------------

Mutation = Callable[[dict[str, Any]], dict[str, Any] | None]


def _primary_route(plan: dict[str, Any]) -> dict[str, Any]:
    return plan["routes"][0]


def _primary_order(plan: dict[str, Any]) -> dict[str, Any]:
    route = _primary_route(plan)
    return next(order for order in plan["orders"] if order["order_id"] == route["order_id"])


def _primary_amr(plan: dict[str, Any]) -> dict[str, Any]:
    route = _primary_route(plan)
    return next(amr for amr in plan["amrs"] if amr["amr_id"] == route["amr_id"])


def _move_count(route: Mapping[str, Any]) -> int:
    return sum(1 for step in route["path"] if step["action"] == "move")


def _first_move_index(route: Mapping[str, Any]) -> int | None:
    path = route["path"]
    for index in range(1, len(path)):
        if path[index]["position"] != path[index - 1]["position"]:
            return index
    return None


def _cells(plan: dict[str, Any]) -> set[tuple[int, int]]:
    return {(cell["x"], cell["y"]) for cell in plan["blocked_cells"]}


def _occupied_starts(plan: dict[str, Any]) -> set[tuple[int, int]]:
    return {(amr["position"]["x"], amr["position"]["y"]) for amr in plan["amrs"]}


def mutate_payload_over_capacity(plan: dict[str, Any]) -> dict[str, Any]:
    mutated = deepcopy(plan)
    _primary_route(mutated)["payload_kg"] = float(mutated["config"]["maximum_load_kg"]) + 1.0
    return mutated


def mutate_battery_below_reserve(plan: dict[str, Any]) -> dict[str, Any]:
    mutated = deepcopy(plan)
    moves = _move_count(_primary_route(mutated))
    _primary_amr(mutated)["battery"] = float(moves) + 14.0
    return mutated


def mutate_battery_at_admission_threshold(plan: dict[str, Any]) -> dict[str, Any]:
    mutated = deepcopy(plan)
    _primary_amr(mutated)["battery"] = 20.0
    return mutated


def mutate_battery_critical(plan: dict[str, Any]) -> dict[str, Any]:
    mutated = deepcopy(plan)
    _primary_amr(mutated)["battery"] = 10.0
    return mutated


def mutate_deadline_before_dropoff(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    order = _primary_order(mutated)
    dropoff_time = int(_primary_route(mutated)["dropoff_time"])
    if dropoff_time - 1 <= int(order["release_time"]):
        return None
    order["deadline"] = dropoff_time - 1
    return mutated


def mutate_release_after_pickup(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    order = _primary_order(mutated)
    pickup_time = int(_primary_route(mutated)["pickup_time"])
    if pickup_time + 1 >= int(order["deadline"]):
        return None
    order["release_time"] = pickup_time + 1
    return mutated


def mutate_blocked_cell_on_path(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    path = _primary_route(mutated)["path"]
    start = path[0]["position"]
    candidates = [step["position"] for step in path[1:] if step["position"] != start]
    if not candidates:
        return None
    cell = candidates[len(candidates) // 2]
    if (cell["x"], cell["y"]) in _cells(mutated):
        return None
    mutated["blocked_cells"].append(dict(cell))
    return mutated


def mutate_blocked_edge_on_path(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    route = _primary_route(mutated)
    index = _first_move_index(route)
    if index is None:
        return None
    mutated["blocked_edges"].append(
        {"from": dict(route["path"][index - 1]["position"]), "to": dict(route["path"][index]["position"])}
    )
    return mutated


def mutate_one_way_reverse_on_path(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    route = _primary_route(mutated)
    index = _first_move_index(route)
    if index is None:
        return None
    mutated["one_way_edges"].append(
        {"from": dict(route["path"][index]["position"]), "to": dict(route["path"][index - 1]["position"])}
    )
    return mutated


def mutate_truncate_before_dropoff(plan: dict[str, Any]) -> dict[str, Any] | None:
    mutated = deepcopy(plan)
    route = _primary_route(mutated)
    order = _primary_order(mutated)
    dropoff = mutated["location_positions"][order["dropoff"]]
    if len(route["path"]) < 3:
        return None
    route["path"] = route["path"][:-1]
    if route["path"][-1]["position"] == dropoff:
        return None
    route["dropoff_time"] = int(route["path"][-1]["time"])
    return mutated


def _free_cell(plan: dict[str, Any], candidates: Sequence[tuple[int, int]]) -> tuple[int, int] | None:
    blocked = _cells(plan)
    starts = _occupied_starts(plan)
    path_cells = {(step["position"]["x"], step["position"]["y"]) for route in plan["routes"] for step in route["path"]}
    for x, y in candidates:
        if not (0 <= x < plan["map_width"] and 0 <= y < plan["map_height"]):
            continue
        if (x, y) in blocked or (x, y) in starts or (x, y) in path_cells:
            continue
        return (x, y)
    return None


def _extra_amr(x: int, y: int) -> dict[str, Any]:
    return {
        "amr_id": "AMR-STL",
        "position": {"x": x, "y": y},
        "heading": 0,
        "battery": 100.0,
        "load": 0.0,
        "task_status": "IDLE",
        "health_status": "HEALTHY",
        "connection_status": "ONLINE",
    }


def mutate_idle_amr_on_path(plan: dict[str, Any]) -> dict[str, Any] | None:
    """在路径中段放一台空闲 AMR：规则层 vertex_conflict，STL fleet_separation。"""

    mutated = deepcopy(plan)
    path = _primary_route(mutated)["path"]
    starts = _occupied_starts(mutated)
    candidates = [step["position"] for step in path[1:] if (step["position"]["x"], step["position"]["y"]) not in starts]
    if not candidates:
        return None
    cell = candidates[len(candidates) // 2]
    mutated["amrs"].append(_extra_amr(cell["x"], cell["y"]))
    return mutated


def mutate_safety_distance_two(plan: dict[str, Any]) -> dict[str, Any] | None:
    """最小安全距离改为 2，并在路径旁放一台空闲 AMR（相邻但不同格）。"""

    mutated = deepcopy(plan)
    path = _primary_route(mutated)["path"]
    mid = path[len(path) // 2]["position"]
    neighbours = [(mid["x"] + 1, mid["y"]), (mid["x"] - 1, mid["y"]), (mid["x"], mid["y"] + 1), (mid["x"], mid["y"] - 1)]
    cell = _free_cell(mutated, neighbours)
    if cell is None:
        return None
    mutated["config"]["minimum_safety_distance_cells"] = 2
    mutated["amrs"].append(_extra_amr(*cell))
    return mutated


def mutate_dependency_unplanned(plan: dict[str, Any]) -> dict[str, Any] | None:
    """增加一个没有路线的前置订单：规则层 task_dependency_unplanned，STL until 永不满足。"""

    mutated = deepcopy(plan)
    order = _primary_order(mutated)
    if order["order_id"] == "ORDER-STL-DEP":
        return None
    pickup, dropoff = ("P2", "S1") if order["pickup"] != "P2" else ("P3", "S4")
    if pickup not in mutated["location_positions"] or dropoff not in mutated["location_positions"]:
        return None
    mutated["orders"].append(
        {
            "order_id": "ORDER-STL-DEP",
            "material_id": "MAT-STL",
            "pickup": pickup,
            "dropoff": dropoff,
            "priority": 3,
            "release_time": 0,
            "deadline": 120,
            "dependencies": [],
        }
    )
    order["dependencies"] = ["ORDER-STL-DEP"]
    return mutated


MUTATIONS: dict[str, Mutation] = {
    "payload_over_capacity": mutate_payload_over_capacity,
    "battery_below_reserve": mutate_battery_below_reserve,
    "battery_at_admission_threshold": mutate_battery_at_admission_threshold,
    "battery_critical": mutate_battery_critical,
    "deadline_before_dropoff": mutate_deadline_before_dropoff,
    "release_after_pickup": mutate_release_after_pickup,
    "blocked_cell_on_path": mutate_blocked_cell_on_path,
    "blocked_edge_on_path": mutate_blocked_edge_on_path,
    "one_way_reverse_on_path": mutate_one_way_reverse_on_path,
    "truncate_before_dropoff": mutate_truncate_before_dropoff,
    "idle_amr_on_path": mutate_idle_amr_on_path,
    "safety_distance_two": mutate_safety_distance_two,
    "dependency_unplanned": mutate_dependency_unplanned,
}


# ---------------------------------------------------------------------------
# 合成场景：Python 版 P0-10 CTest 正反例（多车冲突无法由单订单变异得到）
# ---------------------------------------------------------------------------


def _step(x: int, y: int, heading: int, time_: int, action: str, g_cost: float) -> dict[str, Any]:
    return {"position": {"x": x, "y": y}, "heading": heading, "time": time_, "action": action, "g_cost": g_cost}


def _amr(amr_id: str, x: int, y: int, heading: int = 90, battery: float = 100.0) -> dict[str, Any]:
    return {
        "amr_id": amr_id,
        "position": {"x": x, "y": y},
        "heading": heading,
        "battery": battery,
        "load": 0.0,
        "task_status": "IDLE",
        "health_status": "HEALTHY",
        "connection_status": "ONLINE",
    }


def _order(order_id: str, pickup: str, dropoff: str, dependencies: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "material_id": f"MAT-{order_id}",
        "pickup": pickup,
        "dropoff": dropoff,
        "priority": 3,
        "release_time": 0,
        "deadline": 20,
        "dependencies": list(dependencies),
    }


def _route(amr_id: str, order_id: str, pickup_time: int, dropoff_time: int, path: list[dict[str, Any]], payload: float = 5.0) -> dict[str, Any]:
    return {
        "amr_id": amr_id,
        "order_id": order_id,
        "payload_kg": payload,
        "pickup_time": pickup_time,
        "dropoff_time": dropoff_time,
        "path": path,
    }


def synthetic_base() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "environment_ref": "warehouse_v1@stl-synthetic",
        "map_width": 10,
        "map_height": 5,
        "blocked_cells": [],
        "blocked_edges": [],
        "one_way_edges": [],
        "amrs": [_amr("AMR-01", 0, 0), _amr("AMR-02", 0, 4)],
        "orders": [_order("ORDER-001", "P1", "S1"), _order("ORDER-002", "P2", "S2")],
        "location_positions": {
            "P1": {"x": 1, "y": 0},
            "S1": {"x": 2, "y": 0},
            "P2": {"x": 1, "y": 4},
            "S2": {"x": 2, "y": 4},
        },
        "completed_order_ids": [],
        "routes": [
            _route("AMR-01", "ORDER-001", 1, 2, [_step(0, 0, 90, 0, "start", 0.0), _step(1, 0, 90, 1, "move", 1.0), _step(2, 0, 90, 2, "move", 2.0)]),
            _route("AMR-02", "ORDER-002", 1, 2, [_step(0, 4, 90, 0, "start", 0.0), _step(1, 4, 90, 1, "move", 1.0), _step(2, 4, 90, 2, "move", 2.0)]),
        ],
        "start_time": 0,
        "max_time": 20,
        "config": dict(DEFAULT_CONFIG),
        "workstation_capacities": {"P1": 1, "S1": 1, "P2": 1, "S2": 1},
        "ruleset_version": "p0-10.v1",
    }


def synthetic_scenarios() -> list[tuple[str, dict[str, Any]]]:
    scenarios: list[tuple[str, dict[str, Any]]] = [("synthetic_valid", synthetic_base())]

    dependency = synthetic_base()
    dependency["orders"][1]["dependencies"] = ["ORDER-001"]
    scenarios.append(("synthetic_dependency_time_order", dependency))

    capacity = synthetic_base()
    capacity["amrs"] = [_amr("AMR-01", 0, 1, 90), _amr("AMR-02", 2, 1, 270)]
    capacity["orders"] = [_order("ORDER-001", "P1", "S1"), _order("ORDER-002", "P1", "S2")]
    capacity["location_positions"] = {"P1": {"x": 1, "y": 1}, "S1": {"x": 0, "y": 0}, "S2": {"x": 2, "y": 2}}
    capacity["workstation_capacities"] = {"P1": 1, "S1": 1, "S2": 1}
    capacity["routes"] = [
        _route("AMR-01", "ORDER-001", 1, 5, [
            _step(0, 1, 90, 0, "start", 0.0), _step(1, 1, 90, 1, "move", 1.0), _step(1, 1, 0, 2, "turn_left", 1.25),
            _step(1, 0, 0, 3, "move", 2.25), _step(0, 0, 270, 4, "turn_left", 2.5), _step(0, 0, 270, 5, "move", 3.5)]),
        _route("AMR-02", "ORDER-002", 1, 5, [
            _step(2, 1, 270, 0, "start", 0.0), _step(1, 1, 270, 1, "move", 1.0), _step(1, 1, 180, 2, "turn_left", 1.25),
            _step(1, 2, 180, 3, "move", 2.25), _step(1, 2, 90, 4, "turn_left", 2.5), _step(2, 2, 90, 5, "move", 3.5)]),
    ]
    scenarios.append(("synthetic_workstation_capacity", capacity))

    distance = synthetic_base()
    distance["config"]["minimum_safety_distance_cells"] = 2
    distance["amrs"] = [_amr("AMR-01", 0, 0, 90), _amr("AMR-02", 1, 0, 90)]
    distance["location_positions"] = {"P1": {"x": 0, "y": 0}, "S1": {"x": 0, "y": 1}, "P2": {"x": 1, "y": 0}, "S2": {"x": 1, "y": 1}}
    distance["routes"] = [
        _route("AMR-01", "ORDER-001", 0, 2, [_step(0, 0, 90, 0, "start", 0.0), _step(0, 0, 180, 1, "turn_right", 0.25), _step(0, 1, 180, 2, "move", 1.25)], 1.0),
        _route("AMR-02", "ORDER-002", 0, 2, [_step(1, 0, 90, 0, "start", 0.0), _step(1, 0, 180, 1, "turn_right", 0.25), _step(1, 1, 180, 2, "move", 1.25)], 1.0),
    ]
    scenarios.append(("synthetic_safety_distance", distance))

    vertex = synthetic_base()
    vertex["amrs"] = [_amr("AMR-01", 0, 0, 90), _amr("AMR-02", 2, 0, 270)]
    vertex["location_positions"] = {"P1": {"x": 1, "y": 0}, "S1": {"x": 2, "y": 0}, "P2": {"x": 1, "y": 0}, "S2": {"x": 2, "y": 1}}
    vertex["workstation_capacities"] = {"P1": 2, "S1": 1, "P2": 2, "S2": 1}
    vertex["routes"] = [
        _route("AMR-01", "ORDER-001", 1, 2, [_step(0, 0, 90, 0, "start", 0.0), _step(1, 0, 90, 1, "move", 1.0), _step(2, 0, 90, 2, "move", 2.0)], 1.0),
        _route("AMR-02", "ORDER-002", 1, 5, [
            _step(2, 0, 270, 0, "start", 0.0), _step(1, 0, 270, 1, "move", 1.0), _step(1, 0, 180, 2, "turn_left", 1.25),
            _step(1, 1, 180, 3, "move", 2.25), _step(1, 1, 90, 4, "turn_left", 2.5), _step(2, 1, 90, 5, "move", 3.5)], 1.0),
    ]
    scenarios.append(("synthetic_vertex_conflict", vertex))

    swap = synthetic_base()
    swap["amrs"] = [_amr("AMR-01", 0, 1, 90), _amr("AMR-02", 1, 1, 270)]
    swap["location_positions"] = {"P1": {"x": 1, "y": 1}, "S1": {"x": 1, "y": 2}, "P2": {"x": 0, "y": 1}, "S2": {"x": 0, "y": 2}}
    swap["routes"] = [
        _route("AMR-01", "ORDER-001", 1, 3, [_step(0, 1, 90, 0, "start", 0.0), _step(1, 1, 90, 1, "move", 1.0), _step(1, 1, 180, 2, "turn_right", 1.25), _step(1, 2, 180, 3, "move", 2.25)], 1.0),
        _route("AMR-02", "ORDER-002", 1, 3, [_step(1, 1, 270, 0, "start", 0.0), _step(0, 1, 270, 1, "move", 1.0), _step(0, 1, 180, 2, "turn_left", 1.25), _step(0, 2, 180, 3, "move", 2.25)], 1.0),
    ]
    scenarios.append(("synthetic_swap_edge", swap))
    return scenarios


# ---------------------------------------------------------------------------
# 核对
# ---------------------------------------------------------------------------


def check_plan(
    plan_id: str,
    kind: str,
    plan: Mapping[str, Any],
    cli: ValidatorCli,
    formulas: Sequence[Mapping[str, Any]],
    *,
    source_case_id: str | None = None,
    mutation: str | None = None,
) -> PlanRecord:
    rules_only = cli.run(plan, with_stl=False)
    with_stl = cli.run(plan, with_stl=True)
    rule_codes = rules_only.rule_codes
    if rule_codes != with_stl.rule_codes:
        raise RuntimeError(f"{plan_id}: 加载规约后规则层错误码发生变化，STL 层不应影响规则层")
    stl = with_stl.response.get("stl") or {}
    results = stl.get("results", [])
    violated = sorted({item["formula_id"] for item in results if not item["satisfied"]})
    narrow = sorted({item["formula_id"] for item in results if item.get("narrow_pass")})
    formula_robustness: dict[str, float | None] = {}
    for formula in formulas:
        values = [item["robustness"] for item in results if item["formula_id"] == formula["id"] and item["robustness"] is not None]
        formula_robustness[formula["id"]] = min(values) if values else None
    formula_consistency: dict[str, bool] = {}
    for formula in formulas:
        if not formula["rule_codes"]:
            continue
        expected = any(code in rule_codes for code in formula["rule_codes"])
        observed = formula["id"] in violated
        formula_consistency[formula["id"]] = expected == observed
    covered_codes = {code for formula in formulas for code in formula["rule_codes"]}
    rule_flag = any(code in covered_codes for code in rule_codes)
    stl_flag = bool(violated)
    return PlanRecord(
        plan_id=plan_id,
        kind=kind,
        source_case_id=source_case_id,
        mutation=mutation,
        rules_valid=bool(rules_only.response.get("valid")),
        rule_codes=rule_codes,
        stl_status=str(stl.get("status")),
        stl_satisfied=bool(stl.get("satisfied")),
        violated_formulas=violated,
        narrow_pass_formulas=narrow,
        min_robustness=stl.get("min_robustness"),
        min_robustness_formula_id=stl.get("min_robustness_formula_id"),
        formula_robustness=formula_robustness,
        formula_consistency=formula_consistency,
        plan_consistent=(rule_flag == stl_flag) and all(formula_consistency.values()),
        rules_only_ms=rules_only.elapsed_ms,
        with_stl_ms=with_stl.elapsed_ms,
        gate_valid=bool(with_stl.response.get("valid")),
    )


def run_harness(output_dir: Path = DEFAULT_OUTPUT_DIR, *, dataset_path: Path | None = None) -> dict[str, Any]:
    cli = ValidatorCli()
    spec = cli.describe_spec()
    formulas = spec["formulas"]
    dataset = load_dataset(dataset_path) if dataset_path else load_dataset()
    records: list[PlanRecord] = []
    skipped: list[dict[str, str]] = []
    base_plans: list[tuple[str, dict[str, Any]]] = []

    for case in dataset.cases:
        if not case_has_transport_plan(case):
            continue
        plan, reason = build_case_plan(case, cli)
        if plan is None:
            skipped.append({"case_id": case.case_id, "reason": reason or "unknown"})
            continue
        base_plans.append((case.case_id, plan))
        records.append(check_plan(f"{case.case_id}/base", "case_base", plan, cli, formulas, source_case_id=case.case_id))
        for name, mutation in MUTATIONS.items():
            mutated = mutation(plan)
            if mutated is None:
                skipped.append({"case_id": f"{case.case_id}/{name}", "reason": "mutation_not_applicable"})
                continue
            records.append(
                check_plan(
                    f"{case.case_id}/{name}",
                    "case_mutation",
                    canonical_plan(mutated),
                    cli,
                    formulas,
                    source_case_id=case.case_id,
                    mutation=name,
                )
            )
    for name, plan in synthetic_scenarios():
        records.append(check_plan(name, "synthetic", canonical_plan(plan), cli, formulas))

    report = summarize(records, skipped, formulas, spec, base_case_count=len(base_plans))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "stl_consistency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "stl_consistency.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return report


def summarize(
    records: Sequence[PlanRecord],
    skipped: Sequence[Mapping[str, str]],
    formulas: Sequence[Mapping[str, Any]],
    spec: Mapping[str, Any],
    *,
    base_case_count: int,
) -> dict[str, Any]:
    per_formula: dict[str, dict[str, Any]] = {}
    for formula in formulas:
        checks = [record for record in records if formula["id"] in record.formula_consistency]
        mismatches = [record.plan_id for record in checks if not record.formula_consistency[formula["id"]]]
        base_values = [
            record.formula_robustness[formula["id"]]
            for record in records
            if record.kind == "case_base" and record.formula_robustness.get(formula["id"]) is not None
        ]
        per_formula[formula["id"]] = {
            "scope": formula["scope"],
            "rule_codes": list(formula["rule_codes"]),
            "warn_below": formula.get("warn_below"),
            "checks": len(checks),
            "consistent": len(checks) - len(mismatches),
            "mismatches": mismatches,
            "violations_observed": sum(formula["id"] in record.violated_formulas for record in records),
            "narrow_passes_on_base_plans": sum(
                formula["id"] in record.narrow_pass_formulas for record in records if record.kind == "case_base"
            ),
            "base_plan_robustness": {
                "count": len(base_values),
                "min": min(base_values) if base_values else None,
                "median": statistics.median(base_values) if base_values else None,
                "max": max(base_values) if base_values else None,
            },
        }
    covered_codes = sorted({code for formula in formulas for code in formula["rule_codes"]})
    observed_codes = Counter(code for record in records for code in record.rule_codes)
    uncovered_observed = {code: count for code, count in sorted(observed_codes.items()) if code not in covered_codes}
    inconsistent = [record for record in records if not record.plan_consistent]
    rules_only = [record.rules_only_ms for record in records]
    with_stl = [record.with_stl_ms for record in records]
    base_min = [record.min_robustness for record in records if record.kind == "case_base" and record.min_robustness is not None]
    mutation_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"plans": 0, "stl_violated": 0, "rules_invalid": 0})
    for record in records:
        if record.mutation is None:
            continue
        stats = mutation_stats[record.mutation]
        stats["plans"] += 1
        stats["stl_violated"] += int(not record.stl_satisfied)
        stats["rules_invalid"] += int(not record.rules_valid)
    return {
        "spec_id": spec["spec_id"],
        "spec_version": spec["spec_version"],
        "enforcement": spec["enforcement"],
        "formula_count": len(formulas),
        "covered_rule_codes": covered_codes,
        "covered_rule_code_count": len(covered_codes),
        "base_case_count": base_case_count,
        "plan_count": len(records),
        "plan_counts_by_kind": dict(Counter(record.kind for record in records)),
        "consistent_plan_count": len(records) - len(inconsistent),
        "inconsistent_plans": [
            {
                "plan_id": record.plan_id,
                "rule_codes": record.rule_codes,
                "violated_formulas": record.violated_formulas,
                "formula_consistency": record.formula_consistency,
            }
            for record in inconsistent
        ],
        "formula_check_count": sum(len(record.formula_consistency) for record in records),
        "formula_mismatch_count": sum(
            sum(not value for value in record.formula_consistency.values()) for record in records
        ),
        "per_formula": per_formula,
        "mutations": dict(sorted(mutation_stats.items())),
        "uncovered_rule_codes_observed": uncovered_observed,
        "base_plans": {
            "all_rules_valid": all(record.rules_valid for record in records if record.kind == "case_base"),
            "all_stl_satisfied": all(record.stl_satisfied for record in records if record.kind == "case_base"),
            "gate_valid_count": sum(record.gate_valid for record in records if record.kind == "case_base"),
            "narrow_pass_plan_count": sum(bool(record.narrow_pass_formulas) for record in records if record.kind == "case_base"),
            "min_robustness": {
                "count": len(base_min),
                "min": min(base_min) if base_min else None,
                "median": statistics.median(base_min) if base_min else None,
                "max": max(base_min) if base_min else None,
                "histogram": dict(sorted(Counter(round(value) for value in base_min).items())),
            },
        },
        "timing_ms": {
            "rules_only_median": statistics.median(rules_only) if rules_only else None,
            "with_stl_median": statistics.median(with_stl) if with_stl else None,
            "overhead_median": (statistics.median(with_stl) - statistics.median(rules_only)) if rules_only else None,
            "with_stl_p95": sorted(with_stl)[int(0.95 * (len(with_stl) - 1))] if with_stl else None,
        },
        "skipped": list(skipped),
        "records": [
            {
                "plan_id": record.plan_id,
                "kind": record.kind,
                "mutation": record.mutation,
                "rules_valid": record.rules_valid,
                "rule_codes": record.rule_codes,
                "stl_status": record.stl_status,
                "violated_formulas": record.violated_formulas,
                "narrow_pass_formulas": record.narrow_pass_formulas,
                "min_robustness": record.min_robustness,
                "min_robustness_formula_id": record.min_robustness_formula_id,
                "plan_consistent": record.plan_consistent,
                "gate_valid": record.gate_valid,
                "rules_only_ms": round(record.rules_only_ms, 3),
                "with_stl_ms": round(record.with_stl_ms, 3),
            }
            for record in records
        ],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# P1-1 STL 与规则验证器布尔一致性核对",
        "",
        f"- 规约：`{report['spec_id']}` / `{report['spec_version']}`（enforcement={report['enforcement']}），公式 {report['formula_count']} 条，覆盖规则错误码 {report['covered_rule_code_count']} 个。",
        f"- 基础计划 {report['base_case_count']} 个（P0-18 运输类用例经生产 Hungarian/A* 生成），计划总数 {report['plan_count']}：{report['plan_counts_by_kind']}。",
        f"- 计划级一致：{report['consistent_plan_count']}/{report['plan_count']}；公式级核对 {report['formula_check_count']} 次，不一致 {report['formula_mismatch_count']} 次。",
        f"- 基础计划全部规则层 valid：{report['base_plans']['all_rules_valid']}；全部 STL satisfied：{report['base_plans']['all_stl_satisfied']}；险胜计划数 {report['base_plans']['narrow_pass_plan_count']}。",
        f"- 基础计划最小鲁棒度：min={report['base_plans']['min_robustness']['min']}, median={report['base_plans']['min_robustness']['median']}, max={report['base_plans']['min_robustness']['max']}，直方图 {report['base_plans']['min_robustness']['histogram']}。",
        f"- 单次验证开销中位数：仅规则层 {report['timing_ms']['rules_only_median']:.1f} ms，规则层+STL {report['timing_ms']['with_stl_median']:.1f} ms，STL 增量 {report['timing_ms']['overhead_median']:.1f} ms（含进程启动）。",
        "",
        "## 逐公式",
        "",
        "| 公式 | 作用域 | 规则错误码 | 核对次数 | 一致 | 观察到违反 | 基础计划险胜 | 基础计划鲁棒度 min/median/max |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for formula_id, item in report["per_formula"].items():
        robustness = item["base_plan_robustness"]
        lines.append(
            f"| `{formula_id}` | {item['scope']} | {', '.join(item['rule_codes']) or '（无对应规则）'} | {item['checks']} | {item['consistent']} | "
            f"{item['violations_observed']} | {item['narrow_passes_on_base_plans']} | {robustness['min']} / {robustness['median']} / {robustness['max']} |"
        )
    lines.extend(["", "## 变异", "", "| 变异 | 计划数 | 规则层 invalid | STL violated |", "|---|---:|---:|---:|"])
    for name, stats in report["mutations"].items():
        lines.append(f"| `{name}` | {stats['plans']} | {stats['rules_invalid']} | {stats['stl_violated']} |")
    if report["inconsistent_plans"]:
        lines.extend(["", "## 不一致计划", ""])
        for item in report["inconsistent_plans"]:
            lines.append(f"- `{item['plan_id']}`：规则码 {item['rule_codes']}，STL 违反 {item['violated_formulas']}，公式一致性 {item['formula_consistency']}")
    if report["uncovered_rule_codes_observed"]:
        lines.extend(["", "## 观察到但不在 STL 覆盖范围内的规则错误码（结构/契约类）", ""])
        for code, count in report["uncovered_rule_codes_observed"].items():
            lines.append(f"- `{code}`：{count} 次")
    if report["skipped"]:
        lines.extend(["", "## 跳过", ""])
        for item in report["skipped"]:
            lines.append(f"- `{item['case_id']}`：{item['reason']}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1-1 STL/规则验证器布尔一致性核对")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--allow-mismatch", action="store_true", help="存在不一致时仍以 0 退出（仅用于调试）")
    args = parser.parse_args(argv)
    report = run_harness(args.output_dir, dataset_path=args.dataset)
    print(
        f"plans={report['plan_count']} consistent={report['consistent_plan_count']} "
        f"formula_checks={report['formula_check_count']} mismatches={report['formula_mismatch_count']} "
        f"base_cases={report['base_case_count']} narrow_pass_plans={report['base_plans']['narrow_pass_plan_count']} "
        f"stl_overhead_ms={report['timing_ms']['overhead_median']:.1f} output={args.output_dir}"
    )
    if report["formula_mismatch_count"] or report["consistent_plan_count"] != report["plan_count"]:
        print("STL/规则层布尔不一致，见报告 inconsistent_plans", file=sys.stderr)
        return 0 if args.allow_mismatch else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
