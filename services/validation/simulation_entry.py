"""固定的 P0-17 仿真验证入口。

该入口没有参数，也不读取用户提供的路径或命令；它使用仓库内最小合法运输夹具，
通过 P0-10 前置验证后运行 P0-11 仿真，并把真实 SimulationResult 打印到 stdout。
固定 runner 只登记本模块，不允许把本函数替换为任意脚本。
"""

from __future__ import annotations

import json
import sys

from services.amr_simulator import AMRSimulator, SimulationStatus


def _plan() -> dict[str, object]:
    """构造不依赖外部输入的确定性单订单计划，覆盖 pickup/transport/dropoff。"""

    path = [
        {"position": {"x": 1, "y": 1}, "heading": 90, "time": 0, "action": "start", "g_cost": 0.0},
        {"position": {"x": 2, "y": 1}, "heading": 90, "time": 1, "action": "move", "g_cost": 1.0},
        {"position": {"x": 3, "y": 1}, "heading": 90, "time": 2, "action": "move", "g_cost": 2.0},
        {"position": {"x": 4, "y": 1}, "heading": 90, "time": 3, "action": "move", "g_cost": 3.0},
        {"position": {"x": 5, "y": 1}, "heading": 90, "time": 4, "action": "move", "g_cost": 4.0},
        {"position": {"x": 6, "y": 1}, "heading": 90, "time": 5, "action": "move", "g_cost": 5.0},
    ]
    return {
        "schema_version": "1.0",
        "environment_ref": "warehouse_v1",
        "map_width": 30,
        "map_height": 20,
        "blocked_cells": [],
        "blocked_edges": [],
        "one_way_edges": [],
        "amrs": [{
            "amr_id": "AMR-01",
            "position": {"x": 1, "y": 1},
            "heading": 90,
            "battery": 100.0,
            "load": 0.0,
            "task_status": "IDLE",
            "health_status": "HEALTHY",
            "connection_status": "ONLINE",
        }],
        "orders": [{
            "order_id": "ORDER-VERIFY-01",
            "material_id": "MAT-VERIFY-01",
            "pickup": "P1",
            "dropoff": "S1",
            "priority": 3,
            "release_time": 0,
            "deadline": 30,
            "dependencies": [],
        }],
        "location_positions": {"P1": {"x": 3, "y": 1}, "S1": {"x": 6, "y": 1}},
        "completed_order_ids": [],
        "routes": [{
            "amr_id": "AMR-01",
            "order_id": "ORDER-VERIFY-01",
            "payload_kg": 10.0,
            "pickup_time": 2,
            "dropoff_time": 5,
            "path": path,
        }],
        "start_time": 0,
        "max_time": 7,
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


def main() -> int:
    """运行固定仿真并以真实结果决定进程退出码。"""

    try:
        result = AMRSimulator().run(
            _plan(),
            simulation_id="verification-fixed-simulation",
            seed=17,
            until_time=6,
        )
        payload = result.model_dump(mode="json")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if result.status is SimulationStatus.COMPLETED else 1
    except Exception as exc:  # 验证入口失败必须有可解析的错误文本并返回非零。
        print(f"simulation_entry_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
