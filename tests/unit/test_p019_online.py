"""P0-19 在线三策略的配置、安全 ReAct、采样与报告适配单测。"""

from __future__ import annotations

from types import SimpleNamespace

from evals.p018.contracts import (
    EvalCaseStatus,
    EvalCategory,
    EvalOutcome,
    EvalReportCase,
)
from evals.p018.dataset import load_dataset
from evals.p018.online import OnlineFastHarness, ReActControllerDecision
from evals.p019 import P019Strategy
from evals.p019.dataset import DEFAULT_ONLINE_CONFIG_PATH, load_config
from evals.p019.online import (
    OnlineThreeStrategyComparison,
    ProcessResourceSampler,
    _online_case_result,
)
from evals.p019.replay import _aggregate
from services.model_gateway.contracts import TokenUsage


class _RetryProvider:
    """只返回最小结构化 retry 决定，且记录是否发生真实控制器调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, *args, **kwargs):
        """模拟网关已校验的结构化响应，不复制生产节点实现。"""

        del args, kwargs
        self.calls += 1
        return SimpleNamespace(
            value=ReActControllerDecision(
                action="retry",
                reason_code="retry_transient_idempotent",
                observation_summary="瞬态工具超时且调用幂等，可安全重试一次。",
            ),
            attempts=1,
            repaired=False,
            total_usage=TokenUsage(input_tokens=30, output_tokens=12, total_tokens=42),
            call=SimpleNamespace(version=SimpleNamespace(served_alias="qwen3.6-fast")),
        )


def _fault_exception(*, has_side_effects: bool, side_effect_not_found: bool) -> Exception:
    """构造带 P0-15 FaultSignal 的超时异常，精确控制副作用安全事实。"""

    from agent.runtime.graph import PEVRGraphRunner

    error = RuntimeError("tool timed out")
    error.fault = PEVRGraphRunner.classify_failure(  # type: ignore[attr-defined]
        {"code": "tool_timeout", "message": "tool timed out", "retryable": True},
        idempotent=True,
        has_side_effects=has_side_effects,
        side_effect_not_found=side_effect_not_found,
    )
    return error


def test_online_config_reuses_exact_p018_dataset_and_config() -> None:
    """在线比较不能偷偷替换 P0-18 的 60 例或硬地图配置。"""

    config = load_config(DEFAULT_ONLINE_CONFIG_PATH)
    assert config["execution_mode"] == "online_fast_three_strategy_closed_loop"
    assert config["p018_dataset_path"] == "evals/p018/dataset.json"
    assert config["p018_config_path"] == "evals/p018/online_config.json"
    assert config["react_controller"]["max_retries"] == 1
    assert config["react_controller"]["max_replans"] == 0


def test_latin_square_balances_each_strategy_position() -> None:
    """60 例中每个策略必须各有 20 次位于首、中、末位置。"""

    comparison = object.__new__(OnlineThreeStrategyComparison)
    comparison.config = load_config(DEFAULT_ONLINE_CONFIG_PATH)
    comparison.dataset = load_dataset()
    schedule = comparison._schedule()
    positions = {strategy: [0, 0, 0] for strategy in P019Strategy}
    for case_index in range(60):
        row = schedule[case_index * 3 : case_index * 3 + 3]
        for position, (strategy, _) in enumerate(row):
            positions[strategy][position] += 1
    assert all(counts == [20, 20, 20] for counts in positions.values())


def test_react_calls_fast_only_after_deterministic_safety_gate() -> None:
    """安全瞬态错误可让 Fast 决定 retry，事件不得保存原始思维链。"""

    harness = object.__new__(OnlineFastHarness)
    harness.provider = _RetryProvider()
    harness.verification_timeout_seconds = 120.0
    retry, event = harness._react_recovery_decision(
        _fault_exception(has_side_effects=False, side_effect_not_found=False)
    )
    assert retry is True
    assert harness.provider.calls == 1
    assert event["event_type"] == "model"
    assert event["total_tokens"] == 42
    assert event["metadata"]["raw_chain_of_thought_stored"] is False
    assert "rationale" not in event["metadata"]


def test_react_blocks_unknown_side_effect_before_model_call() -> None:
    """副作用是否落地未知时必须在模型前停止，Prompt 不能覆盖安全事实。"""

    harness = object.__new__(OnlineFastHarness)
    harness.provider = _RetryProvider()
    harness.verification_timeout_seconds = 120.0
    retry, event = harness._react_recovery_decision(
        _fault_exception(has_side_effects=True, side_effect_not_found=False)
    )
    assert retry is False
    assert harness.provider.calls == 0
    assert event["node"] == "react_safety_gate"
    assert event["status"] == "denied"


def test_resource_sampler_reports_observed_cpu_and_rss(monkeypatch) -> None:
    """采样器至少观测当前进程；无 GPU 时显存保持 None 而不是伪造 0。"""

    monkeypatch.setattr(ProcessResourceSampler, "_gpu_memory_mb", staticmethod(lambda pids: None))
    sampler = ProcessResourceSampler(interval_seconds=0.1, listener_ports=[]).start()
    sum(index * index for index in range(10000))
    resource = sampler.stop()
    assert resource.observed is True
    assert resource.sample_count >= 2
    assert resource.cpu_time_ms is not None
    assert resource.peak_rss_mb is not None and resource.peak_rss_mb > 0
    assert resource.peak_gpu_memory_mb is None


def test_online_case_uses_wall_clock_online_tokens_and_actual_react_event() -> None:
    """在线汇总必须读取真实墙钟/Token/资源，而不是复用离线投影口径。"""

    event = {
        "sequence": 1,
        "event_type": "model",
        "node": "react_controller",
        "status": "completed",
        "prompt_id": "amr.eval.p019.react_recovery",
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "latency_ms": 50,
        "metadata": {"attempts": 1, "action": "retry"},
    }
    case = EvalReportCase(
        case_id="p018-normal-001",
        category=EvalCategory.NORMAL,
        scenario="normal_order",
        expected_outcome=EvalOutcome.COMPLETED,
        observed_outcome=EvalOutcome.COMPLETED,
        status=EvalCaseStatus.PASSED,
        evaluation_passed=True,
        trace_id="trace-p019-react-p018-normal-001",
        trace_events=[event],
        metrics={
            "wall_clock_ms": 125.5,
            "resource_sample_count": 3,
            "cpu_time_ms": 40.0,
            "peak_rss_mb": 512.0,
            "peak_gpu_memory_mb": 1024.0,
            "resource_observation_reason": "unit sample",
        },
    )
    result = _online_case_result(P019Strategy.REACT, case)
    summary = _aggregate(
        P019Strategy.REACT,
        [result],
        latency_source="online_wall_clock",
        wall_clock=True,
    )
    assert result.token_usage.source == "online_trace"
    assert result.latency_ms == 125.5
    assert result.projection_events[0]["kind"] == "actual_react_decision"
    assert summary.latency.wall_clock is True
    assert summary.latency.source == "online_wall_clock"
    assert summary.resource.source == "online_sampler"

