"""P0-19 同源策略对照的公平性、原始轨迹和 Smart 延期反例。"""

from __future__ import annotations

from types import SimpleNamespace

from evals.p018.reporting import report_to_json
from evals.p018.runner import run_harness
from evals.p019 import P019Strategy, run_comparison
from evals.p019.replay import load_source_report


class _FakeVerificationRunner:
    """只替换 P0-17 固定子进程，保持 P0-18 源 Trace 结构不变。"""

    def run(self, *args: object, **kwargs: object) -> SimpleNamespace:
        """返回固定验证成功结果，测试不依赖 CTest/pytest 外部进程。"""

        del args, kwargs
        return SimpleNamespace(
            status="passed",
            case_count=1,
            evidence_refs=["verification://p019/fake"],
            report_digest="a" * 64,
        )


def _source_report(tmp_path):
    """生成临时但完整的 P0-18 源报告，避免单测依赖既有 tmp 目录。"""

    report = run_harness(verification_runner=_FakeVerificationRunner())
    source_path = tmp_path / "p018_eval.json"
    source_path.write_text(report_to_json(report), encoding="utf-8")
    return source_path


def test_p019_replays_same_60_cases_for_all_strategies(tmp_path) -> None:
    """三种策略必须各覆盖同一组 60 例，不能用子集优化某一行指标。"""

    source_path = _source_report(tmp_path)
    report = run_comparison(source_report_path=source_path)

    assert report.status == "passed"
    assert report.fairness.same_dataset is True
    assert report.fairness.same_tools is True
    assert report.fairness.same_prompts is True
    assert report.fairness.same_config is True
    assert report.fairness.same_model is True
    assert len(report.raw_results) == 180
    assert {item.strategy for item in report.strategies} == {
        P019Strategy.PLAN_EXECUTE,
        P019Strategy.REACT,
        P019Strategy.PEVR,
    }
    assert all(len([item for item in report.raw_results if item.strategy is strategy]) == 60 for strategy in P019Strategy)


def test_react_is_only_a_control_flow_projection_and_keeps_source_facts(tmp_path) -> None:
    """ReAct 增加可解释控制步，但不能改写终态、错误或安全零容忍事实。"""

    source_path = _source_report(tmp_path)
    report = run_comparison(source_report_path=source_path)
    fixed = next(item for item in report.strategies if item.strategy is P019Strategy.PLAN_EXECUTE)
    react = next(item for item in report.strategies if item.strategy is P019Strategy.REACT)
    pevr = next(item for item in report.strategies if item.strategy is P019Strategy.PEVR)

    assert fixed.evaluation_pass_count == react.evaluation_pass_count == pevr.evaluation_pass_count == 60
    assert fixed.tool_error_count == react.tool_error_count == pevr.tool_error_count == 15
    assert fixed.zero_tolerance.total() == react.zero_tolerance.total() == pevr.zero_tolerance.total() == 0
    assert react.step_count_total > fixed.step_count_total == pevr.step_count_total
    first_react = next(item for item in report.raw_results if item.strategy is P019Strategy.REACT)
    assert [event["kind"] for event in first_react.projection_events[:3]] == ["guard", "think", "observe"]
    assert first_react.source_trace_events == first_react.source_case["trace_events"]


def test_p019_reports_unobserved_tokens_resources_and_deferred_smart(tmp_path) -> None:
    """离线源没有 Token/资源样本时必须显式 N/A，Smart 不能被误写成完成。"""

    source_path = _source_report(tmp_path)
    report = run_comparison(source_report_path=source_path)
    for summary in report.strategies:
        assert summary.token_usage.observed is False
        assert summary.token_usage.total_tokens == 0
        assert summary.resource.observed is False
        assert summary.resource.peak_rss_mb is None
    assert report.smart_comparison.status.value == "deferred"
    assert report.smart_comparison.started is False
    assert report.smart_comparison.completed is False
    assert report.report_id == run_comparison(source_report_path=source_path).report_id


def test_p019_independent_runners_separate_recovery_outcomes() -> None:
    """三种策略必须独立执行；Plan-and-Execute 不能复制 PEVR 的异常恢复终态。"""

    from evals.p019 import P019ExecutionMode, run_independent_comparison

    report = run_independent_comparison()
    assert report.execution_mode is P019ExecutionMode.INDEPENDENT_ORACLE
    assert report.status == "passed"
    assert len(report.raw_results) == 180
    fixed = next(item for item in report.strategies if item.strategy is P019Strategy.PLAN_EXECUTE)
    react = next(item for item in report.strategies if item.strategy is P019Strategy.REACT)
    pevr = next(item for item in report.strategies if item.strategy is P019Strategy.PEVR)
    assert pevr.evaluation_pass_count == 60
    assert fixed.evaluation_pass_count < pevr.evaluation_pass_count
    assert react.evaluation_pass_count < pevr.evaluation_pass_count
    plan_execute_low_battery = next(
        item
        for item in report.raw_results
        if item.strategy is P019Strategy.PLAN_EXECUTE and item.case_id == "p018-exception-001"
    )
    pevr_low_battery = next(
        item
        for item in report.raw_results
        if item.strategy is P019Strategy.PEVR and item.case_id == "p018-exception-001"
    )
    assert plan_execute_low_battery.observed_outcome != pevr_low_battery.observed_outcome
    assert pevr_low_battery.evaluation_passed is True
    assert plan_execute_low_battery.evaluation_passed is False
    assert {item.strategy_trace_id for item in report.raw_results if item.case_id == "p018-exception-001"} == {
        "trace-p019-plan_execute-p018-exception-001",
        "trace-p019-react-p018-exception-001",
        "trace-p019-pevr-p018-exception-001",
    }


def test_tampered_p018_source_digest_is_rejected(tmp_path) -> None:
    """原始轨迹被修改后必须在进入策略比较前失败。"""

    source_path = _source_report(tmp_path)
    payload = source_path.read_text(encoding="utf-8").replace('"status": "passed"', '"status": "failed"', 1)
    source_path.write_text(payload, encoding="utf-8")
    try:
        load_source_report(source_path)
    except ValueError as exc:
        assert "源报告" in str(exc) or "digest" in str(exc)
    else:
        raise AssertionError("篡改后的 P0-18 源报告不应通过")
