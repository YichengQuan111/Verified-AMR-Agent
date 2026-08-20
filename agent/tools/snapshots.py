"""固定仓库环境快照与工具层的状态存取协议。

P0-12 工具参数只携带 ``environment_ref``，绝不携带文件路径。默认 Provider 从
仓库提交的 warehouse/AMR/order 种子读取一次可信快照；测试或未来 P0-06 接入可
注入同一协议的数据库实现。这样 Python 不会因为用户提供了一个路径而执行任意
文件读取，同时分配、路径和验证三个工具使用同一份地图/状态事实。
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from domains.amr_warehouse import AMRState, GridPosition, TransportOrder


class SnapshotContract(BaseModel):
    """工具内部快照基类；外部数据进入后仍拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class EnvironmentSnapshot(SnapshotContract):
    """分配、路线和验证共同消费的不可变环境视图。"""

    environment_ref: str = Field(min_length=1)
    state_version: str = Field(min_length=1)
    map_width: int = Field(ge=1, le=30)
    map_height: int = Field(ge=1, le=20)
    blocked_cells: list[GridPosition]
    blocked_edges: list[dict[str, GridPosition]]
    one_way_edges: list[dict[str, GridPosition]]
    amrs: list[AMRState]
    orders: list[TransportOrder]
    location_positions: dict[str, GridPosition]
    completed_order_ids: list[str]
    start_time: int = Field(ge=0)
    max_time: int = Field(ge=1)
    workstation_capacities: dict[str, int]


class SnapshotProviderProtocol(Protocol):
    """工具 handler 所需的最小环境快照接口。"""

    def get_snapshot(self, environment_ref: str) -> EnvironmentSnapshot: ...


class SnapshotNotFoundError(LookupError):
    """环境引用不在受控快照集合中。"""


class DefaultWarehouseSnapshotProvider:
    """读取仓库提交的固定 seed，不根据用户输入拼接路径。"""

    def __init__(self, *, data_root: str | Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        # data_root 仅供测试注入；生产默认值固定到仓库目录，调用方不能通过
        # 工具参数改变它。文件内容进入后立即按领域契约校验。
        self._data_root = Path(data_root) if data_root is not None else (
            project_root / "domains" / "amr_warehouse" / "data"
        )
        self._lock = RLock()
        self._snapshot: EnvironmentSnapshot | None = None

    def get_snapshot(self, environment_ref: str) -> EnvironmentSnapshot:
        """返回深拷贝快照；未知引用在任何 C++/仿真调用前失败。"""

        if not environment_ref or not environment_ref.startswith("warehouse_v1"):
            raise SnapshotNotFoundError(f"未知环境快照: {environment_ref}")
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._load_snapshot(environment_ref)
            elif self._snapshot.environment_ref != environment_ref:
                # 同一进程只允许一个固定 seed 视图，避免把不同状态版本静默混用。
                raise SnapshotNotFoundError(
                    f"环境快照版本与当前固定 seed 不一致: {environment_ref}"
                )
            return self._snapshot.model_copy(deep=True)

    def _load_snapshot(self, environment_ref: str) -> EnvironmentSnapshot:
        """从三个固定 JSON 文件组装统一快照。"""

        warehouse = self._read_json("warehouse_v1.json")
        amr_payload = self._read_json("amrs_v1.json")
        order_payload = self._read_json("orders_seed_v1.json")
        locations: dict[str, GridPosition] = {}
        for group in ("pickup_points", "dropoff_points", "charging_stations"):
            for item in warehouse.get(group, []):
                location_id = str(item["id"])
                if location_id in locations:
                    raise ValueError(f"固定地图包含重复位置: {location_id}")
                locations[location_id] = GridPosition(x=item["x"], y=item["y"])

        state_version = environment_ref.split("@", 1)[1] if "@" in environment_ref else "seed-v1"
        return EnvironmentSnapshot(
            environment_ref=environment_ref,
            state_version=state_version,
            map_width=warehouse["width"],
            map_height=warehouse["height"],
            # 静态 obstacles 与运行快照临时封路对路径器/Validator 都是不可进入
            # 栅格；此前只读取后者会在地图增加障碍时形成跨层安全缺口。
            blocked_cells=self._blocked_cells(warehouse),
            blocked_edges=[
                {"from": GridPosition(x=item["from"]["x"], y=item["from"]["y"]),
                 "to": GridPosition(x=item["to"]["x"], y=item["to"]["y"])}
                for item in warehouse.get("blocked_edges", [])
            ],
            one_way_edges=[
                {"from": GridPosition(x=item["from"]["x"], y=item["from"]["y"]),
                 "to": GridPosition(x=item["to"]["x"], y=item["to"]["y"])}
                for item in warehouse.get("one_way_edges", [])
            ],
            amrs=[item for item in amr_payload["amrs"]],
            orders=[item for item in order_payload["orders"]],
            location_positions=locations,
            completed_order_ids=[],
            start_time=0,
            max_time=120,
            workstation_capacities={},
        )

    @staticmethod
    def _blocked_cells(warehouse: Mapping[str, Any]) -> list[GridPosition]:
        """合并静态障碍和临时封路，并拒绝重复坐标。"""

        values = [
            GridPosition(x=item["x"], y=item["y"])
            for key in ("obstacles", "temporary_blocked_cells")
            for item in warehouse.get(key, [])
        ]
        coordinates = [(item.x, item.y) for item in values]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("固定地图的 obstacles/temporary_blocked_cells 包含重复坐标")
        return sorted(values, key=lambda item: (item.x, item.y))

    def _read_json(self, filename: str) -> dict[str, Any]:
        """读取内部固定文件；filename 不来自用户输入。"""

        path = self._data_root / filename
        if not path.is_file():
            raise FileNotFoundError(f"固定环境快照文件不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"固定环境快照必须是对象: {filename}")
        return payload


class ExecutionStateStoreProtocol(Protocol):
    """查询工具和仿真工具共享的最小状态仓储接口。"""

    def put(self, run_id: str, snapshot: BaseModel | Mapping[str, JsonValue]) -> None: ...

    def get(self, run_id: str) -> dict[str, JsonValue] | None: ...


class InMemoryExecutionStateStore:
    """进程内状态存储，作为 P0-06 PostgreSQL Checkpoint 前的确定性适配器。

    存储层只保存 JSON 深拷贝，不返回可被调用方原地修改的内部对象；未来替换为
    PostgreSQL 时保持 put/get 契约，工具注册表和审计字段无需改变。
    """

    def __init__(self) -> None:
        self._values: dict[str, dict[str, JsonValue]] = {}
        self._lock = RLock()

    def put(self, run_id: str, snapshot: BaseModel | Mapping[str, JsonValue]) -> None:
        """以 run_id 覆盖同一确定性快照；重复写不会产生第二个 effect。"""

        if isinstance(snapshot, BaseModel):
            value = snapshot.model_dump(mode="json")
        else:
            value = dict(snapshot)
        with self._lock:
            self._values[run_id] = deepcopy(value)

    def get(self, run_id: str) -> dict[str, JsonValue] | None:
        """返回快照副本；未知 run_id 用 None 表示，不猜测外部状态。"""

        with self._lock:
            value = self._values.get(run_id)
            return None if value is None else deepcopy(value)


__all__ = [
    "DefaultWarehouseSnapshotProvider",
    "EnvironmentSnapshot",
    "ExecutionStateStoreProtocol",
    "InMemoryExecutionStateStore",
    "SnapshotNotFoundError",
    "SnapshotProviderProtocol",
]
