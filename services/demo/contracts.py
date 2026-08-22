"""演示 UI 扩展的公开 JSON 契约。

背景：docs/scope.md 把「完整前端」划在 P0 之外，本包是用户明确指令要求的
演示扩展（不是 P0 Release PASS 的一部分）。浏览器只消费这里定义的模型：
地图快照来自固定 seed（warehouse_v1），轨迹子集来自 Python AMRSimulator 的
``amr.path_step`` 事件。所有模型禁止未声明字段与非有限浮点，避免前端把
猜测字段当成公共契约，也避免演示路径悄悄长成第二套未审接口。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.amr_warehouse import (
    AMRState,
    GridPosition,
    NarrowAisle,
    TransportOrder,
    WarehouseEdge,
    WarehouseLocation,
)
from services.amr_simulator import (
    ChargingStationState,
    RouteStep,
    SimulationEvent,
    SimulationOrderState,
    SimulationStatus,
    WorkstationState,
)


class DemoContract(BaseModel):
    """演示契约的共同基类：禁止未知字段、禁止 NaN/Inf、按字段名填充。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )


class DemoWarehouseMap(DemoContract):
    """GET /demo/warehouse 的规范化地图快照；前端不得自行猜测任何地图元素。

    字段直接复用 domains.amr_warehouse 的已验证模型，保证 UI 图例与
    warehouse_v1.json 逐格一致；amrs/orders 来自同一 seed 的快照读取，
    因此初始位姿与可选订单和 C++ 规划看到的完全一致。
    """

    environment_ref: str
    state_version: str
    map_id: str
    # warehouse_v1.json 的 version 是整数序号，与 WarehouseMap 领域模型保持一致。
    version: int
    width: int
    height: int
    resolution_m: float
    obstacles: list[GridPosition]
    temporary_blocked_cells: list[GridPosition]
    narrow_aisles: list[NarrowAisle]
    blocked_edges: list[WarehouseEdge]
    one_way_edges: list[WarehouseEdge]
    pickup_points: list[WarehouseLocation]
    dropoff_points: list[WarehouseLocation]
    charging_stations: list[WarehouseLocation]
    amrs: list[AMRState]
    orders: list[TransportOrder]
    start_time: int
    max_time: int


class DemoSimulateRequest(DemoContract):
    """POST /demo/simulate 请求体；只允许选择种子数据中已存在的订单 ID。

    服务端会校验 order_id 属于 warehouse_v1@seed-v1；未知 ID 返回 404，
    而不是默默跑一份前端编造的订单。
    """

    order_id: str = Field(default="ORDER-001", min_length=1, max_length=64)


class DemoPathStep(DemoContract):
    """画轨迹用的 ``amr.path_step`` 子集；坐标与 SimulationResult.events 逐字段一致。

    前端只允许按这些离散格子步画折线（move）与格内标记（wait/turn），
    不做连续物理插值；battery 随步下发，供状态栏/悬停展示电量衰减。
    """

    time: int = Field(ge=0)
    amr_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=128)
    action: Literal["start", "move", "turn_left", "turn_right", "wait"]
    position: GridPosition
    heading: int
    battery: float = Field(ge=0.0, le=100.0)
    g_cost: float = Field(ge=0.0)


class DemoRouteInfo(DemoContract):
    """Validator 已验证的单车路线快照；仅用于对照，不是前端寻路输入。"""

    amr_id: str
    order_id: str
    payload_kg: float
    pickup_time: int
    dropoff_time: int
    total_cost: float
    path: list[RouteStep]


class DemoSimulationOutcome(DemoContract):
    """SimulationResult 的稳定展示子集：状态、最终 AMR/订单快照与完整事件流。

    逐 tick Observation 体量大且前端播放只需要事件流，因此响应不内嵌
    observations；需要完整结果调试时用 scripts/export_demo_trajectory.py
    离线导出。events 保持原样下发，便于核对 path_steps 没有伪造。
    """

    simulation_id: str
    seed: int
    status: SimulationStatus
    start_time: int
    end_time: int
    amrs: list[AMRState]
    orders: list[SimulationOrderState]
    workstations: list[WorkstationState]
    charging_stations: list[ChargingStationState]
    events: list[SimulationEvent]


class DemoSimulationSummary(DemoContract):
    """状态栏摘要：Validator 判定、仿真状态与完成订单一屏可见。

    ``order`` 是本次实际执行的完整订单真值：自然语言链路中抽取结果由服务端
    重建，前端历史清单只能以它为准标注物料与取/送点，不得自行解析用户原文。
    """

    order_id: str
    order: TransportOrder
    allocation_status: str
    route_status: str
    validator_valid: bool
    validator_error_count: int = Field(ge=0)
    validator_ruleset_version: str
    simulation_status: SimulationStatus
    completed_order_ids: list[str]
    path_step_count: int = Field(ge=0)


class DemoSimulateResponse(DemoContract):
    """POST /demo/simulate 成功响应：地图 + 对照路线 + 仿真结果 + 轨迹子集。"""

    map: DemoWarehouseMap
    routes: list[DemoRouteInfo]
    result: DemoSimulationOutcome
    path_steps: list[DemoPathStep]
    summary: DemoSimulationSummary


