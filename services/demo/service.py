"""演示编排服务：固定 seed → C++ Hungarian → C++ A* → C++ Validator → Python 仿真。

与 P0 生产链路的差异（有意为之，差异已写入 docs/HANDOFF_CONTEXT.md）：

- 不经过 ToolRegistry.dispatch_simulation，因此不写 Effect Ledger、不触发
  HITL 审批；本服务返回的轨迹只能作为可视化演示证据，不能当发布证据，
  ``--approve-dispatch`` 在这条链路上不存在。
- 每次请求都从 warehouse_v1@seed-v1 重新读取固定快照，演示是无状态的：
  刷新页面重跑同一订单得到同一份确定性结果。
- C++ 请求 envelope 逐字段镜像 agent/tools/registry.py 的 P0-08/09/10 组装
  以及 agent/runtime/graph.py 的 SimulationPlan 组装，保证演示路径与生产
  路径面对同一个 C++ 判定；改动那两处时必须同步检查本文件。
- 仿真器不回放浏览器输入；前端只拿到 Validator 通过后的 amr.path_step 事件。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from agent.tools.cpp_client import CppAdapterError, FixedCppJsonClient
from agent.tools.contracts import ToolErrorCategory
from agent.tools.schemas import (
    AllocationResponse,
    RoutePlanResponse,
    ValidationResponse,
)
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider, EnvironmentSnapshot
from domains.amr_warehouse import TransportOrder, WarehouseMap
from services.amr_simulator import (
    AMRSimulator,
    ChargingStationSpec,
    FleetPlanRoute,
    PlanValidationError,
    SimulationConfigurationError,
    SimulationInvariantError,
    SimulationOrderStatus,
    SimulationPlan,
    SimulationResult,
    SimulatorConfig,
    ValidatorConfig,
    ValidatorExecutionError,
)
from services.demo.contracts import (
    DemoOrderExtraction,
    DemoPathStep,
    DemoRouteInfo,
    DemoSimulateRequest,
    DemoSimulateResponse,
    DemoSimulationOutcome,
    DemoSimulationSummary,
    DemoWarehouseMap,
)
from services.model_gateway.exceptions import (
    ModelGatewayStartupError,
    ModelGenerationError,
    StructuredOutputError,
)
from services.model_gateway.protocols import ModelProviderProtocol


class DemoServiceError(RuntimeError):
    """演示服务稳定失败；HTTP 层把 code/status_code/evidence 原样透出。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.evidence = dict(evidence or {})

    def to_detail(self) -> dict[str, Any]:
        """构造与 docs/API.md 一致、并附带 C++ 证据的 detail 对象。"""

        return {"code": self.code, "message": str(self), **self.evidence}


