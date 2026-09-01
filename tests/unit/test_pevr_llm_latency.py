"""llama.cpp 日志 Prefill 解析、旧 TTFT 误判场景与 legacy 作废。"""

from __future__ import annotations

from evals.perf.legacy import restated_legacy_metrics, summarize_log_prefill_only
from evals.perf.llama_log import (
    LlamaLogCursor,
    attach_prefill_from_log_delta,
    cache_hit_inference_from_log,
    parse_llama_slot_timings,
)
from evals.perf.stats import compare_cache_summaries, summarize_samples
from evals.perf.contracts import LatencySample


PROGRESS_EARLY_LOG = """
0.00.000.000 I slot get_availabl: id  0 | task -1 | selected slot by LRU, t_last = -1
0.00.100.000 I slot launch_slot_: id  0 | task 5 | processing task, is_child = 0
0.05.200.000 I slot print_timing: id  0 | task 5 | prompt processing, n_tokens =   4516, progress = 1.00, t =   5.10 s / 885.49 tokens per second
0.05.364.000 I slot print_timing: id  0 | task 5 | prompt eval time =    5264.00 ms /  4520 tokens (    1.16 ms per token,   858.66 tokens per second)
0.16.000.000 I slot print_timing: id  0 | task 5 |        eval time =   10736.00 ms /   200 tokens
0.16.000.000 I slot print_timing: id  0 | task 5 |       total time =   16000.00 ms /  4720 tokens
""".strip()

MISSING_PROGRESS_WITH_PREFILL = """
0.00.100.000 I slot launch_slot_: id  0 | task 9 | processing task, is_child = 0
0.01.200.000 I slot print_timing: id  0 | task 9 | prompt eval time =     800.00 ms /   120 tokens (    6.67 ms per token,   150.00 tokens per second)
0.01.400.000 I slot print_timing: id  0 | task 9 |        eval time =     200.00 ms /     8 tokens
0.01.400.000 I slot print_timing: id  0 | task 9 |       total time =    1000.00 ms /   128 tokens
""".strip()

TWO_TASKS_IN_ONE_DELTA = """
0.00.100.000 I slot launch_slot_: id  0 | task 1 | processing task, is_child = 0
0.00.200.000 I slot print_timing: id  0 | task 1 | prompt eval time =     100.00 ms /    57 tokens
0.00.300.000 I slot launch_slot_: id  0 | task 2 | processing task, is_child = 0
0.00.900.000 I slot print_timing: id  0 | task 2 | prompt eval time =     500.00 ms /  4000 tokens
""".strip()


def test_rounded_progress_100_is_not_ttft_and_is_earlier_than_prefill() -> None:
    timings = parse_llama_slot_timings(PROGRESS_EARLY_LOG)
    assert len(timings) == 1
    item = timings[0]
    assert item.prompt_eval_ms == 5264.0
    assert item.prompt_eval_tokens == 4520
    assert item.rounded_progress_100_ms == 5100.0
    assert item.rounded_progress_100_ms < item.prompt_eval_ms
    payload = item.to_prefill_dict()
    assert payload["ttft_ms"] is None
    assert payload["ttft_missing_reason"] == "rounded_progress_is_not_ttft"
    assert payload["prompt_eval_ms"] == 5264.0


def test_missing_progress_is_not_cache_hit_and_does_not_fill_ttft() -> None:
    timings = parse_llama_slot_timings(MISSING_PROGRESS_WITH_PREFILL)
    item = timings[0]
    assert item.progress_events == []
    assert item.prompt_eval_ms == 800.0
    inferred = cache_hit_inference_from_log(item)
    assert inferred["cache_hit"] is None
    assert inferred["missing_progress_used"] is False
    assert inferred["reason"] == "missing_progress_is_not_cache_hit"
    assert item.to_prefill_dict()["ttft_ms"] is None
    assert item.to_prefill_dict()["ttft_missing_reason"] == "legacy_log_only_no_client_clock"


def test_cache_hit_from_release_n_tokens_not_from_missing_progress() -> None:
    log = """
0.00.100.000 I slot launch_slot_: id  0 | task 46 | processing task, is_child = 0
0.00.200.000 I slot print_timing: id  0 | task 46 | prompt eval time =      80.63 ms /     4 tokens
0.00.230.000 I slot print_timing: id  0 | task 46 |        eval time =      30.05 ms /     2 tokens
0.00.230.000 I slot print_timing: id  0 | task 46 |       total time =     110.67 ms /     6 tokens
0.00.230.000 I slot      release: id  0 | task 46 | stop processing: n_tokens = 433, truncated = 0
""".strip()
    item = parse_llama_slot_timings(log)[0]
    inferred = cache_hit_inference_from_log(item)
    assert inferred["missing_progress_used"] is False
    assert inferred["prompt_tokens"] == 431
    assert inferred["cached_tokens"] == 427
    assert inferred["cache_hit"] is True
    assert "n_tokens_stop" in inferred["source"]


