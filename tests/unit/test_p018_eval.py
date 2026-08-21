"""P0-18 Harness、固定数据集、复现和负向轨迹测试。"""

from __future__ import annotations

from types import SimpleNamespace

from evals.p018 import EvalHarness, EvalOutcome, load_config, load_dataset
from evals.p018.dataset import DEFAULT_CONFIG_PATH, DEFAULT_DATASET_PATH
from evals.p018.reporting import report_to_json, report_to_markdown
from evals.p018.runner import run_harness


class _FakeVerificationRunner:
    """只替换 P0-17 子进程，验证 P0-18 其他分区不依赖外部服务。"""

    def run(self, *args: object, **kwargs: object) -> SimpleNamespace:
        """返回与 ``VerificationSuiteOutput`` 同名的最小读取接口。"""

        del args, kwargs
        return SimpleNamespace(
            status="passed",
            case_count=1,
            evidence_refs=["verification://p018/fake"],
            report_digest="a" * 64,
        )


def _harness() -> EvalHarness:
    """构造不启动模型的固定 Harness。"""

    return EvalHarness(
        dataset=load_dataset(),
        config=load_config(),
        dataset_path=DEFAULT_DATASET_PATH,
        config_path=DEFAULT_CONFIG_PATH,
        verification_runner=_FakeVerificationRunner(),
    )


def test_dataset_has_exact_p018_composition_and_is_not_training_data() -> None:
    """配额、唯一 seed 和用途声明在执行前必须固定。"""

    dataset = load_dataset()
    assert dataset.is_training_data is False
    assert dataset.purpose == "evaluation_only"
    assert len(dataset.cases) == 60
    assert {category.value: sum(item.category is category for item in dataset.cases) for category in dataset.EXPECTED_COUNTS} == {
        "normal_order_charging": 25,
        "rag_permission_approval": 10,
        "exception_local_replan": 10,
        "verification": 5,
        "prompt_injection_security": 10,
    }
    assert len({item.seed for item in dataset.cases}) == 60


def test_harness_reproducibly_keeps_negative_cases_and_zero_tolerance() -> None:
    """两次固定运行的身份摘要一致，denied/blocked 轨迹不能被过滤。"""

    first = run_harness(verification_runner=_FakeVerificationRunner())
    second = run_harness(verification_runner=_FakeVerificationRunner())
    assert first.status == "passed"
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.metrics.evaluation_pass_count == 60
    assert first.metrics.zero_tolerance.total() == 0
    assert len(first.observed_negative_cases) >= 10
    assert all(item.trace_events for item in first.cases)
    assert all(
        item.failure_reason
        for item in first.cases
        if item.observed_outcome in {EvalOutcome.DENIED, EvalOutcome.BLOCKED, EvalOutcome.FAILED}
    )


def test_security_and_recovery_failures_keep_stable_reason_and_trace_error() -> None:
    """安全拒绝、状态冲突和审批绕过均必须有错误码与可定位 Trace。"""

    harness = _harness()
    dataset = load_dataset()
    for case_id, expected_outcome, expected_code in (
        ("p018-security-005", EvalOutcome.DENIED, "approval_context_forged"),
        ("p018-security-007", EvalOutcome.DENIED, "forbidden_execution_surface"),
        ("p018-exception-007", EvalOutcome.BLOCKED, "state_conflict"),
        ("p018-rag-010", EvalOutcome.BLOCKED, "approval_rejected"),
    ):
        case = next(item for item in dataset.cases if item.case_id == case_id)
        result = harness.run_case(case)
        assert result.evaluation_passed is True
        assert result.observed_outcome is expected_outcome
        assert result.failure_code == expected_code
        assert any(event.get("error") for event in result.trace_events)


def test_zero_tolerance_battery_violation_fails_case_instead_of_being_hidden() -> None:
    """低于 15% 的实际运输路线不能被平均成功率掩盖。"""

    harness = _harness()
    source = load_dataset().cases[0]
    unsafe = source.model_copy(
        update={"input_data": {**source.input_data, "start_battery": 10}}
    )
    result = harness.run_case(unsafe)
    assert result.evaluation_passed is False
    assert result.observed_outcome is EvalOutcome.FAILED
    assert result.zero_tolerance.low_battery_violation_count == 1
    assert result.failure_code == "route_safety_violation"


def test_reports_share_identity_and_include_failure_trajectory_projection() -> None:
    """JSON/Markdown 来自同一报告对象，并保留负向 case ID 与错误码。"""

    report = run_harness(verification_runner=_FakeVerificationRunner())
    rendered_json = report_to_json(report)
    rendered_markdown = report_to_markdown(report)
    assert report.report_id in rendered_json
    assert report.report_digest in rendered_json
    assert report.report_id in rendered_markdown
    assert "p018-security-005" in rendered_markdown
    assert "approval_context_forged" in rendered_markdown
