"""在线 60 例与演示页共用的加难地图，以及按 seed 叠加障碍。

生产种子 ``domains/amr_warehouse/data/warehouse_v1.json`` 仍不改写。本模块服务
P0-18 ``online_fast_closed_loop`` 和 ``/demo``：货架墙迫使绕行，再在通道上放
少量额外障碍。评测按用例 seed 只保护一条订单走廊；演示允许任意 P→S，必须用
四邻域 BFS 证明全部 AMR 起点、取货、交付、充电仍连通。
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import random
from typing import Iterable, Sequence

from domains.amr_warehouse import GridPosition, TransportOrder, WarehouseMap
from agent.planning import ChargingGoal
from agent.tools.snapshots import EnvironmentSnapshot, SnapshotNotFoundError

from .dataset import PROJECT_ROOT

_AMR_STARTS = {
    "AMR-01": (1, 2),
    "AMR-02": (1, 5),
    "AMR-03": (1, 8),
    "AMR-04": (1, 11),
}
_ORDER_LOCATIONS = {
    "ORDER-001": ("P1", "S3"),
    "ORDER-002": ("P2", "S1"),
    "ORDER-003": ("P3", "S4"),
}


HARD_MAP_PATH = Path(__file__).resolve().parent / "maps" / "warehouse_v1_hard.json"
HARD_ENVIRONMENT_REF = "warehouse_v1@eval-hard"
MAP_WIDTH = 30
MAP_HEIGHT = 20
# 东西向通道覆盖 AMR 出生行、底部走廊，以及取货/交付工位所在行。
# 工位 y=3/6/9/12/15/18 必须留空，否则从 P* 到 S* 只能在货架缝里折行，
# A* 仍可解但时间窗更紧。货架墙仍在 x=8/13/18/23，评测地图比生产图难。
AISLE_YS = (1, 2, 3, 6, 9, 12, 15, 18)
RACK_XS = (8, 13, 18, 23)
# 每例 2 个 seed 通道障碍：保留绕行，避免 6 个障碍把单格缝堵成死迷宫。
EXTRA_OBSTACLES_PER_CASE = 2
# 演示页固定 seed：与 ORDER-001 评测族同号段，但连通性约束覆盖全部工位走廊。
DEMO_EXTRA_SEED = 18001
SEED_DATA_ROOT = PROJECT_ROOT / "domains" / "amr_warehouse" / "data"


def protected_cells(warehouse: WarehouseMap) -> set[tuple[int, int]]:
    """工位、充电站和 AMR 出生点不能被障碍占用。"""

    cells: set[tuple[int, int]] = set()
    for group in (warehouse.pickup_points, warehouse.dropoff_points, warehouse.charging_stations):
        for item in group:
            cells.add((item.x, item.y))
    cells.update(_AMR_STARTS.values())
    return cells


def rack_obstacle_cells() -> list[tuple[int, int]]:
    """生成货架墙坐标；通道行保持空闲。"""

    cells: list[tuple[int, int]] = []
    for x in RACK_XS:
        for y in range(MAP_HEIGHT):
            if y not in AISLE_YS:
                cells.append((x, y))
    # 保留原地图两格障碍，使加难集是 warehouse_v1 的超集而不是另一张无关图。
    cells.extend(((15, 0), (15, 1)))
    unique = sorted(set(cells))
    return unique


def build_hard_warehouse_map() -> WarehouseMap:
    """从生产种子复制工位，替换为货架障碍后通过 WarehouseMap 校验。"""

    source = json.loads((SEED_DATA_ROOT / "warehouse_v1.json").read_text(encoding="utf-8"))
    payload = dict(source)
    payload["map_id"] = "warehouse_v1_hard"
    payload["version"] = 3
    payload["obstacles"] = [{"x": x, "y": y} for x, y in rack_obstacle_cells()]
    # 原 (14,19) 临时封路与 x=13 货架墙相邻；加难图改为通道末端封路，避免与货架重复。
    payload["temporary_blocked_cells"] = [{"x": 16, "y": 19}]
    return WarehouseMap.model_validate(payload)


def export_hard_map(path: Path = HARD_MAP_PATH) -> Path:
    """把加难地图写成评测固定输入，供 SHA-256 指纹使用。"""

    warehouse = build_hard_warehouse_map()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(warehouse.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def neighbors(cell: tuple[int, int]) -> Iterable[tuple[int, int]]:
    """四邻域；越界格子直接丢弃。"""

    x, y = cell
    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
        if 0 <= nx < MAP_WIDTH and 0 <= ny < MAP_HEIGHT:
            yield (nx, ny)


def reachable(start: tuple[int, int], goal: tuple[int, int], blocked: set[tuple[int, int]]) -> bool:
    """判断两点在障碍集合下是否四邻域连通。"""

    if start in blocked or goal in blocked:
        return False
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            return True
        for nxt in neighbors(current):
            if nxt in seen or nxt in blocked:
                continue
            seen.add(nxt)
            queue.append(nxt)
    return False


def location_xy(warehouse: WarehouseMap, location_id: str) -> tuple[int, int]:
    """按工位 ID 取坐标；未知 ID 视为评测数据错误。"""

    for group in (warehouse.pickup_points, warehouse.dropoff_points, warehouse.charging_stations):
        for item in group:
            if item.id == location_id:
                return (item.x, item.y)
    raise KeyError(f"加难地图缺少位置: {location_id}")


def case_waypoints(warehouse: WarehouseMap, *, amr_id: str, order_id: str, pickup: str | None, dropoff: str | None) -> list[tuple[int, int]]:
    """该例必须保持连通的路径锚点：起点、取货、交付。"""

    start = _AMR_STARTS.get(amr_id, (1, 2))
    default_pickup, default_dropoff = _ORDER_LOCATIONS.get(order_id, ("P1", "S3"))
    pickup_id = pickup or default_pickup
    dropoff_id = dropoff or default_dropoff
    return [start, location_xy(warehouse, pickup_id), location_xy(warehouse, dropoff_id)]


def path_still_open(waypoints: Sequence[tuple[int, int]], blocked: set[tuple[int, int]]) -> bool:
    """锚点两两顺序可达才允许加入该障碍。"""

    for left, right in zip(waypoints, waypoints[1:]):
        if not reachable(left, right, blocked):
            return False
    return True


def extra_obstacles_for_seed(
    warehouse: WarehouseMap,
    *,
    seed: int,
    amr_id: str,
    order_id: str,
    pickup: str | None = None,
    dropoff: str | None = None,
    count: int = EXTRA_OBSTACLES_PER_CASE,
) -> list[GridPosition]:
    """按 seed 在通道上放置额外障碍，失败则跳过该格，绝不封闭主链。"""

    base = {(item.x, item.y) for item in [*warehouse.obstacles, *warehouse.temporary_blocked_cells]}
    reserved = protected_cells(warehouse) | base
    waypoints = case_waypoints(warehouse, amr_id=amr_id, order_id=order_id, pickup=pickup, dropoff=dropoff)
    candidates = [
        (x, y)
        for x in range(MAP_WIDTH)
        for y in AISLE_YS
        if (x, y) not in reserved
    ]
    rng = random.Random(seed)
    rng.shuffle(candidates)
    chosen: list[tuple[int, int]] = []
    blocked = set(base)
    for cell in candidates:
        if len(chosen) >= count:
            break
        trial = blocked | {cell}
        if path_still_open(waypoints, trial):
            blocked = trial
            chosen.append(cell)
    return [GridPosition(x=x, y=y) for x, y in sorted(chosen)]


def demo_corridors_open(warehouse: WarehouseMap, blocked: set[tuple[int, int]]) -> bool:
    """演示任意自然语言选点时，全部 AMR 起点、P、S、C 走廊必须仍四邻域可达。"""

    pickups = [(item.x, item.y) for item in warehouse.pickup_points]
    dropoffs = [(item.x, item.y) for item in warehouse.dropoff_points]
    charges = [(item.x, item.y) for item in warehouse.charging_stations]
    starts = list(_AMR_STARTS.values())
    for start in starts:
        for pickup in pickups:
            if not reachable(start, pickup, blocked):
                return False
        for charge in charges:
            if not reachable(start, charge, blocked):
                return False
    for pickup in pickups:
        for dropoff in dropoffs:
            if not reachable(pickup, dropoff, blocked):
                return False
    return True


def extra_obstacles_for_demo(
    warehouse: WarehouseMap,
    *,
    seed: int = DEMO_EXTRA_SEED,
    count: int = EXTRA_OBSTACLES_PER_CASE,
) -> list[GridPosition]:
    """演示页固定 2 个通道障碍；所有 P→S 与 AMR 起点→P/C 仍连通。

    评测每例只保护一条订单走廊。演示允许任意自然语言选点，所以额外障碍
    必须对全部工位走廊保持四邻域可达，否则前端随机下单会无解。
    """

    base = {(item.x, item.y) for item in [*warehouse.obstacles, *warehouse.temporary_blocked_cells]}
    reserved = protected_cells(warehouse) | base
    candidates = [
        (x, y)
        for x in range(MAP_WIDTH)
        for y in AISLE_YS
        if (x, y) not in reserved
    ]
    rng = random.Random(seed)
    rng.shuffle(candidates)
    chosen: list[tuple[int, int]] = []
    blocked = set(base)
    for cell in candidates:
        if len(chosen) >= count:
            break
        trial = blocked | {cell}
        if demo_corridors_open(warehouse, trial):
            blocked = trial
            chosen.append(cell)
    if len(chosen) < count:
        raise RuntimeError(
            f"演示加难障碍无法在保持全工位连通的前提下放置 {count} 格（仅得到 {len(chosen)}）"
        )
    return [GridPosition(x=x, y=y) for x, y in sorted(chosen)]


def overlay_demo_extras(
    warehouse: WarehouseMap,
    extras: Sequence[GridPosition] | None = None,
) -> WarehouseMap:
    """把演示额外障碍并入 temporary_blocked_cells，供 GET /demo/warehouse 与规划同图。"""

    extras = list(extras) if extras is not None else extra_obstacles_for_demo(warehouse)
    occupied = {(item.x, item.y) for item in [*warehouse.obstacles, *warehouse.temporary_blocked_cells]}
    added = [GridPosition(x=item.x, y=item.y) for item in extras if (item.x, item.y) not in occupied]
    if not added:
        return warehouse
    return warehouse.model_copy(
        update={"temporary_blocked_cells": [*warehouse.temporary_blocked_cells, *added]}
    )


class HardMapSnapshotProvider:
    """在线评测快照：加难地图 + 本例额外障碍 + 可选动态订单/电量。

    ``environment_ref`` 仍必须以 ``warehouse_v1@`` 开头，满足 PEVR 入口约束；
    实际栅格来自评测硬地图，不会去读生产 ``warehouse_v1.json``。

    非空 ``orders`` 时必须暴露 ``injected_orders``：PEVR ``_understand_node``
    用 ``getattr(provider, "injected_orders", None)`` 决定是否 canonicalize。
    这是与 ``DynamicOrderSnapshotProvider`` 对齐的 duck-type 契约。
    """

    def __init__(
        self,
        *,
        extra_obstacles: Sequence[GridPosition] = (),
        orders: Sequence[TransportOrder] | None = None,
        amr_batteries: dict[str, float] | None = None,
        charging: ChargingGoal | None = None,
        fault_code: str | None = None,
        completed_order_ids: Sequence[str] = (),
        map_path: Path = HARD_MAP_PATH,
    ) -> None:
        if not map_path.is_file():
            raise FileNotFoundError(f"加难评测地图不存在: {map_path}")
        self._warehouse = WarehouseMap.model_validate(json.loads(map_path.read_text(encoding="utf-8")))
        amr_payload = json.loads((SEED_DATA_ROOT / "amrs_v1.json").read_text(encoding="utf-8"))
        order_payload = json.loads((SEED_DATA_ROOT / "orders_seed_v1.json").read_text(encoding="utf-8"))
        self._amrs = [item for item in amr_payload["amrs"]]
        self._seed_orders = [item for item in order_payload["orders"]]
        self._extra = [item.model_copy(deep=True) for item in extra_obstacles]
        # 空列表表示“本例没有运输订单”（充电场景）；与 None（回退种子订单）必须区分。
        self._orders = None if orders is None else [item.model_copy(deep=True) for item in orders]
        self._charging = charging.model_copy(deep=True) if charging is not None else None
        if self._charging is not None:
            self.injected_charging = self._charging.model_copy(deep=True)
            # 充电合同禁止占位 TransportOrder；understand 走 injected_charging。
            self._orders = []
        # PEVR understand 只在 getattr(..., "injected_orders", None) 为真时才把合同
        # 订单/环境约束覆盖为快照真值并清零 missing_information。充电 NL 不填运输
        # 必填项，模型会留下 missing_information；没有本属性就会被 fail-closed，
        # 从未进入路径规划。必须与 DynamicOrderSnapshotProvider 暴露同名属性。
        # - orders is None：回退种子，不设 injected_orders（与默认种子 Provider 一致）。
        # - orders=[]：属性为 []（falsy），避免 canonicalize 在空快照上抛 dynamic_order_missing。
        # - orders 非空：暴露深拷贝，getattr 为真，understand 才能走 canonicalize。
        if self._orders:
            self.injected_orders = [item.model_copy(deep=True) for item in self._orders]
        elif self._orders is not None:
            self.injected_orders = []
        self._amr_batteries = dict(amr_batteries or {})
        self._fault_code = fault_code
        self._completed_order_ids = sorted({str(item) for item in completed_order_ids if str(item)})
        self._locations = self._location_map(self._warehouse)

    @staticmethod
    def _location_map(warehouse: WarehouseMap) -> dict[str, GridPosition]:
        """工位 ID → 坐标；重复 ID 在 WarehouseMap 层已经拒绝。"""

        locations: dict[str, GridPosition] = {}
        for group in (warehouse.pickup_points, warehouse.dropoff_points, warehouse.charging_stations):
            for item in group:
                locations[item.id] = item.position
        return locations

    def get_snapshot(self, environment_ref: str) -> EnvironmentSnapshot:
        """返回深拷贝；未知前缀在进入 C++ 前失败。"""

        if not environment_ref.startswith("warehouse_v1@"):
            raise SnapshotNotFoundError(f"未知环境快照: {environment_ref}")
        blocked = [
            *self._warehouse.obstacles,
            *self._warehouse.temporary_blocked_cells,
            *self._extra,
        ]
        coordinates = [(item.x, item.y) for item in blocked]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("加难地图与额外障碍出现重复坐标")
        amrs = []
        for item in self._amrs:
            amr = item.model_copy(deep=True) if hasattr(item, "model_copy") else dict(item)
            amr_id = str(amr.amr_id if hasattr(amr, "amr_id") else amr["amr_id"])
            updates: dict[str, object] = {}
            if amr_id in self._amr_batteries:
                updates["battery"] = self._amr_batteries[amr_id]
            if self._charging is not None and amr_id == self._charging.amr_id:
                station = self._locations[self._charging.charge_station]
                updates["position"] = station.model_copy(deep=True)
                updates["task_status"] = "IDLE"
            if updates:
                if hasattr(amr, "model_copy"):
                    amr = amr.model_copy(update=updates)
                else:
                    position = updates.get("position")
                    amr = {
                        **amr,
                        **{
                            key: (
                                position.model_dump(mode="json")
                                if key == "position" and hasattr(position, "model_dump")
                                else value
                            )
                            for key, value in updates.items()
                        },
                    }
            amrs.append(amr)
        orders = [] if self._charging is not None else (self._orders if self._orders is not None else self._seed_orders)
        return EnvironmentSnapshot(
            environment_ref=environment_ref,
            state_version=environment_ref.split("@", 1)[1] if "@" in environment_ref else "eval-hard",
            map_width=self._warehouse.width,
            map_height=self._warehouse.height,
            blocked_cells=sorted(blocked, key=lambda item: (item.x, item.y)),
            blocked_edges=[{"from": item.from_, "to": item.to} for item in self._warehouse.blocked_edges],
            one_way_edges=[{"from": item.from_, "to": item.to} for item in self._warehouse.one_way_edges],
            narrow_aisles=list(self._warehouse.narrow_aisles),
            amrs=list(amrs),
            orders=list(orders),
            location_positions=dict(self._locations),
            completed_order_ids=list(self._completed_order_ids),
            start_time=0,
            max_time=120,
            workstation_capacities={
                item.id: 1
                for item in [*self._warehouse.pickup_points, *self._warehouse.dropoff_points]
            },
            fault_code=self._fault_code,
        )


def snapshot_provider_for_case(
    *,
    amr_id: str,
    order_id: str,
    seed: int,
    pickup: str | None = None,
    dropoff: str | None = None,
    orders: Sequence[TransportOrder] | None = None,
    amr_batteries: dict[str, float] | None = None,
    charging: ChargingGoal | None = None,
    fault_code: str | None = None,
    completed_order_ids: Sequence[str] = (),
    extra_count: int = EXTRA_OBSTACLES_PER_CASE,
) -> HardMapSnapshotProvider:
    """为单例构造带额外障碍的快照 Provider。"""

    warehouse = WarehouseMap.model_validate(json.loads(HARD_MAP_PATH.read_text(encoding="utf-8")))
    extras = extra_obstacles_for_seed(
        warehouse,
        seed=seed,
        amr_id=amr_id,
        order_id=order_id,
        pickup=pickup,
        dropoff=dropoff,
        count=extra_count,
    )
    return HardMapSnapshotProvider(
        extra_obstacles=extras,
        orders=orders,
        amr_batteries=amr_batteries,
        charging=charging,
        fault_code=fault_code,
        completed_order_ids=completed_order_ids,
    )


__all__ = [
    "DEMO_EXTRA_SEED",
    "EXTRA_OBSTACLES_PER_CASE",
    "HARD_ENVIRONMENT_REF",
    "HARD_MAP_PATH",
    "HardMapSnapshotProvider",
    "build_hard_warehouse_map",
    "demo_corridors_open",
    "export_hard_map",
    "extra_obstacles_for_demo",
    "extra_obstacles_for_seed",
    "overlay_demo_extras",
    "path_still_open",
    "snapshot_provider_for_case",
]


if __name__ == "__main__":
    export_hard_map()
    print(HARD_MAP_PATH)
