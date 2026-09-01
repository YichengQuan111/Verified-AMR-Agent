"""串行流式 TTFT Benchmark。生产 ``ModelProvider`` 仍保持 ``stream=False``。

默认使用共享 system 前缀加一句固定用户指令，便于观察 prefix KV 命中，
同时把 ``max_tokens`` 收得很小，只为拿到首个生成 token。
warmup / breaker 会计入排除数，不进百分位。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.context.shared_prefix import (
    SHARED_PREFIX_ID,
    SHARED_PREFIX_VERSION,
    render_shared_system_prefix,
    shared_system_prefix_digest,
)
from evals.perf.client import StreamingChatClient
from evals.perf.contracts import LatencySample, SampleKind, metric_definitions_for_report
from evals.perf.llama_log import LlamaLogCursor
from evals.perf.stats import compare_cache_summaries, summarize_samples
from services.config import load_settings
from services.config.settings import ModelGatewaySettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LLAMA_LOG = PROJECT_ROOT / "tmp" / "llama-server.err.log"
STABLE_USER_PROMPT = "只回复一个汉字：好"


def measured_messages() -> list[dict[str, str]]:
    """对照用的稳定消息。请求 ID 只放 HTTP 头，避免破坏 Token 前缀。"""

    return [
        {"role": "system", "content": render_shared_system_prefix()},
        {"role": "user", "content": STABLE_USER_PROMPT},
    ]


def breaker_messages() -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": f"CACHE-BREAKER {uuid.uuid4().hex} 只回复两个字母 OK。",
        },
        {"role": "user", "content": "OK"},
    ]


def warmup_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Warmup. Reply with OK."},
        {"role": "user", "content": "OK"},
    ]


def client_from_settings(
    settings: ModelGatewaySettings,
    *,
    llama_log_path: Path | None = DEFAULT_LLAMA_LOG,
    transport: Any = None,
) -> StreamingChatClient:
    profile = settings.active_profile
    cursor = LlamaLogCursor(llama_log_path) if llama_log_path else None
    return StreamingChatClient(
        base_url=settings.base_url,
        api_key=settings.api_key.get_secret_value(),
        model=profile.alias,
        temperature=profile.temperature,
        top_p=profile.top_p,
        top_k=profile.top_k,
        reasoning_enabled=profile.reasoning_enabled,
        reasoning_budget=profile.reasoning_budget_tokens,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        timeout_seconds=settings.generation_timeout_seconds,
        transport=transport,
        llama_log_cursor=cursor,
    )


def _run_call(
    client: StreamingChatClient,
    messages: Sequence[Mapping[str, str]],
    *,
    cache_prompt: bool,
    kind: SampleKind,
    max_tokens: int,
) -> LatencySample:
    return client.complete_stream(
        messages,
        cache_prompt=cache_prompt,
        kind=kind,
        max_tokens=max_tokens,
    )


def run_phase(
    client: StreamingChatClient,
    *,
    name: str,
    cache_prompt: bool,
    repeats: int,
    max_tokens: int,
    include_warmup: bool,
    include_breaker: bool,
) -> dict[str, Any]:
    """跑一个缓存开关阶段。串行发出 warmup/breaker/测量请求。"""

    samples: list[LatencySample] = []
    if include_warmup:
        samples.append(
            _run_call(
                client,
                warmup_messages(),
                cache_prompt=cache_prompt,
                kind="warmup",
                max_tokens=min(8, max_tokens),
            )
        )
    if include_breaker:
        samples.append(
            _run_call(
                client,
                breaker_messages(),
                cache_prompt=False,
                kind="breaker",
                max_tokens=min(8, max_tokens),
            )
        )
    for _ in range(repeats):
        samples.append(
            _run_call(
                client,
                measured_messages(),
                cache_prompt=cache_prompt,
                kind="measured",
                max_tokens=max_tokens,
            )
        )
    summary = summarize_samples(samples)
    return {
        "name": name,
        "cache_prompt": cache_prompt,
        "summary": summary,
        "samples": [item.to_dict() for item in samples],
        "_samples": samples,
    }


def run_benchmark(
    *,
    repeats: int = 2,
    max_tokens: int = 16,
    compare_cache: bool = False,
    llama_log_path: Path | None = DEFAULT_LLAMA_LOG,
    environ: Mapping[str, str] | None = None,
    transport: Any = None,
    settings: ModelGatewaySettings | None = None,
) -> dict[str, Any]:
    """执行 Benchmark 并返回可 JSON 序列化的报告。"""

    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")
    loaded = settings or load_settings(environ=dict(environ or os.environ), load_dotenv_file=True)
    gateway = loaded.model_gateway
    if gateway.profile != "fast":
        raise RuntimeError("TTFT Benchmark 只允许 Fast Profile")
    client = client_from_settings(gateway, llama_log_path=llama_log_path, transport=transport)
    try:
        phases: list[dict[str, Any]] = []
        if compare_cache:
            off_phase = run_phase(
                client,
                name="cache_off",
                cache_prompt=False,
                repeats=repeats,
                max_tokens=max_tokens,
                include_warmup=True,
                include_breaker=True,
            )
            on_phase = run_phase(
                client,
                name="cache_on",
                cache_prompt=True,
                repeats=repeats,
                max_tokens=max_tokens,
                include_warmup=False,
                include_breaker=True,
            )
            phases = [off_phase, on_phase]
            comparison = compare_cache_summaries(off_phase["summary"], on_phase["summary"])
        else:
            enabled = gateway.prompt_cache_enabled
            phase = run_phase(
                client,
                name="cache_on" if enabled else "cache_off",
                cache_prompt=enabled,
                repeats=repeats,
                max_tokens=max_tokens,
                include_warmup=True,
                include_breaker=True,
            )
            phases = [phase]
            comparison = None
    finally:
        client.close()

    serializable_phases = []
    for phase in phases:
        serializable_phases.append({key: value for key, value in phase.items() if key != "_samples"})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "amr.llm_ttft_benchmark.v1",
        "prompt": {
            "shared_prefix_id": SHARED_PREFIX_ID,
            "shared_prefix_version": SHARED_PREFIX_VERSION,
            "shared_prefix_sha256": shared_system_prefix_digest(),
            "user": STABLE_USER_PROMPT,
        },
        "settings": {
            "profile": gateway.profile,
            "alias": gateway.active_profile.alias,
            "base_url": gateway.base_url,
            "repeats": repeats,
            "max_tokens": max_tokens,
            "compare_cache": compare_cache,
            "llama_log_path": None if llama_log_path is None else str(llama_log_path),
            "production_provider_stream": False,
            "benchmark_stream": True,
        },
        "definitions": metric_definitions_for_report(),
        "phases": serializable_phases,
        "comparison": comparison,
        "legacy_ttft_from_progress_100_usable": False,
    }


__all__ = [
    "DEFAULT_LLAMA_LOG",
    "STABLE_USER_PROMPT",
    "breaker_messages",
    "client_from_settings",
    "measured_messages",
    "run_benchmark",
    "run_phase",
    "warmup_messages",
]
