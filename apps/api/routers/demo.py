"""演示 UI 扩展路由（用户明确指令要求；scope.md 的「完整前端」P0 排除项差异见交接文档）。

编排约束：浏览器只调这里的 HTTP 接口，绝不直接接触 C++ CLI 或仿真器；
仿真必须先通过 C++ Validator，Validator 拒绝时返回 422 + C++ 证据且不带轨迹。
权限矩阵（2026-08-22 晚调整，用户明确选择本机演示免 Token）：/demo 页面、
地图快照、轻量自然语言下单链与受控启动器匿名可用（启动器仍只允许白名单
脚本 scripts/start_local.ps1 + 至多一个 -StartFast 开关，无法选择其他脚本）；
/demo/simulate 与 /demo/nl/* 完整 PEVR 闭环仍走 JWT（写入口 operator、
读入口 viewer+），发布证据链的安全姿态不变。所有响应不含 .env、JWT 或密码。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from agent.security import Principal
from apps.api.dependencies import (
    get_current_principal,
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
    """托管单文件演示页；页面本身匿名可读，所有数据接口仍走 JWT 门禁。"""

    page_path = Path(__file__).resolve().parents[1] / "static" / "demo.html"
    return HTMLResponse(page_path.read_text(encoding="utf-8"))


@router.get("/demo/warehouse", response_model=DemoWarehouseMap)
def get_demo_warehouse(
    service: WarehouseDemoService = Depends(get_demo_service),
) -> DemoWarehouseMap:
    """匿名读取固定 seed 地图快照；前端不得自行猜测地图。

    2026-08-22 晚起按用户指令放开匿名：地图只是 warehouse_v1 的只读视图，
    不含密钥/订单外数据；免 Token 让本机演示页开箱即用。
    """

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
    principal: Principal = Depends(get_operator_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """operator 提交自然语言订单：拉起 PEVR 闭环，预期停在 waiting_approval。

    需要 Fast 模型在线（先走受控启动器）；审批决定不由本端点做出，
    前端应在 waiting 后调受保护 API 批准，再调 /demo/nl/resume。
    """

    del principal
    try:
        return runner.start(request_text=payload.request)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.get("/demo/nl/active", response_model=DemoNLRunStatus | None)
def get_demo_nl_active(
    principal: Principal = Depends(get_current_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus | None:
    """viewer 及以上角色查询当前槽位；无运行时返回 null（页面刷新恢复用）。"""

    del principal
    return runner.active()


@router.get("/demo/nl/status/{run_id}", response_model=DemoNLRunStatus)
def get_demo_nl_status(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """viewer 及以上角色轮询运行状态；API 重启后仍可从产物重建。"""

    del principal
    try:
        return runner.status(run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/nl/resume", response_model=DemoNLRunStatus)
def post_demo_nl_resume(
    payload: DemoNLResumeRequest,
    principal: Principal = Depends(get_operator_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """operator 在受保护 API 批准之后恢复运行；本端点不签发审批。"""

    del principal
    try:
        return runner.resume(payload.run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.post("/demo/nl/dismiss", response_model=DemoNLRunStatus)
def post_demo_nl_dismiss(
    payload: DemoNLDismissRequest,
    principal: Principal = Depends(get_operator_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLRunStatus:
    """operator 清理演示槽位；不改写 PostgreSQL 中的运行/审批事实。"""

    del principal
    try:
        return runner.dismiss(payload.run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


@router.get("/demo/nl/result/{run_id}", response_model=DemoNLResultResponse)
def get_demo_nl_result(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    runner: ControlledNLRunner = Depends(get_demo_nl_runner),
) -> DemoNLResultResponse:
    """viewer 及以上角色读取 PEVR 证据摘要与轨迹子集；未完成返回 409。"""

    del principal
    try:
        return runner.result(run_id)
    except DemoServiceError as exc:
        _raise_http(exc)


__all__ = ["router"]
