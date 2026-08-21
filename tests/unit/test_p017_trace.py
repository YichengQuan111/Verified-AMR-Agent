"""P0-17 Trace 契约、失败索引和验证事件适配器测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.runtime.trace import TraceCollector, TraceError, TraceEvent
from agent.tools.contracts import ToolError, ToolErrorCategory, ToolName, ToolResult, ToolResultStatus
from services.validation.contracts import (
    ParsedVerificationCase,
    VerificationFailureType,
)


def _time_pair(milliseconds: int = 125) -> tuple[datetime, datetime]:
    """统一构造带时区时间，确保 Trace 延迟可被精确重算。"""

    started = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    return started, started + timedelta(milliseconds=milliseconds)


def test_trace_event_carries_model_prompt_token_latency_and_evidence() -> None:
    """模型事件必须同时暴露版本、Prompt、Token、延迟、摘要和证据引用。"""

    started, finished = _time_pair()
    collector = TraceCollector(trace_id="trace-p017", run_id="run-p017")

    event = collector.emit(
        event_type="model",
        status="completed",
        node="understand",
        task_id="TASK-001",
        model_version="qwen3.6-fast",
        prompt_id="understand_goal",
        prompt_version="p0-05.v1",
        input_tokens=80,
        output_tokens=20,
        started_at=started,
        finished_at=finished,
        parameters_digest="a" * 64,
        evidence_refs=["prompt://understand_goal/p0-05.v1"],
    )

    assert event.trace_id == "trace-p017"
    assert event.run_id == "run-p017"
    assert event.task == "TASK-001"
    assert event.model == "qwen3.6-fast"
    assert event.total_tokens == 100
    assert event.latency_ms == 125
    assert collector.events[0].evidence_refs == ["prompt://understand_goal/p0-05.v1"]


def test_trace_rejects_unlocated_failure_and_non_contiguous_events() -> None:
    """失败没有错误索引或序号跳跃时必须 fail-closed。"""

    started, finished = _time_pair(0)
    with pytest.raises(ValidationError, match="必须携带 error"):
        TraceEvent(
            trace_id="trace-p017",
            run_id="run-p017",
            sequence=1,
            event_type="tool",
            status="failed",
            node="execute",
            tool_name=ToolName.ALLOCATE_TASKS.value,
            latency_ms=0,
            started_at=started,
            finished_at=finished,
        )

    collector = TraceCollector(trace_id="trace-p017", run_id="run-p017")
    collector.emit(
        event_type="node",
        status="completed",
        node="guard",
        started_at=started,
        finished_at=finished,
    )
    with pytest.raises(ValueError, match="sequence"):
        collector.append(
            {
                "trace_id": "trace-p017",
                "run_id": "run-p017",
                "sequence": 3,
                "event_type": "node",
                "status": "completed",
                "node": "understand",
                "latency_ms": 0,
                "started_at": started,
                "finished_at": finished,
            }
        )


def test_trace_adapters_index_tool_failure_and_verification_failure() -> None:
    """工具和验证失败共享 task/tool/失败类型/证据定位字段。"""

    started, finished = _time_pair(20)
    collector = TraceCollector(trace_id="trace-p017", run_id="run-p017")
    tool_result = ToolResult(
        tool_name=ToolName.RUN_VERIFICATION_SUITE,
        call_id="call-p017",
        status=ToolResultStatus.TIMEOUT,
        output=None,
        error=ToolError(
            category=ToolErrorCategory.TIMEOUT,
            code="verification_timeout",
            message="受控验证超时",
            retryable=True,
            details={"suite_id": "p0_python"},
        ),
        started_at=started,
        finished_at=finished,
        duration_ms=20,
        evidence_refs=["log://p0_python/case/stdout#L1"],
        effect_id=None,
        tool_version="p0-12.v2",
        input_digest="b" * 64,
        output_digest=None,
        audit_metadata={"parameters": "redacted"},
    )
    tool_event = collector.record_tool_result(
        tool_result,
        node="execute",
        task_id="TASK-VERIFY",
        parameters_digest="c" * 64,
    )
    case = ParsedVerificationCase(
        case_id="case-1",
        status="failed",
        exit_code=1,
        duration_ms=30,
        stdout_digest="d" * 64,
        stderr_digest="e" * 64,
        failure_type=VerificationFailureType.ASSERTION,
        task_id="TASK-VERIFY",
        tool_name=ToolName.RUN_VERIFICATION_SUITE.value,
        parameters_digest="c" * 64,
        evidence_refs=["log://p0_python/case/stderr#L2"],
        summary="断言失败",
    )
    verification_event = collector.record_verification_case(case, suite_id="p0_python")

    assert tool_event.status == "timeout"
    assert tool_event.error is not None
    assert tool_event.error.category == ToolErrorCategory.TIMEOUT.value
    assert tool_event.task_id == "TASK-VERIFY"
    assert verification_event.status == "failed"
    assert verification_event.task_id == "TASK-VERIFY"
    assert verification_event.tool_name == ToolName.RUN_VERIFICATION_SUITE.value
    assert verification_event.error is not None
    assert verification_event.error.code == VerificationFailureType.ASSERTION.value
    assert verification_event.evidence_refs == ["log://p0_python/case/stderr#L2"]
    assert [item.sequence for item in collector.events] == [1, 2]


def test_model_adapter_converts_budget_route_to_located_trace_error() -> None:
    """模型预算拒绝也要保留 Prompt、原因码和可计算 Token 字段。"""

    started, finished = _time_pair(50)
    result = SimpleNamespace(
        route=SimpleNamespace(value="fallback"),
        node_name=SimpleNamespace(value="plan_tasks"),
        prompt_id="plan_tasks",
        prompt_version="p0-05.v1",
        model_alias=None,
        context_digest="f" * 64,
        estimated_input_tokens=200,
        reason_code="INPUT_BUDGET_EXCEEDED",
        reason="输入预算不足",
        started_at=started,
        finished_at=finished,
        usage_before=SimpleNamespace(input_tokens=10, output_tokens=2),
        usage_after=SimpleNamespace(input_tokens=10, output_tokens=2),
    )
    event = TraceCollector(trace_id="trace-p017", run_id="run-p017").record_model_result(result)

    assert event.status == "failed"
    assert event.prompt_id == "plan_tasks"
    assert event.parameters_digest == "f" * 64
    assert event.error == TraceError(
        category="budget",
        code="INPUT_BUDGET_EXCEEDED",
        message="输入预算不足",
        details={},
    )
