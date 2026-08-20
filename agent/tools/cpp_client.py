"""固定 C++ JSON stdin/stdout 适配器。

P0-08～P0-10 的 CLI 是跨语言安全边界：Python 只从内部枚举选择三个已编译
程序和固定参数，不接受调用方传入 executable、工作目录或命令字符串。请求在
进入 subprocess 前已经过 Pydantic/业务快照校验；进程超时、契约退出码和非法
JSON 被转换成稳定异常，调用方不会把“进程退出 0”误认为计划合法。
"""

from __future__ import annotations

import json
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from agent.tools.contracts import ToolErrorCategory


class CppProgram(str, Enum):
    """仓库内允许被工具层调用的固定可执行文件。"""

    TASK_ALLOCATOR = "task_allocator_cli.exe"
    ROUTE_PLANNER = "route_planner_cli.exe"
    FLEET_PLAN_VALIDATOR = "fleet_plan_validator_cli.exe"


class CppAdapterError(RuntimeError):
    """固定 C++ 边界失败；携带可直接映射到 ToolError 的稳定分类。"""

    def __init__(
        self,
        message: str,
        *,
        category: ToolErrorCategory,
        code: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})


class ProcessRunnerProtocol(Protocol):
    """subprocess.run 的最小注入接口；生产默认实现仍是标准库函数。"""

    def __call__(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]: ...


class FixedCppJsonClient:
    """调用固定 C++ CLI 的无 Shell 客户端。"""

    MAX_INPUT_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        *,
        process_runner: ProcessRunnerProtocol | None = None,
    ) -> None:
        self._repository_root = Path(__file__).resolve().parents[2]
        self._build_root = self._repository_root / "build" / "cpp" / "services" / "planner_cpp"
        self._process_runner = process_runner or subprocess.run

    @property
    def repository_root(self) -> Path:
        """返回固定工作目录；该值不从工具参数或用户输入读取。"""

        return self._repository_root

    def executable_path(self, program: CppProgram) -> Path:
        """取得固定程序路径，并拒绝把枚举之外的字符串当作命令。"""

        return self._build_root / program.value

    def _run(
        self,
        program: CppProgram,
        payload: Mapping[str, Any],
        *,
        arguments: Sequence[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """在执行前完成 JSON 大小/有限数校验，再以 argv + stdin 调用 CLI。"""

        if timeout_seconds <= 0:
            raise CppAdapterError(
                "C++ 调用超时必须为正数",
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="invalid_cpp_timeout",
                retryable=False,
            )
        try:
            request = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise CppAdapterError(
                f"C++ 请求不是有限 JSON: {exc}",
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="invalid_cpp_request",
                retryable=False,
            ) from exc
        if len(request.encode("utf-8")) > self.MAX_INPUT_BYTES:
            raise CppAdapterError(
                "C++ 请求超过 4 MiB stdin 上限",
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="cpp_request_too_large",
                retryable=False,
            )

        executable = self.executable_path(program)
        # 注入 runner 只用于契约测试，因此允许在没有本地 build 产物的机器上
        # 验证 argv/shell 门禁；生产默认 subprocess.run 仍必须先看到固定 exe。
        if self._process_runner is subprocess.run and not executable.is_file():
            raise CppAdapterError(
                f"固定 C++ 可执行文件不存在: {executable}",
                category=ToolErrorCategory.UNAVAILABLE,
                code="cpp_executable_unavailable",
                retryable=True,
                details={"program": program.value, "path": str(executable)},
            )

        argv = [str(executable), *arguments]
        try:
            completed = self._process_runner(
                argv,
                input=request,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout_seconds,
                check=False,
                shell=False,
                cwd=str(self._repository_root),
            )
        except subprocess.TimeoutExpired as exc:
            raise CppAdapterError(
                f"固定 C++ 程序超时: {program.value}",
                category=ToolErrorCategory.TIMEOUT,
                code="cpp_timeout",
                retryable=True,
                details={"program": program.value, "timeout_seconds": timeout_seconds},
            ) from exc
        except OSError as exc:
            raise CppAdapterError(
                f"无法启动固定 C++ 程序: {program.value}",
                category=ToolErrorCategory.UNAVAILABLE,
                code="cpp_process_start_failed",
                retryable=True,
                details={"program": program.value, "error_type": type(exc).__name__},
            ) from exc

        try:
            response = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CppAdapterError(
                f"固定 C++ 输出不是合法 JSON: {program.value}",
                category=ToolErrorCategory.INTERNAL,
                code="cpp_invalid_stdout",
                retryable=False,
                details={
                    "program": program.value,
                    "exit_code": completed.returncode,
                    "stderr": (completed.stderr or "")[-1000:],
                },
            ) from exc
        if not isinstance(response, dict):
            raise CppAdapterError(
                "固定 C++ 输出必须是 JSON 对象",
                category=ToolErrorCategory.INTERNAL,
                code="cpp_stdout_not_object",
                retryable=False,
                details={"program": program.value, "exit_code": completed.returncode},
            )
        if completed.returncode == 0:
            return response

        error_payload = response.get("error")
        error_code = (
            str(error_payload.get("code"))
            if isinstance(error_payload, dict) and error_payload.get("code")
            else "cpp_process_failed"
        )
        error_message = (
            str(error_payload.get("message"))
            if isinstance(error_payload, dict) and error_payload.get("message")
            else "固定 C++ 程序返回非零退出码"
        )
        category = (
            ToolErrorCategory.INVALID_ARGUMENT
            if completed.returncode == 2
            else ToolErrorCategory.INTERNAL
        )
        raise CppAdapterError(
            error_message,
            category=category,
            code=error_code,
            retryable=False,
            details={
                "program": program.value,
                "exit_code": completed.returncode,
                "stderr": (completed.stderr or "")[-1000:],
            },
        )

    def allocate(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """只调用生产 Hungarian；nearest_idle 不作为隐式失败回退。"""

        return self._run(
            CppProgram.TASK_ALLOCATOR,
            payload,
            arguments=("--algorithm", "hungarian"),
            timeout_seconds=timeout_seconds,
        )

    def plan_routes(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """只调用生产 A*；Dijkstra 需由显式独立测试入口调用。"""

        return self._run(
            CppProgram.ROUTE_PLANNER,
            payload,
            arguments=("--algorithm", "astar"),
            timeout_seconds=timeout_seconds,
        )

    def validate_plan(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """调用 P0-10 Validator；status=invalid 仍由调用方读取为安全证据。"""

        return self._run(
            CppProgram.FLEET_PLAN_VALIDATOR,
            payload,
            arguments=("--validate",),
            timeout_seconds=timeout_seconds,
        )


__all__ = ["CppAdapterError", "CppProgram", "FixedCppJsonClient", "ProcessRunnerProtocol"]
