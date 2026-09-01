"""把旧的非流式日志统计改写成符合新契约的报告。

2026-08-31 的 ``llm_only_cache_metrics.json`` 用 ``progress=1.00`` 冒充 TTFT，
并在缺少 progress 时用 ``prompt eval time`` 回填。本模块：

- 删除/作废这些 TTFT 数字；
- 保留 Prefill、缓存命中率和 PEVR 案例 E2E（它们不是伪 TTFT）；
- 若提供 llama.cpp 日志，只重算 Prefill，绝不输出 progress 派生 TTFT。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from evals.perf.contracts import metric_definitions_for_report
from evals.perf.llama_log import parse_llama_slot_timings
from evals.perf.stats import percentile, speedup


LEGACY_TTFT_NOTE = (
    "2026-08-31 的 TTFT 由 launch_slot→progress=1.00 得出，且 progress 缺失时用 "
    "prompt eval time 回填。该口径会提前约 110–264ms，且混合了服务端 Prefill。"
    "正式结论不得再引用这些 TTFT 数字。"
)


def restated_legacy_metrics(
    legacy: Mapping[str, Any],
    *,
    log_text: str | None = None,
    min_prompt_eval_tokens: int = 200,
) -> dict[str, Any]:
    """输出修正后的对照表：TTFT 全部缺失，Prefill/E2E/命中率可保留。"""

    def _side(name: str) -> dict[str, Any]:
        block = dict(legacy.get(name) or {})
        e2e = dict(block.get("e2e") or {})
        return {
            "model_calls_logged": block.get("model_calls_logged"),
            "ttft_ms_p50": None,
            "ttft_ms_p95": None,
            "ttft_ms_mean": None,
            "ttft_missing_reason": "legacy_log_only_no_client_clock",
            "legacy_invalid_ttft_ms_p50": block.get("ttft_ms_p50"),
            "legacy_invalid_ttft_ms_p95": block.get("ttft_ms_p95"),
            "legacy_invalid_ttft_ms_mean": block.get("ttft_ms_mean"),
            "prefill_ms_mean": block.get("prefill_ms_mean"),
            "prefill_ms_p50": block.get("prefill_ms_p50"),
            "prefill_ms_p95": block.get("prefill_ms_p95"),
            "cache_hit_rate": block.get("cache_hit_rate"),
            "cached_input_tokens_sum": block.get("cached_input_tokens_sum"),
            "prompt_tokens_sum": block.get("prompt_tokens_sum"),
            "prefill_tokens_sum": block.get("prefill_tokens_sum"),
            "e2e": e2e,
            "e2e_note": (
                "PEVR 案例墙钟（含 C++/仿真），不是单次模型调用 E2E。"
                "该数字仍可与 2026-08-31 对照一起引用。"
            ),
        }

    off = _side("cache_off")
    on = _side("cache_on")
    parsed_log: dict[str, Any] | None = None
    if log_text:
        parsed_log = summarize_log_prefill_only(log_text, min_prompt_eval_tokens=min_prompt_eval_tokens)

    original_speedup = dict(legacy.get("speedup_off_over_on") or {})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "amr.llm_legacy_metrics.ttft_withdrawn.v1",
        "source": "tmp/p018_pevr_cache_compare_20260831/llm_only_cache_metrics.json",
        "definitions": metric_definitions_for_report(),
        "ttft_status": "invalid_do_not_cite",
        "ttft_note": LEGACY_TTFT_NOTE,
        "prefill_status": "retained",
        "prefill_note": (
            "Prefill 来自 llama.cpp 最终 prompt eval time，不受 progress 四舍五入影响。"
            "约 1.44× 的 Prefill 加速结论仍然成立。"
        ),
        "e2e_status": "retained_as_pevr_case_wall",
        "cache_off": off,
        "cache_on": on,
        "speedup_off_over_on": {
            "ttft_p50": None,
            "ttft_p50_legacy_invalid": original_speedup.get("ttft_p50"),
            "prefill_p50": original_speedup.get("prefill_p50"),
            "e2e_p50": original_speedup.get("e2e_p50"),
            "e2e_mean": original_speedup.get("e2e_mean"),
        },
        "log_reparse": parsed_log,
    }


def summarize_log_prefill_only(text: str, *, min_prompt_eval_tokens: int = 200) -> dict[str, Any]:
    """只从日志提取 Prefill。TTFT 恒为缺失；没有 progress 也不判为缓存命中。"""

    timings = parse_llama_slot_timings(text)
    excluded = {"breaker_or_short_prompt": 0, "missing_prompt_eval": 0}
    prefill_values: list[float] = []
    progress_early: list[float] = []
    missing_progress = 0
    for item in timings:
        if item.prompt_eval_ms is None:
            excluded["missing_prompt_eval"] += 1
            continue
        if item.prompt_eval_tokens is not None and item.prompt_eval_tokens < min_prompt_eval_tokens:
            excluded["breaker_or_short_prompt"] += 1
            continue
        prefill_values.append(float(item.prompt_eval_ms))
        if not item.progress_events:
            missing_progress += 1
        rounded = item.rounded_progress_100_ms
        if rounded is not None:
            delta = float(item.prompt_eval_ms) - rounded
            progress_early.append(delta)

    return {
        "slot_tasks_seen": len(timings),
        "prefill_samples": len(prefill_values),
        "prefill_ms": {
            "n": len(prefill_values),
            "p50": percentile(prefill_values, 50),
            "p95": percentile(prefill_values, 95),
        },
        "ttft_ms": None,
        "ttft_missing_reason": "legacy_log_only_no_client_clock",
        "missing_progress_count": missing_progress,
        "missing_progress_interpreted_as_cache_hit": False,
        "progress_100_earlier_than_prefill_ms": {
            "n": len(progress_early),
            "p50": percentile(progress_early, 50),
            "p95": percentile(progress_early, 95),
            "note": "正值表示 rounded progress=1.00 早于真正的 prompt eval 结束。",
        },
        "excluded": excluded,
        "speedup_placeholder": speedup(None, None),
    }


def load_legacy_metrics(path: str | Path) -> dict[str, Any]:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "LEGACY_TTFT_NOTE",
    "load_legacy_metrics",
    "restated_legacy_metrics",
    "summarize_log_prefill_only",
]
