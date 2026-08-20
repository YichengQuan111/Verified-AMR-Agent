"""P0-11 Python AMR 固定时间步离散事件仿真器。

仿真输入必须先通过 P0-10 C++ Validator；通过后，执行循环直接索引 P0-09
路径中的 ``time``，不重新计算路线、不跳过终点占用，也不接受 LLM/调用方的
“已验证”标记。每个 tick 产生一份确定性 Observation，并把状态变化、订单、
工位、充电和故障记录为结构化事件，供后续 P0-12/P0-13/Eval 复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from typing import Any, Iterable, Mapping, Protocol

from agent.runtime import (
    ConstraintViolation,
    Observation,
    ObservationSource,
    ObservationStatus,
)
from domains.amr_warehouse import (
    AMRState,
    AMRTaskStatus,
    ConnectionStatus,
    GridPosition,
    HealthStatus,
    Heading,
)

from .contracts import (
    ChargingStationSpec,
    ChargingStationState,
    ChargingStationStatus,
    FaultInjection,
    FaultType,
    FleetPlanRoute,
    RouteStep,
    SimulationEvent,
    SimulationOrderState,
    SimulationOrderStatus,
    SimulationPlan,
    SimulationResult,
    SimulationStatus,
    SimulatorConfig,
    WorkstationState,
)
from .validator import FleetPlanValidatorClient


class SimulationConfigurationError(ValueError):
    """仿真侧配置或故障注入请求不满足确定性安全边界。"""


class SimulationInvariantError(RuntimeError):
    """Validator 已通过但运行时仍发现无法解释的计划/状态不一致。"""


class ValidatorClientProtocol(Protocol):
    """允许测试注入受控 Validator fake；生产默认实现仍是固定 C++ CLI。"""

    def validate(self, plan: SimulationPlan | Mapping[str, Any]) -> dict[str, Any]:
        """返回 P0-10 的 valid JSON 结果，非法或失败由实现抛出异常。"""


@dataclass
class _Issue:
    """一个 tick 内待写入 Observation 的故障/运行时问题。"""

    code: str
    message: str
    amr_id: str | None
    order_id: str | None
    evidence: dict[str, Any]
    requires_replan: bool
    requires_human: bool
    severity: str


@dataclass
class _RuntimeAMR:
    """仿真循环内部的可变 AMR 状态，最终仍转换回 P0-04 ``AMRState``。"""

    state: AMRState
    route: FleetPlanRoute | None
    steps_by_time: dict[int, RouteStep]
    initial_battery: float
    move_count: int = 0
    faulted: bool = False
    fault_code: str | None = None
    fault_reason: str | None = None
    charge_station_id: str | None = None
    charge_wait_reported: bool = False


@dataclass
class _RuntimeOrder:
    """订单运行态；P0-04 的静态订单对象始终保留在计划中不被修改。"""

    order_id: str
    status: SimulationOrderStatus
    assigned_amr_id: str | None
    payload_kg: float | None
    pickup_time: int | None = None
    dropoff_time: int | None = None
    blocked_reason: str | None = None


@dataclass
class _RuntimeWorkstation:
    """工位在单个离散 tick 的事件占用和累计服务计数。"""

    workstation_id: str
    position: GridPosition
    capacity: int
    occupied_amr_ids: list[str]
    last_event_time: int | None = None
    last_event_type: str | None = None
    service_count: int = 0


class AMRSimulator:
    """执行已通过 P0-10 的多 AMR 时间戳计划。

    一个实例可以重复调用 ``run``，每次调用都会重新建立内部快照；调用不
    共享上一次运行的电量、订单或事件。实例不是并发容器，未来若接入服务层
    应为每个 dispatch 请求创建独立实例或在外层加锁。
    """

    def __init__(
        self,
        *,
        config: SimulatorConfig | None = None,
        validator_client: ValidatorClientProtocol | None = None,
    ) -> None:
        self.config = config or SimulatorConfig()
        self.validator_client = validator_client or FleetPlanValidatorClient()

    def run(
        self,
        plan: SimulationPlan | Mapping[str, Any],
        *,
        simulation_id: str = "simulation",
        seed: int = 0,
        until_time: int | None = None,
        faults: Iterable[FaultInjection | Mapping[str, Any]] = (),
    ) -> SimulationResult:
        """验证并执行一份计划，返回可 JSON 序列化的最终结果。

        ``until_time`` 默认使用计划 ``max_time``；即使订单已完成也继续推进到
        指定 tick，以便观察 UNLOADING→IDLE、IDLE→CHARGING 等状态收敛。故障
        注入在同一 tick 的路径动作前生效，并采用安全停机而不是跳过路径恢复。
        """

        if not simulation_id:
            raise SimulationConfigurationError("simulation_id 不能为空")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise SimulationConfigurationError("seed 必须是整数")
        simulation_plan = SimulationPlan.model_validate(plan)
        validation_result = self.validator_client.validate(simulation_plan)
        end_time = simulation_plan.max_time if until_time is None else until_time
        if end_time < simulation_plan.start_time or end_time > simulation_plan.max_time:
            raise SimulationConfigurationError(
                "until_time 必须落在 plan.start_time..plan.max_time 内"
            )

        parsed_faults = self._prepare_faults(
            faults,
            simulation_plan,
            until_time=end_time,
        )
        self._validate_simulator_config(simulation_plan)
        # 当前 P0 没有概率型故障；保留独立 RNG 是为了让后续 Eval 在增加随机
        # 故障时沿用同一个 seed，而不把 Python 全局随机状态带入仿真。
        self._rng = random.Random(seed)
        del self._rng
        self._reset_runtime(simulation_plan, simulation_id)

        self._emit_event(
            simulation_plan.start_time,
            "simulation.started",
            "info",
            payload={
                "seed": seed,
                "validator_ruleset": validation_result.get("ruleset_version"),
                "until_time": end_time,
            },
        )

        for current_time in range(simulation_plan.start_time, end_time + 1):
            self._tick_event_ids = []
            self._tick_issues = []
            self._reset_workstation_tick_state()
            self._apply_faults_at_time(current_time, parsed_faults)
            self._apply_route_steps(current_time, simulation_plan)
            self._update_charging(current_time, simulation_plan)
            self._observations.append(self._make_observation(current_time))

        status = self._final_status()
        self._emit_event(
            end_time,
            "simulation.finished",
            "warning" if status is not SimulationStatus.COMPLETED else "info",
            payload={"status": status.value},
        )
        return SimulationResult(
            simulation_id=simulation_id,
            seed=seed,
            status=status,
            start_time=simulation_plan.start_time,
            end_time=end_time,
            validation_result=validation_result,
            amrs=[runtime.state for runtime in self._amrs.values()],
            orders=[self._order_state(item) for item in self._orders.values()],
            workstations=[self._workstation_state(item) for item in self._workstations.values()],
            charging_stations=self._charging_station_states(),
            observations=self._observations,
            events=self._events,
        )

    def _reset_runtime(self, plan: SimulationPlan, simulation_id: str) -> None:
        """从计划创建一次全新的内部运行态，所有遍历均按 ID 稳定排序。"""

        self._simulation_id = simulation_id
        route_by_amr = {route.amr_id: route for route in plan.routes}
        self._amrs: dict[str, _RuntimeAMR] = {}
        for amr in sorted(plan.amrs, key=lambda item: item.amr_id):
            route = route_by_amr.get(amr.amr_id)
            steps = {} if route is None else {step.time: step for step in route.path}
            self._amrs[amr.amr_id] = _RuntimeAMR(
                state=amr.model_copy(deep=True),
                route=route,
                steps_by_time=steps,
                initial_battery=amr.battery,
            )

        route_by_order = {route.order_id: route for route in plan.routes}
        completed = set(plan.completed_order_ids)
        self._orders: dict[str, _RuntimeOrder] = {}
        for order in sorted(plan.orders, key=lambda item: item.order_id):
            route = route_by_order.get(order.order_id)
            self._orders[order.order_id] = _RuntimeOrder(
                order_id=order.order_id,
                status=(
                    SimulationOrderStatus.COMPLETED
                    if order.order_id in completed
                    else SimulationOrderStatus.PENDING
                ),
                assigned_amr_id=None if route is None else route.amr_id,
                payload_kg=None if route is None else route.payload_kg,
            )

        self._workstations = {}
        for workstation_id, position in sorted(plan.location_positions.items()):
            capacity = plan.workstation_capacities.get(
                workstation_id,
                plan.config.default_workstation_capacity,
            )
            self._workstations[workstation_id] = _RuntimeWorkstation(
                workstation_id=workstation_id,
                position=position,
                capacity=capacity,
                occupied_amr_ids=[],
            )

        self._station_energy = {station_id: 0.0 for station_id in self.config.charging_stations}
        self._events: list[SimulationEvent] = []
        self._observations: list[Observation] = []
        self._event_sequence = 0
        self._tick_event_ids: list[str] = []
        self._tick_issues: list[_Issue] = []
        self._has_fault = False

    def _prepare_faults(
        self,
        faults: Iterable[FaultInjection | Mapping[str, Any]],
        plan: SimulationPlan,
        *,
        until_time: int,
    ) -> list[FaultInjection]:
        """解析并稳定排序故障；故障不是正常 Agent 工具或计划字段。"""

        known_amr_ids = {amr.amr_id for amr in plan.amrs}
        parsed = [FaultInjection.model_validate(item) for item in faults]
        seen: set[tuple[int, str, FaultType]] = set()
        for fault in parsed:
            if fault.amr_id not in known_amr_ids:
                raise SimulationConfigurationError(
                    f"故障注入引用未知 AMR: {fault.amr_id}"
                )
            if fault.at_time < plan.start_time or fault.at_time > plan.max_time:
                raise SimulationConfigurationError(
                    f"故障时间必须落在计划时间范围内: {fault.at_time}"
                )
            if fault.at_time > until_time:
                # 允许为更长评测窗口预先声明故障，但本次窗口不会执行它。
                continue
            key = (fault.at_time, fault.amr_id, fault.fault_type)
            if key in seen:
                raise SimulationConfigurationError(f"重复故障注入: {key}")
            seen.add(key)
        return sorted(
            [fault for fault in parsed if fault.at_time <= until_time],
            key=lambda item: (item.at_time, item.amr_id, item.fault_type.value),
        )

    def _validate_simulator_config(self, plan: SimulationPlan) -> None:
        """验证仿真专属充电快照，防止充电状态使用隐含/越界资源。"""

        blocked = {(cell.x, cell.y) for cell in plan.blocked_cells}
        seen_positions: set[tuple[int, int]] = set()
        for station_id, station in sorted(self.config.charging_stations.items()):
            position = station.position
            if not (0 <= position.x < plan.map_width and 0 <= position.y < plan.map_height):
                raise SimulationConfigurationError(
                    f"充电站 {station_id} 坐标超出地图边界: {position.model_dump()}"
                )
            if (position.x, position.y) in blocked:
                raise SimulationConfigurationError(f"充电站 {station_id} 位于禁行格")
            if (position.x, position.y) in seen_positions:
                raise SimulationConfigurationError("两个充电站不能共享同一坐标")
            seen_positions.add((position.x, position.y))

    def _apply_faults_at_time(
        self,
        current_time: int,
        faults: list[FaultInjection],
    ) -> None:
        """在路径动作前执行当前 tick 故障，并把受影响订单转为 blocked。"""

        for fault in (item for item in faults if item.at_time == current_time):
            runtime = self._amrs[fault.amr_id]
            if runtime.faulted:
                continue
            before_battery = runtime.state.battery
            runtime.faulted = True
            runtime.fault_code = fault.fault_type.value
            runtime.fault_reason = fault.reason
            runtime.charge_station_id = None
            if fault.fault_type is FaultType.BATTERY_DRAIN:
                after_battery = max(0.0, before_battery - float(fault.magnitude or 0.0))
                runtime.state = runtime.state.model_copy(
                    update={
                        "battery": after_battery,
                        "health_status": HealthStatus.FAULT,
                        "connection_status": ConnectionStatus.OFFLINE,
                        "task_status": AMRTaskStatus.OFFLINE,
                    }
                )
            elif fault.fault_type is FaultType.STUCK:
                runtime.state = runtime.state.model_copy(
                    update={
                        "health_status": HealthStatus.DEGRADED,
                        "connection_status": ConnectionStatus.DEGRADED,
                        "task_status": AMRTaskStatus.OFFLINE,
                    }
                )
            else:
                runtime.state = runtime.state.model_copy(
                    update={
                        "health_status": HealthStatus.FAULT,
                        "connection_status": ConnectionStatus.OFFLINE,
                        "task_status": AMRTaskStatus.OFFLINE,
                    }
                )
            order = self._assigned_order(runtime.state.amr_id)
            if order is not None and order.status is not SimulationOrderStatus.COMPLETED:
                order.status = SimulationOrderStatus.BLOCKED
                order.blocked_reason = fault.reason
            event_id = self._emit_event(
                current_time,
                "fault.injected",
                "error",
                amr_id=fault.amr_id,
                order_id=None if order is None else order.order_id,
                payload={
                    "fault_type": fault.fault_type.value,
                    "reason": fault.reason,
                    "magnitude": fault.magnitude,
                    "duration_ticks": fault.duration_ticks,
                    "battery_before": before_battery,
                    "battery_after": runtime.state.battery,
                },
            )
            self._tick_event_ids.append(event_id)
            self._tick_issues.append(
                _Issue(
                    code=f"fault_{fault.fault_type.value}",
                    message=f"AMR {fault.amr_id} 注入 {fault.fault_type.value} 故障并安全停机",
                    amr_id=fault.amr_id,
                    order_id=None if order is None else order.order_id,
                    evidence={
                        "time": current_time,
                        "fault_type": fault.fault_type.value,
                        "reason": fault.reason,
                        "battery_before": before_battery,
                        "battery_after": runtime.state.battery,
                    },
                    requires_replan=True,
                    requires_human=False,
                    severity="error",
                )
            )
            self._has_fault = True

    def _apply_route_steps(self, current_time: int, plan: SimulationPlan) -> None:
        """按 P0-09 原始 timestamp 执行一步，不插入隐含移动或服务延时。"""

        for amr_id, runtime in self._amrs.items():
            if runtime.faulted or runtime.charge_station_id is not None:
                continue
            route = runtime.route
            if route is None:
                continue
            if current_time == plan.start_time:
                event_id = self._emit_event(
                    current_time,
                    "amr.route_started",
                    "info",
                    amr_id=amr_id,
                    order_id=route.order_id,
                    payload={"path_start_time": route.path[0].time},
                )
                self._tick_event_ids.append(event_id)
            step = runtime.steps_by_time.get(current_time)
            if step is None:
                if current_time > route.dropoff_time:
                    # 已经进入 TO_CHARGE 的 AMR 不能在下一 tick 被运输路线的
                    # “终点保持”逻辑反复改回 IDLE，否则会制造无意义的状态抖动。
                    if runtime.state.task_status is not AMRTaskStatus.TO_CHARGE:
                        self._set_task_status(
                            runtime,
                            AMRTaskStatus.IDLE,
                            current_time,
                            route.order_id,
                        )
                continue

            if current_time > plan.start_time:
                if step.action == "move":
                    runtime.move_count += 1
                    battery = runtime.state.battery - plan.config.energy_per_cell_percent
                else:
                    battery = runtime.state.battery
            else:
                battery = runtime.state.battery
            runtime.state = runtime.state.model_copy(
                update={
                    "position": step.position,
                    "heading": Heading(step.heading),
                    "battery": max(0.0, battery),
                }
            )
            action_event = self._emit_event(
                current_time,
                "amr.path_step",
                "info",
                amr_id=amr_id,
                order_id=route.order_id,
                payload={
                    "action": step.action,
                    "position": step.position.model_dump(mode="json"),
                    "heading": int(step.heading),
                    "g_cost": step.g_cost,
                    "battery": runtime.state.battery,
                },
            )
            self._tick_event_ids.append(action_event)

            order = self._orders[route.order_id]
            if current_time == plan.start_time and current_time != route.pickup_time:
                # P0-09 的 start tick 只表示初始快照；第一次真实动作后才进入 TO_PICKUP，
                # 这样 Observation 能保留 IDLE→TO_PICKUP 的清晰迁移证据。
                continue
            if current_time < route.pickup_time:
                self._set_task_status(runtime, AMRTaskStatus.TO_PICKUP, current_time, route.order_id)
            elif current_time == route.pickup_time:
                self._set_task_status(runtime, AMRTaskStatus.LOADING, current_time, route.order_id)
                if order.status is SimulationOrderStatus.PENDING:
                    order.status = SimulationOrderStatus.IN_PROGRESS
                    order.pickup_time = current_time
                    runtime.state = runtime.state.model_copy(
                        update={"load": runtime.state.load + route.payload_kg}
                    )
                    self._service_workstation(
                        current_time,
                        plan,
                        workstation_id=self._order_pickup(plan, route.order_id),
                        amr_id=amr_id,
                        order_id=route.order_id,
                        event_type="pickup",
                        payload_kg=route.payload_kg,
                    )
            elif current_time < route.dropoff_time:
                self._set_task_status(runtime, AMRTaskStatus.TO_DROPOFF, current_time, route.order_id)
            elif current_time == route.dropoff_time:
                self._set_task_status(runtime, AMRTaskStatus.UNLOADING, current_time, route.order_id)
                if order.status is SimulationOrderStatus.IN_PROGRESS:
                    order.status = SimulationOrderStatus.COMPLETED
                    order.dropoff_time = current_time
                    runtime.state = runtime.state.model_copy(
                        update={"load": max(0.0, runtime.state.load - route.payload_kg)}
                    )
                    self._service_workstation(
                        current_time,
                        plan,
                        workstation_id=self._order_dropoff(plan, route.order_id),
                        amr_id=amr_id,
                        order_id=route.order_id,
                        event_type="dropoff",
                        payload_kg=route.payload_kg,
                    )
            else:
                self._set_task_status(runtime, AMRTaskStatus.IDLE, current_time, route.order_id)

    def _update_charging(self, current_time: int, plan: SimulationPlan) -> None:
        """更新充电站容量、充电速率和 TO_CHARGE/CHARGING 状态。

        只有 AMR 实际位于配置的充电站坐标才会占用充电位；没有对应安全路径的
        AMR 不会被仿真器“瞬移”到充电站，而是停留在 TO_CHARGE 等待后续规划。
        """

        station_members: dict[str, list[str]] = {
            station_id: [] for station_id in self.config.charging_stations
        }
        for runtime in self._amrs.values():
            if runtime.charge_station_id is None:
                continue
            if runtime.faulted:
                runtime.charge_station_id = None
                continue
            station = self.config.charging_stations[runtime.charge_station_id]
            if runtime.state.battery >= self.config.charge_target_percent:
                completed_id = self._emit_event(
                    current_time,
                    "charging.completed",
                    "info",
                    amr_id=runtime.state.amr_id,
                    charging_station_id=runtime.charge_station_id,
                    payload={"battery": runtime.state.battery},
                )
                self._tick_event_ids.append(completed_id)
                runtime.charge_station_id = None
                self._set_task_status(runtime, AMRTaskStatus.IDLE, current_time, None)
                continue
            if runtime.state.position != station.position:
                runtime.charge_station_id = None
                self._set_task_status(runtime, AMRTaskStatus.TO_CHARGE, current_time, None)
                continue
            station_members[runtime.charge_station_id].append(runtime.state.amr_id)

        # 新进入的 AMR 先按 ID 排序，保证同时抢占多个站位时跨运行字节一致。
        for amr_id, runtime in self._amrs.items():
            if runtime.faulted or runtime.charge_station_id is not None:
                continue
            if not self.config.auto_charge:
                continue
            if runtime.state.task_status not in {
                AMRTaskStatus.IDLE,
                AMRTaskStatus.TO_CHARGE,
            }:
                continue
            if runtime.state.battery > self.config.charge_threshold_percent:
                continue
            candidates = [
                station_id
                for station_id, station in sorted(self.config.charging_stations.items())
                if station.position == runtime.state.position
            ]
            if not candidates:
                self._set_task_status(runtime, AMRTaskStatus.TO_CHARGE, current_time, None)
                if not runtime.charge_wait_reported:
                    event_id = self._emit_event(
                        current_time,
                        "charging.unavailable",
                        "warning",
                        amr_id=amr_id,
                        payload={"battery": runtime.state.battery},
                    )
                    self._tick_event_ids.append(event_id)
                    runtime.charge_wait_reported = True
                continue
            selected_station = candidates[0]
            if len(station_members[selected_station]) >= self.config.charging_stations[
                selected_station
            ].capacity:
                self._set_task_status(runtime, AMRTaskStatus.TO_CHARGE, current_time, None)
                if not runtime.charge_wait_reported:
                    event_id = self._emit_event(
                        current_time,
                        "charging.waiting",
                        "warning",
                        amr_id=amr_id,
                        charging_station_id=selected_station,
                        payload={
                            "battery": runtime.state.battery,
                            "capacity": self.config.charging_stations[selected_station].capacity,
                        },
                    )
                    self._tick_event_ids.append(event_id)
                    runtime.charge_wait_reported = True
                continue
            runtime.charge_station_id = selected_station
            station_members[selected_station].append(amr_id)
            runtime.charge_wait_reported = False
            started_id = self._emit_event(
                current_time,
                "charging.started",
                "info",
                amr_id=amr_id,
                charging_station_id=selected_station,
                payload={"battery": runtime.state.battery},
            )
            self._tick_event_ids.append(started_id)

        for station_id, amr_ids in station_members.items():
            for amr_id in sorted(amr_ids):
                runtime = self._amrs[amr_id]
                before = runtime.state.battery
                after = min(
                    self.config.charge_target_percent,
                    before + self.config.charge_rate_percent_per_tick,
                )
                delta = after - before
                runtime.state = runtime.state.model_copy(
                    update={
                        "battery": after,
                        "task_status": AMRTaskStatus.CHARGING,
                    }
                )
                self._station_energy[station_id] += delta
                progress_id = self._emit_event(
                    current_time,
                    "charging.progress",
                    "info",
                    amr_id=amr_id,
                    charging_station_id=station_id,
                    payload={
                        "battery_before": before,
                        "battery_after": after,
                        "energy_delivered_percent": delta,
                    },
                )
                self._tick_event_ids.append(progress_id)

    def _service_workstation(
        self,
        current_time: int,
        plan: SimulationPlan,
        *,
        workstation_id: str,
        amr_id: str,
        order_id: str,
        event_type: str,
        payload_kg: float,
    ) -> None:
        """记录零时长工位事件，并在运行时再次守住容量边界。"""

        workstation = self._workstations.get(workstation_id)
        if workstation is None:
            # Validator 已经检查过位置快照；这里保留防御性失败，避免数据漂移
            # 时继续伪造“已装卸”结果。
            raise SimulationInvariantError(f"工位不在仿真快照中: {workstation_id}")
        if len(workstation.occupied_amr_ids) >= workstation.capacity:
            self._tick_issues.append(
                _Issue(
                    code="workstation_capacity_exceeded_runtime",
                    message=f"工位 {workstation_id} 在 tick {current_time} 超过容量",
                    amr_id=amr_id,
                    order_id=order_id,
                    evidence={
                        "time": current_time,
                        "workstation_id": workstation_id,
                        "observed": len(workstation.occupied_amr_ids) + 1,
                        "limit": workstation.capacity,
                    },
                    requires_replan=True,
                    requires_human=True,
                    severity="error",
                )
            )
            return
        workstation.occupied_amr_ids.append(amr_id)
        workstation.last_event_time = current_time
        workstation.last_event_type = event_type
        workstation.service_count += 1
        event_id = self._emit_event(
            current_time,
            f"order.{event_type}",
            "info",
            amr_id=amr_id,
            order_id=order_id,
            workstation_id=workstation_id,
            payload={
                "position": workstation.position.model_dump(mode="json"),
                "payload_kg": payload_kg,
                "capacity": workstation.capacity,
                "occupancy": len(workstation.occupied_amr_ids),
            },
        )
        self._tick_event_ids.append(event_id)

    def _set_task_status(
        self,
        runtime: _RuntimeAMR,
        status: AMRTaskStatus,
        current_time: int,
        order_id: str | None,
    ) -> None:
        """只在状态真正变化时记录迁移，避免事件日志充斥重复快照。"""

        previous = runtime.state.task_status
        if previous is status:
            return
        runtime.state = runtime.state.model_copy(update={"task_status": status})
        event_id = self._emit_event(
            current_time,
            "amr.state_changed",
            "info" if status is not AMRTaskStatus.OFFLINE else "error",
            amr_id=runtime.state.amr_id,
            order_id=order_id,
            payload={"from": previous.value, "to": status.value},
        )
        self._tick_event_ids.append(event_id)

    def _assigned_order(self, amr_id: str) -> _RuntimeOrder | None:
        """按稳定 ID 找到 AMR 当前的运输订单。"""

        for order in self._orders.values():
            if order.assigned_amr_id == amr_id:
                return order
        return None

    def _order_pickup(self, plan: SimulationPlan, order_id: str) -> str:
        """读取订单 pickup 工位，缺失时以运行时不变量失败。"""

        order = next((item for item in plan.orders if item.order_id == order_id), None)
        if order is None:
            raise SimulationInvariantError(f"未知订单: {order_id}")
        return order.pickup

    def _order_dropoff(self, plan: SimulationPlan, order_id: str) -> str:
        """读取订单 dropoff 工位，保持 P0-04 订单语义为唯一来源。"""

        order = next((item for item in plan.orders if item.order_id == order_id), None)
        if order is None:
            raise SimulationInvariantError(f"未知订单: {order_id}")
        return order.dropoff

    def _reset_workstation_tick_state(self) -> None:
        """工位服务是零时长事件，每个 tick 重新计算当前占用。"""

        for workstation in self._workstations.values():
            workstation.occupied_amr_ids = []

    def _emit_event(
        self,
        current_time: int,
        event_type: str,
        severity: str,
        *,
        amr_id: str | None = None,
        order_id: str | None = None,
        workstation_id: str | None = None,
        charging_station_id: str | None = None,
        payload: dict[str, Any],
    ) -> str:
        """用单调序号生成事件 ID，确保不依赖 UUID/墙上时钟而可重放。"""

        self._event_sequence += 1
        event_id = f"{self._simulation_id}:event:{self._event_sequence:06d}"
        event = SimulationEvent(
            event_id=event_id,
            simulation_id=self._simulation_id,
            time=current_time,
            event_type=event_type,
            severity=severity,  # type: ignore[arg-type]
            amr_id=amr_id,
            order_id=order_id,
            workstation_id=workstation_id,
            charging_station_id=charging_station_id,
            payload=payload,
        )
        self._events.append(event)
        return event_id

    def _make_observation(self, current_time: int) -> Observation:
        """把当前 tick 的完整快照封装成 P0-04 ``Observation``。"""

        violations = [
            ConstraintViolation(
                code=issue.code,
                message=issue.message,
                task_id=issue.order_id,
                amr_id=issue.amr_id,
                evidence=issue.evidence,
            )
            for issue in self._tick_issues
        ]
        if any(issue.requires_replan or issue.requires_human for issue in self._tick_issues):
            status = ObservationStatus.BLOCKED
        elif any(issue.severity == "error" for issue in self._tick_issues):
            status = ObservationStatus.ERROR
        elif self._tick_issues:
            status = ObservationStatus.WARNING
        else:
            status = ObservationStatus.OK
        codes = ", ".join(issue.code for issue in self._tick_issues)
        summary = (
            f"tick={current_time} 状态快照完成"
            if not codes
            else f"tick={current_time} 检测到: {codes}"
        )
        state_delta = {
            "time": current_time,
            "amrs": [runtime.state.model_dump(mode="json") for runtime in self._amrs.values()],
            "orders": [self._order_state(item).model_dump(mode="json") for item in self._orders.values()],
            "workstations": [
                self._workstation_state(item).model_dump(mode="json")
                for item in self._workstations.values()
            ],
            "charging_stations": [
                item.model_dump(mode="json") for item in self._charging_station_states()
            ],
        }
        observed_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=current_time)
        first_issue = self._tick_issues[0] if self._tick_issues else None
        return Observation(
            observation_id=f"{self._simulation_id}:observation:{current_time:06d}",
            run_id=self._simulation_id,
            task_id=None if first_issue is None else first_issue.order_id,
            source=ObservationSource.SIMULATOR,
            observed_at=observed_at,
            status=status,
            summary=summary,
            state_delta=state_delta,
            evidence_refs=list(self._tick_event_ids),
            tool_result=None,
            violations=violations,
            requires_replan=any(issue.requires_replan for issue in self._tick_issues),
            requires_human=any(issue.requires_human for issue in self._tick_issues),
        )

    def _order_state(self, order: _RuntimeOrder) -> SimulationOrderState:
        """将内部订单转换为稳定的 Pydantic 快照。"""

        return SimulationOrderState(
            order_id=order.order_id,
            status=order.status,
            assigned_amr_id=order.assigned_amr_id,
            payload_kg=order.payload_kg,
            pickup_time=order.pickup_time,
            dropoff_time=order.dropoff_time,
            blocked_reason=order.blocked_reason,
        )

    @staticmethod
    def _workstation_state(workstation: _RuntimeWorkstation) -> WorkstationState:
        """将内部工位转换为契约快照。"""

        event_type = workstation.last_event_type
        if event_type not in {None, "pickup", "dropoff"}:
            raise SimulationInvariantError(f"未知工位事件类型: {event_type}")
        return WorkstationState(
            workstation_id=workstation.workstation_id,
            position=workstation.position,
            capacity=workstation.capacity,
            occupied_amr_ids=sorted(workstation.occupied_amr_ids),
            last_event_time=workstation.last_event_time,
            last_event_type=event_type,  # type: ignore[arg-type]
            service_count=workstation.service_count,
        )

    def _charging_station_states(self) -> list[ChargingStationState]:
        """返回按充电站 ID 排序的占用与累计供电快照。"""

        states: list[ChargingStationState] = []
        for station_id, station in sorted(self.config.charging_stations.items()):
            charging_ids = sorted(
                runtime.state.amr_id
                for runtime in self._amrs.values()
                if runtime.charge_station_id == station_id and not runtime.faulted
            )
            states.append(
                ChargingStationState(
                    station_id=station_id,
                    position=station.position,
                    capacity=station.capacity,
                    status=(
                        ChargingStationStatus.OCCUPIED
                        if charging_ids
                        else ChargingStationStatus.AVAILABLE
                    ),
                    charging_amr_ids=charging_ids,
                    total_energy_delivered_percent=self._station_energy[station_id],
                )
            )
        return states

    def _final_status(self) -> SimulationStatus:
        """按订单是否完成、是否故障和时间上限生成终态。"""

        if self._has_fault or any(
            item.status is SimulationOrderStatus.BLOCKED for item in self._orders.values()
        ):
            return SimulationStatus.BLOCKED
        if all(item.status is SimulationOrderStatus.COMPLETED for item in self._orders.values()):
            return SimulationStatus.COMPLETED
        return SimulationStatus.TIMEOUT


# 语义别名：P0 文档称“离散事件仿真器”，保留更明确的类名供后续调用方选择。
DiscreteEventSimulator = AMRSimulator


def simulate_plan(
    plan: SimulationPlan | Mapping[str, Any],
    *,
    simulation_id: str = "simulation",
    seed: int = 0,
    until_time: int | None = None,
    faults: Iterable[FaultInjection | Mapping[str, Any]] = (),
    config: SimulatorConfig | None = None,
    validator_client: ValidatorClientProtocol | None = None,
) -> SimulationResult:
    """一次性执行仿真的便捷入口，仍保留 Validator 前置门禁。"""

    return AMRSimulator(config=config, validator_client=validator_client).run(
        plan,
        simulation_id=simulation_id,
        seed=seed,
        until_time=until_time,
        faults=faults,
    )


__all__ = [
    "AMRSimulator",
    "DiscreteEventSimulator",
    "SimulationConfigurationError",
    "SimulationInvariantError",
    "ValidatorClientProtocol",
    "simulate_plan",
]
