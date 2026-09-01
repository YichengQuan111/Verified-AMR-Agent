"""36 个 LLM 例 PEVR 有/无 ``cache_prompt`` 对照，TTFT 走流式探针。

正式 P0-18 仍是 60 例。本实验：

- 只跑固定 36 个 LLM case_id；
- 每阶段 ``--measure-ttft``，TTFT 只来自客户端首个非空 SSE delta；
- 阶段前发送 breaker，避免槽内 KV 污染关缓存组；
- 子进程隔离 ``LLM_PROMPT_CACHE_ENABLED``；
- 不得用 Prefill / ``progress=1.00`` 回填 TTFT；
- 产物不是正式 P0-18 发布报告。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.prompt_registry import P005_PROMPT_VERSION
from agent.context.shared_prefix import SHARED_PREFIX_ID, SHARED_PREFIX_VERSION, shared_system_prefix_digest
from evals.perf.contracts import LatencySample, metric_definitions_for_report
from evals.perf.llm36 import (
    EXPECTED_LLM_CASES,
    LLM_CASE_IDS,
    load_eval_report,
    load_ttft_samples,
    summarize_pevr_llm_report,
)
from evals.perf.stats import compare_cache_summaries, summarize_samples
from services.config import load_settings
from services.model_gateway import ChatMessage, ModelProvider


PYTHON_EXE = Path(os.environ.get("AMR_PYTHON_EXE") or r"E:\Anaconda\envs\torch128\python.exe")
ONLINE_CONFIG = PROJECT_ROOT / "evals" / "p018" / "online_config.json"
# 带日期的新目录，避免覆盖 8/30、8/31、9/1 已有对照产物。
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "tmp" / "p018_pevr_llm36_ttft_cache_20260901"


def _print(message: str) -> None:
    print(message, flush=True)


def _break_slot_cache() -> dict[str, Any]:
    """发送与 AMR 共享前缀无关的短请求，打断槽内可复用 KV。"""

    environment = dict(os.environ)
    environment["LLM_PROFILE"] = "fast"
    environment.pop("LLM_MODEL", None)
    settings = load_settings(environ=environment, load_dotenv_file=True)
    provider = ModelProvider(settings.model_gateway)
    provider.startup()
    started = time.perf_counter()
    call = provider.generate_text(
        [
            ChatMessage(
                role="system",
                content=f"CACHE-BREAKER {uuid.uuid4().hex} 只回复两个字母 OK。",
            ),
            ChatMessage(role="user", content="OK"),
        ]
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    usage = call.usage
    return {
        "kind": "breaker",
        "wall_ms": round(wall_ms, 1),
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "cached_input_tokens": None if usage is None else usage.cached_input_tokens,
    }


def _log_offset(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _samples_from_probe(output_dir: Path) -> list[LatencySample]:
    raw = load_ttft_samples(output_dir / "pevr_ttft_metrics.json")
    return [LatencySample.from_dict(item) for item in raw]


def _assert_ttft_rigor(summary: dict[str, Any], *, phase: str) -> None:
    ttft = summary.get("ttft_ms") or {}
    if ttft.get("filled_from_prefill") is True:
        raise RuntimeError(f"{phase} TTFT 被 Prefill 回填，拒绝作为结论")
    invariants = summary.get("invariants") or {}
    if invariants.get("pseudo_ttft_from_progress_100") is True:
        raise RuntimeError(f"{phase} 使用了 progress=1.00 伪 TTFT")
    violations = invariants.get("ttft_lte_e2e_violations") or []
    if violations:
        raise RuntimeError(f"{phase} 存在 TTFT>E2E：{violations[:5]}")


def _run_phase(*, name: str, cache_enabled: bool, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LLM_PROFILE"] = "fast"
    env.pop("LLM_MODEL", None)
    env["LLM_PROMPT_CACHE_ENABLED"] = "true" if cache_enabled else "false"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("LLM_EVAL_TTFT", None)
    log_path = PROJECT_ROOT / "tmp" / "llama-server.err.log"
    log_offset = _log_offset(log_path)
    command = [
        str(PYTHON_EXE),
        "-u",
        "-m",
        "evals.p018.run_eval",
        "--config",
        str(ONLINE_CONFIG),
        "--output-dir",
        str(output_dir),
        "--verification-timeout",
        "120",
        "--measure-ttft",
        "--llm-only",
    ]
    _print(f"\n=== {name} cache_prompt={cache_enabled} output={output_dir} ttft=on llm36 ===")
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), env=env, check=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    report_path = output_dir / "p018_online_eval.json"
    if not report_path.is_file():
        raise RuntimeError(f"{name} 未写出 {report_path}，退出码 {completed.returncode}")
    report = load_eval_report(report_path)
    cases = list(report.get("cases") or [])
    if len(cases) != EXPECTED_LLM_CASES:
        raise RuntimeError(f"{name} 例数是 {len(cases)}，不是 {EXPECTED_LLM_CASES}")
    samples = _samples_from_probe(output_dir)
    probe_summary = summarize_samples(samples) if samples else None
    if probe_summary is not None:
        _assert_ttft_rigor(probe_summary, phase=name)
    llm36 = summarize_pevr_llm_report(
        report,
        cache_prompt=cache_enabled,
        ttft_samples=[item.to_dict() for item in samples],
    )
    _print(
        f"=== {name} done exit={completed.returncode} elapsed_ms={elapsed_ms} "
        f"pass={llm36['all_cases_e2e']['evaluation_pass_count']}/{EXPECTED_LLM_CASES} "
        f"ttft_n={(probe_summary or {}).get('ttft_ms', {}).get('n')} "
        f"wall_p50={llm36['all_cases_e2e']['wall_ms_p50']} ==="
    )
    return {
        "name": name,
        "cache_prompt": cache_enabled,
        "exit_code": completed.returncode,
        "phase_elapsed_ms": elapsed_ms,
        "log_byte_offset": log_offset,
        "report_id": report.get("report_id"),
        "report_digest": report.get("report_digest"),
        "report_status": report.get("status"),
        "report_path": str(report_path),
        "official_p018_publish": False,
        "llm36": llm36,
        "ttft_probe_summary": probe_summary,
        "ttft_sample_count": len(samples),
        "cases": cases,
    }


def run_llm36_cache_compare(*, output_root: Path) -> dict[str, Any]:
    """breaker → cache_off → breaker → cache_on，全程流式 TTFT。"""

    output_root.mkdir(parents=True, exist_ok=True)
    warmup_settings = load_settings()
    if warmup_settings.model_gateway.profile != "fast":
        raise RuntimeError("对照必须使用 Fast Profile")
    ModelProvider(warmup_settings.model_gateway).startup()
    breaker_before_off = _break_slot_cache()
    off_phase = _run_phase(
        name="cache_off",
        cache_enabled=False,
        output_dir=output_root / "cache_off",
    )
    breaker_before_on = _break_slot_cache()
    on_phase = _run_phase(
        name="cache_on",
        cache_enabled=True,
        output_dir=output_root / "cache_on",
    )
    off_summary = off_phase["ttft_probe_summary"] or {}
    on_summary = on_phase["ttft_probe_summary"] or {}
    speedups = compare_cache_summaries(off_summary, on_summary)
    off_wall = off_phase["llm36"]["all_cases_e2e"]
    on_wall = on_phase["llm36"]["all_cases_e2e"]
    wall_speedup = None
    if off_wall.get("wall_ms_p50") and on_wall.get("wall_ms_p50"):
        from evals.perf.stats import speedup

        wall_speedup = speedup(float(off_wall["wall_ms_p50"]), float(on_wall["wall_ms_p50"]))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "amr.pevr_llm36_ttft_cache_compare.v1",
        "definitions": metric_definitions_for_report(),
        "strategy": "pevr",
        "experiment_scope": "llm36",
        "official_p018_publish": False,
        "case_ids": list(LLM_CASE_IDS),
        "prompt_version_p005": P005_PROMPT_VERSION,
        "shared_prefix_id": SHARED_PREFIX_ID,
        "shared_prefix_version": SHARED_PREFIX_VERSION,
        "shared_prefix_sha256": shared_system_prefix_digest(),
        "note": (
            "只跑 36 个 LLM 例的生产 PEVR 闭环。TTFT 来自评测专用 stream=true 探针，"
            "不是生产 ModelProvider，也不是 progress=1.00。案例墙钟含 C++/仿真。"
            "本对照不是正式 P0-18 发布分数。"
        ),
        "breaker_before_cache_off": breaker_before_off,
        "breaker_before_cache_on": breaker_before_on,
        "phases": [
            {key: value for key, value in off_phase.items() if key != "cases"},
            {key: value for key, value in on_phase.items() if key != "cases"},
        ],
        "comparison": {
            "ttft_prefill_model_e2e": speedups,
            "case_wall_p50_speedup_off_over_on": wall_speedup,
            "cache_off": {
                "pass": f"{off_wall['evaluation_pass_count']}/{EXPECTED_LLM_CASES}",
                "model_call_count": off_wall.get("model_call_count"),
                "wall_ms_p50": off_wall.get("wall_ms_p50"),
                "ttft_ms_p50": (off_summary.get("ttft_ms") or {}).get("p50"),
                "prefill_ms_p50": (off_summary.get("prefill_ms") or {}).get("p50"),
                "cache_hit_ratio": (off_summary.get("cache") or {}).get("hit_ratio"),
                "ttft_valid_n": (off_summary.get("ttft_ms") or {}).get("n"),
            },
            "cache_on": {
                "pass": f"{on_wall['evaluation_pass_count']}/{EXPECTED_LLM_CASES}",
                "model_call_count": on_wall.get("model_call_count"),
                "wall_ms_p50": on_wall.get("wall_ms_p50"),
                "ttft_ms_p50": (on_summary.get("ttft_ms") or {}).get("p50"),
                "prefill_ms_p50": (on_summary.get("prefill_ms") or {}).get("p50"),
                "cache_hit_ratio": (on_summary.get("cache") or {}).get("hit_ratio"),
                "ttft_valid_n": (on_summary.get("ttft_ms") or {}).get("n"),
            },
        },
    }
    output_path = output_root / "llm36_ttft_cache_compare.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _print("\n=== llm36 ttft cache comparison ===")
    _print(json.dumps(payload["comparison"], ensure_ascii=False, indent=2))
    _print(f"wrote {output_path}")
    return payload


__all__ = ["run_llm36_cache_compare"]
