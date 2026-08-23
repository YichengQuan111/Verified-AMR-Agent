"""演示 UI 扩展路由（用户明确指令要求；scope.md 的「完整前端」P0 排除项差异见交接文档）。

编排约束：浏览器只调这里的 HTTP 接口，绝不直接接触 C++ CLI 或仿真器；
仿真必须先通过 C++ Validator，Validator 拒绝时返回 422 + C++ 证据且不带轨迹。
权限矩阵（2026-08-22 用户指令：演示闭环完全不考虑安全）：/demo 页面、
地图快照、轻量自然语言下单链、受控启动器、完整 PEVR 闭环 ``/demo/nl/*``
以及 HITL 审批/拒绝端点一律匿名。``POST /demo/order`` 接口保留给既有测试；
演示页唯一提交入口走 ``/demo/nl/run``。所有响应不含 .env、JWT 或密码。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from agent.security import Principal
from apps.api.dependencies import (
    get_demo_launcher,
    get_demo_nl_runner,
    get_demo_service,
    get_model_provider,
    get_operator_principal,
)
from services.demo import (
    ControlledLauncher,
    ControlledNLRunner,
    DemoLauncherRequest,
    DemoLauncherStatus,
    DemoNLDismissRequest,
    DemoNLOrderRequest,
    DemoNLResultResponse,
    DemoNLResumeRequest,
    DemoNLRunRequest,
    DemoNLRunStatus,
    DemoServiceError,
    DemoSimulateRequest,
    DemoSimulateResponse,
    DemoWarehouseMap,
    WarehouseDemoService,
)
from services.model_gateway.protocols import ModelProviderProtocol

router = APIRouter(tags=["demo"])


def _raise_http(exc: DemoServiceError) -> None:
    """把服务层稳定错误原样映射成 HTTP detail（code/message/证据）。"""

    raise HTTPException(status_code=exc.status_code, detail=exc.to_detail()) from exc


@router.get(
    "/demo",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def demo_page() -> HTMLResponse:
    """托管单文件演示页；页面与闭环接口均匿名（2026-08-22 用户指令）。"""

    page_path = Path(__file__).resolve().parents[1] / "static" / "demo.html"
    return HTMLResponse(
        page_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/demo/warehouse", response_model=DemoWarehouseMap)
def get_demo_warehouse(
    response: Response,
    service: WarehouseDemoService = Depends(get_demo_service),
) -> DemoWarehouseMap:
    """匿名读取评测加难地图快照；前端不得自行猜测地图。

    2026-08-22 晚起按用户指令放开匿名。2026-08-23 起返回
    ``warehouse_v1@eval-hard``（货架墙 + 2 格全走廊通道障碍），不是生产空旷图。
    ``Cache-Control: no-store`` 避免浏览器把改图前的 JSON 一直用下去。
    """

    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_warehouse_map()
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/order", response_model=DemoSimulateResponse)
def post_demo_order(
    payload: DemoNLOrderRequest,
    service: WarehouseDemoService = Depends(get_demo_service),
    model_provider: ModelProviderProtocol = Depends(get_model_provider),
) -> DemoSimulateResponse:
    """匿名任意自然语言下单（轻量演示链）：LLM 抽取 → 动态订单 → C++ 链 → 仿真。

    与 /demo/nl/run 的完整 PEVR 闭环刻意不同：本端点不写 Effect Ledger、
    不需要 HITL 审批、不持久化历史、不作发布证据；Validator 拒绝时同样
    返回 422 + C++ 证据且不带轨迹。Fast 离线时返回 503 fast_model_unavailable。
    """

    try:
        return service.run_nl_order(payload.request, model_provider=model_provider)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/simulate", response_model=DemoSimulateResponse)
def post_demo_simulate(
    payload: DemoSimulateRequest,
    principal: Principal = Depends(get_operator_principal),
    service: WarehouseDemoService = Depends(get_demo_service),
) -> DemoSimulateResponse:
    """operator 触发演示仿真：C++ 计划 → Validator 门禁 → Python AMRSimulator。"""

    del principal
    try:
        return service.run_simulation(payload)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/launcher/start", response_model=DemoLauncherStatus)
def post_demo_launcher_start(
    payload: DemoLauncherRequest,
    launcher: ControlledLauncher = Depends(get_demo_launcher),
) -> DemoLauncherStatus:
    """匿名触发受控启动（2026-08-22 晚起，用户明确选择本机演示免 Token）。

    安全边界不变：脚本路径固定为仓库内 scripts/start_local.ps1，请求体只有
    一个 start_fast 布尔开关，未知字段 422；Smart 没有入口。仅限本机回环
    演示使用——API 若绑定非回环地址，应恢复 operator 门禁。
    """

    try:
        return launcher.start(start_fast=payload.start_fast)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.get("/demo/launcher/status", response_model=DemoLauncherStatus)
def get_demo_launcher_status(
    launcher: ControlledLauncher = Depends(get_demo_launcher),
) -> DemoLauncherStatus:
    """匿名轮询启动状态；不暴露密钥与完整命令行之外的信息。"""

    return launcher.status()


@router.post("/demo/nl/run", response_model=DemoNLRunStatus)
def post_demo_nl_run(
    payload: DemoNLRunRequest,
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
    service: WarehouseDemoService = Depends(get_demo_service),
    model_provider: ModelProviderProtocol = Depends(get_model_provider),
) -> DemoNLRunStatus:
    """匿名提交自然语言订单：先服务端重建动态订单，再拉起完整 PEVR 闭环。

    抽取失败（地点不在白名单 / Schema 失败）在占槽前返回 422，不启动 CLI。
    审批在 waiting_approval 后由页面匿名调用 HITL 接口完成。
    """

    try:
        order = service.prepare_dynamic_order(payload.request, model_provider=model_provider)
        return runner.start(request_text=payload.request, order=order)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.get("/demo/nl/active", response_model=DemoNLRunStatus | None)
def get_demo_nl_active(
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus | None:
    """匿名查询当前槽位；无运行时返回 null。"""

    return runner.active()


@router.get("/demo/nl/status/{run_id}", response_model=DemoNLRunStatus)
def get_demo_nl_status(
    run_id: str,
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """匿名轮询运行状态；API 重启后仍可从产物重建。"""

    try:
        return runner.status(run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/nl/resume", response_model=DemoNLRunStatus)
def post_demo_nl_resume(
    payload: DemoNLResumeRequest,
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """匿名恢复；调用前页面须已匿名批准 HITL，本端点不签发审批。"""

    try:
        return runner.resume(payload.run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/nl/dismiss", response_model=DemoNLRunStatus)
def post_demo_nl_dismiss(
    payload: DemoNLDismissRequest,
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """匿名清理演示槽位；不改写 PostgreSQL 中的运行/审批事实。"""

    try:
        return runner.dismiss(payload.run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.get("/demo/nl/result/{run_id}", response_model=DemoNLResultResponse)
def get_demo_nl_result(
    run_id: str,
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLResultResponse:
    """匿名读取 PEVR 证据摘要与轨迹子集；未完成返回 409。"""

    try:
        return runner.result(run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


__all__ = ["router"]
