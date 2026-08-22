"""POST /demo/order 轻量自然语言任意下单链的单元测试。

链路：匿名请求 → Fast LLM 抽取四要素（测试用假 Provider 注入）→ 服务端按
快照地点白名单重建动态订单 → 真实 C++ Hungarian/A*/Validator → Python 仿真。
与 /demo/nl/* 的完整 PEVR 闭环不同，本链不写 Ledger、不需审批、不持久化。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.security import JWTAuthenticator
from agent.tools.contracts import UserRole
from agent.tools.cpp_client import CppProgram
from apps.api.dependencies import get_demo_service, get_model_provider
from apps.api.main import create_app
from services.config.settings import AppSettings
from services.demo.contracts import DemoOrderExtraction
from services.demo.service import WarehouseDemoService
from services.model_gateway.exceptions import (
    ModelConnectionError,
    StructuredOutputError,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# 与 test_demo_api.py 同一约定：仿真用例依赖真实 C++ 构建产物。
_CPP_EXECUTABLES_PRESENT = all(
    (REPOSITORY_ROOT / "build" / "cpp" / "services" / "planner_cpp" / program.value).is_file()
    for program in CppProgram
)
requires_cpp = pytest.mark.skipif(
    not _CPP_EXECUTABLES_PRESENT,
    reason="需要真实 C++ build 产物（先运行 scripts/build_cpp.ps1）",
)


class _FakeProvider:
    """按测试剧本返回固定抽取结果或抛出网关异常；不发起任何网络调用。"""

    def __init__(self, *, value: DemoOrderExtraction | None = None, error: Exception | None = None) -> None:
        self._value = value
        self._error = error
        self.calls: list[tuple[object, object]] = []

    def generate_structured(self, messages, response_model, *, max_output_tokens=None, timeout_seconds=None):  # noqa: ANN001, ANN201, ANN202 - 测试替身
        self.calls.append((messages, response_model))
        if self._error is not None:
            raise self._error
        assert self._value is not None
        return SimpleNamespace(value=self._value)


def _make_client(provider: _FakeProvider) -> TestClient:
    """构造关闭模型启动门禁的测试 App，并把假 Provider 注入依赖层。"""

    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)
    app.dependency_overrides[get_model_provider] = lambda: provider
    return TestClient(app)


class _OverloadPlanService(WarehouseDemoService):
    """把第一条路线载荷改成 101kg（上限 100kg），让真实 C++ Validator 拒绝。"""

    def _assemble_plan(self, snapshot, routes, order):  # noqa: ANN001, ANN202 - 测试替身
        plan = super()._assemble_plan(snapshot, routes, order)
        overloaded = plan.routes[0].model_copy(update={"payload_kg": 101.0})
        return plan.model_copy(update={"routes": [overloaded, *plan.routes[1:]]})


@requires_cpp
def test_demo_order_anonymous_happy_path_with_real_cpp() -> None:
    """匿名提交「MAT-001 从 P3 运到 S3」：Validator 通过、仿真完成、轨迹一致。"""

    provider = _FakeProvider(
        value=DemoOrderExtraction(material_id="MAT-001", pickup="P3", dropoff="S3", deadline=120)
    )
    client = _make_client(provider)
    with client:
        # 匿名（无 Authorization 头）即可下单——本机演示免 Token 是用户明确决策。
        response = client.post("/demo/order", json={"request": "请把 MAT-001 从 P3 运到 S3"})
    assert response.status_code == 200, response.text
    body = response.json()

    # LLM 只被问了一次，且抽取模型是 DemoOrderExtraction。
    assert len(provider.calls) == 1
    assert provider.calls[0][1] is DemoOrderExtraction

    summary = body["summary"]
    assert summary["order_id"].startswith("NL-")
    assert summary["validator_valid"] is True
    assert summary["validator_error_count"] == 0
    assert summary["simulation_status"] == "completed"
    assert summary["completed_order_ids"] == [summary["order_id"]]

    steps = body["path_steps"]
    assert len(steps) == summary["path_step_count"] > 0
    assert steps == sorted(steps, key=lambda item: (item["time"], item["amr_id"]))

    # 动态订单的取货/交付点必须与地图 P3/S3 坐标一致，轨迹终点在 S3。
    # WarehouseLocation 序列化为 {id, x, y}；position 只是领域模型的计算属性。
    locations = {
        item["id"]: {"x": item["x"], "y": item["y"]}
        for item in [*body["map"]["pickup_points"], *body["map"]["dropoff_points"]]
    }
    route = body["routes"][0]
    assert route["order_id"] == summary["order_id"]
    pickup_step = next(s for s in steps if s["time"] == route["pickup_time"])
    assert pickup_step["position"] == locations["P3"]
    assert steps[-1]["position"] == locations["S3"]
    assert steps[-1]["time"] == route["dropoff_time"]


def test_demo_order_rejects_blank_and_unknown_fields() -> None:
    """纯空白请求与未知字段都在入口 422，不消耗任何模型调用。"""

    provider = _FakeProvider(value=DemoOrderExtraction(material_id="M", pickup="P1", dropoff="S1", deadline=120))
    client = _make_client(provider)
    with client:
        assert client.post("/demo/order", json={"request": "   "}).status_code == 422
        assert client.post("/demo/order", json={"request": "x", "path_override": []}).status_code == 422
    assert provider.calls == []


def test_demo_order_unknown_location_returns_422() -> None:
    """LLM 抽出地图外地点时：422 + 合法地点清单，不得进入 C++ 链。"""

    provider = _FakeProvider(
        value=DemoOrderExtraction(material_id="MAT-001", pickup="P9", dropoff="S3", deadline=120)
    )
    client = _make_client(provider)
    with client:
        response = client.post("/demo/order", json={"request": "把 MAT-001 从 P9 运到 S3"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_location"
    assert detail["valid_pickup_ids"] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert detail["valid_dropoff_ids"] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert "path_steps" not in response.json()


def test_demo_order_structured_output_failure_returns_422() -> None:
    """Fast 两次输出都不过 Schema：如实 422，不猜测用户意图。"""

    provider = _FakeProvider(error=StructuredOutputError(attempts=2, last_error="bad json"))
    client = _make_client(provider)
    with client:
        response = client.post("/demo/order", json={"request": "随便写点什么"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "nl_extract_failed"


def test_demo_order_model_offline_returns_503() -> None:
    """Fast 未启动：503 fast_model_unavailable，提示先启动模型。"""

    provider = _FakeProvider(error=ModelConnectionError("cannot connect to local model service"))
    client = _make_client(provider)
    with client:
        response = client.post("/demo/order", json={"request": "把 MAT-001 从 P1 运到 S1"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "fast_model_unavailable"
    assert detail["retryable"] is True


@requires_cpp
def test_demo_order_validator_rejection_returns_cpp_evidence() -> None:
    """超载坏计划：真实 C++ Validator 拒绝 → 422 + 证据，绝不返回轨迹。"""

    provider = _FakeProvider(
        value=DemoOrderExtraction(material_id="MAT-001", pickup="P3", dropoff="S3", deadline=120)
    )
    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    app = create_app(settings=settings)
    app.dependency_overrides[get_model_provider] = lambda: provider
    app.dependency_overrides[get_demo_service] = lambda: _OverloadPlanService()
    client = TestClient(app)
    with client:
        response = client.post("/demo/order", json={"request": "请把 MAT-001 从 P3 运到 S3"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "fleet_plan_invalid"
    assert detail["error_count"] > 0
    assert "path_steps" not in response.json()