class DemoLauncherRequest(DemoContract):
    """受控启动器请求体：脚本路径与参数由服务端白名单固定，前端只有布尔开关。

    start_fast=True 等价于人工追加 ``-StartFast``（仍禁止 Smart）；默认 False
    对应「不启动模型」的地图/仿真演示，Fast 不是本步演示的前置条件。
    """

    start_fast: bool = False


class DemoLauncherStatus(DemoContract):
    """启动器状态：不回传密钥、环境变量或完整命令行，只暴露排障所需最小信息。"""

    state: Literal["idle", "running", "exited", "failed", "unavailable"]
    script: str
    start_fast: bool
    pid: int | None
    exit_code: int | None
    started_at: str | None
    message: str
    log_tail: list[str]


class DemoNLRunRequest(DemoContract):
    """POST /demo/nl/run 请求体：自然语言运输订单原文。

    文本只作为 ``run_p013_e2e.py --request`` 的一个独立 argv 元素传递（无 Shell），
    长度上限比 PEVRRequest 的 4000 更紧，避免演示页提交超长 prompt 打满 Fast 上下文。
    """

    request: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_request_not_blank(self) -> "DemoNLRunRequest":
        """与 PEVRRequest 同一边界：纯空白文本在 API 入口就拒绝，不占用运行槽位。"""

        if not self.request.strip():
            raise ValueError("request 不能只包含空白字符")
        return self


class DemoNLResumeRequest(DemoContract):
    """POST /demo/nl/resume 请求体：只允许按 run_id 恢复，审批决定本身走受保护 API。"""

    run_id: str = Field(min_length=1, max_length=64)


class DemoNLOrderRequest(DemoContract):
    """POST /demo/order 请求体：任意自然语言运输订单（轻量演示链）。

    与 ``/demo/nl/run`` 的完整 PEVR 闭环不同，本接口不写 Effect Ledger、
    不需要 HITL 审批、不作发布证据；文本只作为 LLM 抽取提示，订单真值由
    服务端按快照地点白名单重建，LLM 输出本身不被信任。
    """

    request: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_request_not_blank(self) -> "DemoNLOrderRequest":
        """纯空白文本在 API 入口就拒绝，不占用模型调用。"""

        if not self.request.strip():
            raise ValueError("request 不能只包含空白字符")
        return self


class DemoOrderExtraction(BaseModel):
    """Fast 从自然语言抽取的订单要素；这是 LLM 输出 Schema，不是 HTTP 公共契约。

    只承载 LLM 可见的四个字段；地点是否在地图内、deadline 语义是否合理
    都由服务层对照快照二次校验。字段刻意少而具体——上一步 PEVR 链路的
    实测教训是：交给 LLM 的固定真值字段越多，幻觉失败面越大。
    """

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(min_length=1, max_length=64, description="物料标识，如 MAT-001")
    pickup: str = Field(min_length=1, max_length=16, description="取货点 ID，如 P3")
    dropoff: str = Field(min_length=1, max_length=16, description="交付点 ID，如 S3")
    deadline: int = Field(ge=10, le=3600, description="截止仿真秒；请求未提及时填 120")


class DemoNLDismissRequest(DemoContract):
    """POST /demo/nl/dismiss 请求体：清理演示槽位，不改写任何运行/审批事实。"""

    run_id: str = Field(min_length=1, max_length=64)


class DemoNLRunStatus(DemoContract):
    """自然语言闭环的运行状态；审批信息直接取自 CLI 落盘的 waiting artifact。

    不回传 JWT、命令行或 .env；log_tail 只保留尾部若干行供排障。
    """

    run_id: str
    state: Literal["running", "waiting_approval", "completed", "failed"]
    request: str
    pid: int | None
    exit_code: int | None
    started_at: str | None
    approval_id: str | None
    approval_reason_code: str | None
    approval_expires_at: str | None
    final_status: str | None
    message: str
    log_tail: list[str]


class DemoNLReportSummary(DemoContract):
    """PEVR 最终报告的展示子集：谁批准、模型版本、完成订单与仿真状态。"""

    final_status: str
    summary: str
    completed_order_ids: list[str]
    approval_id: str | None
    principal_subject: str | None
    model_alias: str | None
    simulation_status: str
    simulation_end_time: int = Field(ge=0)


class DemoNLResultResponse(DemoContract):
    """GET /demo/nl/result/{run_id}：PEVR 证据摘要 + 与演示页一致的轨迹子集。

    path_steps 从 dispatch_simulation 工具结果内嵌的 SimulationResult 原样截取，
    与 ``POST /demo/simulate`` 的轨迹语义完全一致，前端复用同一渲染器。
    """

    run_id: str
    report: DemoNLReportSummary
    path_steps: list[DemoPathStep]


__all__ = [
    "DemoContract",
    "DemoLauncherRequest",
    "DemoLauncherStatus",
    "DemoNLDismissRequest",
    "DemoNLReportSummary",
    "DemoNLResultResponse",
    "DemoNLResumeRequest",
    "DemoNLRunRequest",
    "DemoNLRunStatus",
    "DemoPathStep",
    "DemoRouteInfo",
    "DemoSimulateRequest",
    "DemoSimulateResponse",
    "DemoSimulationOutcome",
    "DemoSimulationSummary",
    "DemoWarehouseMap",
]