class WarehouseDemoService:
    """面向演示 UI 的无状态编排：地图读取 + 受 Validator 门禁的仿真执行。"""

    # 与 evals/P0-13 共用的固定 seed 引用；不接受调用方改环境。
    ENVIRONMENT_REF = "warehouse_v1@seed-v1"
    # 与 agent/runtime/graph.py DEFAULT_PAYLOAD_KG 保持一致；种子订单不带重量字段。
    DEMO_PAYLOAD_KG = 1.0
    # 与 P0-13 dispatch 默认 seed 一致，保证演示结果可复现。
    DEMO_SEED = 7
    # C++ 子进程超时：演示只跑单订单，远小于工具层预算即可覆盖。
    ALLOCATE_TIMEOUT_SECONDS = 15.0
    ROUTE_TIMEOUT_SECONDS = 30.0
    VALIDATE_TIMEOUT_SECONDS = 15.0

    def __init__(
        self,
        *,
        snapshot_provider: DefaultWarehouseSnapshotProvider | None = None,
        cpp_client: FixedCppJsonClient | None = None,
        data_root: str | Path | None = None,
    ) -> None:
        # data_root 仅供测试注入；生产固定到仓库内 seed 目录，前端无法改路径。
        self._data_root = Path(data_root) if data_root is not None else (
            Path(__file__).resolve().parents[2] / "domains" / "amr_warehouse" / "data"
        )
        self._snapshot_provider = snapshot_provider or DefaultWarehouseSnapshotProvider(
            data_root=self._data_root
        )
        self._cpp_client = cpp_client or FixedCppJsonClient()
        self._warehouse_cache: WarehouseMap | None = None

    def get_warehouse_map(self) -> DemoWarehouseMap:
        """返回规范化地图 + 初始 AMR 位姿 + 可演示订单清单。"""

        warehouse = self._warehouse_map()
        snapshot = self._snapshot()
        return DemoWarehouseMap(
            environment_ref=snapshot.environment_ref,
            state_version=snapshot.state_version,
            map_id=warehouse.map_id,
            version=warehouse.version,
            width=warehouse.width,
            height=warehouse.height,
            resolution_m=warehouse.resolution_m,
            obstacles=list(warehouse.obstacles),
            temporary_blocked_cells=list(warehouse.temporary_blocked_cells),
            narrow_aisles=list(warehouse.narrow_aisles),
            blocked_edges=list(warehouse.blocked_edges),
            one_way_edges=list(warehouse.one_way_edges),
            pickup_points=list(warehouse.pickup_points),
            dropoff_points=list(warehouse.dropoff_points),
            charging_stations=list(warehouse.charging_stations),
            amrs=sorted(snapshot.amrs, key=lambda item: item.amr_id),
            orders=sorted(snapshot.orders, key=lambda item: item.order_id),
            start_time=snapshot.start_time,
            max_time=snapshot.max_time,
        )

    def run_simulation(self, request: DemoSimulateRequest) -> DemoSimulateResponse:
        """执行完整演示链路；任一环节失败都返回稳定错误且不附带轨迹。"""

        snapshot = self._snapshot()
        order = self._require_order(snapshot, request.order_id)
        return self._execute_order_chain(snapshot, order)

    def run_nl_order(
        self,
        request_text: str,
        *,
        model_provider: ModelProviderProtocol,
    ) -> DemoSimulateResponse:
        """任意自然语言下单（轻量演示链）：LLM 抽取 → 动态订单 → 同一 C++ 链。

        与 ``/demo/nl/run`` 的完整 PEVR 闭环刻意不同：不写 Effect Ledger、
        不需要 HITL 审批、不作发布证据、不持久化任何历史。LLM 只负责从文本
        抽出四个订单要素；订单 ID、地点合法性、deadline 下限都由本服务对照
        固定快照重建/校验，C++ Validator 仍是仿真前的硬门禁。
        """

        snapshot = self._snapshot()
        order = self.prepare_dynamic_order(request_text, model_provider=model_provider)
        return self._execute_order_chain(snapshot, order, route_orders=[order])

    def prepare_dynamic_order(
        self,
        request_text: str,
        *,
        model_provider: ModelProviderProtocol,
    ) -> TransportOrder:
        """LLM 抽四要素后按地点白名单重建订单；供轻量链与闭环链共用。"""

        extraction = self._extract_order(request_text, model_provider)
        return self._build_nl_order(extraction)

    def _execute_order_chain(
        self,
        snapshot: EnvironmentSnapshot,
        order: TransportOrder,
        *,
        route_orders: list[TransportOrder] | None = None,
    ) -> DemoSimulateResponse:
        """种子订单与自然语言订单共用的确定性链：分配→路线→校验→仿真。

        ``route_orders`` 是发给 A* 的订单全集：种子链路保持历史行为传整个
        快照订单表（与生产 registry envelope 逐字段一致）；自然语言链路只传
        动态订单本身，因为 assignments 只引用它。
        """

        allocation = self._allocate(snapshot, order)
        routes = self._plan_routes(snapshot, allocation, orders=route_orders)
        plan = self._assemble_plan(snapshot, routes, order)
        validation = self._validate(plan)
        result = self._simulate(plan)
        path_steps = self._extract_path_steps(result)
        return DemoSimulateResponse(
            map=self.get_warehouse_map(),
            routes=[
                DemoRouteInfo(
                    amr_id=route.amr_id,
                    order_id=route.order_id,
                    payload_kg=route.payload_kg,
                    pickup_time=route.pickup_time,
                    dropoff_time=route.dropoff_time,
                    total_cost=route.total_cost,
                    path=list(route.path),
                )
                for route in plan.routes
            ],
            result=DemoSimulationOutcome(
                simulation_id=result.simulation_id,
                seed=result.seed,
                status=result.status,
                start_time=result.start_time,
                end_time=result.end_time,
                amrs=list(result.amrs),
                orders=list(result.orders),
                workstations=list(result.workstations),
                charging_stations=list(result.charging_stations),
                events=list(result.events),
            ),
            path_steps=path_steps,
            summary=DemoSimulationSummary(
                order_id=order.order_id,
                order=order.model_copy(deep=True),
                allocation_status=allocation.status,
                route_status=routes.status,
                validator_valid=validation.valid,
                validator_error_count=validation.error_count,
                validator_ruleset_version=validation.ruleset_version,
                simulation_status=result.status,
                completed_order_ids=[
                    item.order_id
                    for item in result.orders
                    if item.status is SimulationOrderStatus.COMPLETED
                ],
                path_step_count=len(path_steps),
            ),
        )

    def _snapshot(self) -> EnvironmentSnapshot:
        """读取固定环境快照；未知 seed 在任何 C++ 调用前失败。"""

        return self._snapshot_provider.get_snapshot(self.ENVIRONMENT_REF)

    def _warehouse_map(self) -> WarehouseMap:
        """读取 warehouse_v1.json 的领域视图（快照只保留合并后的位置字典）。

        EnvironmentSnapshot 把 P/S/C 合并进 location_positions 且把障碍与临时
        封路合并进 blocked_cells；前端图例需要分类清单，因此这里从同一
        data_root 再读一次地图定义，与快照读取共用同一份文件，不产生第二
        数据源的漂移风险。
        """

        if self._warehouse_cache is None:
            path = self._data_root / "warehouse_v1.json"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DemoServiceError(
                    f"演示地图 seed 不可读: {exc}",
                    status_code=503,
                    code="demo_map_unavailable",
                    evidence={"path": str(path)},
                ) from exc
            self._warehouse_cache = WarehouseMap.model_validate(raw)
        return self._warehouse_cache.model_copy(deep=True)

    @staticmethod
    def _require_order(snapshot: EnvironmentSnapshot, order_id: str) -> TransportOrder:
        """只允许跑种子里真实存在的订单；未知 ID 是 404 而不是隐式新建。"""

        for order in snapshot.orders:
            if order.order_id == order_id:
                return order
        raise DemoServiceError(
            f"种子快照中不存在订单: {order_id}",
            status_code=404,
            code="demo_order_not_found",
            evidence={
                "order_id": order_id,
                "available_order_ids": sorted(item.order_id for item in snapshot.orders),
            },
        )

    # 自然语言抽取的调用级预算：四字段输出极小，收紧 tokens/超时避免演示页长等。
    NL_EXTRACT_MAX_OUTPUT_TOKENS = 256
    NL_EXTRACT_TIMEOUT_SECONDS = 60.0
    # 请求未提及截止时间时的默认仿真秒，与种子订单 ORDER-001 的 deadline 对齐。
    NL_DEFAULT_DEADLINE = 120

    def _extract_order(
        self,
        request_text: str,
        model_provider: ModelProviderProtocol,
    ) -> DemoOrderExtraction:
        """用 Fast 把自然语言抽成四要素；LLM 输出只是线索，不是订单真值。"""

        warehouse = self._warehouse_map()
        pickup_ids = sorted(item.id for item in warehouse.pickup_points)
        dropoff_ids = sorted(item.id for item in warehouse.dropoff_points)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是仓库运输订单抽取器。从用户的中文请求中抽取四个字段并只输出 JSON："
                    "material_id（物料标识，形如 MAT-001；未提及则填 MAT-001）、"
                    f"pickup（取货点，只能是 {', '.join(pickup_ids)} 之一）、"
                    f"dropoff（交付点，只能是 {', '.join(dropoff_ids)} 之一）、"
                    f"deadline（整数，仿真秒；未提及则填 {self.NL_DEFAULT_DEADLINE}）。"
                    "不要输出解释、Markdown 或额外字段。"
                ),
            },
            {"role": "user", "content": request_text.strip()},
        ]
        try:
            generation = model_provider.generate_structured(
                messages,
                DemoOrderExtraction,
                max_output_tokens=self.NL_EXTRACT_MAX_OUTPUT_TOKENS,
                timeout_seconds=self.NL_EXTRACT_TIMEOUT_SECONDS,
            )
        except StructuredOutputError as exc:
            # 首次生成 + 一次修复都不过 Schema：如实 422，不猜测用户意图。
            raise DemoServiceError(
                "Fast 未能从请求中抽出合法订单要素",
                status_code=422,
                code="nl_extract_failed",
                evidence={"attempts": exc.attempts, "last_error": exc.last_error[-400:]},
            ) from exc
        except (ModelGatewayStartupError, ModelGenerationError) as exc:
            # 连接失败/超时/空响应/alias 不符都归为「Fast 未就绪」。
            raise DemoServiceError(
                f"Fast 模型不可用，请先通过 scripts/start_local.ps1 -StartFast 启动: {exc}",
                status_code=503,
                code="fast_model_unavailable",
                evidence={"retryable": True},
            ) from exc
        return generation.value

    def _build_nl_order(self, extraction: DemoOrderExtraction) -> TransportOrder:
        """把抽取结果重建成受快照白名单约束的动态订单。

        地点必须命中 warehouse_v1 的 P*/S* 清单（大小写归一后精确匹配）；
        订单 ID 由服务端生成，LLM 无权命名，避免与种子订单/历史运行撞号。
        """

        warehouse = self._warehouse_map()
        pickup_ids = sorted(item.id for item in warehouse.pickup_points)
        dropoff_ids = sorted(item.id for item in warehouse.dropoff_points)
        pickup = extraction.pickup.strip().upper()
        dropoff = extraction.dropoff.strip().upper()
        if pickup not in pickup_ids or dropoff not in dropoff_ids:
            raise DemoServiceError(
                f"请求的地点不在演示地图内: pickup={extraction.pickup}, dropoff={extraction.dropoff}",
                status_code=422,
                code="unknown_location",
                evidence={
                    "valid_pickup_ids": pickup_ids,
                    "valid_dropoff_ids": dropoff_ids,
                },
            )
        try:
            return TransportOrder(
                order_id=f"NL-{uuid4().hex[:8].upper()}",
                material_id=extraction.material_id.strip(),
                pickup=pickup,
                dropoff=dropoff,
                priority=3,
                release_time=0,
                deadline=extraction.deadline,
                dependencies=[],
            )
        except ValidationError as exc:
            raise DemoServiceError(
                f"抽取结果无法构成合法订单: {exc}",
                status_code=422,
                code="nl_extract_failed",
            ) from exc

    def _allocate(self, snapshot: EnvironmentSnapshot, order: TransportOrder) -> AllocationResponse:
        """调用生产 Hungarian；envelope 镜像 registry._allocation_handler。"""

        # 注意：allocator 的 C++ 严格 codec 不接受 environment_ref（与
        # registry._allocation_handler 的 envelope 保持一致，字段差即 422）。
        payload = {
            "schema_version": "1.0",
            "completed_order_ids": sorted(snapshot.completed_order_ids),
            "location_positions": {
                key: snapshot.location_positions[key].model_dump(mode="json")
                for key in sorted(snapshot.location_positions)
            },
            "amrs": [
                item.model_dump(mode="json")
                for item in sorted(snapshot.amrs, key=lambda value: value.amr_id)
            ],
            "orders": [order.model_dump(mode="json")],
            "weights": {
                "distance": 1.0,
                "lateness_risk": 10.0,
                "battery_risk": 5.0,
                "load_penalty": 2.0,
                "priority_bonus": 1.0,
            },
            "config": {
                "current_time": snapshot.start_time,
                "maximum_load_kg": 100.0,
                "travel_speed_cells_per_second": 1.0,
                "energy_per_cell_percent": 1.0,
                "battery_warning_threshold_percent": 30.0,
                "new_task_battery_threshold_percent": 20.0,
                "critical_battery_threshold_percent": 10.0,
                "battery_safety_reserve_percent": 15.0,
            },
        }
        try:
            raw = self._cpp_client.allocate(
                payload,
                timeout_seconds=self.ALLOCATE_TIMEOUT_SECONDS,
            )
            output = AllocationResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise self._translate_cpp_error(exc) from exc
        except ValidationError as exc:
            raise DemoServiceError(
                f"Hungarian 输出不符合 Schema: {exc}",
                status_code=502,
                code="demo_cpp_output_schema_violation",
            ) from exc
        assigned_order_ids = {item.order_id for item in output.assignments}
        if output.status != "complete" or order.order_id not in assigned_order_ids:
            # 依赖未满足/电量不足等不可分配是业务事实：带 C++ 理由码返回 422。
            raise DemoServiceError(
                f"Hungarian 无法为订单 {order.order_id} 找到可行分配",
                status_code=422,
                code="allocation_infeasible",
                evidence={
                    "allocation_status": output.status,
                    "unassigned_orders": [
                        item.model_dump(mode="json") for item in output.unassigned_orders
                    ],
                    "algorithm": output.algorithm,
                },
            )
        return output

    def _plan_routes(
        self,
        snapshot: EnvironmentSnapshot,
        allocation: AllocationResponse,
        *,
        orders: list[TransportOrder] | None = None,
    ) -> RoutePlanResponse:
        """调用生产 A*；envelope 镜像 registry._route_handler。

        ``orders`` 为 None 时沿用快照全量订单表（种子链路的历史行为）；
        自然语言链路显式传入只含动态订单的单元素列表。
        """

        route_orders = snapshot.orders if orders is None else orders

        # 快照的 blocked_cells 已合并静态障碍 + 临时封路（见 snapshots.py 注释），
        # 演示不接受额外 blocked_cells 输入，避免前端绕过 warehouse_v1 封路事实。
        unique_cells = {(cell.x, cell.y): cell for cell in snapshot.blocked_cells}
        payload = {
            "schema_version": "1.0",
            "environment_ref": snapshot.environment_ref,
            "map_width": snapshot.map_width,
            "map_height": snapshot.map_height,
            "blocked_cells": [
                unique_cells[key].model_dump(mode="json") for key in sorted(unique_cells)
            ],
            "blocked_edges": [
                {
                    "from": edge["from"].model_dump(mode="json"),
                    "to": edge["to"].model_dump(mode="json"),
                }
                for edge in snapshot.blocked_edges
            ],
            "one_way_edges": [
                {
                    "from": edge["from"].model_dump(mode="json"),
                    "to": edge["to"].model_dump(mode="json"),
                }
                for edge in snapshot.one_way_edges
            ],
            "location_positions": {
                key: snapshot.location_positions[key].model_dump(mode="json")
                for key in sorted(snapshot.location_positions)
            },
            "amrs": [
                item.model_dump(mode="json")
                for item in sorted(snapshot.amrs, key=lambda value: value.amr_id)
            ],
            "orders": [
                item.model_dump(mode="json")
                for item in sorted(route_orders, key=lambda value: value.order_id)
            ],
            "completed_order_ids": sorted(snapshot.completed_order_ids),
            # 与 graph 一致：assignments 保留 Hungarian 的 components 审计快照，
            # 让演示链路与生产链路发给 A* 的请求逐字段相同。
            "assignments": [
                item.model_dump(mode="json") for item in allocation.assignments
            ],
            "start_time": snapshot.start_time,
            "max_time": snapshot.max_time,
            "costs": {"move_cost": 1.0, "turn_cost": 0.25, "wait_cost": 1.0},
        }
        try:
            raw = self._cpp_client.plan_routes(
                payload,
                timeout_seconds=self.ROUTE_TIMEOUT_SECONDS,
            )
            output = RoutePlanResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise self._translate_cpp_error(exc) from exc
        except ValidationError as exc:
            raise DemoServiceError(
                f"A* 输出不符合 Schema: {exc}",
                status_code=502,
                code="demo_cpp_output_schema_violation",
            ) from exc
        if output.status == "infeasible":
            raise DemoServiceError(
                "A* 无法为分配结果生成安全路线",
                status_code=422,
                code="route_infeasible",
                evidence={
                    "route_status": output.status,
                    "routes": [item.model_dump(mode="json") for item in output.routes],
                },
            )
        return output

    def _assemble_plan(
        self,
        snapshot: EnvironmentSnapshot,
        routes: RoutePlanResponse,
        order: TransportOrder,
    ) -> SimulationPlan:
        """把 A* 输出包装成 P0-10/P0-11 计划；镜像 graph._build_simulation_plan。

        独立成方法是为了让测试能用子类注入「必然被 Validator 拒绝」的坏计划，
        生产代码路径本身不携带任何绕过 Validator 的开关。
        """

        return SimulationPlan(
            schema_version="1.0",
            environment_ref=snapshot.environment_ref,
            map_width=snapshot.map_width,
            map_height=snapshot.map_height,
            blocked_cells=[item.model_copy(deep=True) for item in snapshot.blocked_cells],
            blocked_edges=[
                {"from": edge["from"], "to": edge["to"]} for edge in snapshot.blocked_edges
            ],
            one_way_edges=[
                {"from": edge["from"], "to": edge["to"]} for edge in snapshot.one_way_edges
            ],
            amrs=[item.model_copy(deep=True) for item in snapshot.amrs],
            orders=[order.model_copy(deep=True)],
            location_positions={
                key: value.model_copy(deep=True)
                for key, value in snapshot.location_positions.items()
            },
            completed_order_ids=sorted(snapshot.completed_order_ids),
            routes=[
                FleetPlanRoute(
                    **item.model_dump(mode="python"),
                    payload_kg=self.DEMO_PAYLOAD_KG,
                )
                for item in routes.routes
            ],
            start_time=snapshot.start_time,
            max_time=snapshot.max_time,
            config=ValidatorConfig(
                maximum_load_kg=100.0,
                energy_per_cell_percent=1.0,
                battery_safety_reserve_percent=15.0,
                new_task_battery_threshold_percent=20.0,
                critical_battery_threshold_percent=10.0,
                minimum_safety_distance_cells=1,
                default_workstation_capacity=1,
            ),
            workstation_capacities=dict(snapshot.workstation_capacities),
            ruleset_version="p0-10.v1",
        )

    def _validate(self, plan: SimulationPlan) -> ValidationResponse:
        """Validator 是仿真前置门禁；invalid 计划直接 422，绝不进入仿真器。"""

        payload = plan.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            raw = self._cpp_client.validate_plan(
                payload,
                timeout_seconds=self.VALIDATE_TIMEOUT_SECONDS,
            )
            output = ValidationResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise self._translate_cpp_error(exc) from exc
        except ValidationError as exc:
            raise DemoServiceError(
                f"Validator 输出不符合 Schema: {exc}",
                status_code=502,
                code="demo_cpp_output_schema_violation",
            ) from exc
        if not output.valid:
            raise DemoServiceError(
                "C++ Validator 拒绝该计划，演示不生成轨迹",
                status_code=422,
                code="fleet_plan_invalid",
                evidence={
                    "error_count": output.error_count,
                    "ruleset_version": output.ruleset_version,
                    "errors": [item.model_dump(mode="json") for item in output.errors],
                },
            )
        return output

    def _simulate(self, plan: SimulationPlan) -> SimulationResult:
        """仅对已验证计划跑 Python AMRSimulator；仿真 ID 由内容哈希派生。

        确定性 ID 让同一订单重复演示得到同一 simulation_id，便于对照；
        该 ID 只存在于本次响应，不写入任何执行存储/Effect Ledger。
        """

        digest = hashlib.sha256(
            json.dumps(
                {
                    "plan": plan.model_dump(mode="json", by_alias=True),
                    "seed": self.DEMO_SEED,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        simulation_id = f"demo-{digest}"
        # 地图展示 C1/C2 充电站，因此仿真器也注入同一坐标快照（容量=1），
        # 保持「所见即所算」；不注入 FaultInjection，演示路径不暴露故障入口。
        warehouse = self._warehouse_map()
        config = SimulatorConfig(
            charging_stations={
                item.id: ChargingStationSpec(
                    position=item.position,
                    capacity=1,
                )
                for item in warehouse.charging_stations
            }
        )
        try:
            return AMRSimulator(config=config).run(
                plan,
                simulation_id=simulation_id,
                seed=self.DEMO_SEED,
            )
        except PlanValidationError as exc:
            # 理论上不可达：本服务已先跑过 Validator 门禁。防御性映射成 422，
            # 证据直接取自 P0-10 判定结果，仍然不生成轨迹。
            raise DemoServiceError(
                str(exc),
                status_code=422,
                code="fleet_plan_invalid",
                evidence={
                    "error_count": exc.result.get("error_count", 0),
                    "ruleset_version": exc.result.get("ruleset_version", ""),
                    "errors": exc.result.get("errors", []),
                },
            ) from exc
        except ValidatorExecutionError as exc:
            raise DemoServiceError(
                f"仿真器内置 Validator 执行失败: {exc}",
                status_code=503,
                code="cpp_executable_unavailable",
                evidence={"retryable": True},
            ) from exc
        except (SimulationConfigurationError, SimulationInvariantError) as exc:
            # Validator 已通过却仍触发仿真不变量，属于平台缺陷而非用户输入问题。
            raise DemoServiceError(
                f"已验证计划触发仿真器异常: {exc}",
                status_code=500,
                code="demo_simulation_failed",
            ) from exc

    @staticmethod
    def _extract_path_steps(result: SimulationResult) -> list[DemoPathStep]:
        """从事件流截取 amr.path_step 子集，按 (time, amr_id) 排序供前端播放。"""

        steps = [
            DemoPathStep(
                time=event.time,
                amr_id=event.amr_id or "",
                order_id=event.order_id or "",
                action=event.payload["action"],
                position=event.payload["position"],
                heading=int(event.payload["heading"]),
                battery=float(event.payload["battery"]),
                g_cost=float(event.payload["g_cost"]),
            )
            for event in result.events
            if event.event_type == "amr.path_step"
        ]
        steps.sort(key=lambda item: (item.time, item.amr_id))
        return steps

    @staticmethod
    def _translate_cpp_error(exc: CppAdapterError) -> DemoServiceError:
        """把 C++ 适配器失败映射成稳定 HTTP 语义，不吞掉底层证据。

        C++ 退出码 2（INVALID_ARGUMENT）表示「请求本身不可规划」，例如所选
        订单的依赖未完成（task_allocator: order dependency is unknown）——
        这对调用方是 4xx 语义；其他非零退出是平台内部错误，映射 502。
        """

        if exc.category in {ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.TIMEOUT}:
            return DemoServiceError(
                str(exc),
                status_code=503,
                code=exc.code,
                evidence={"retryable": exc.retryable, **exc.details},
            )
        if exc.category is ToolErrorCategory.INVALID_ARGUMENT:
            return DemoServiceError(
                str(exc),
                status_code=422,
                code="demo_cpp_request_rejected",
                evidence={"cpp_code": exc.code, **exc.details},
            )
        return DemoServiceError(
            str(exc),
            status_code=502,
            code=exc.code,
            evidence=dict(exc.details),
        )


__all__ = ["DemoServiceError", "WarehouseDemoService"]
