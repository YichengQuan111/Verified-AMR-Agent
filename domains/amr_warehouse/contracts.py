"""AMR 仓储领域的稳定数据契约。

本模块只描述数据形状和确定性校验，不读取数据库，也不执行任务分配、路径规划或仿真。
这样 P0-05 之后的各层可以共享同一套位置、AMR 状态和运输订单定义。
"""

from __future__ import annotations

from enum import Enum

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

    x: int = Field(ge=0, lt=30, description="横坐标，允许 0～29")
    y: int = Field(ge=0, lt=20, description="纵坐标，允许 0～19")


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

    amr_id: str = Field(min_length=1, description="AMR 的稳定唯一标识")
    position: GridPosition
    heading: Heading
    battery: float = Field(ge=0, le=100, description="剩余电量百分比")
    load: float = Field(ge=0, description="当前载荷，单位为千克")
    task_status: AMRTaskStatus
    health_status: HealthStatus
    connection_status: ConnectionStatus


class TransportOrder(WarehouseContract):
    """仓库内一次 pickup-transport-dropoff 运输订单。"""

    order_id: str = Field(min_length=1, description="订单唯一标识")
    material_id: str = Field(min_length=1, description="待运输物料标识")
    pickup: str = Field(min_length=1, description="取货点标识")
    dropoff: str = Field(min_length=1, description="交付点标识")
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
    "TransportOrder",
]
