"""实测对照 llama.cpp ``cache_prompt`` 开与关时的前缀 KV 命中与耗时。

复用 P0-05 五节点固定在线样例：每个节点连续调用两次，使“同节点完整 system
命中”和“跨节点只命中共享前缀”都能被观察到。随后追加两轮独立 ReAct 决定，
模拟多轮循环共用同一 system。阶段之间插入不共享前缀的 breaker 请求，避免
上一阶段残留 KV 污染下一阶段。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent.context.contracts import NodeRoute
from agent.context.prompt_registry import P005_PROMPT_VERSION
from agent.context.shared_prefix import SHARED_PREFIX_ID, SHARED_PREFIX_VERSION, shared_system_prefix_digest
from evals.p019.react_contracts import REACT_PROMPT_ID, REACT_PROMPT_VERSION, ReActDecision
from evals.p019.react_runner import REACT_SYSTEM_PROMPT
from scripts.smoke_p005_prompts import RecordingProvider, _build_cases
from services.config import load_settings
from services.model_gateway import ChatMessage, ModelProvider
from services.model_gateway.contracts import TokenUsage


def _provider(*, cache_enabled: bool) -> RecordingProvider:
    """按开关构造独立 Provider，不让调用方注入 extra_body。"""

    environment = dict(os.environ)
    environment["LLM_PROFILE"] = "fast"
    environment.pop("LLM_MODEL", None)
    environment["LLM_PROMPT_CACHE_ENABLED"] = "true" if cache_enabled else "false"
    # 关闭缓存时完整 prefill+生成经常超过默认 120s；对照脚本单独放宽，不改仓库默认。
    environment.setdefault("LLM_GENERATION_TIMEOUT_SECONDS", "180")
    settings = load_settings(environ=environment, load_dotenv_file=True)
    if settings.model_gateway.prompt_cache_enabled is not cache_enabled:
        raise RuntimeError("prompt_cache_enabled 没有按环境变量生效")
    return RecordingProvider(ModelProvider(settings.model_gateway))


def _usage_fields(usage: TokenUsage | None) -> dict[str, int | None]:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
    }


def hit_ratio(cached: int | None, prompt_tokens: int | None) -> float | None:
    """缓存命中率；缺 usage 或分母为 0 时返回 None。"""

    if cached is None or prompt_tokens is None or prompt_tokens <= 0:
        return None
    return min(1.0, max(0.0, cached / prompt_tokens))


def summarize_calls(records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一轮对照的墙钟、Token 与命中率，不含 breaker/warmup。"""

    walls = [float(item["wall_ms"]) for item in records]
    inputs = [item["input_tokens"] for item in records if isinstance(item.get("input_tokens"), int)]
    cached = [
        item["cached_input_tokens"]
        for item in records
        if isinstance(item.get("cached_input_tokens"), int)
    ]
    successes = sum(1 for item in records if item.get("ok") is True)
    return {
        "calls": len(records),
        "successes": successes,
        "wall_ms_sum": round(sum(walls), 1) if walls else 0.0,
        "wall_ms_mean": round(sum(walls) / len(walls), 1) if walls else 0.0,
        "input_tokens_sum": sum(inputs) if inputs else None,
        "cached_input_tokens_sum": sum(cached) if cached else 0,
        "cached_input_tokens_reported": len(cached),
        "hit_ratio": hit_ratio(sum(cached), sum(inputs)) if inputs and cached else None,
    }


def call_key(record: dict[str, Any]) -> tuple[str, str, int]:
    return (str(record.get("kind")), str(record.get("node")), int(record.get("repeat") or 0))


