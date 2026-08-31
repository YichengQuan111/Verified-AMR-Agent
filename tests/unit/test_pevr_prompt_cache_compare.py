"""PEVR 有/无缓存对照脚本的汇总公式离线测试；不启动模型。"""

from __future__ import annotations

from scripts.compare_pevr_prompt_cache import paired_wall_comparison, percentile, summarize_pevr_cases


def test_percentile_interpolates_and_handles_empty() -> None:
    assert percentile([], 50) == 0.0
    assert percentile([10.0], 95) == 10.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0


def test_summarize_pevr_cases_recomputes_from_metrics() -> None:
    summary = summarize_pevr_cases(
        [
            {
                "evaluation_passed": True,
                "metrics": {"wall_clock_ms": 100.0, "model_call_count": 2},
            },
            {
                "evaluation_passed": False,
                "metrics": {"wall_clock_ms": 50.0, "model_call_count": 1},
            },
        ]
    )
    assert summary["case_count"] == 2
    assert summary["evaluation_pass_count"] == 1
    assert summary["model_call_count"] == 3
    assert summary["wall_ms_sum"] == 150.0
    assert summary["wall_ms_mean"] == 75.0


def test_paired_wall_comparison_aligns_by_case_id() -> None:
    paired = paired_wall_comparison(
        [
            {
                "case_id": "a",
                "category": "normal_order_charging",
                "evaluation_passed": True,
                "metrics": {"wall_clock_ms": 80.0, "model_call_count": 4},
            },
            {
                "case_id": "orphan",
                "evaluation_passed": True,
                "metrics": {"wall_clock_ms": 999.0, "model_call_count": 1},
            },
        ],
        [
            {
                "case_id": "a",
                "evaluation_passed": True,
                "metrics": {"wall_clock_ms": 20.0, "model_call_count": 4},
            }
        ],
    )
    assert paired["pairs"] == 1
    assert paired["wall_ms_sum_cache_off"] == 80.0
    assert paired["wall_ms_sum_cache_on"] == 20.0
    assert paired["wall_speedup_off_over_on"] == 4.0
    assert paired["calls"][0]["case_id"] == "a"
