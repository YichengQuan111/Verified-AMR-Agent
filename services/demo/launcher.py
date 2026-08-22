"""本机受控启动器：浏览器唯一允许的进程出口是白名单脚本 scripts/start_local.ps1。

设计约束（对应用户指令「浏览器不能直接 docker / 不能任意 Shell」）：

- 脚本路径由服务端从仓库布局固定推导，请求体没有任何字符串字段能影响 argv；
  前端唯一可控的是一个布尔开关（是否追加 ``-StartFast``），Smart 永远不可达。
- 进程异步拉起、输出重定向到 gitignore 的 ``tmp/demo_launcher.log``，HTTP 请求
  不会被数分钟的 Docker/模型启动阻塞；状态与日志尾部通过 status() 轮询读取。
- 仅在 Windows 主机可用；Docker/Linux API 容器内没有 docker.exe/脚本宿主环境，
  此时返回 unavailable 而不是尝试拼接跨平台命令。
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from services.demo.contracts import DemoLauncherStatus
from services.demo.service import DemoServiceError


class ProcessStarterProtocol(Protocol):
    """subprocess.Popen 的最小注入接口；测试用假句柄验证白名单，不真起进程。"""

    def __call__(
        self,
        argv: list[str],
        *,
        log_path: Path,
        cwd: Path,
    ) -> "LauncherProcessProtocol": ...


class LauncherProcessProtocol(Protocol):
    """启动器需要的最小进程句柄：pid + poll()。"""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...


class ControlledLauncher:
    """scripts/start_local.ps1 的单脚本白名单启动器。"""

    ALLOWED_SCRIPT_NAME = "start_local.ps1"
    LOG_FILE_NAME = "demo_launcher.log"
    LOG_TAIL_LINES = 20

    def __init__(
        self,
        *,
        scripts_dir: str | Path | None = None,
        tmp_dir: str | Path | None = None,
        process_starter: ProcessStarterProtocol | None = None,
        os_name: str | None = None,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        self._script_path = (
            Path(scripts_dir) if scripts_dir is not None else repository_root / "scripts"
        ) / self.ALLOWED_SCRIPT_NAME
        self._repository_root = repository_root
        self._tmp_dir = Path(tmp_dir) if tmp_dir is not None else repository_root / "tmp"
        self._process_starter = process_starter or self._start_process
        # os_name 注入点只服务测试；生产读取真实平台。
        self._os_name = os_name if os_name is not None else os.name
        self._process: LauncherProcessProtocol | None = None
        self._started_at: datetime | None = None
        self._start_fast = False

    def build_argv(self, *, start_fast: bool) -> list[str]:
        """构造固定 argv；这是全仓库唯一允许从 HTTP 触发的命令行。

        Shell 选择镜像 start_local.ps1 自身的策略：优先 pwsh（PowerShell 7 按
        UTF-8 解析脚本），回退 Windows PowerShell 5.1。仓库脚本含中文注释且
        无 BOM 时 5.1 会解析失败，因此能用 pwsh 就不用 5.1。
        """

        argv = [
            self._resolve_shell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._script_path),
        ]
        if start_fast:
            # 对应脚本内已有的安全启动器门禁（制品哈希、密钥检查）；Smart 无入口。
            argv.append("-StartFast")
        return argv

    @staticmethod
    def _resolve_shell() -> str:
        """优先 pwsh.exe；测试可注入 os_name 但 shell 选择仍从真实 PATH 读取。"""

        import shutil

        return shutil.which("pwsh.exe") or "powershell.exe"

    def start(self, *, start_fast: bool) -> DemoLauncherStatus:
        """拉起白名单脚本；已在运行时幂等返回当前状态，不叠加第二个进程。"""

        if self._os_name != "nt":
            raise DemoServiceError(
                "受控启动器只在 Windows 主机可用；容器内请使用 compose 手动启动",
                status_code=503,
                code="demo_launcher_unavailable",
            )
        if not self._script_path.is_file():
            raise DemoServiceError(
                f"白名单脚本不存在: {self._script_path}",
                status_code=503,
                code="demo_launcher_unavailable",
                evidence={"script": self._script_path.name},
            )
        if self._process is not None and self._process.poll() is None:
            # 幂等语义：重复点击按钮返回同一进程状态，避免叠加多个启动脚本
            # 互相抢占 8000/8080 端口。
            return self.status(message="启动进程已在运行，忽略重复请求")

        log_path = self._tmp_dir / self.LOG_FILE_NAME
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            # 每次启动截断旧日志，状态接口只回传本次启动的尾部。
            log_path.write_text("", encoding="utf-8")
            self._process = self._process_starter(
                self.build_argv(start_fast=start_fast),
                log_path=log_path,
                cwd=self._repository_root,
            )
        except OSError as exc:
            self._process = None
            raise DemoServiceError(
                f"无法启动白名单脚本: {exc}",
                status_code=503,
                code="demo_launcher_start_failed",
                evidence={"error_type": type(exc).__name__},
            ) from exc
        self._started_at = datetime.now(timezone.utc)
        self._start_fast = start_fast
        return self.status()

    def status(self, *, message: str | None = None) -> DemoLauncherStatus:
        """读取当前状态；不泄漏 .env、JWT 或命令行以外的环境信息。"""

        if self._os_name != "nt":
            return DemoLauncherStatus(
                state="unavailable",
                script=f"scripts/{self.ALLOWED_SCRIPT_NAME}",
                start_fast=False,
                pid=None,
                exit_code=None,
                started_at=None,
                message="受控启动器只在 Windows 主机可用",
                log_tail=[],
            )
        if self._process is None:
            return DemoLauncherStatus(
                state="idle",
                script=f"scripts/{self.ALLOWED_SCRIPT_NAME}",
                start_fast=False,
                pid=None,
                exit_code=None,
                started_at=None,
                message=message or "尚未通过演示页发起启动",
                log_tail=self._read_log_tail(),
            )
        exit_code = self._process.poll()
        if exit_code is None:
            state = "running"
            default_message = "启动脚本运行中；前端应继续轮询 GET /health"
        elif exit_code == 0:
            state = "exited"
            default_message = "启动脚本已完成（退出码 0）"
        else:
            state = "failed"
            default_message = f"启动脚本失败（退出码 {exit_code}），详见日志尾部"
        return DemoLauncherStatus(
            state=state,
            script=f"scripts/{self.ALLOWED_SCRIPT_NAME}",
            start_fast=self._start_fast,
            pid=self._process.pid,
            exit_code=exit_code,
            started_at=(
                self._started_at.isoformat() if self._started_at is not None else None
            ),
            message=message or default_message,
            log_tail=self._read_log_tail(),
        )

    def _read_log_tail(self) -> list[str]:
        """读取启动日志尾部；文件可能尚不存在，失败时返回空列表而非异常。"""

        log_path = self._tmp_dir / self.LOG_FILE_NAME
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-self.LOG_TAIL_LINES :]

    @staticmethod
    def _start_process(
        argv: list[str],
        *,
        log_path: Path,
        cwd: Path,
    ) -> LauncherProcessProtocol:
        """生产进程出口：无 Shell、输出落盘、新进程组避免信号串扰。

        句柄用独立函数持有文件对象，保证 Popen 返回后日志文件不被 GC 关闭。
        """

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
        # Popen 复制了文件描述符，父进程可以立即关闭自己的句柄。
        log_handle.close()
        return process


__all__ = ["ControlledLauncher", "LauncherProcessProtocol", "ProcessStarterProtocol"]
