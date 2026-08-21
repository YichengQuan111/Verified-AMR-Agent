"""AMR 仓储领域的稳定数据契约。

本模块只描述数据形状和确定性校验，不读取数据库，也不执行任务分配、路径规划或仿真。
这样 P0-05 之后的各层可以共享同一套位置、AMR 状态和运输订单定义。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WarehouseContract(BaseModel):
    """仓储契约基类：拒绝未知字段，并在对象被修改时继续校验。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class GridPosition(WarehouseContract):
    """30 × 20 仓库栅格中的一个合法位置。

    坐标从 0 开始，所以宽度为 30 时 x 最大只能是 29，高度为 20 时 y 最大只能是 19。
    """

    # strict=True 很重要：Python 中 bool 是 int 的子类，Pydantic 默认还会把
    # 1.0 收缩成 1；两者跨 JSON/C++ 边界都会掩盖上游字段漂移。
    x: int = Field(strict=True, ge=0, lt=30, description="横坐标，允许 0～29")
    y: int = Field(strict=True, ge=0, lt=20, description="纵坐标，允许 0～19")


class WarehouseLocation(WarehouseContract):
    """固定地图中一个带稳定 ID 的工位或充电点。"""

    id: str = Field(min_length=1, max_length=128)
    x: int = Field(strict=True, ge=0, lt=30)
    y: int = Field(strict=True, ge=0, lt=20)

    @property
    def position(self) -> GridPosition:
        """按公共坐标契约返回位置，避免调用方重复拼装字段。"""

        return GridPosition(x=self.x, y=self.y)


class WarehouseEdge(WarehouseContract):
    """地图中的一条相邻有向边。"""

    from_: GridPosition = Field(alias="from")
    to: GridPosition

    @model_validator(mode="after")
    def validate_adjacent(self) -> "WarehouseEdge":
        """C++ 规划器只接受四邻接边，Python 种子也必须使用相同语义。"""

        distance = abs(self.from_.x - self.to.x) + abs(self.from_.y - self.to.y)
        if distance != 1:
            raise ValueError("地图边必须连接相邻栅格")
        return self


class NarrowAisle(WarehouseContract):
    """固定地图中的窄通道标签及其连续栅格。"""

    aisle_id: str = Field(min_length=1, max_length=128)
    cells: list[GridPosition] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_cells(self) -> "NarrowAisle":
        """窄通道不能重复或跳跃，便于故障实体稳定映射到 cell/channel。"""

        coordinates = [(cell.x, cell.y) for cell in self.cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("窄通道 cells 不能重复")
        for left, right in zip(self.cells, self.cells[1:]):
            if abs(left.x - right.x) + abs(left.y - right.y) != 1:
                raise ValueError("窄通道 cells 必须连续")
        return self


class WarehouseMap(WarehouseContract):
    """P0 固定 30×20 地图的唯一 Python/JSON 公共契约。"""

    map_id: str = Field(min_length=1, max_length=128)
    version: int = Field(strict=True, ge=1)
    width: Literal[30]
    height: Literal[20]
    resolution_m: float = Field(gt=0)
    pickup_points: list[WarehouseLocation]
    dropoff_points: list[WarehouseLocation]
    charging_stations: list[WarehouseLocation]
    obstacles: list[GridPosition]
    narrow_aisles: list[NarrowAisle]
    blocked_edges: list[WarehouseEdge]
    one_way_edges: list[WarehouseEdge]
    temporary_blocked_cells: list[GridPosition]

    @model_validator(mode="after")
    def validate_map_sets(self) -> "WarehouseMap":
        """拒绝重复地点、重复障碍及互相冲突的固定地图声明。"""

        locations = [*self.pickup_points, *self.dropoff_points, *self.charging_stations]
        location_ids = [item.id for item in locations]
        if len(location_ids) != len(set(location_ids)):
            raise ValueError("地图位置 ID 不能重复")
        blocked = [(item.x, item.y) for item in [*self.obstacles, *self.temporary_blocked_cells]]
        if len(blocked) != len(set(blocked)):
            raise ValueError("obstacles/temporary_blocked_cells 不能重复")
        edge_values = [
            ((item.from_.x, item.from_.y), (item.to.x, item.to.y))
            for item in [*self.blocked_edges, *self.one_way_edges]
        ]
        if len(edge_values) != len(set(edge_values)):
            raise ValueError("blocked_edges/one_way_edges 不能重复")
        return self


class Heading(int, Enum):
    """AMR 的四个栅格朝向，单位为度。"""

    NORTH = 0
    EAST = 90
    SOUTH = 180
    WEST = 270


class AMRTaskStatus(str, Enum):
    """P0 场景中 AMR 可观测到的任务阶段。"""

    IDLE = "IDLE"
    TO_PICKUP = "TO_PICKUP"
    LOADING = "LOADING"
    TO_DROPOFF = "TO_DROPOFF"
    UNLOADING = "UNLOADING"
    TO_CHARGE = "TO_CHARGE"
    CHARGING = "CHARGING"
    OFFLINE = "OFFLINE"


class HealthStatus(str, Enum):
    """AMR 自检健康状态。"""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAULT = "FAULT"


class ConnectionStatus(str, Enum):
    """AMR 与控制系统的连接状态。"""

    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class AMRState(WarehouseContract):
    """一台 AMR 在某个确定时刻的完整状态快照。"""

    amr_id: str = Field(min_length=1, max_length=128, description="AMR 的稳定唯一标识")
    position: GridPosition
    heading: Heading
    battery: float = Field(ge=0, le=100, description="剩余电量百分比")
    load: float = Field(ge=0, description="当前载荷，单位为千克")
    task_status: AMRTaskStatus
    health_status: HealthStatus
    connection_status: ConnectionStatus


class TransportOrder(WarehouseContract):
    """仓库内一次 pickup-transport-dropoff 运输订单。"""

    order_id: str = Field(min_length=1, max_length=128, description="订单唯一标识")
    material_id: str = Field(min_length=1, max_length=128, description="待运输物料标识")
    pickup: str = Field(min_length=1, max_length=128, description="取货点标识")
    dropoff: str = Field(min_length=1, max_length=128, description="交付点标识")
    priority: int = Field(ge=1, le=5, description="优先级；5 最高，1 最低")
    release_time: int = Field(ge=0, description="订单可开始时间，单位为仿真秒")
    deadline: int = Field(gt=0, description="订单截止时间，单位为仿真秒")
    dependencies: list[str] = Field(description="必须先完成的订单 ID")

    @model_validator(mode="after")
    def validate_order_semantics(self) -> "TransportOrder":
        """拒绝无意义路线、非法时间窗和不可能的自依赖。"""

        if self.pickup == self.dropoff:
            raise ValueError("pickup 与 dropoff 不能相同")
        if self.deadline <= self.release_time:
            raise ValueError("deadline 必须晚于 release_time")
        if self.order_id in self.dependencies:
            raise ValueError("运输订单不能依赖自身")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies 不能包含重复订单 ID")
        return self


__all__ = [
    "AMRState",
    "AMRTaskStatus",
    "ConnectionStatus",
    "GridPosition",
    "Heading",
    "HealthStatus",
    "NarrowAisle",
    "TransportOrder",
    "WarehouseEdge",
    "WarehouseLocation",
    "WarehouseMap",
]