def test_log_delta_rejects_two_prompt_evals_as_mismatch() -> None:
    timing, reason = attach_prefill_from_log_delta(log_delta=TWO_TASKS_IN_ONE_DELTA)
    assert timing is None
    assert reason == "mismatched_server_log"
    empty, empty_reason = attach_prefill_from_log_delta(log_delta="")
    assert empty is None
    assert empty_reason == "no_prompt_eval_in_log"


def test_log_cursor_detects_truncation(tmp_path) -> None:
    path = tmp_path / "llama-server.err.log"
    path.write_text("abcdef", encoding="utf-8")
    cursor = LlamaLogCursor(path)
    cursor.mark()
    assert cursor._offset == 6
    path.write_text("xy", encoding="utf-8")
    delta = cursor.read_delta()
    assert delta == ""
    assert cursor.truncated is True


def test_summarize_log_prefill_only_never_emits_ttft() -> None:
    report = summarize_log_prefill_only(PROGRESS_EARLY_LOG + "\n" + MISSING_PROGRESS_WITH_PREFILL)
    assert report["ttft_ms"] is None
    assert report["ttft_missing_reason"] == "legacy_log_only_no_client_clock"
    assert report["missing_progress_interpreted_as_cache_hit"] is False
    assert report["prefill_ms"]["n"] == 1  # 120-token call excluded by default min 200
    assert report["prefill_ms"]["p50"] == 5264.0
    early = report["progress_100_earlier_than_prefill_ms"]
    assert early["n"] == 1
    assert early["p50"] == 164.0


def test_restated_legacy_metrics_withdraws_ttft_keeps_prefill() -> None:
    legacy = {
        "cache_off": {
            "model_calls_logged": 132,
            "ttft_ms_p50": 4648.9,
            "ttft_ms_p95": 7077.1,
            "ttft_ms_mean": 4937.8,
            "prefill_ms_p50": 4829.7,
            "prefill_ms_p95": 7269.8,
            "prefill_ms_mean": 5112.8,
            "cache_hit_rate": 0.0,
            "e2e": {"wall_ms_p50": 75114.2},
        },
        "cache_on": {
            "model_calls_logged": 132,
            "ttft_ms_p50": 3233.0,
            "ttft_ms_p95": 4407.1,
            "ttft_ms_mean": 2960.8,
            "prefill_ms_p50": 3349.2,
            "prefill_ms_p95": 4611.2,
            "prefill_ms_mean": 3062.5,
            "cache_hit_rate": 0.4319,
            "e2e": {"wall_ms_p50": 68549.9},
        },
        "speedup_off_over_on": {
            "ttft_p50": 1.438,
            "prefill_p50": 1.442,
            "e2e_p50": 1.096,
            "e2e_mean": 1.088,
        },
    }
    restated = restated_legacy_metrics(legacy)
    assert restated["ttft_status"] == "invalid_do_not_cite"
    assert restated["cache_on"]["ttft_ms_p50"] is None
    assert restated["cache_off"]["ttft_ms_p50"] is None
    assert restated["cache_on"]["legacy_invalid_ttft_ms_p50"] == 3233.0
    assert restated["cache_on"]["prefill_ms_p50"] == 3349.2
    assert restated["speedup_off_over_on"]["ttft_p50"] is None
    assert restated["speedup_off_over_on"]["prefill_p50"] == 1.442
    assert restated["speedup_off_over_on"]["e2e_p50"] == 1.096
    assert restated["speedup_off_over_on"]["ttft_p50_legacy_invalid"] == 1.438


def test_compare_cache_does_not_substitute_prefill_for_missing_ttft() -> None:
    off = summarize_samples(
        [
            LatencySample(
                request_id="a",
                kind="measured",
                cache_prompt=False,
                outcome="missing_ttft",
                ok=False,
                ttft_ms=None,
                ttft_missing_reason="legacy_log_only_no_client_clock",
                e2e_ms=80.0,
                prefill_ms=50.0,
            )
        ]
    )
    on = summarize_samples(
        [
            LatencySample(
                request_id="b",
                kind="measured",
                cache_prompt=True,
                outcome="missing_ttft",
                ok=False,
                ttft_ms=None,
                ttft_missing_reason="legacy_log_only_no_client_clock",
                e2e_ms=40.0,
                prefill_ms=25.0,
            )
        ]
    )
    compared = compare_cache_summaries(off, on)
    assert compared["ttft_p50_speedup_off_over_on"] is None
    assert compared["ttft_speedup_uses_prefill"] is False
    assert compared["prefill_p50_speedup_off_over_on"] == 2.0
    assert compared["e2e_p50_speedup_off_over_on"] == 2.0


