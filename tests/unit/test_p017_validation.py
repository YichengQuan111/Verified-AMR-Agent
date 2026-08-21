"""P0-17 日志解析和证据报告的正反例。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess

import pytest

from services.validation import VerificationLogParser, VerificationReportGenerator
from agent.tools.verification import (
    FixedVerificationRunner,
    VerificationRunnerError,
    VerificationRunnerTimeout,
)
from agent.tools.schemas import VerificationCaseOutput
from services.validation.contracts import VerificationFailureType


def _digest(value: str) -> str:
    """测试夹具使用合法长度的固定摘要，不把真实日志正文当作报告身份。"""

    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_parser_extracts_failure_type_task_tool_and_log_location() -> None:
    """失败日志必须同时保留稳定分类、相关实体和可跳回行号。"""

    parsed = VerificationLogParser().parse(
        suite_id="p0_12",
        case_id="security",
        stdout="",
        stderr=(
            "FAILED tests/unit/test_tools.py::test_dispatch\n"
            "E   AssertionError: task_id=TASK-42 tool_name=dispatch_simulation\n"
            "E   input_digest=" + "a" * 64 + "\n"
        ),
        exit_code=1,
        duration_ms=21,
        stdout_digest=_digest("stdout"),
        stderr_digest=_digest("stderr"),
    )

    assert parsed.status == "failed"
    assert parsed.failure_type.value == "assertion"
    assert parsed.task_id == "TASK-42"
    assert parsed.tool_name == "dispatch_simulation"
    assert parsed.parameters_digest == "a" * 64
    assert any("stderr#L2" in item.citation for item in parsed.evidence_locations)
    assert any("stderr#L2" in item for item in parsed.evidence_refs)


def test_parser_uses_simulation_evidence_and_timeout_status() -> None:
    """仿真 JSON 的已有引用与固定入口 timeout 都不能被丢成普通文本。"""

    payload = {"simulation_id": "sim-1", "status": "blocked", "evidence_refs": ["event://sim-1/7"]}
    parsed = VerificationLogParser().parse(
        suite_id="p0_simulation",
        case_id="normal",
        stdout=json.dumps(payload),
        stderr="",
        exit_code=1,
        duration_ms=9,
        entry_kind="simulation",
        stdout_digest=_digest("simulation"),
        stderr_digest=_digest("empty"),
    )
    assert parsed.failure_type.value == "simulation_blocked"
    assert "event://sim-1/7" in parsed.evidence_refs
    assert "simulation://sim-1" in parsed.evidence_refs
    assert parsed.tool_name == "dispatch_simulation"

    timed_out = VerificationLogParser().parse(
        suite_id="p0_cpp",
        case_id="all",
        stdout="",
        stderr="",
        exit_code=None,
        duration_ms=100,
        timed_out=True,
        stdout_digest=_digest("timeout-out"),
        stderr_digest=_digest("timeout-err"),
    )
    assert timed_out.status == "timeout"
    assert timed_out.failure_type.value == "timeout"
    assert timed_out.evidence_locations[0].source == "metadata"


def test_report_generator_recomputes_conclusion_and_renders_both_formats() -> None:
    """报告结论来自逐 case 状态，且 JSON/Markdown 共享报告和证据身份。"""

    parser = VerificationLogParser()
    cases = [
        parser.parse(
            suite_id="p0_python",
            case_id="all",
            stdout="12 passed",
            stderr="",
            exit_code=0,
            duration_ms=30,
            stdout_digest=_digest("pass-out"),
            stderr_digest=_digest("pass-err"),
        ),
        parser.parse(
            suite_id="p0_python",
            case_id="all-failed",
            stdout="",
            stderr="ERROR tool_error task_id=TASK-1",
            exit_code=1,
            duration_ms=40,
            stdout_digest=_digest("fail-out"),
            stderr_digest=_digest("fail-err"),
        ),
    ]
    now = datetime.now(timezone.utc)
    report = VerificationReportGenerator().build(
        suite_id="p0_python",
        run_id="RUN-17",
        trace_id="TRACE-17",
        cases=cases,
        started_at=now,
        finished_at=now,
    )
    generator = VerificationReportGenerator()
    json_text = generator.to_json(report)
    markdown = generator.to_markdown(report)

    assert report.status == "failed"
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert report.report_digest in json_text
    assert "TASK-1" in markdown
    assert "tool_error" in markdown
    assert any(item.startswith("report://") for item in report.evidence_refs)

    with pytest.raises(ValueError):
        generator.build(
            suite_id="p0_python",
            run_id=None,
            trace_id=None,
            cases=[],
            started_at=now,
            finished_at=now,
        )


def test_fixed_runner_executes_only_registered_simulation_entry_and_reports_exit() -> None:
    """仿真 adapter 只允许固定 argv，报告状态必须来自真实 CompletedProcess。"""

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_process(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "simulation_id": "sim-p017",
                    "status": "completed",
                    "evidence_refs": ["sim://sim-p017/events"],
                }
            ),
            "",
        )

    runner = FixedVerificationRunner(process_runner=fake_process)
    with pytest.raises(VerificationRunnerError):
        runner.run(
            "p0_simulation",
            run_id="RUN-17",
            trace_id="TRACE-17",
            case_ids=["arbitrary-command"],
            timeout_seconds=1,
        )
    assert calls == []

    output = runner.run(
        "p0_simulation",
        run_id="RUN-17",
        trace_id="TRACE-17",
        case_ids=["all"],
        timeout_seconds=1,
    )

    assert output.status == "passed"
    assert output.trace_id == "TRACE-17"
    assert output.report_id is not None
    assert output.report_digest is not None
    assert output.cases[0].tool_name == "dispatch_simulation"
    assert "sim://sim-p017/events" in output.evidence_refs
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1:3] == ["-m", "services.validation.simulation_entry"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"]


def test_validation_contract_rejects_forged_passed_case() -> None:
    """报告和工具输出都不能把非零退出码改写成 passed。"""

    with pytest.raises(ValueError, match="exit_code=0"):
        VerificationCaseOutput(
            case_id="forged",
            status="passed",
            exit_code=1,
            duration_ms=1,
            stdout_digest=_digest("out"),
            stderr_digest=_digest("err"),
            failure_type=VerificationFailureType.NONE,
        )


def test_fixed_runner_preserves_timeout_case_and_evidence_report() -> None:
    """固定入口超时时仍返回结构化 timeout case，而不是吞掉部分日志。"""

    def timeout_process(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            args,
            kwargs.get("timeout", 1),
            output="partial task_id=TASK-TIMEOUT",
            stderr="timeout tool_name=dispatch_simulation",
        )

    runner = FixedVerificationRunner(process_runner=timeout_process)
    with pytest.raises(VerificationRunnerTimeout) as error:
        runner.run(
            "p0_simulation",
            run_id="RUN-TIMEOUT",
            trace_id="TRACE-TIMEOUT",
            case_ids=["all"],
            timeout_seconds=1,
        )

    output = error.value.output
    assert output is not None
    assert output.status == "timeout"
    assert output.cases[0].status == "timeout"
    assert output.cases[0].failure_type is VerificationFailureType.TIMEOUT
    assert output.cases[0].task_id == "TASK-TIMEOUT"
    assert output.cases[0].tool_name == "dispatch_simulation"
    assert output.cases[0].evidence_refs
