"""P0-19 在线三策略的配置、采样、公平性与报告适配单测。"""

from __future__ import annotations

import json

import pytest

from evals.p018.contracts import (
    EvalCaseStatus,
    EvalCategory,
    EvalOutcome,
    EvalReportCase,
)
from evals.p018.dataset import load_dataset
from evals.p019 import P019Strategy
from evals.p019.dataset import DEFAULT_ONLINE_CONFIG_PATH, load_config
from evals.p019.online import (
    OnlineComparisonError,
    OnlineThreeStrategyComparison,
    ProcessResourceSampler,
    _online_case_result,
)
from evals.p019.react_contracts import REACT_PROMPT_ID, REACT_PROMPT_VERSION, REACT_RUNNER_VERSION
from evals.p019.replay import _aggregate, _recovery_metrics


def test_online_config_reuses_exact_p018_dataset_and_independent_react_agent() -> None:
    """在线比较不能偷偷替换 P0-18 的 60 例，且必须声明独立 ReAct Agent。"""

    config = load_config(DEFAULT_ONLINE_CONFIG_PATH)
    assert config["execution_mode"] == "online_fast_three_strategy_closed_loop"
    assert config["version"] == "p0-19.online.v2"
    assert config["p018_dataset_path"] == "evals/p018/dataset.json"
    assert config["p018_config_path"] == "evals/p018/online_config.json"
    assert config["react_agent"]["prompt_id"] == REACT_PROMPT_ID
    assert config["react_agent"]["prompt_version"] == REACT_PROMPT_VERSION
    assert config["react_agent"]["runner_version"] == REACT_RUNNER_VERSION
    assert config["react_agent"]["uses_pevr_runner"] is False
    assert config["react_agent"]["allow_repeat_retrieve"] is False
    assert "react_controller" not in config


def test_legacy_online_v1_config_is_rejected(tmp_path) -> None:
    """旧一次 retry 配置不能再被当成当前在线实验加载。"""

    payload = json.loads(DEFAULT_ONLINE_CONFIG_PATH.read_text(encoding="utf-8"))
    payload["version"] = "p0-19.online.v1"
    payload["react_controller"] = {
        "prompt_id": "amr.eval.p019.react_recovery",
        "prompt_version": "1.0.0",
        "max_retries": 1,
        "max_replans": 0,
    }
    payload.pop("react_agent", None)
    path = tmp_path / "online_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="p0-19.online.v2"):
        load_config(path)


def test_latin_square_balances_each_strategy_position() -> None:
    """60 例中每个策略必须各有 20 次位于首、中、末位置，共 180 条独立身份。"""

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
    assert len(schedule) == 180
    assert len({(strategy, case.case_id) for strategy, case in schedule}) == 180


def test_v2_manifest_rejects_legacy_v1_progress(tmp_path) -> None:
    """旧 v1 manifest/progress 不得通过 --resume 被新实现复用。"""

    from evals.p019.dataset import rooted_path

    comparison = object.__new__(OnlineThreeStrategyComparison)
    comparison.resume = True
    comparison.config = load_config(DEFAULT_ONLINE_CONFIG_PATH)
    comparison.config_path = DEFAULT_ONLINE_CONFIG_PATH
    comparison.dataset = load_dataset()
    comparison.p018_dataset_path = rooted_path("evals/p018/dataset.json")
    comparison.p018_config_path = rooted_path("evals/p018/online_config.json")
    comparison.manifest_path = tmp_path / "p019_online_run_manifest.json"
    comparison.progress_path = tmp_path / "p019_online_progress.jsonl"
    comparison.manifest_path.write_text(
        json.dumps(
            {
                "execution_mode": "online_fast_three_strategy_closed_loop",
                "dataset_sha256": "a" * 64,
                "p018_config_sha256": "b" * 64,
                "p019_config_sha256": "c" * 64,
                "case_id_digest": "d" * 64,
                "schedule_digest": "e" * 64,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    comparison.progress_path.write_text("", encoding="utf-8")
    with pytest.raises(OnlineComparisonError, match="身份已变化"):
        comparison._load_progress()


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


def test_online_case_uses_wall_clock_online_tokens_and_actual_react_loop() -> None:
    """在线汇总必须读取真实墙钟/Token/资源，并记录独立 ReAct 循环事件。"""

    event = {
        "sequence": 1,
        "event_type": "model",
        "node": "react_decide",
        "status": "completed",
        "prompt_id": REACT_PROMPT_ID,
        "prompt_version": REACT_PROMPT_VERSION,
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "latency_ms": 50,
        "metadata": {"action_type": "tool", "raw_chain_of_thought_stored": False},
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
    assert result.projection_events[0]["kind"] == "actual_react_loop"
    assert summary.latency.wall_clock is True
    assert summary.latency.source == "online_wall_clock"
    assert summary.resource.source == "online_sampler"


def test_recovery_terminal_correct_recomputes_from_expected_observed() -> None:
    """禁止把执行过 retry/replan 计为最终终态正确。"""

    events = [{"sequence": 1, "event_type": "node", "node": "finish", "status": "failed"}]
    case = EvalReportCase(
        case_id="p018-exception-004",
        category=EvalCategory.EXCEPTION,
        scenario="workstation_occupied",
        expected_outcome=EvalOutcome.COMPLETED,
        observed_outcome=EvalOutcome.FAILED,
        status=EvalCaseStatus.FAILED,
        evaluation_passed=False,
        failure_code="recovery_fallback",
        failure_reason="工位持续占用",
        trace_id="trace-p019-pevr-p018-exception-004",
        trace_events=events,
        metrics={"recovery_terminal_correct": 1, "recovery_replan_success": 1},
        replan_count=2,
        retry_count=2,
    )
    applicable, terminal_correct, successful_recovery = _recovery_metrics(case)
    assert applicable is True
    assert terminal_correct is False
    assert successful_recovery is False
    result = _online_case_result(P019Strategy.PEVR, case)
    assert result.recovery_success is False
    assert result.expected_outcome == EvalOutcome.COMPLETED
    assert result.observed_outcome == EvalOutcome.FAILED
    assert result.recovery_success == (result.expected_outcome == result.observed_outcome)
