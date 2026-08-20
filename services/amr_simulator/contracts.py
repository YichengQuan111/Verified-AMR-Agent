"""P0-11 Python 仿真器的输入、状态、事件和故障注入契约。

本模块只描述数据形状，不执行路径、不调用 C++ Validator，也不向
``agent.tools`` 注册工具。计划 envelope 刻意复用 P0-10 的字段和 P0-09 的
``RouteStep`` 语义；业务合法性仍由固定 Validator 在执行前独立裁决。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.runtime import Observation
from domains.amr_warehouse import AMRState, GridPosition, Heading, TransportOrder


class SimulatorContract(BaseModel):
    """仿真契约基类：拒绝旁路字段，避免改变跨语言安全边界。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        populate_by_name=True,
    )


class SimulationEdge(SimulatorContract):
    """地图中一条带方向的相邻边，字段名与 P0-09/P0-10 JSON 保持一致。"""

    from_position: GridPosition = Field(alias="from")
    to_position: GridPosition = Field(alias="to")


class RouteStep(SimulatorContract):
    """P0-09 的逐时刻路径状态；仿真器只消费，不重新规划。"""

    position: GridPosition
    heading: Heading
    time: int
    action: Literal["start", "move", "turn_left", "turn_right", "wait"]
    g_cost: float


class FleetPlanRoute(SimulatorContract):
    """P0-10 计划中的一条完整 pickup → dropoff 路线。"""

    amr_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    payload_kg: float
    pickup_time: int
    dropoff_time: int
    path: list[RouteStep]
    # 下列字段是 P0-09 审计快照；它们保留在 envelope 中，但不能替代 Validator。
    status: Literal["planned", "infeasible"] = "planned"
    reason_code: str | None = None
    reason: str | None = None
    priority: int | None = None
    total_cost: float | None = None
    expanded_states: int | None = None


class ValidatorConfig(SimulatorContract):
    """与 P0-10 同名的安全配置，避免仿真使用第二套电量/载荷规则。"""

    maximum_load_kg: float
    energy_per_cell_percent: float
    battery_safety_reserve_percent: float
    new_task_battery_threshold_percent: float
    critical_battery_threshold_percent: float
    minimum_safety_distance_cells: int
    default_workstation_capacity: int


class SimulationPlan(SimulatorContract):
    """P0-10 Validator 请求 envelope，也是 P0-11 唯一可执行计划输入。

    这里仅做 JSON 形状校验。地图、订单依赖、时间戳、载荷、电量和车队冲突等
    业务规则必须继续交给 P0-10；否则 Python 侧的“看起来合法”会形成安全旁路。
    """

    schema_version: Literal["1.0"] = "1.0"
    environment_ref: str = Field(min_length=1)
    map_width: int = Field(ge=1, le=100)
    map_height: int = Field(ge=1, le=100)
    blocked_cells: list[GridPosition]
    blocked_edges: list[SimulationEdge]
    one_way_edges: list[SimulationEdge]
    amrs: list[AMRState]
    orders: list[TransportOrder]
    location_positions: dict[str, GridPosition]
    completed_order_ids: list[str]
    routes: list[FleetPlanRoute]
    start_time: int
    max_time: int
    config: ValidatorConfig
    workstation_capacities: dict[str, int]
    ruleset_version: Literal["p0-10.v1"] = "p0-10.v1"


class ChargingStationSpec(SimulatorContract):
    """仿真侧充电站快照。

    P0-10 的运输计划 envelope 不包含充电站资源，因此充电站作为仿真环境
    配置单独传入；其坐标仍必须落在同一地图内且不能进入 blocked_cells。
    """

    position: GridPosition
    capacity: int = Field(gt=0)