def test_breaker_and_warmup_not_in_percentiles() -> None:
    summary = summarize_samples(
        [
            LatencySample(
                request_id="breaker",
                kind="breaker",
                cache_prompt=False,
                outcome="excluded",
                ok=False,
                exclusion_reason="breaker",
                ttft_ms=5.0,
                e2e_ms=6.0,
                prefill_ms=4.0,
                prompt_eval_tokens=57,
            ),
            LatencySample(
                request_id="ok",
                kind="measured",
                cache_prompt=False,
                outcome="valid",
                ok=True,
                ttft_ms=100.0,
                e2e_ms=200.0,
                prefill_ms=110.0,
            ),
        ]
    )
    assert summary["sample_counts"]["valid"] == 1
    assert summary["sample_counts"]["excluded"]["breaker"] == 1
    assert summary["ttft_ms"]["p50"] == 100.0
    assert summary["ttft_ms"]["n"] == 1


def test_summarize_pevr_llm_report_filters_36_and_never_fills_ttft(tmp_path) -> None:
    from evals.perf.llm36 import read_log_delta, summarize_pevr_llm_report

    cases = [
        {
            "case_id": f"p018-normal-{index:03d}",
            "evaluation_passed": True,
            "metrics": {"model_call_count": 4, "wall_clock_ms": 1000.0 + index},
        }
        for index in range(1, 37)
    ]
    cases.append(
        {
            "case_id": "p018-security-010",
            "evaluation_passed": True,
            "metrics": {"model_call_count": 0, "wall_clock_ms": 12.0},
        }
    )
    cases[3]["evaluation_passed"] = False
    cases[3]["observed_outcome"] = "failed"
    cases[3]["failure_reason"] = "TOOL_BUDGET_EXHAUSTED"
    log = tmp_path / "llama-server.err.log"
    log.write_bytes(b"WARMUP\n")
    offset = log.stat().st_size
    log.write_bytes(
        log.read_bytes()
        + (
            "0.00.100.000 I slot launch_slot_: id  0 | task 7 | processing task, is_child = 0 "
            "n_prompt = 4000 n_past = 1000\n"
            "0.01.000.000 I slot print_timing: id  0 | task 7 | "
            "prompt eval time =    3000.00 ms /  3000 tokens\n"
        ).encode("utf-8")
    )
    payload = summarize_pevr_llm_report(
        {"report_id": "p018-online-test", "status": "passed", "cases": cases},
        log_text=read_log_delta(log, byte_offset=offset),
        log_byte_offset=offset,
        cache_prompt=True,
    )
    assert payload["dataset_case_count"] == 37
    assert payload["llm_case_count"] == 36
    assert payload["ttft_ms"] is None
    assert payload["ttft_missing_reason"] == "non_streaming_response"
    assert payload["e2e"]["evaluation_pass_count"] == 35
    assert payload["e2e"]["model_call_count"] == 144
    assert payload["e2e"]["failed_cases"][0]["case_id"] == "p018-normal-004"
    assert "p018-security-010" not in payload["case_ids"]
    assert payload["prefill"]["prefill_ms"]["p50"] == 3000.0
    assert payload["prefill"]["ttft_ms"] is None
    assert payload["prefill"]["cache_hit_rate"] == 0.25


def test_summarize_pevr_llm_report_uses_probe_ttft_not_prefill() -> None:
    from evals.perf.contracts import LatencySample
    from evals.perf.llm36 import summarize_pevr_llm_report

    cases = [
        {
            "case_id": f"p018-normal-{index:03d}",
            "evaluation_passed": True,
            "metrics": {"model_call_count": 1, "wall_clock_ms": 1000.0},
        }
        for index in range(1, 37)
    ]
    payload = summarize_pevr_llm_report(
        {"report_id": "p018-online-test", "status": "passed", "cases": cases},
        ttft_samples=[
            LatencySample(
                request_id="r1",
                kind="measured",
                cache_prompt=True,
                outcome="valid",
                ok=True,
                ttft_ms=120.0,
                e2e_ms=400.0,
                prefill_ms=80.0,
            )
        ],
    )
    assert payload["ttft_missing_reason"] is None
    assert payload["ttft_ms"]["p50"] == 120.0
    assert payload["ttft_ms"]["p50"] != payload["ttft_probe_summary"]["prefill_ms"]["p50"]
    assert "探针" in payload["ttft_note"]
