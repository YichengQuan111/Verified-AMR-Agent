"""从 P0-18 在线报告抽出 36 个 LLM 案例，并附 llama.cpp Prefill。

P0-18 正式身份仍是完整 60 例。缓存/TTFT 实验可用固定 36 个 LLM case_id
预先筛选（不改 ``dataset.json`` 配额），报告须标明 ``experiment_scope=llm36``，
不得当成正式 P0-18 发布分数。

生产 PEVR 默认仍走 ``ModelProvider`` 的 ``stream=false``，此时 TTFT 保持缺失，
原因码 ``non_streaming_response``。若在线评测启用了 ``--measure-ttft`` 探针，
可把 ``pevr_ttft_metrics.json`` / jsonl 样本传入，TTFT 才来自流式首 token。
Prefill 只来自日志增量里的 ``prompt eval time``，不得用 ``progress=1.00`` 冒充。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from evals.perf.contracts import LatencySample, metric_definitions_for_report
from evals.perf.llama_log import cache_hit_inference_from_log, parse_llama_slot_timings
from evals.perf.stats import percentile, summarize_samples


EXPECTED_DATASET_CASES = 60
EXPECTED_LLM_CASES = 36
DEFAULT_MIN_PROMPT_EVAL_TOKENS = 200
TTFT_MISSING_REASON = "non_streaming_response"

# 2026-08-31 / 2026-09-01 有缓存 60 例里 ``model_call_count>0`` 的固定 36 个 ID。
# 实验筛选用这份名单，避免先跑一遍 60 例才知道谁调了模型。
LLM_CASE_IDS: tuple[str, ...] = (
    *(f"p018-normal-{index:03d}" for index in range(1, 26)),
    "p018-rag-009",
    "p018-rag-010",
    "p018-exception-001",
    "p018-exception-002",
    "p018-exception-003",
    "p018-exception-004",
    "p018-exception-005",
    "p018-exception-006",
    "p018-exception-009",
    "p018-exception-010",
    "p018-security-001",
)


def _case_id_of(case: Any) -> str | None:
    value = getattr(case, "case_id", None)
    if value is None and isinstance(case, Mapping):
        value = case.get("case_id")
    return str(value) if value is not None else None


def filter_llm_cases_by_id(cases: list[Any]) -> list[Any]:
    """按固定 36 个 LLM case_id 筛选，保持数据集原顺序。"""

    wanted = set(LLM_CASE_IDS)
    return [case for case in cases if _case_id_of(case) in wanted]


def is_llm_case(case: Mapping[str, Any]) -> bool:
    """有真实模型调用的案例；sidecar / 安全门禁短路径会被排除。"""

    return int((case.get("metrics") or {}).get("model_call_count") or 0) > 0


def select_llm_cases(cases: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [case for case in cases if is_llm_case(case)]


def summarize_case_walls(cases: list[Mapping[str, Any]]) -> dict[str, Any]:
    """PEVR 案例墙钟，含 C++/仿真，不是单次模型调用 E2E。"""

    walls = [float((case.get("metrics") or {}).get("wall_clock_ms") or 0.0) for case in cases]
    passed = sum(1 for case in cases if case.get("evaluation_passed") is True)
    model_calls = sum(int((case.get("metrics") or {}).get("model_call_count") or 0) for case in cases)
    failed = [
        {
            "case_id": case.get("case_id"),
            "observed_outcome": case.get("observed_outcome"),
            "failure_reason": case.get("failure_reason"),
            "model_call_count": (case.get("metrics") or {}).get("model_call_count"),
        }
        for case in cases
        if case.get("evaluation_passed") is not True
    ]
    return {
        "case_count": len(cases),
        "evaluation_pass_count": passed,
        "model_call_count": model_calls,
        "wall_ms_sum": round(sum(walls), 1) if walls else None,
        "wall_ms_mean": round(sum(walls) / len(walls), 1) if walls else None,
        "wall_ms_p50": percentile(walls, 50),
        "wall_ms_p95": percentile(walls, 95),
        "wall_ms_max": round(max(walls), 1) if walls else None,
        "failed_cases": failed,
        "note": "PEVR 案例墙钟（含 C++/仿真），不是单次模型调用 E2E。",
    }


def summarize_prefill_from_log(
    text: str,
    *,
    min_prompt_eval_tokens: int = DEFAULT_MIN_PROMPT_EVAL_TOKENS,
) -> dict[str, Any]:
    """只统计日志里的 Prefill；短 Prompt（breaker）按 token 阈值排除。"""

    timings = parse_llama_slot_timings(text)
    excluded = {"breaker_or_short_prompt": 0, "missing_prompt_eval": 0}
    prefill_values: list[float] = []
    prompt_tokens_sum = 0
    prefill_tokens_sum = 0
    cached_tokens_sum = 0
    hit_source_known = 0
    for item in timings:
        if item.prompt_eval_ms is None:
            excluded["missing_prompt_eval"] += 1
            continue
        if item.prompt_eval_tokens is not None and item.prompt_eval_tokens < min_prompt_eval_tokens:
            excluded["breaker_or_short_prompt"] += 1
            continue
        prefill_values.append(float(item.prompt_eval_ms))
        inferred = cache_hit_inference_from_log(item)
        prompt_tokens = inferred.get("prompt_tokens")
        # 命中率分母只用能还原 Prompt 长度的样本。
        if prompt_tokens:
            prompt_tokens_sum += int(prompt_tokens)
        if item.prompt_eval_tokens is not None:
            prefill_tokens_sum += int(item.prompt_eval_tokens)
        if inferred.get("cached_tokens") is not None:
            cached_tokens_sum += int(inferred["cached_tokens"])
            hit_source_known += 1

    hit_rate = None
    if prompt_tokens_sum > 0 and hit_source_known > 0:
        hit_rate = round(min(1.0, max(0.0, cached_tokens_sum / prompt_tokens_sum)), 4)

    return {
        "slot_tasks_seen": len(timings),
        "prefill_samples": len(prefill_values),
        "prefill_ms": {
            "n": len(prefill_values),
            "mean": round(sum(prefill_values) / len(prefill_values), 1) if prefill_values else None,
            "p50": percentile(prefill_values, 50),
            "p95": percentile(prefill_values, 95),
        },
        "cache_hit_rate": hit_rate,
        "cached_input_tokens_sum": cached_tokens_sum if hit_source_known else None,
        "prompt_tokens_sum": prompt_tokens_sum or None,
        "prefill_tokens_sum": prefill_tokens_sum or None,
        "cache_hit_source": "n_prompt_or_n_tokens_stop_minus_prompt_eval_tokens",
        "ttft_ms": None,
        "ttft_missing_reason": TTFT_MISSING_REASON,
        "excluded": excluded,
    }


def read_log_delta(path: str | Path, *, byte_offset: int = 0) -> str:
    """从评测开始前记录的字节偏移读取增量，避免混入更早的 Benchmark 日志。"""

    log_path = Path(path)
    if not log_path.is_file():
        return ""
    size = log_path.stat().st_size
    if byte_offset < 0 or byte_offset > size:
        return ""
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(byte_offset)
        return handle.read()


def _ttft_block(
    samples: list[Any] | None,
) -> tuple[Any, str | None, str, dict[str, Any] | None]:
    """没有探针样本时 TTFT 保持缺失；有样本时用契约百分位，不用 Prefill 回填。"""

    production_note = (
        "生产 PEVR 使用 ModelProvider stream=false，无法取得客户端首 token。"
        "不得用 Prefill 或 progress=1.00 回填。"
    )
    if not samples:
        return None, TTFT_MISSING_REASON, production_note, None
    parsed: list[LatencySample] = []
    for item in samples:
        if isinstance(item, LatencySample):
            parsed.append(item)
        else:
            parsed.append(LatencySample.from_dict(item))
    summary = summarize_samples(parsed)
    ttft = summary.get("ttft_ms") or {}
    probe_note = (
        "TTFT 来自评测专用 stream=true 探针（--measure-ttft），不是生产 ModelProvider。"
        "不得用 Prefill 或 progress=1.00 回填。"
    )
    if int(ttft.get("n") or 0) > 0:
        return ttft, None, probe_note, summary
    return ttft, "no_generated_text_delta", probe_note, summary


def summarize_pevr_llm_report(
    report: Mapping[str, Any],
    *,
    log_text: str | None = None,
    log_byte_offset: int = 0,
    cache_prompt: bool | None = True,
    ttft_samples: list[Any] | None = None,
) -> dict[str, Any]:
    """把完整 60 例报告收成 36 LLM 案例指标。无探针时 TTFT 为 null。"""

    cases = list(report.get("cases") or [])
    llm_cases = select_llm_cases(cases)
    prefill = summarize_prefill_from_log(log_text) if log_text else None
    ttft_ms, ttft_missing_reason, ttft_note, ttft_summary = _ttft_block(ttft_samples)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "amr.pevr_llm36.v1",
        "definitions": metric_definitions_for_report(),
        "cache_prompt": cache_prompt,
        "dataset_case_count": len(cases),
        "expected_dataset_case_count": EXPECTED_DATASET_CASES,
        "llm_case_count": len(llm_cases),
        "expected_llm_case_count": EXPECTED_LLM_CASES,
        "report_id": report.get("report_id"),
        "report_digest": report.get("report_digest"),
        "report_status": report.get("status"),
        "ttft_ms": ttft_ms,
        "ttft_missing_reason": ttft_missing_reason,
        "ttft_note": ttft_note,
        "ttft_probe_summary": ttft_summary,
        "case_ids": [case.get("case_id") for case in llm_cases],
        "e2e": summarize_case_walls(llm_cases),
        "all_cases_e2e": summarize_case_walls(cases),
        "prefill": prefill,
        "log_byte_offset": log_byte_offset,
    }


def load_eval_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_ttft_samples(path: str | Path) -> list[dict[str, Any]]:
    """读取 ``pevr_ttft_metrics.json`` 或 jsonl 样本。"""

    sample_path = Path(path)
    if not sample_path.is_file():
        return []
    if sample_path.suffix.lower() == ".jsonl":
        return [
            json.loads(line)
            for line in sample_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    samples = payload.get("samples")
    return list(samples) if isinstance(samples, list) else []


__all__ = [
    "EXPECTED_DATASET_CASES",
    "EXPECTED_LLM_CASES",
    "LLM_CASE_IDS",
    "TTFT_MISSING_REASON",
    "filter_llm_cases_by_id",
    "is_llm_case",
    "load_eval_report",
    "load_ttft_samples",
    "read_log_delta",
    "select_llm_cases",
    "summarize_case_walls",
    "summarize_pevr_llm_report",
    "summarize_prefill_from_log",
]