class SimulatorConfig(SimulatorContract):
    """固定时间步和充电行为配置。

    ``tick_seconds`` 固定为 1，是 P0-09 路径时间戳的单位；装卸不增加隐藏
    时间步，只在到达 tick 产生 LOADING/UNLOADING 状态和工位事件。这样不会
    擅自改变已通过 P0-10 的 pickup_time/dropoff_time。
    """

    tick_seconds: Literal[1] = 1
    charge_threshold_percent: float = Field(ge=0.0, le=100.0, default=30.0)
    charge_target_percent: float = Field(ge=0.0, le=100.0, default=100.0)
    charge_rate_percent_per_tick: float = Field(gt=0.0, default=10.0)
    auto_charge: bool = True
    charging_stations: dict[str, ChargingStationSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_charge_range(self) -> "SimulatorConfig":
        """目标电量不能低于进入充电的阈值，否则会造成永不结束的充电状态。"""

        if self.charge_target_percent < self.charge_threshold_percent:
            raise ValueError("charge_target_percent 不能低于 charge_threshold_percent")
        return self


class FaultType(str, Enum):
    """仅供 Eval/测试使用的故障类型；不会进入正常 ToolName 白名单。"""

    OFFLINE = "offline"
    BATTERY_DRAIN = "battery_drain"
    STUCK = "stuck"


class FaultInjection(SimulatorContract):
    """在确定性 tick 注入一次故障。

    故障按 ``(at_time, amr_id, fault_type)`` 稳定排序。故障默认采取安全停机，
    不尝试“自动恢复并跳过路径”，因为跳过 P0-09 时间戳会让执行轨迹失去可
    审计性；后续 Eval 可在此契约上增加显式恢复事件。
    """

    at_time: int = Field(ge=0)
    amr_id: str = Field(min_length=1)
    fault_type: FaultType
    magnitude: float | None = None
    duration_ticks: int | None = Field(default=None, ge=1)
    reason: str = Field(default="eval fault injection", min_length=1)

    @model_validator(mode="after")
    def validate_fault_arguments(self) -> "FaultInjection":
        """限制故障参数，避免通过任意 JSON 值改变仿真控制流。"""

        if self.fault_type is FaultType.BATTERY_DRAIN:
            if self.magnitude is None or self.magnitude <= 0:
                raise ValueError("battery_drain 必须提供正的 magnitude")
        elif self.magnitude is not None:
            raise ValueError("只有 battery_drain 允许设置 magnitude")
        if self.fault_type is not FaultType.STUCK and self.duration_ticks is not None:
            raise ValueError("只有 stuck 允许设置 duration_ticks")
        return self


class SimulationOrderStatus(str, Enum):
    """订单在仿真中的最小生命周期。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SimulationOrderState(SimulatorContract):
    """订单的可审计运行态，不修改 P0-04 的 TransportOrder 定义。"""

    order_id: str = Field(min_length=1)
    status: SimulationOrderStatus
    assigned_amr_id: str | None
    payload_kg: float | None
    pickup_time: int | None
    dropoff_time: int | None
    blocked_reason: str | None


class WorkstationState(SimulatorContract):
    """工位的离散事件状态；容量由 P0-10 计划配置决定。"""

    workstation_id: str = Field(min_length=1)
    position: GridPosition
    capacity: int
    occupied_amr_ids: list[str]
    last_event_time: int | None
    last_event_type: Literal["pickup", "dropoff"] | None
    service_count: int = Field(ge=0)


class ChargingStationStatus(str, Enum):
    """充电站在当前 tick 的资源状态。"""

    AVAILABLE = "available"
    OCCUPIED = "occupied"


class ChargingStationState(SimulatorContract):
    """充电站占用和累计供电的结构化快照。"""

    station_id: str = Field(min_length=1)
    position: GridPosition
    capacity: int
    status: ChargingStationStatus
    charging_amr_ids: list[str]
    total_energy_delivered_percent: float = Field(ge=0.0)


class SimulationEvent(SimulatorContract):
    """单个确定性仿真事件。

    ``time`` 是离散仿真秒，不使用墙上时钟；``payload`` 只保存 JSON 值，便于
    后续 P0-06 events、Trace 和 Eval 直接序列化而不执行任意对象。
    """

    event_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    time: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    severity: Literal["info", "warning", "error"]
    amr_id: str | None = None
    order_id: str | None = None
    workstation_id: str | None = None
    charging_station_id: str | None = None
    payload: dict[str, JsonValue]


class SimulationStatus(str, Enum):
    """仿真终态：完成、故障阻塞或运行到时间上限。"""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class SimulationResult(SimulatorContract):
    """仿真最终快照、Observation 和事件日志。"""

    simulation_id: str = Field(min_length=1)
    seed: int
    status: SimulationStatus
    start_time: int
    end_time: int
    validation_result: dict[str, JsonValue]
    amrs: list[AMRState]
    orders: list[SimulationOrderState]
    workstations: list[WorkstationState]
    charging_stations: list[ChargingStationState]
    observations: list[Observation]
    events: list[SimulationEvent]


__all__ = [
    "ChargingStationSpec",
    "ChargingStationState",
    "ChargingStationStatus",
    "FaultInjection",
    "FaultType",
    "FleetPlanRoute",
    "RouteStep",
    "SimulationEdge",
    "SimulationEvent",
    "SimulationOrderState",
    "SimulationOrderStatus",
    "SimulationPlan",
    "SimulationResult",
    "SimulationStatus",
    "SimulatorConfig",
    "ValidatorConfig",
    "WorkstationState",
]
