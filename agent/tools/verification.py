"""受控验证套件 runner。

``run_verification_suite`` 只从本文件的固定 suite/case 映射中选择命令。用户输入
不能提供 executable、脚本路径、Shell 片段或任意 pytest 表达式；即使命令失败，
返回也只包含稳定 case 摘要和 digest，不把完整 stderr 当作下一轮工具参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import shutil
import subprocess
import sys
from pathlib import Path
from time import monotonic
from typing import Any, Sequence

from agent.tools.schemas import (
    VerificationCaseOutput,
    VerificationSuiteOutput,
)
from services.validation import VerificationLogParser, VerificationReportGenerator


class VerificationRunnerError(RuntimeError):
    """验证套件选择或执行失败。"""


class VerificationRunnerTimeout(VerificationRunnerError):
    """固定验证命令超时。"""

    def __init__(self, message: str, *, output: VerificationSuiteOutput | None = None) -> None:
        super().__init__(message)
        self.output = output


class VerificationRunnerUnavailable(VerificationRunnerError):
    """固定 Python/CTest/PowerShell 入口不存在或无法启动。"""


@dataclass(frozen=True)
class _FixedCase:
    """固定 case 的可执行 argv；argv 不含任何用户输入。"""

    case_id: str
    argv: tuple[str, ...]
    entry_kind: str = "test"


class FixedVerificationRunner:
    """仅运行仓库内预登记的 Python/CTest/Smoke 入口。"""

    def __init__(self, *, process_runner: Any = subprocess.run) -> None:
        self._repository_root = Path(__file__).resolve().parents[2]
        self._process_runner = process_runner
        self._log_parser = VerificationLogParser()
        self._report_generator = VerificationReportGenerator()

    @staticmethod
    def _resolve_executable(*names: str) -> str:
        """从受信进程环境解析一次绝对路径，不把 PATH/路径暴露成工具参数。"""

        for name in names:
            candidate = shutil.which(name)
            if candidate is None:
                continue
            path = Path(candidate).resolve()
            if path.is_file():
                return str(path)
        raise VerificationRunnerUnavailable(
            f"找不到固定验证程序: {', '.join(names)}"
        )

    def _cases_for_suite(
        self,
        suite_id: str,
        case_ids: list[str] | None,
    ) -> list[_FixedCase]:
        """把 suite/case 白名单解析成固定 argv，未知项在启动前拒绝。"""

        python = str(Path(sys.executable).resolve())
        if suite_id in {"p0_12", "p0-12"}:
            test_file = "tests/unit/test_p012_tools.py"

            def pytest_nodes(*test_names: str) -> tuple[str, ...]:
                """生成固定 pytest node ID；不使用用户可控的 -k 表达式。"""

                return (
                    python,
                    "-m",
                    "pytest",
                    *(f"{test_file}::{name}" for name in test_names),
                    "-q",
                    "-p",
                    "no:cacheprovider",
                )

            fixed = {
                "contract": _FixedCase(
                    "contract",
                    (python, "-m", "pytest", test_file, "-q", "-p", "no:cacheprovider"),
                ),
                "security": _FixedCase(
                    "security",
                    pytest_nodes(
                        "test_registry_contains_exactly_nine_tools_and_explicit_metadata",
                        "test_invalid_arguments_are_rejected_before_handler",
                        "test_viewer_cannot_execute_operator_only_tools",
                        "test_dispatch_rejects_fault_injection_before_simulator",
                        "test_cpp_client_uses_fixed_argv_and_shell_false",
                        "test_verification_runner_resolves_fixed_argv_and_rejects_unknown_case",
                        "test_rag_output_acl_violation_is_fused_before_return",
                    ),
                ),
                "idempotency": _FixedCase(
                    "idempotency",
                    pytest_nodes(
                        "test_duplicate_call_id_returns_cached_effect_without_repeating_request",
                        "test_concurrent_duplicate_call_id_executes_handler_once",
                        "test_call_id_reuse_with_different_payload_is_conflict",
                    ),
                ),
                "cpp_adapter": _FixedCase(
                    "cpp_adapter",
                    pytest_nodes(
                        "test_cpp_client_uses_fixed_argv_and_shell_false",
                        "test_cross_language_summary_models_reject_contradictions",
                        "test_fixed_cpp_and_simulation_tool_chain_is_integrated",
                    ),
                ),
            }
            selected = list(fixed) if case_ids is None else case_ids
            if not selected:
                raise VerificationRunnerError(f"suite {suite_id} 不能选择空 case")
            unknown = sorted(set(selected) - set(fixed))
            if unknown:
                raise VerificationRunnerError(
                    f"suite {suite_id} 不允许 case: {', '.join(unknown)}"
                )
            return [fixed[item] for item in selected]

        if suite_id == "p0_python":
            if case_ids not in (None, [], ["all"]):
                raise VerificationRunnerError("p0_python 只允许 case all")
            return [
                _FixedCase(
                    "all",
                    (python, "-m", "pytest", "-q", "-p", "no:cacheprovider"),
                )
            ]
        if suite_id == "p0_cpp":
            if case_ids not in (None, [], ["all"]):
                raise VerificationRunnerError("p0_cpp 只允许 case all")
            ctest = self._resolve_executable("ctest.exe", "ctest")
            return [
                _FixedCase(
                    "all",
                    (ctest, "--test-dir", "build/cpp", "--output-on-failure"),
                )
            ]
        if suite_id == "p0_smoke":
            if case_ids not in (None, [], ["all"]):
                raise VerificationRunnerError("p0_smoke 只允许 case all")
            powershell = self._resolve_executable("powershell.exe", "pwsh.exe", "pwsh")
            return [
                _FixedCase(
                    "all",
                    (
                        powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(self._repository_root / "scripts" / "run_smoke.ps1"),
                        "-SkipCpp",
                    ),
                )
            ]
        if suite_id in {"p0_simulation", "p0_sim"}:
            if case_ids not in (None, [], ["all"]):
                raise VerificationRunnerError(f"{suite_id} 只允许 case all")
            return [
                _FixedCase(
                    "normal",
                    (python, "-m", "services.validation.simulation_entry"),
                    entry_kind="simulation",
                )
            ]
        raise VerificationRunnerError(f"未知验证套件: {suite_id}")

    def run(
        self,
        suite_id: str,
        *,
        run_id: str | None,
        trace_id: str | None = None,
        case_ids: list[str] | None,
        timeout_seconds: float,
    ) -> VerificationSuiteOutput:
        """执行固定 case，并以摘要形式返回；命令不会经由 shell。"""

        cases = self._cases_for_suite(suite_id, case_ids)
        if timeout_seconds <= 0:
            raise VerificationRunnerError("验证套件 timeout 必须为正数")
        if not cases:
            raise VerificationRunnerError("没有实际登记的验证 case")
        resolved_trace_id = trace_id or self._default_trace_id(suite_id, run_id, cases)
        results: list[VerificationCaseOutput] = []
        started_at = datetime.now(timezone.utc)
        deadline = monotonic() + timeout_seconds
        for case in cases:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise VerificationRunnerTimeout(f"验证套件超时: {suite_id}")
            started = monotonic()
            try:
                completed = self._process_runner(
                    list(case.argv),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=remaining,
                    check=False,
                    shell=False,
                    cwd=str(self._repository_root),
                )
            except subprocess.TimeoutExpired as exc:
                partial_stdout = self._as_text(getattr(exc, "stdout", ""))
                partial_stderr = self._as_text(getattr(exc, "stderr", ""))
                duration_ms = max(0, int(round((monotonic() - started) * 1000)))
                parsed_timeout = self._log_parser.parse(
                    suite_id=suite_id,
                    case_id=case.case_id,
                    stdout=partial_stdout,
                    stderr=partial_stderr,
                    exit_code=None,
                    duration_ms=duration_ms,
                    timed_out=True,
                    entry_kind=case.entry_kind,
                    stdout_digest=sha256(partial_stdout.encode("utf-8")).hexdigest(),
                    stderr_digest=sha256(partial_stderr.encode("utf-8")).hexdigest(),
                )
                timeout_output = self._make_output(
                    suite_id=suite_id,
                    run_id=run_id,
                    trace_id=resolved_trace_id,
                    results=[
                        *results,
                        VerificationCaseOutput.model_validate(
                            parsed_timeout.model_dump(mode="json")
                        ),
                    ],
                    started_at=started_at,
                )
                raise VerificationRunnerTimeout(
                    f"验证 case 超时: {case.case_id}",
                    output=timeout_output,
                ) from exc
            except OSError as exc:
                raise VerificationRunnerUnavailable(
                    f"固定验证 case 无法启动: {case.case_id}"
                ) from exc
            duration_ms = max(0, int(round((monotonic() - started) * 1000)))
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            parsed = self._log_parser.parse(
                suite_id=suite_id,
                case_id=case.case_id,
                stdout=stdout,
                stderr=stderr,
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                entry_kind=case.entry_kind,
                stdout_digest=sha256(stdout.encode("utf-8")).hexdigest(),
                stderr_digest=sha256(stderr.encode("utf-8")).hexdigest(),
            )
            results.append(VerificationCaseOutput.model_validate(parsed.model_dump(mode="json")))

        return self._make_output(
            suite_id=suite_id,
            run_id=run_id,
            trace_id=resolved_trace_id,
            results=results,
            started_at=started_at,
        )

    @staticmethod
    def _as_text(value: Any) -> str:
        """将 subprocess timeout 可能提供的 bytes/text 统一成可审计文本。"""

        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _default_trace_id(suite_id: str, run_id: str | None, cases: list[_FixedCase]) -> str:
        """无上层 Trace 时按固定选择器生成可重放的关联 ID。"""

        raw = f"{suite_id}|{run_id or ''}|{','.join(item.case_id for item in cases)}"
        return f"trace-verification-{sha256(raw.encode('utf-8')).hexdigest()[:24]}"

    def _make_output(
        self,
        *,
        suite_id: str,
        run_id: str | None,
        trace_id: str,
        results: list[VerificationCaseOutput | Any],
        started_at: datetime,
    ) -> VerificationSuiteOutput:
        """用同一组逐 case 真实结果生成工具输出和 JSON/Markdown 报告。"""

        cases = [
            item
            if isinstance(item, VerificationCaseOutput)
            else VerificationCaseOutput.model_validate(item.model_dump(mode="json"))
            for item in results
        ]
        report = self._report_generator.build(
            suite_id=suite_id,
            run_id=run_id,
            trace_id=trace_id,
            cases=cases,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        return VerificationSuiteOutput(
            suite_id=suite_id,
            run_id=run_id,
            trace_id=trace_id,
            status=report.status,
            case_count=report.case_count,
            passed_count=report.passed_count,
            failed_count=report.failed_count,
            cases=cases,
            report_id=report.report_id,
            report_digest=report.report_digest,
            report_json=self._report_generator.to_json(report),
            report_markdown=self._report_generator.to_markdown(report),
            evidence_refs=report.evidence_refs,
        )


__all__ = [
    "FixedVerificationRunner",
    "VerificationRunnerError",
    "VerificationRunnerTimeout",
    "VerificationRunnerUnavailable",
]
