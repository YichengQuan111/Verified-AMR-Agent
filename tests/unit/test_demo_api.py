"""演示 UI 扩展测试：/demo/* 后端契约、权限门禁与真实 C++ 链路。

约定：
- 仿真类用例必须真实调用 build/ 下的三个 C++ exe（Hungarian/A*/Validator），
  不允许全程 Fake；缺少 build 产物时这些用例跳过（权限类用例不受影响）。
- 坏计划用例通过 ``_assemble_plan`` 子类钩子注入「超载 1kg」的计划，
  Validator 仍是真实 C++ 进程，拒绝证据来自 C++ 而非 Python 编造。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.security import JWTAuthenticator
from agent.tools import UserRole
from agent.tools.cpp_client import CppProgram
from apps.api.dependencies import get_demo_launcher, get_demo_service
from apps.api.main import create_app
from services.config.settings import AppSettings
from services.demo import (
    ControlledLauncher,
    DemoServiceError,
    DemoSimulateRequest,
    WarehouseDemoService,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# 仿真用例依赖真实 C++ 构建产物；run_smoke.ps1 会先构建，因此官方流程恒为真。
_CPP_EXECUTABLES_PRESENT = all(
    (REPOSITORY_ROOT / "build" / "cpp" / "services" / "planner_cpp" / program.value).is_file()
    for program in CppProgram
)
requires_cpp = pytest.mark.skipif(
    not _CPP_EXECUTABLES_PRESENT,
    reason="需要真实 C++ build 产物（先运行 scripts/build_cpp.ps1）",
)


def _make_app_and_tokens() -> tuple[TestClient, dict[str, str], dict[str, str]]:
    """构造关闭模型启动门禁的测试 App，并签发 viewer/operator 两个真实 JWT。"""

    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)
    authenticator = JWTAuthenticator(
        settings.security.jwt_secret.get_secret_value(),
        issuer=settings.security.issuer,
        audience=settings.security.audience,
    )
    viewer = {
        "Authorization": f"Bearer {authenticator.issue_token(subject='demo-viewer', role=UserRole.VIEWER)}"
    }
    operator = {
        "Authorization": f"Bearer {authenticator.issue_token(subject='demo-operator', role=UserRole.OPERATOR)}"
    }
    return TestClient(app), viewer, operator


class _OverloadPlanService(WarehouseDemoService):
    """把第一条路线载荷改成 101kg（上限 100kg），让真实 C++ Validator 拒绝。"""

    def _assemble_plan(self, snapshot, routes, order):  # noqa: ANN001, ANN202 - 测试替身
        plan = super()._assemble_plan(snapshot, routes, order)
        overloaded = plan.routes[0].model_copy(update={"payload_kg": 101.0})
        return plan.model_copy(update={"routes": [overloaded, *plan.routes[1:]]})


def test_demo_warehouse_map_anonymous_and_matches_eval_hard() -> None:
    """地图接口：匿名可读；字段与在线评测加难图 + 演示固定通道障碍一致。"""

    from evals.p018.hard_map import (
        HARD_ENVIRONMENT_REF,
        RACK_XS,
        build_hard_warehouse_map,
        extra_obstacles_for_demo,
    )

    client, _, _ = _make_app_and_tokens()
    with client:
        response = client.get("/demo/warehouse")
    assert response.status_code == 200
    body = response.json()
    assert body["width"] == 30
    assert body["height"] == 20
    assert body["environment_ref"] == HARD_ENVIRONMENT_REF
    assert body["map_id"] == "warehouse_v1_hard"
    assert body["version"] == 3
    obstacle_xy = {(c["x"], c["y"]) for c in body["obstacles"]}
    assert (15, 0) in obstacle_xy and (15, 1) in obstacle_xy
    assert any(x in RACK_XS for x, _y in obstacle_xy)
    warehouse = build_hard_warehouse_map()
    extras = {(c.x, c.y) for c in extra_obstacles_for_demo(warehouse)}
    temp = {(c["x"], c["y"]) for c in body["temporary_blocked_cells"]}
    assert (16, 19) in temp
    assert extras <= temp
    aisle = {a["aisle_id"]: a for a in body["narrow_aisles"]}["NA-01"]
    assert {(c["x"], c["y"]) for c in aisle["cells"]} == {(10, 17), (11, 17), (12, 17)}
    assert body["blocked_edges"] == [{"from": {"x": 5, "y": 0}, "to": {"x": 6, "y": 0}}]
    assert body["one_way_edges"] == [{"from": {"x": 20, "y": 18}, "to": {"x": 21, "y": 18}}]
    assert [p["id"] for p in body["pickup_points"]] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert [p["id"] for p in body["dropoff_points"]] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert [p["id"] for p in body["charging_stations"]] == ["C1", "C2"]
    amrs = {a["amr_id"]: a for a in body["amrs"]}
    assert len(amrs) == 4
    assert (amrs["AMR-01"]["position"], amrs["AMR-01"]["battery"]) == ({"x": 1, "y": 2}, 100.0)
    assert [o["order_id"] for o in body["orders"]] == ["ORDER-001", "ORDER-002", "ORDER-003"]


def test_demo_page_is_anonymous_and_static() -> None:
    """演示页本身匿名可读（纯静态页面，不含任何数据/密钥）。"""

    client, _, _ = _make_app_and_tokens()
    with client:
        response = client.get("/demo")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "/demo/warehouse" in response.text
    assert "/demo/nl/run" in response.text
    assert "POST /demo/order" not in response.text


@requires_cpp
def test_demo_simulate_order_001_happy_path_with_real_cpp() -> None:
    """operator 跑 ORDER-001：Validator 通过、仿真完成、轨迹与事件流逐格一致。"""

    client, _, operator = _make_app_and_tokens()
    with client:
        response = client.post("/demo/simulate", json={}, headers=operator)
    assert response.status_code == 200, response.text
    body = response.json()

    summary = body["summary"]
    assert summary["validator_valid"] is True
    assert summary["validator_error_count"] == 0
    assert summary["validator_ruleset_version"] == "p0-10.v1"
    assert summary["simulation_status"] == "completed"
    assert summary["completed_order_ids"] == ["ORDER-001"]

    # 轨迹子集必须按 (time, amr_id) 排序且全部来自 amr.path_step 事件。
    steps = body["path_steps"]
    assert len(steps) == summary["path_step_count"] > 0
    assert steps == sorted(steps, key=lambda item: (item["time"], item["amr_id"]))
    event_index = {
        (event["time"], event["amr_id"]): event
        for event in body["result"]["events"]
        if event["event_type"] == "amr.path_step"
    }
    assert len(event_index) == len(steps)
    for step in steps:
        event = event_index[(step["time"], step["amr_id"])]
        assert event["payload"]["position"] == step["position"]
        assert event["payload"]["action"] == step["action"]

    # AMR-01：起点 (1,2) → P1 (2,3) → S3 (27,9)，与 routes 对照一致。
    assert [route["amr_id"] for route in body["routes"]] == ["AMR-01"]
    route = body["routes"][0]
    assert route["order_id"] == "ORDER-001"
    assert steps[0]["action"] == "start"
    assert steps[0]["position"] == {"x": 1, "y": 2}
    assert steps[-1]["position"] == {"x": 27, "y": 9}
    assert steps[-1]["time"] == route["dropoff_time"]
    pickup_step = next(s for s in steps if s["time"] == route["pickup_time"])
    assert pickup_step["position"] == {"x": 2, "y": 3}

    # 响应内嵌地图，前端无需猜测；simulation_id 是确定性演示标识。
    assert body["map"]["width"] == 30
    assert body["result"]["simulation_id"].startswith("demo-")
    assert body["result"]["status"] == "completed"


def test_demo_simulate_requires_operator_role() -> None:
    """匿名 401、viewer 403；只有 operator 能触发仿真。"""

    client, viewer, _ = _make_app_and_tokens()
    with client:
        assert client.post("/demo/simulate", json={}).status_code == 401
        response = client.post("/demo/simulate", json={}, headers=viewer)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "OPERATOR_REQUIRED"


def test_demo_simulate_rejects_unknown_fields() -> None:
    """未知字段一律 422，防止前端把猜测字段当成契约。"""

    client, _, operator = _make_app_and_tokens()
    with client:
        response = client.post(
            "/demo/simulate",
            json={"order_id": "ORDER-001", "path_override": []},
            headers=operator,
        )
    assert response.status_code == 422


def test_demo_simulate_unknown_order_returns_404_without_trajectory() -> None:
    """不在种子快照中的订单：404 + 可选订单清单，不得返回轨迹。"""

    client, _, operator = _make_app_and_tokens()
    with client:
        response = client.post(
            "/demo/simulate", json={"order_id": "ORDER-999"}, headers=operator
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "demo_order_not_found"
    assert detail["available_order_ids"] == ["ORDER-001", "ORDER-002", "ORDER-003"]
    assert "path_steps" not in response.json()


@requires_cpp
def test_demo_simulate_validator_rejection_returns_cpp_evidence() -> None:
    """超载 1kg 的坏计划：真实 C++ Validator 拒绝 → 422 + C++ 证据，无轨迹。"""

    client, _, operator = _make_app_and_tokens()
    # 依赖覆盖必须返回实例而不是类本身：FastAPI 会把类 __init__ 的注入参数
    # 误当成请求参数建模（DefaultWarehouseSnapshotProvider 不是合法字段类型）。
    client.app.dependency_overrides[get_demo_service] = lambda: _OverloadPlanService()
    with client:
        response = client.post("/demo/simulate", json={}, headers=operator)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "fleet_plan_invalid"
    assert detail["error_count"] >= 1
    assert detail["ruleset_version"] == "p0-10.v1"
    codes = [item["code"] for item in detail["errors"]]
    assert "load_capacity_exceeded" in codes
    # 拒绝响应不得携带任何轨迹字段，前端无假路径可画。
    assert "path_steps" not in response.json()
    assert "result" not in response.json()


@requires_cpp
def test_demo_simulate_order_003_dependency_rejected_by_real_cpp() -> None:
    """ORDER-003 依赖 ORDER-001（种子中未完成）：C++ 分配器拒绝 → 422 证据。"""

    client, _, operator = _make_app_and_tokens()
    with client:
        response = client.post(
            "/demo/simulate", json={"order_id": "ORDER-003"}, headers=operator
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "demo_cpp_request_rejected"
    assert "ORDER-001" in detail["message"]
    assert "path_steps" not in response.json()


class _FakeProcess:
    """受控启动器测试用的假进程句柄：记录 argv，永不真实拉起进程。"""

    _next_pid = 9000

    def __init__(self, argv: list[str], *, log_path: Path, cwd: Path) -> None:
        _FakeProcess._next_pid += 1
        self._pid = _FakeProcess._next_pid
        self.argv = argv
        self.log_path = log_path
        self.cwd = cwd
        self.exit_code: int | None = None

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.exit_code


def _make_fake_launcher(started: list[_FakeProcess]) -> ControlledLauncher:
    """构造注入假 starter 的启动器；脚本存在性检查仍指向真实仓库脚本。"""

    def starter(argv: list[str], *, log_path: Path, cwd: Path) -> _FakeProcess:
        process = _FakeProcess(argv, log_path=log_path, cwd=cwd)
        started.append(process)
        return process

    return ControlledLauncher(process_starter=starter)


def test_demo_launcher_anonymous_and_whitelist() -> None:
    """启动器：2026-08-22 晚起匿名可用（本机演示免 Token）；白名单 argv 不变。"""

    client, _, _ = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    launcher = _make_fake_launcher(started)
    client.app.dependency_overrides[get_demo_launcher] = lambda: launcher
    with client:
        response = client.post("/demo/launcher/start", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "running"
        assert body["script"] == "scripts/start_local.ps1"

        # 白名单证据：argv 引用真实仓库脚本绝对路径，且默认不带 -StartFast。
        # Shell 优先 pwsh.exe（仓库脚本含无 BOM 中文注释，PS 5.1 会解析失败）。
        assert len(started) == 1
        argv = started[0].argv
        assert Path(argv[0]).name.lower() in {"pwsh.exe", "powershell.exe"}
        assert "-StartFast" not in argv
        script_arg = Path(argv[argv.index("-File") + 1])
        assert script_arg == (REPOSITORY_ROOT / "scripts" / "start_local.ps1").resolve()
        assert script_arg.is_file()

        # 运行中重复启动是幂等的，不叠加第二个进程。
        again = client.post("/demo/launcher/start", json={})
        assert again.status_code == 200
        assert again.json()["state"] == "running"
        assert len(started) == 1

        # 状态接口同样匿名可读。
        status = client.get("/demo/launcher/status")
        assert status.status_code == 200
        assert status.json()["state"] == "running"


def test_demo_launcher_rejects_script_selection_and_start_fast_flag() -> None:
    """请求体无法选择其他脚本；start_fast=true 只能追加固定 -StartFast。"""

    client, _, _ = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    client.app.dependency_overrides[get_demo_launcher] = lambda: _make_fake_launcher(started)
    with client:
        # 试图注入其他脚本路径：未知字段被 Pydantic 拒绝。
        rejected = client.post(
            "/demo/launcher/start",
            json={"script": "evil.ps1"},
        )
        assert rejected.status_code == 422
        assert not started

        response = client.post(
            "/demo/launcher/start", json={"start_fast": True}
        )
        assert response.status_code == 200
        assert response.json()["start_fast"] is True
        assert started[0].argv[-1] == "-StartFast"
        # Smart 没有入口：argv 只有固定脚本 + 至多一个 -StartFast 开关。
        assert all("smart" not in part.lower() for part in started[0].argv)


def test_demo_launcher_unavailable_off_windows() -> None:
    """非 Windows 主机：启动器明确报 unavailable，而不是拼接跨平台命令。"""

    launcher = ControlledLauncher(os_name="posix")
    with pytest.raises(DemoServiceError) as captured:
        launcher.start(start_fast=False)
    assert captured.value.status_code == 503
    assert captured.value.code == "demo_launcher_unavailable"
    assert launcher.status().state == "unavailable"


def test_demo_launcher_failed_exit_code_is_reported() -> None:
    """脚本非零退出时状态为 failed，并携带退出码供排障。"""

    started: list[_FakeProcess] = []
    launcher = _make_fake_launcher(started)
    launcher.start(start_fast=False)
    started[0].exit_code = 3
    status = launcher.status()
    assert status.state == "failed"
    assert status.exit_code == 3


@requires_cpp
def test_demo_service_runs_without_http_layer() -> None:
    """服务层直连真实 C++：HTTP 之外也能复现演示链路（脚本共用此入口）。"""

    # 显式默认订单，保证脚本与 HTTP 默认行为一致。
    response = WarehouseDemoService().run_simulation(DemoSimulateRequest())
    assert response.summary.simulation_status == "completed"
    assert response.summary.completed_order_ids == ["ORDER-001"]
