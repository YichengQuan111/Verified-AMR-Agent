"""P0-11 到 P0-10 C++ 计划验证器的受控适配层。

仿真器不能把 Python 自己的局部判断当成安全结论。本模块只启动仓库内固定
的 ``fleet_plan_validator_cli.exe``，以 JSON stdin/stdout 通信，禁止
``shell=True``、任意命令和按 ``environment_ref`` 读取文件。业务非法计划与
进程/契约故障分成不同异常，调用方不会误把退出码 0 当成 Validator 通过。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .contracts import SimulationPlan


class ValidatorExecutionError(RuntimeError):
    """固定 Validator 未能返回可解析业务结果。"""


class PlanValidationError(RuntimeError):
    """P0-10 明确判定计划非法；``result`` 保留完整错误证据。"""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        errors = self.result.get("errors", [])
        codes = [str(item.get("code", "unknown")) for item in errors if isinstance(item, dict)]
        suffix = ", ".join(codes[:6]) or "unknown_validation_error"
        super().__init__(f"P0-10 Validator 拒绝计划: {suffix}")


class FleetPlanValidatorClient:
    """通过固定可执行文件调用 P0-10 Validator。

    ``executable`` 仅用于本地构建目录/测试注入，不会被计划 JSON 控制；正常
    默认路径固定在仓库 ``build/cpp/services/planner_cpp`` 下。输出 JSON 会先
    检查 ``status=valid``、``valid=true`` 和空错误列表，再交给仿真循环。
    """

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        repository_root = Path(__file__).resolve().parents[2]
        default_executable = (
            repository_root
            / "build"
            / "cpp"
            / "services"
            / "planner_cpp"
            / "fleet_plan_validator_cli.exe"
        )
        self._repository_root = repository_root
        self._executable = Path(executable) if executable is not None else default_executable
        # P1-1：STL 规约与 agent.tools.cpp_client 使用同一固定文件，仿真前置门禁
        # 因此与工具层跑的是同一份两层验证；缺失时 fail-closed。
        self._stl_specification = repository_root / "config" / "stl" / "fleet_plan_stl_spec.json"
        self._timeout_seconds = timeout_seconds

    @property
    def executable(self) -> Path:
        """返回固定 Validator 路径，供启动检查和审计日志使用。"""

        return self._executable

    @property
    def stl_specification(self) -> Path:
        """返回固定 STL 规约路径；计划 JSON 不能覆盖它。"""

        return self._stl_specification

    def validate(self, plan: SimulationPlan | Mapping[str, Any]) -> dict[str, Any]:
        """调用 Validator 并在未通过时抛出结构化 ``PlanValidationError``。"""

        validated_plan = SimulationPlan.model_validate(plan)
        payload = validated_plan.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        request = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if not self._executable.is_file():
            raise ValidatorExecutionError(
                f"找不到 P0-10 Validator 可执行文件: {self._executable}"
            )
        if not self._stl_specification.is_file():
            raise ValidatorExecutionError(
                f"找不到 P1-1 STL 规约文件: {self._stl_specification}"
            )

        try:
            completed = subprocess.run(
                [str(self._executable), "--validate", "--stl-spec", str(self._stl_specification)],
                input=request,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
                cwd=self._repository_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidatorExecutionError(
                f"P0-10 Validator 超时 ({self._timeout_seconds:g}s)"
            ) from exc
        except OSError as exc:
            raise ValidatorExecutionError(f"无法启动 P0-10 Validator: {exc}") from exc

        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ValidatorExecutionError(
                f"P0-10 Validator 输出不是合法 JSON (exit={completed.returncode}): {detail}"
            ) from exc
        if not isinstance(response, dict):
            raise ValidatorExecutionError("P0-10 Validator 输出必须是 JSON 对象")
        if completed.returncode != 0:
            raise ValidatorExecutionError(
                f"P0-10 Validator 输入/进程失败 (exit={completed.returncode}): {response}"
            )

        # 业务非法计划也以 exit=0 返回；只有同时满足三项才允许进入仿真。
        if (
            response.get("status") != "valid"
            or response.get("valid") is not True
            or response.get("errors") != []
        ):
            raise PlanValidationError(response)
        return response


__all__ = [
    "FleetPlanValidatorClient",
    "PlanValidationError",
    "ValidatorExecutionError",
]
