"""LLM 延迟 Benchmark 包：TTFT 来自流式首 token，Prefill 来自 llama.cpp prompt eval。"""

from evals.perf.benchmark import run_benchmark
from evals.perf.contracts import LatencySample, metric_definitions_for_report
from evals.perf.legacy import restated_legacy_metrics
from evals.perf.stats import percentile, summarize_samples

__all__ = [
    "LatencySample",
    "metric_definitions_for_report",
    "percentile",
    "restated_legacy_metrics",
    "run_benchmark",
    "summarize_samples",
]
