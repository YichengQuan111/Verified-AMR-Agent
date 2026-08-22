"""自然语言下单的受控运行器：演示页到 P0-13 PEVR 闭环的唯一桥梁。

设计约束（对应「浏览器不能任意 Shell / 必须走已测入口」）：

- 全仓库只允许本模块从 HTTP 触发 ``scripts/run_p013_e2e.py``；python 解释器取
  API 进程自身的 ``sys.executable``，脚本路径由仓库布局固定推导，请求体里的
  自然语言只作为 ``--request`` 的独立 argv 元素传递（无 Shell，无注入面）。
- 每次拉起子进程前由服务端现铸一枚短期 operator JWT 写入 ``tmp/demo_nl_*.jwt``；
  浏览器永远看不到这枚令牌，令牌也不进日志、不进响应。
- 单并发槽位：本地只有一个 Fast 模型实例，同时跑多个 PEVR 闭环只会互相拖垮；
  运行中或等待审批时拒绝新运行（409），dismiss 可清理终态/放弃等待。
- 状态可从产物重建：CLI 把 waiting/完成事实落盘到 ``tmp/demo_nl_*.json``，
  meta 边车保存原始请求文本；API 进程重启后 status/resume 仍然可用。
- 审批决定不在本模块内发生：浏览器 operator 调受保护 API 签发 grant 后，
  本模块只用 ``--resume-approved`` 恢复，与 P0-20 实测的 HITL 路径一致。
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.amr_simulator import SimulationResult
from services.demo.contracts import (
    DemoNLReportSummary,
    DemoNLResultResponse,
    DemoNLRunStatus,
)
from services.demo.launcher import LauncherProcessProtocol, ProcessStarterProtocol
from services.demo.service import DemoServiceError, WarehouseDemoService


class ControlledNLRunner:
    """run_p013_e2e.py 的单槽位白名单运行器。"""

    SCRIPT_NAME = "run_p013_e2e.py"
    RUN_ID_PREFIX = "demo-nl-"
    LOG_TAIL_LINES = 20

    def __init__(
        self,
        *,
        token_factory: Callable[[], str],
        scripts_dir: str | Path | None = None,
        tmp_dir: str | Path | None = None,
        process_starter: ProcessStarterProtocol | None = None,
        python_exe: str | None = None,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        self._script_path = (
            Path(scripts_dir) if scripts_dir is not None else repository_root / "scripts"
        ) / self.SCRIPT_NAME
        self._repository_root = repository_root
        self._tmp_dir = Path(tmp_dir) if tmp_dir is not None else repository_root / "tmp"
        self._process_starter = process_starter or self._start_process
        # 缺省跟随 API 进程解释器，保证子进程与 API 同环境（依赖、C++ 路径一致）。
        self._python_exe = python_exe or sys.executable
        # token_factory 由依赖层注入（app.state.authenticator.issue_token）；
        # 每次拉起现铸，避免把长期令牌落盘或复用浏览器令牌。
        self._token_factory = token_factory
        self._process: LauncherProcessProtocol | None = None
        self._current_run_id: str | None = None
        self._started_at: datetime | None = None

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def start(self, *, request_text: str) -> DemoNLRunStatus:
        """拉起首次 PEVR 运行；预期以退出码 3 停在 waiting_approval。"""

        self._ensure_script_available()
        current = self._current_status()
        if current is not None and current.state in {"running", "waiting_approval"}:
            raise DemoServiceError(
                f"已有自然语言运行处于 {current.state}（run_id={current.run_id}），请先完成或 dismiss",
                status_code=409,
                code="demo_nl_busy",
                evidence={"run_id": current.run_id, "state": current.state},
            )
        run_id = f"{self.RUN_ID_PREFIX}{uuid.uuid4().hex[:12]}"
        self._write_meta(run_id, request_text)
        token_path = self._mint_token_file(run_id)
        argv = self._build_argv(run_id=run_id, request_text=request_text, token_path=token_path)
        self._spawn(run_id, argv)
        return self.status(run_id, message="PEVR 闭环已启动；LLM 理解与规划中")

    def status(self, run_id: str, *, message: str | None = None) -> DemoNLRunStatus:
        """读取运行状态；进程句柄丢失时从落盘产物重建。"""

        meta = self._read_meta(run_id)
        output = self._read_output(run_id)
        process = self._process if self._current_run_id == run_id else None
        exit_code = process.poll() if process is not None else None

        if process is not None and exit_code is None:
            state = "running"
            default_message = "PEVR 闭环运行中（LLM 理解 → 规划 → C++ 校验）"
        elif output is not None and output.get("status") == "waiting_approval":
            state = "waiting_approval"
            default_message = "计划在 dispatch 前暂停，等待 operator 审批"
        elif exit_code == 0 and output is not None and "report" in output:
            state = "completed"
            default_message = "闭环完成"
        elif output is not None and "report" in output:
            # 无进程句柄（API 重启）但产物完整：以产物为准。
            state = "completed"
            default_message = "闭环完成"
        elif exit_code is None and output is None:
            if meta is not None:
                # API 重启后进程句柄丢失、CLI 产物尚未落盘：子进程很可能仍在跑，
                # 按 running 呈现并等待产物出现，而不是误报 404 丢掉这次运行。
                state = "running"
                default_message = "运行产物尚未落盘（可能刚从 API 重启恢复），继续等待"
            else:
                raise DemoServiceError(
                    f"未知或已清理的自然语言运行: {run_id}",
                    status_code=404,
                    code="demo_nl_not_found",
                    evidence={"run_id": run_id},
                )
        else:
            state = "failed"
            default_message = "PEVR 闭环失败，详见日志尾部"

        interrupt = output.get("interrupt") if output else None
        report = output.get("report") if output else None
        return DemoNLRunStatus(
            run_id=run_id,
            state=state,  # type: ignore[arg-type]
            request=meta.get("request", "") if meta else "",
            pid=process.pid if process is not None else None,
            exit_code=exit_code,
            started_at=self._started_at.isoformat()
            if (self._started_at is not None and self._current_run_id == run_id)
            else (meta.get("started_at") if meta else None),
            approval_id=interrupt.get("approval_id") if interrupt else None,
            approval_reason_code=interrupt.get("reason_code") if interrupt else None,
            approval_expires_at=interrupt.get("expires_at") if interrupt else None,
            final_status=report.get("final_status") if report else None,
            message=message or default_message,
            log_tail=self._read_log_tail(run_id),
        )

    def active(self) -> DemoNLRunStatus | None:
        """返回当前槽位状态；无运行时返回 None（页面据此决定是否展示进度卡）。"""

        return self._current_status()

    def resume(self, run_id: str) -> DemoNLRunStatus:
        """用 ``--resume-approved`` 恢复；grant 必须已由受保护 API 签发。"""

        self._ensure_script_available()
        current = self.status(run_id)
        if current.state != "waiting_approval":
            raise DemoServiceError(
                f"运行 {run_id} 当前状态为 {current.state}，只有 waiting_approval 可恢复",
                status_code=409,
                code="demo_nl_not_waiting",
                evidence={"run_id": run_id, "state": current.state},
            )
        approval_id = current.approval_id
        if not approval_id:
            raise DemoServiceError(
                f"运行 {run_id} 的 waiting 产物缺少 approval_id",
                status_code=500,
                code="demo_nl_artifact_corrupt",
                evidence={"run_id": run_id},
            )
        meta = self._read_meta(run_id) or {}
        request_text = meta.get("request")
        if not request_text:
            raise DemoServiceError(
                f"运行 {run_id} 的 meta 产物缺失，无法安全恢复",
                status_code=500,
                code="demo_nl_artifact_corrupt",
                evidence={"run_id": run_id},
            )
        token_path = self._mint_token_file(run_id)
        argv = self._build_argv(
            run_id=run_id,
            request_text=request_text,
            token_path=token_path,
            resume_approval_id=approval_id,
        )
        self._spawn(run_id, argv)
        return self.status(run_id, message="已批准，正在从 Checkpoint 恢复执行")

    def dismiss(self, run_id: str) -> DemoNLRunStatus:
        """清理槽位：终态直接清除；running 先 terminate 再清除。

        只影响演示槽位与进程；PostgreSQL 中的 run/审批事实保持原样，
        rejected 终态依旧不可恢复，dismiss 不等于撤销审批。
        """

        current = self.status(run_id)
        process = self._process if self._current_run_id == run_id else None
        if current.state == "running" and process is not None:
            process.terminate()
        if self._current_run_id == run_id:
            self._process = None
            self._current_run_id = None
            self._started_at = None
        return DemoNLRunStatus(
            run_id=run_id,
            state=current.state if current.state != "running" else "failed",
            request=current.request,
            pid=None,
            exit_code=current.exit_code,
            started_at=current.started_at,
            approval_id=current.approval_id,
            approval_reason_code=current.approval_reason_code,
            approval_expires_at=current.approval_expires_at,
            final_status=current.final_status,
            message="演示槽位已清理；运行/审批事实仍保留在 PostgreSQL",
            log_tail=current.log_tail,
        )

    def result(self, run_id: str) -> DemoNLResultResponse:
        """提取 PEVR 证据摘要与轨迹子集；只有 completed 才能取结果。"""

        current = self.status(run_id)
        if current.state != "completed":
            raise DemoServiceError(
                f"运行 {run_id} 尚未完成（{current.state}），没有可取的结果",
                status_code=409,
                code="demo_nl_not_completed",
                evidence={"run_id": run_id, "state": current.state},
            )
        output = self._read_output(run_id)
        assert output is not None  # completed 状态保证产物存在
        report = output["report"]
        simulation = self._extract_simulation(run_id, output)
        path_steps = WarehouseDemoService._extract_path_steps(simulation)
        metrics = report.get("metrics") or {}
        model = report.get("model") or {}
        return DemoNLResultResponse(
            run_id=run_id,
            report=DemoNLReportSummary(
                final_status=report["final_status"],
                summary=report["summary"],
                completed_order_ids=list(report.get("completed_order_ids", [])),
                approval_id=report.get("approval_id"),
                principal_subject=report.get("principal_subject"),
                model_alias=model.get("served_alias"),
                simulation_status=str(metrics.get("simulation_status", simulation.status.value)),
                simulation_end_time=int(metrics.get("simulation_end_time", simulation.end_time)),
            ),
            path_steps=path_steps,
        )

    # ------------------------------------------------------------------
    # argv 与进程
    # ------------------------------------------------------------------

    def _build_argv(
        self,
        *,
        run_id: str,
        request_text: str,
        token_path: Path,
        resume_approval_id: str | None = None,
    ) -> list[str]:
        """构造固定 argv；自然语言只是 ``--request`` 的独立元素，无 Shell 拼接。"""

        argv = [
            self._python_exe,
            str(self._script_path),
            "--request",
            request_text,
            "--run-id",
            run_id,
            "--output",
            str(self._output_path(run_id)),
            "--jwt-token-file",
            str(token_path),
        ]
        if resume_approval_id is not None:
            # 恢复路径对应 P0-20 实测的「API 批准 + CLI --resume-approved」组合；
            # 本模块不提供 --approve-and-resume，审批决定必须留在受保护 API。
            argv.extend(["--resume-approved", resume_approval_id])
        return argv

    def _spawn(self, run_id: str, argv: list[str]) -> None:
        """异步拉起 CLI；输出落盘到 per-run 日志，HTTP 不被 LLM 延迟阻塞。"""

        log_path = self._log_path(run_id)
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            self._process = self._process_starter(
                argv,
                log_path=log_path,
                cwd=self._repository_root,
            )
        except OSError as exc:
            self._process = None
            raise DemoServiceError(
                f"无法启动 PEVR CLI: {exc}",
                status_code=503,
                code="demo_nl_start_failed",
                evidence={"error_type": type(exc).__name__},
            ) from exc
        self._current_run_id = run_id
        self._started_at = datetime.now(timezone.utc)

    @staticmethod
    def _start_process(
        argv: list[str],
        *,
        log_path: Path,
        cwd: Path,
    ) -> LauncherProcessProtocol:
        """生产进程出口：无 Shell、输出落盘、新进程组避免信号串扰。"""

        log_handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except BaseException:
            log_handle.close()
            raise
        log_handle.close()
        return process

    # ------------------------------------------------------------------
    # 产物读写
    # ------------------------------------------------------------------

    def _output_path(self, run_id: str) -> Path:
        return self._tmp_dir / f"demo_nl_{run_id}.json"

    def _meta_path(self, run_id: str) -> Path:
        return self._tmp_dir / f"demo_nl_{run_id}.meta.json"

    def _log_path(self, run_id: str) -> Path:
        return self._tmp_dir / f"demo_nl_{run_id}.log"

    def _token_path(self, run_id: str) -> Path:
        return self._tmp_dir / f"demo_nl_{run_id}.jwt"

    def _mint_token_file(self, run_id: str) -> Path:
        """现铸短期 operator JWT 并落盘；文件只被子进程读取，不进响应。"""

        token = self._token_factory()
        path = self._token_path(run_id)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        return path

    def _write_meta(self, run_id: str, request_text: str) -> None:
        """meta 边车保存原始请求文本，供 API 重启后 resume 重建 argv。"""

        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path(run_id).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "request": request_text,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _read_meta(self, run_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._meta_path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read_output(self, run_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._output_path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read_log_tail(self, run_id: str) -> list[str]:
        try:
            lines = self._log_path(run_id).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []
        return lines[-self.LOG_TAIL_LINES :]

    def _current_status(self) -> DemoNLRunStatus | None:
        if self._current_run_id is None:
            return None
        try:
            return self.status(self._current_run_id)
        except DemoServiceError:
            return None

    def _ensure_script_available(self) -> None:
        if not self._script_path.is_file():
            raise DemoServiceError(
                f"白名单脚本不存在: {self._script_path}",
                status_code=503,
                code="demo_nl_unavailable",
                evidence={"script": self._script_path.name},
            )

    @staticmethod
    def _extract_simulation(run_id: str, output: dict[str, Any]) -> SimulationResult:
        """从 PEVR 结果中定位 dispatch_simulation 的完整 SimulationResult。

        PEVRRunResult.tool_results 原样携带每次工具调用的 output；dispatch 的
        output 就是 SimulationResult（registry 里 DispatchSimulationOutput 即其别名）。
        找不到说明闭环证据不完整，按 500 处理而不是伪造轨迹。
        """

        for item in output.get("tool_results", []):
            if item.get("tool_name") == "dispatch_simulation" and item.get("status") == "success":
                return SimulationResult.model_validate(item["output"])
        raise DemoServiceError(
            f"运行 {run_id} 的结果缺少 dispatch_simulation 成功证据",
            status_code=500,
            code="demo_nl_result_incomplete",
            evidence={"run_id": run_id},
        )


__all__ = ["ControlledNLRunner"]
