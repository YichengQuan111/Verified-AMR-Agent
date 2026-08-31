"""对照脚本的汇总公式离线测试；不启动模型。"""

from __future__ import annotations

from scripts.compare_prompt_cache import hit_ratio, matched_success_comparison, summarize_calls


def test_hit_ratio_clamps_and_rejects_missing_usage() -> None:
    assert hit_ratio(None, 100) is None
    assert hit_ratio(8, 0) is None
    assert hit_ratio(8, 10) == 0.8
    assert hit_ratio(12, 10) == 1.0


def test_summarize_calls_adds_cached_tokens_and_mean_wall() -> None:
    summary = summarize_calls(
        [
            {"ok": True, "wall_ms": 100.0, "input_tokens": 10, "cached_input_tokens": 0},
            {"ok": True, "wall_ms": 50.0, "input_tokens": 10, "cached_input_tokens": 8},
            {"ok": False, "wall_ms": 20.0, "input_tokens": None, "cached_input_tokens": None},
        ]
    )
    assert summary["calls"] == 3
    assert summary["successes"] == 2
    assert summary["wall_ms_sum"] == 170.0
    assert summary["wall_ms_mean"] == round(170.0 / 3, 1)
    assert summary["input_tokens_sum"] == 20
    assert summary["cached_input_tokens_sum"] == 8
    assert summary["hit_ratio"] == 0.4


def test_matched_success_comparison_ignores_timeouts() -> None:
    """超时失败不得进入加速比，只保留两侧都成功的同一节点/轮次。"""

    matched = matched_success_comparison(
        [
            {
                "kind": "p005",
                "node": "understand_goal",
                "repeat": 1,
                "ok": False,
                "wall_ms": 120000.0,
                "cached_input_tokens": None,
                "hit_ratio": None,
            },
            {
                "kind": "p005",
                "node": "compose_report",
                "repeat": 1,
                "ok": True,
                "wall_ms": 80000.0,
                "cached_input_tokens": 0,
                "hit_ratio": 0.0,
            },
        ],
        [
            {
                "kind": "p005",
                "node": "understand_goal",
                "repeat": 1,
                "ok": True,
                "wall_ms": 30000.0,
                "cached_input_tokens": 3500,
                "hit_ratio": 0.87,
            },
            {
                "kind": "p005",
                "node": "compose_report",
                "repeat": 1,
                "ok": True,
                "wall_ms": 20000.0,
                "cached_input_tokens": 2000,
                "hit_ratio": 0.8,
            },
        ],
    )
    assert matched["pairs"] == 1
    assert matched["wall_ms_sum_cache_off"] == 80000.0
    assert matched["wall_ms_sum_cache_on"] == 20000.0
    assert matched["wall_speedup_off_over_on"] == 4.0
    assert matched["calls"][0]["node"] == "compose_report"
