"""自然语言闭环演示（/demo/nl/*）测试：白名单 argv、单并发槽位、审批衔接与结果解析。

约定：
- 全程用假进程句柄，不真实拉起 PEVR CLI（真实 Fast 链路属在线验收，不进单测）。
- waiting/完成事实通过手写 CLI 产物 JSON 模拟，形状与 run_p013_e2e.py 落盘格式一致；
  结果解析用的 SimulationResult 由真实 Pydantic 模型构造，不是手写假 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.security import JWTAuthenticator
from agent.tools import UserRole
from apps.api.dependencies import get_demo_nl_runner
from apps.api.main import create_app
from services.amr_simulator import (
    SimulationEvent,
    SimulationOrderState,
    SimulationOrderStatus,
    SimulationResult,
    SimulationStatus,
)
from services.config.settings import AppSettings
from services.demo import ControlledNLRunner
from domains.amr_warehouse import AMRState, GridPosition

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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
        "Authorization": f"Bearer {authenticator.issue_token(subject='nl-viewer', role=UserRole.VIEWER)}"
    }
    operator = {
        "Authorization": f"Bearer {authenticator.issue_token(subject='nl-operator', role=UserRole.OPERATOR)}"
    }
    return TestClient(app), viewer, operator


class _FakeProcess:
    """假进程句柄：记录 argv，由测试手动设置退出码，永不真实拉起。"""

    _next_pid = 7000

    def __init__(self, argv: list[str], *, log_path: Path, cwd: Path) -> None:
        _FakeProcess._next_pid += 1
        self._pid = _FakeProcess._next_pid
        self.argv = argv
        self.log_path = log_path
        self.cwd = cwd
        self.exit_code: int | None = None
        self.terminated = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -1


def _make_runner(tmp_path: Path, started: list[_FakeProcess]) -> ControlledNLRunner:
    """注入假 starter 与固定 token 工厂；脚本存在性检查仍指向真实仓库。"""

    def starter(argv: list[str], *, log_path: Path, cwd: Path) -> _FakeProcess:
        process = _FakeProcess(argv, log_path=log_path, cwd=cwd)
        started.append(process)
        return process

    return ControlledNLRunner(
        token_factory=lambda: "test-operator-token",
        tmp_dir=tmp_path,
        process_starter=starter,
        python_exe="python-test",
    )


def _waiting_artifact(run_id: str, approval_id: str) -> dict:
    """与 run_p013_e2e.py 退出码 3 时落盘的 waiting payload 同形。"""

    return {
        "schema_version": "p013-secure-cli.v1",
        "run_id": run_id,
        "status": "waiting_approval",
        "principal_subject": "demo-nl-runner",
        "principal_role": "operator",
        "interrupt": {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": "task-dispatch",
            "approval_id": approval_id,
            "checkpoint_id": "ckpt-1",
            "reason_code": "high_risk_write",
            "status": "waiting_approval",
            "created_at": "2026-08-22T00:00:00Z",
            "expires_at": "2026-08-22T01:00:00Z",
        },
        "checkpoint_sequence": 5,
        "evidence_refs": [f"approval://{approval_id}/pending", "checkpoint://ckpt-1"],
    }


def _simulation_fixture() -> SimulationResult:
    """用真实模型构造最小合法 SimulationResult（含两个 path_step 事件）。"""

    return SimulationResult(
        simulation_id="sim-nl-test",
        seed=7,
        status=SimulationStatus.COMPLETED,
        start_time=0,
        end_time=2,
        validation_result={"valid": True},
        amrs=[
            AMRState(
                amr_id="AMR-01",
                position=GridPosition(x=2, y=2),
                heading=90,
                battery=99.0,
                load=1.0,
                task_status="IDLE",
                health_status="HEALTHY",
                connection_status="ONLINE",
            )
        ],
        orders=[
            SimulationOrderState(
                order_id="ORDER-001",
                status=SimulationOrderStatus.COMPLETED,
                assigned_amr_id="AMR-01",
                payload_kg=1.0,
                pickup_time=1,
                dropoff_time=2,
                blocked_reason=None,
            )
        ],
        workstations=[],
        charging_stations=[],
        observations=[],
        events=[
            SimulationEvent(
                event_id="e1",
                simulation_id="sim-nl-test",
                time=0,
                event_type="amr.path_step",
                severity="info",
                amr_id="AMR-01",
                order_id="ORDER-001",
                payload={
                    "action": "start",
                    "position": {"x": 1, "y": 2},
                    "heading": 0,
                    "battery": 100.0,
                    "g_cost": 0.0,
                },
            ),
            SimulationEvent(
                event_id="e2",
                simulation_id="sim-nl-test",
                time=1,
                event_type="amr.path_step",
                severity="info",
                amr_id="AMR-01",
                order_id="ORDER-001",
                payload={
                    "action": "move",
                    "position": {"x": 2, "y": 2},
                    "heading": 90,
                    "battery": 99.0,
                    "g_cost": 1.0,
                },
            ),
        ],
    )


def _final_artifact(run_id: str) -> dict:
    """与 CLI 退出码 0 时落盘的 PEVRRunResult 同形（只保留 runner 读取的字段）。"""

    return {
        "report": {
            "final_status": "completed",
            "summary": "订单 ORDER-001 已完成。",
            "completed_order_ids": ["ORDER-001"],
            "approval_id": "appr-1",
            "principal_subject": "demo-nl-runner",
            "model": {"served_alias": "qwen3.6-fast"},
            "metrics": {"simulation_status": "completed", "simulation_end_time": 2},
        },
        "tool_results": [
            {
                "tool_name": "dispatch_simulation",
                "status": "success",
                "output": _simulation_fixture().model_dump(mode="json"),
            }
        ],
    }


def _write_output(tmp_path: Path, run_id: str, payload: dict) -> None:
    (tmp_path / f"demo_nl_{run_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_nl_auth_matrix(tmp_path: Path) -> None:
    """匿名 401；viewer 只读（active 200），写入口（run/resume/dismiss）403。"""

    client, viewer, operator = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    client.app.dependency_overrides[get_demo_nl_runner] = lambda: runner
    with client:
        assert client.post("/demo/nl/run", json={"request": "x"}).status_code == 401
        assert client.post("/demo/nl/resume", json={"run_id": "r"}).status_code == 401
        assert client.post("/demo/nl/dismiss", json={"run_id": "r"}).status_code == 401
        assert client.get("/demo/nl/active").status_code == 401
        assert client.post("/demo/nl/run", json={"request": "x"}, headers=viewer).status_code == 403
        assert client.post("/demo/nl/resume", json={"run_id": "r"}, headers=viewer).status_code == 403
        assert client.post("/demo/nl/dismiss", json={"run_id": "r"}, headers=viewer).status_code == 403
        active = client.get("/demo/nl/active", headers=viewer)
        assert active.status_code == 200
        assert active.json() is None


def test_nl_run_rejects_unknown_fields(tmp_path: Path) -> None:
    """未知字段一律 422；纯空白 request 也在 API 入口拒绝，不占用槽位。"""

    client, _, operator = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    client.app.dependency_overrides[get_demo_nl_runner] = lambda: runner
    with client:
        assert client.post(
            "/demo/nl/run", json={"request": "x", "script": "evil"}, headers=operator
        ).status_code == 422
        assert client.post(
            "/demo/nl/run", json={"request": "   "}, headers=operator
        ).status_code == 422
        assert not started


def test_nl_happy_path_waiting_approve_resume_result(tmp_path: Path) -> None:
    """完整状态机：running → waiting_approval → resume → completed → 轨迹结果。"""

    client, _, operator = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    client.app.dependency_overrides[get_demo_nl_runner] = lambda: runner

    with client:
        # 1) 提交：单并发槽位占用，argv 白名单证据。
        resp = client.post(
            "/demo/nl/run",
            json={"request": "请把 MAT-001 从 P1 运到 S3。"},
            headers=operator,
        )
        assert resp.status_code == 200, resp.text
        st = resp.json()
        assert st["state"] == "running"
        run_id = st["run_id"]
        assert run_id.startswith("demo-nl-")

        argv = started[0].argv
        assert argv[0] == "python-test"
        assert Path(argv[1]) == (REPOSITORY_ROOT / "scripts" / "run_p013_e2e.py").resolve()
        assert argv[argv.index("--request") + 1] == "请把 MAT-001 从 P1 运到 S3。"
        assert argv[argv.index("--run-id") + 1] == run_id
        token_file = Path(argv[argv.index("--jwt-token-file") + 1])
        assert token_file.read_text(encoding="utf-8") == "test-operator-token"
        assert "--approve-and-resume" not in argv  # 本模块永不自动批准

        # 2) 运行中再次提交 → 409。
        busy = client.post("/demo/nl/run", json={"request": "再来一单"}, headers=operator)
        assert busy.status_code == 409
        assert busy.json()["detail"]["code"] == "demo_nl_busy"

        # 3) CLI 退出码 3 + waiting 产物 → waiting_approval，审批信息透出。
        _write_output(tmp_path, run_id, _waiting_artifact(run_id, "appr-1"))
        started[0].exit_code = 3
        st = client.get(f"/demo/nl/status/{run_id}", headers=operator).json()
        assert st["state"] == "waiting_approval"
        assert st["approval_id"] == "appr-1"
        assert st["approval_reason_code"] == "high_risk_write"

        # 4) 恢复：argv 必须携带 --resume-approved appr-1 且复用同一 run_id/原文。
        resume = client.post("/demo/nl/resume", json={"run_id": run_id}, headers=operator)
        assert resume.status_code == 200, resume.text
        assert len(started) == 2
        argv2 = started[1].argv
        assert argv2[argv2.index("--resume-approved") + 1] == "appr-1"
        assert argv2[argv2.index("--run-id") + 1] == run_id
        assert argv2[argv2.index("--request") + 1] == "请把 MAT-001 从 P1 运到 S3。"

        # 5) CLI 退出码 0 + 最终产物 → completed；结果含真实轨迹子集。
        _write_output(tmp_path, run_id, _final_artifact(run_id))
        started[1].exit_code = 0
        st = client.get(f"/demo/nl/status/{run_id}", headers=operator).json()
        assert st["state"] == "completed"
        assert st["final_status"] == "completed"

        result = client.get(f"/demo/nl/result/{run_id}", headers=operator)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["run_id"] == run_id
        assert body["report"]["completed_order_ids"] == ["ORDER-001"]
        assert body["report"]["approval_id"] == "appr-1"
        assert body["report"]["model_alias"] == "qwen3.6-fast"
        assert body["report"]["simulation_end_time"] == 2
        steps = body["path_steps"]
        assert [(s["time"], s["action"], s["position"]) for s in steps] == [
            (0, "start", {"x": 1, "y": 2}),
            (1, "move", {"x": 2, "y": 2}),
        ]

        # 6) dismiss 清理槽位后可开新运行。
        dismiss = client.post("/demo/nl/dismiss", json={"run_id": run_id}, headers=operator)
        assert dismiss.status_code == 200
        assert client.get("/demo/nl/active", headers=operator).json() is None
        again = client.post("/demo/nl/run", json={"request": "下一单"}, headers=operator)
        assert again.status_code == 200


def test_nl_resume_requires_waiting(tmp_path: Path) -> None:
    """running 状态恢复 → 409；未知 run_id → 404。"""

    client, _, operator = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    client.app.dependency_overrides[get_demo_nl_runner] = lambda: runner
    with client:
        resp = client.post("/demo/nl/run", json={"request": "订单"}, headers=operator)
        run_id = resp.json()["run_id"]
        resume = client.post("/demo/nl/resume", json={"run_id": run_id}, headers=operator)
        assert resume.status_code == 409
        assert resume.json()["detail"]["code"] == "demo_nl_not_waiting"
        assert client.get("/demo/nl/status/demo-nl-unknown", headers=operator).status_code == 404
        result = client.get(f"/demo/nl/result/{run_id}", headers=operator)
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "demo_nl_not_completed"


def test_nl_failed_exit_reports_log_tail(tmp_path: Path) -> None:
    """CLI 非 0/3 退出 → failed，状态携带日志尾部供排障。"""

    client, _, operator = _make_app_and_tokens()
    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    client.app.dependency_overrides[get_demo_nl_runner] = lambda: runner
    with client:
        resp = client.post("/demo/nl/run", json={"request": "订单"}, headers=operator)
        run_id = resp.json()["run_id"]
        started[0].log_path.write_text("line1\n模型连接失败\n", encoding="utf-8")
        started[0].exit_code = 1
        st = client.get(f"/demo/nl/status/{run_id}", headers=operator).json()
        assert st["state"] == "failed"
        assert st["exit_code"] == 1
        assert "模型连接失败" in st["log_tail"]


def test_nl_status_recovers_from_artifacts_after_restart(tmp_path: Path) -> None:
    """API 重启（新 runner、无进程句柄）后，waiting 状态仍能从产物重建。"""

    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    st = runner.start(request_text="请把 MAT-001 从 P1 运到 S3。")
    run_id = st.run_id
    _write_output(tmp_path, run_id, _waiting_artifact(run_id, "appr-9"))
    started[0].exit_code = 3

    # 模拟 API 重启：全新 runner 实例共享同一 tmp_dir，没有进程句柄。
    runner2 = _make_runner(tmp_path, [])
    recovered = runner2.status(run_id)
    assert recovered.state == "waiting_approval"
    assert recovered.approval_id == "appr-9"
    assert recovered.request == "请把 MAT-001 从 P1 运到 S3。"

    # 恢复同样可用：新 runner 能从 meta + waiting 产物重建 argv。
    resumed = runner2.resume(run_id)
    assert resumed.state in {"running", "waiting_approval"}


def test_nl_dismiss_terminates_running_process(tmp_path: Path) -> None:
    """dismiss 运行中的进程：先 terminate 再清槽位。"""

    started: list[_FakeProcess] = []
    runner = _make_runner(tmp_path, started)
    st = runner.start(request_text="订单")
    dismissed = runner.dismiss(st.run_id)
    assert started[0].terminated is True
    assert dismissed.state == "failed"
    assert runner.active() is None
