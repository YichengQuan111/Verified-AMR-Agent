"""延迟样本的百分位、排除计数与 cache on/off 加速比。

百分位只在有效数值上计算。TTFT 缺失时保持缺失，绝不用 Prefill 回填。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from evals.perf.contracts import (
    LatencySample,
    metric_definitions_for_report,
)


def percentile(values: list[float], p: float) -> float | None:
    """线性插值百分位；空列表返回 None，避免把“没有样本”显示成 0 ms。"""

    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError("percentile p must be within [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return round(ordered[low] * (1.0 - frac) + ordered[high] * frac, 1)


def speedup(off_value: float | None, on_value: float | None) -> float | None:
    """无缓存 / 有缓存。任一侧缺失时返回 None，禁止用另一指标冒充。"""

    if off_value is None or on_value is None or on_value <= 0:
        return None
    return round(off_value / on_value, 3)


def hit_ratio(cached: int | None, prompt_tokens: int | None) -> float | None:
    if cached is None or prompt_tokens is None or prompt_tokens <= 0:
        return None
    return round(min(1.0, max(0.0, cached / prompt_tokens)), 4)


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 1) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "min": round(min(values), 1) if values else None,
        "max": round(max(values), 1) if values else None,
    }


def summarize_samples(samples: Iterable[LatencySample]) -> dict[str, Any]:
    """按契约汇总一批样本。warmup/breaker/失败不进入百分位。"""

    items = list(samples)
    excluded = Counter()
    failed = 0
    missing_ttft = 0
    valid: list[LatencySample] = []
    invariant_violations: list[str] = []
    ttft_values: list[float] = []
    e2e_values: list[float] = []
    prefill_values: list[float] = []
    cached_sum = 0
    prompt_sum = 0
    cached_reported = 0

    for sample in items:
        if sample.kind in {"warmup", "breaker"}:
            excluded[sample.kind] += 1
            continue
        failed_reasons = {"timeout", "request_failed", "incomplete_stream", "overlapping_requests"}
        if sample.outcome == "failed" or sample.exclusion_reason in failed_reasons:
            failed += 1
            excluded[sample.exclusion_reason or "request_failed"] += 1
            continue
        # 测量样本：TTFT 缺失时仍可统计 Prefill/E2E，且不得把 Prefill 写进 TTFT。
        if sample.ttft_ms is None:
            missing_ttft += 1
            if sample.ttft_missing_reason:
                excluded[sample.ttft_missing_reason] += 1
        else:
            ttft_values.append(float(sample.ttft_ms))
            valid.append(sample)
            if sample.e2e_ms is not None and sample.ttft_ms > sample.e2e_ms + 1.0:
                invariant_violations.append(sample.request_id)
        if sample.e2e_ms is not None:
            e2e_values.append(float(sample.e2e_ms))
        if sample.prefill_ms is not None:
            prefill_values.append(float(sample.prefill_ms))
        if isinstance(sample.cached_input_tokens, int) and isinstance(sample.prompt_tokens, int):
            cached_sum += sample.cached_input_tokens
            prompt_sum += sample.prompt_tokens
            cached_reported += 1

    missing_prefill = sum(
        1
        for item in items
        if item.kind == "measured"
        and item.outcome not in {"failed", "excluded"}
        and item.exclusion_reason not in {"timeout", "request_failed", "incomplete_stream", "overlapping_requests"}
        and item.prefill_ms is None
    )

    return {
        "definitions": metric_definitions_for_report(),
        "sample_counts": {
            "total": len(items),
            "valid": len(valid),
            "missing_ttft": missing_ttft,
            "failed": failed,
            "excluded": dict(excluded),
        },
        "ttft_ms": {
            **_distribution(ttft_values),
            "missing": missing_ttft,
            "filled_from_prefill": False,
        },
        "prefill_ms": {
            **_distribution(prefill_values),
            "missing": missing_prefill,
        },
        "e2e_ms": _distribution(e2e_values),
        "cache": {
            "hit_ratio": hit_ratio(cached_sum, prompt_sum) if cached_reported else None,
            "cached_input_tokens_sum": cached_sum if cached_reported else None,
            "prompt_tokens_sum": prompt_sum if cached_reported else None,
            "samples_with_usage": cached_reported,
        },
        "invariants": {
            "ttft_lte_e2e_violations": invariant_violations,
            "pseudo_ttft_from_progress_100": False,
        },
    }


def compare_cache_summaries(
    cache_off: Mapping[str, Any],
    cache_on: Mapping[str, Any],
) -> dict[str, Any]:
    """两侧都有有效百分位时才给加速比；TTFT 缺失时不会用 Prefill 比冒充 TTFT。"""

    def _p50(block: Mapping[str, Any], key: str) -> float | None:
        metric = block.get(key) or {}
        value = metric.get("p50")
        return float(value) if isinstance(value, (int, float)) else None

    ttft_speedup = speedup(_p50(cache_off, "ttft_ms"), _p50(cache_on, "ttft_ms"))
    return {
        "ttft_p50_speedup_off_over_on": ttft_speedup,
        "prefill_p50_speedup_off_over_on": speedup(
            _p50(cache_off, "prefill_ms"), _p50(cache_on, "prefill_ms")
        ),
        "e2e_p50_speedup_off_over_on": speedup(
            _p50(cache_off, "e2e_ms"), _p50(cache_on, "e2e_ms")
        ),
        "ttft_speedup_uses_prefill": False,
        "note": None
        if ttft_speedup is not None
        else "TTFT 加速比缺失：至少一侧没有客户端首 token 样本，未用 Prefill 代替。",
    }


__all__ = [
    "compare_cache_summaries",
    "hit_ratio",
    "percentile",
    "speedup",
    "summarize_samples",
]