def matched_success_comparison(
    off_calls: list[dict[str, Any]],
    on_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """只比较两阶段都成功的同一节点/轮次，避免把超时失败算进加速比。"""

    on_map = {call_key(item): item for item in on_calls}
    pairs: list[dict[str, Any]] = []
    off_sum = 0.0
    on_sum = 0.0
    for off in off_calls:
        if off.get("ok") is not True:
            continue
        on = on_map.get(call_key(off))
        if on is None or on.get("ok") is not True:
            continue
        off_ms = float(off["wall_ms"])
        on_ms = float(on["wall_ms"])
        off_sum += off_ms
        on_sum += on_ms
        pairs.append(
            {
                "node": off.get("node"),
                "repeat": off.get("repeat"),
                "cache_off_wall_ms": off_ms,
                "cache_on_wall_ms": on_ms,
                "speedup": round(off_ms / max(on_ms, 0.001), 3),
                "cache_off_cached_input_tokens": off.get("cached_input_tokens"),
                "cache_on_cached_input_tokens": on.get("cached_input_tokens"),
                "cache_on_hit_ratio": on.get("hit_ratio"),
            }
        )
    return {
        "pairs": len(pairs),
        "wall_ms_sum_cache_off": round(off_sum, 1),
        "wall_ms_sum_cache_on": round(on_sum, 1),
        "wall_speedup_off_over_on": round(off_sum / max(on_sum, 0.001), 3) if pairs else None,
        "calls": pairs,
    }


def _break_slot_cache(provider: ModelProvider) -> dict[str, Any]:
    """发送与 AMR 前缀无关的短请求，清空槽内可复用前缀。"""

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
    return {
        "kind": "breaker",
        "wall_ms": round(wall_ms, 1),
        **_usage_fields(call.usage),
    }


def _warmup(provider: ModelProvider) -> dict[str, Any]:
    """先跑一次短生成，让 GPU kernel 进入稳态，不计入对照成绩。"""

    started = time.perf_counter()
    call = provider.generate_text(
        [
            ChatMessage(role="system", content="Warmup. Reply with OK."),
            ChatMessage(role="user", content="OK"),
        ]
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    return {
        "kind": "warmup",
        "wall_ms": round(wall_ms, 1),
        **_usage_fields(call.usage),
    }


def _run_p005_pairs(recording: RecordingProvider, *, repeats: int) -> list[dict[str, Any]]:
    """每个节点连续 repeats 次，便于观察同节点第二次的完整前缀命中。"""

    cases = _build_cases(datetime.now(timezone.utc))
    records: list[dict[str, Any]] = []
    for node_name, runner, context, semantic_check in cases:
        for repeat_index in range(1, repeats + 1):
            recording.last_generation = None
            started = time.perf_counter()
            ok = False
            error: str | None = None
            route = None
            try:
                result = runner(recording, context)
                route = result.route.value
                if result.route is not NodeRoute.SUCCESS or result.output is None:
                    raise AssertionError(
                        f"route={result.route.value} reason={result.reason_code}:{result.reason}"
                    )
                semantic_check(result.output)
                ok = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            wall_ms = (time.perf_counter() - started) * 1000.0
            generation = recording.last_generation
            usage = generation.total_usage if generation is not None else None
            record = {
                "kind": "p005",
                "node": node_name.value,
                "prompt_version": P005_PROMPT_VERSION,
                "repeat": repeat_index,
                "ok": ok,
                "route": route,
                "error": error,
                "wall_ms": round(wall_ms, 1),
                "attempts": None if generation is None else generation.attempts,
                "repaired": None if generation is None else generation.repaired,
                **_usage_fields(usage),
                "hit_ratio": hit_ratio(
                    None if usage is None else usage.cached_input_tokens,
                    None if usage is None else usage.input_tokens,
                ),
            }
            records.append(record)
            status = "PASS" if ok else "FAIL"
            cached = record["cached_input_tokens"]
            print(
                f"  [{status}] {node_name.value} repeat={repeat_index} "
                f"wall_ms={record['wall_ms']:.0f} input={record['input_tokens']} "
                f"cached={cached} hit={record['hit_ratio']}",
                flush=True,
            )
    return records


def _run_react_pairs(recording: RecordingProvider, *, repeats: int) -> list[dict[str, Any]]:
    """同一 ReAct system 前缀、不同 user JSON，模拟循环中的前缀复用。"""

    records: list[dict[str, Any]] = []
    for repeat_index in range(1, repeats + 1):
        recording.last_generation = None
        user_payload = {
            "step": repeat_index,
            "goal": "完成 ORDER-LIVE-701",
            "allowed_tools": ["allocate_tasks", "plan_multi_amr_routes"],
        }
        started = time.perf_counter()
        ok = False
        error: str | None = None
        try:
            generation = recording.generate_structured(
                [
                    ChatMessage(role="system", content=REACT_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                ],
                ReActDecision,
                max_output_tokens=256,
                timeout_seconds=90,
            )
            if generation.value is None:
                raise AssertionError("empty ReActDecision")
            ok = True
        except Exception as exc:
            generation = recording.last_generation
            error = f"{type(exc).__name__}: {exc}"
        wall_ms = (time.perf_counter() - started) * 1000.0
        usage = None if recording.last_generation is None else recording.last_generation.total_usage
        record = {
            "kind": "react_decide",
            "node": "react_decide",
            "prompt_id": REACT_PROMPT_ID,
            "prompt_version": REACT_PROMPT_VERSION,
            "repeat": repeat_index,
            "ok": ok,
            "error": error,
            "wall_ms": round(wall_ms, 1),
            "attempts": None if recording.last_generation is None else recording.last_generation.attempts,
            "repaired": None if recording.last_generation is None else recording.last_generation.repaired,
            **_usage_fields(usage),
            "hit_ratio": hit_ratio(
                None if usage is None else usage.cached_input_tokens,
                None if usage is None else usage.input_tokens,
            ),
        }
        records.append(record)
        status = "PASS" if ok else "FAIL"
        print(
            f"  [{status}] react_decide repeat={repeat_index} "
            f"wall_ms={record['wall_ms']:.0f} input={record['input_tokens']} "
            f"cached={record['cached_input_tokens']} hit={record['hit_ratio']}",
            flush=True,
        )
    return records


def _run_phase(name: str, *, cache_enabled: bool, repeats: int) -> dict[str, Any]:
    recording = _provider(cache_enabled=cache_enabled)
    version = recording.provider.startup()
    print(
        f"\n=== {name} cache_prompt={cache_enabled} alias={version.served_alias} ===",
        flush=True,
    )
    p005 = _run_p005_pairs(recording, repeats=repeats)
    react = _run_react_pairs(recording, repeats=repeats)
    scored = [*p005, *react]
    return {
        "name": name,
        "cache_prompt": cache_enabled,
        "model_alias": version.served_alias,
        "calls": scored,
        "summary": summarize_calls(scored),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2, help="每个节点连续调用次数")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "tmp" / "prompt_cache_compare.json"),
    )
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats 必须 >= 1")

    warmup_provider = _provider(cache_enabled=True)
    warmup_provider.provider.startup()
    warmup = _warmup(warmup_provider.provider)
    print(
        f"warmup wall_ms={warmup['wall_ms']:.0f} cached={warmup['cached_input_tokens']}",
        flush=True,
    )
    breaker_before_off = _break_slot_cache(warmup_provider.provider)

    off_phase = _run_phase("cache_off", cache_enabled=False, repeats=args.repeats)
    breaker_before_on = _break_slot_cache(warmup_provider.provider)
    on_phase = _run_phase("cache_on", cache_enabled=True, repeats=args.repeats)

    off_summary = off_phase["summary"]
    on_summary = on_phase["summary"]
    speedup = None
    if off_summary["wall_ms_sum"] > 0:
        speedup = round(off_summary["wall_ms_sum"] / max(on_summary["wall_ms_sum"], 0.001), 3)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prompt_version_p005": P005_PROMPT_VERSION,
        "prompt_version_react": REACT_PROMPT_VERSION,
        "shared_prefix_id": SHARED_PREFIX_ID,
        "shared_prefix_version": SHARED_PREFIX_VERSION,
        "shared_prefix_sha256": shared_system_prefix_digest(),
        "repeats": args.repeats,
        "warmup": warmup,
        "breaker_before_cache_off": breaker_before_off,
        "breaker_before_cache_on": breaker_before_on,
        "phases": [off_phase, on_phase],
        "comparison": {
            "cache_off": off_summary,
            "cache_on": on_summary,
            "wall_speedup_off_over_on": speedup,
            "matched_successes": matched_success_comparison(off_phase["calls"], on_phase["calls"]),
            "note": (
                "全量 wall_speedup 会把 cache_off 超时计入；公平口径看 matched_successes。"
            ),
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\n=== comparison ===", flush=True)
    print(json.dumps(report["comparison"], ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {output_path}", flush=True)
    off_ok = off_summary["successes"] == off_summary["calls"]
    on_ok = on_summary["successes"] == on_summary["calls"]
    return 0 if off_ok and on_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
