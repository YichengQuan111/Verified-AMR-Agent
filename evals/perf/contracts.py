"""LLM 延迟指标的稳定契约。

本模块是 TTFT / Prefill / E2E 的唯一口径来源。统计代码必须引用这里的
常量与原因码，禁止在脚本里重新发明“progress=1.00 即首 token”之类的定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


# 对外报告里的指标名称。TTFT 与 Prefill 不得互相回填。
METRIC_TTFT = "ttft_ms"
METRIC_PREFILL = "prefill_ms"
METRIC_E2E = "e2e_ms"

CLOCK_CLIENT_MONOTONIC = "client_perf_counter"
CLOCK_SERVER_LLAMA_TIMING = "llama_cpp_prompt_eval_time"

SampleKind = Literal["measured", "warmup", "breaker"]
CallOutcome = Literal["valid", "missing_ttft", "failed", "excluded"]

# TTFT 缺失原因。出现这些值时 ttft_ms 必须为 None。
TTFT_MISSING_REASONS = frozenset(
    {
        "non_streaming_response",
        "no_generated_text_delta",
        "request_failed",
        "timeout",
        "incomplete_stream",
        "excluded_warmup",
        "excluded_breaker",
        "legacy_log_only_no_client_clock",
        "rounded_progress_is_not_ttft",
    }
)

PREFILL_MISSING_REASONS = frozenset(
    {
        "no_timings_in_response",
        "no_prompt_eval_in_log",
        "mismatched_server_log",
        "overlapping_requests",
        "log_truncated",
        "excluded_warmup",
        "excluded_breaker",
        "request_failed",
        "timeout",
        "incomplete_stream",
    }
)

EXCLUSION_REASONS = frozenset(
    {
        "warmup",
        "breaker",
        "timeout",
        "request_failed",
        "incomplete_stream",
        "no_generated_text",
        "overlapping_requests",
    }
)


METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    METRIC_TTFT: {
        "name": "TTFT",
        "unit": "ms",
        "clock": CLOCK_CLIENT_MONOTONIC,
        "definition": (
            "客户端发出 HTTP 请求的单调时钟时刻，到收到第一个含非空生成文本的 "
            "SSE delta 为止。空 delta、role 元数据、心跳、usage 块不计入。"
        ),
        "includes": "本机排队、鉴权代理转发、网络往返、服务端排队与 Prefill，以及直到首个生成 token 发出。",
        "excludes": "后续 decode、完整响应收尾。不得用 llama.cpp progress 或 prompt eval time 代替。",
    },
    METRIC_PREFILL: {
        "name": "Prefill",
        "unit": "ms",
        "clock": CLOCK_SERVER_LLAMA_TIMING,
        "definition": (
            "llama.cpp 最终 `prompt eval time`（响应 timings.prompt_ms 或日志同名字段）。"
            "这是服务端计算时长，不是客户端首 token 时刻。"
        ),
        "includes": "服务端对未命中 KV 的 Prompt token 的实际计算。",
        "excludes": "客户端排队/网络、decode、以及四舍五入后的 progress=1.00 日志时刻。",
    },
    METRIC_E2E: {
        "name": "E2E",
        "unit": "ms",
        "clock": CLOCK_CLIENT_MONOTONIC,
        "definition": "客户端发出请求到流式响应完全结束（含 [DONE] 或连接关闭）的单调时钟间隔。",
        "includes": "排队、网络、Prefill、decode 与收尾。Benchmark 路径是单次模型调用，不是 PEVR 案例墙钟。",
        "excludes": "被排除的 warmup/breaker，以及失败/超时样本。",
    },
}


def metric_definitions_for_report() -> dict[str, Any]:
    """序列化到 JSON 报告中的指标定义，避免阅读者误用旧 TTFT 口径。"""

    return {
        "units": "milliseconds",
        "client_clock": CLOCK_CLIENT_MONOTONIC,
        "server_clock": CLOCK_SERVER_LLAMA_TIMING,
        "metrics": METRIC_DEFINITIONS,
        "rules": {
            "ttft_never_filled_from_prefill": True,
            "ttft_never_from_rounded_progress": True,
            "missing_progress_is_not_cache_hit": True,
            "failed_requests_excluded_from_percentiles": True,
        },
    }


@dataclass
class LatencySample:
    """一次模型调用的延迟样本。缺失字段必须保留原因，禁止用其它指标填洞。"""

    request_id: str
    kind: SampleKind
    cache_prompt: bool
    outcome: CallOutcome
    ok: bool
    exclusion_reason: str | None = None
    error: str | None = None
    ttft_ms: float | None = None
    ttft_missing_reason: str | None = None
    e2e_ms: float | None = None
    e2e_missing_reason: str | None = None
    prefill_ms: float | None = None
    prefill_source: str | None = None
    prefill_missing_reason: str | None = None
    prompt_tokens: int | None = None
    prompt_eval_tokens: int | None = None
    completion_tokens: int | None = None
    cached_input_tokens: int | None = None
    generated_text: str = ""
    http_status: int | None = None
    llama_task_id: int | None = None
    # 仅用于证明旧口径会提前触发；禁止复制到 ttft_ms。
    rounded_progress_100_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "kind": self.kind,
            "cache_prompt": self.cache_prompt,
            "outcome": self.outcome,
            "ok": self.ok,
            "exclusion_reason": self.exclusion_reason,
            "error": self.error,
            "ttft_ms": self.ttft_ms,
            "ttft_missing_reason": self.ttft_missing_reason,
            "e2e_ms": self.e2e_ms,
            "e2e_missing_reason": self.e2e_missing_reason,
            "prefill_ms": self.prefill_ms,
            "prefill_source": self.prefill_source,
            "prefill_missing_reason": self.prefill_missing_reason,
            "prompt_tokens": self.prompt_tokens,
            "prompt_eval_tokens": self.prompt_eval_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "generated_text_preview": self.generated_text[:80],
            "http_status": self.http_status,
            "llama_task_id": self.llama_task_id,
            "rounded_progress_100_ms": self.rounded_progress_100_ms,
        }
        if self.extra:
            payload["extra"] = self.extra
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LatencySample":
        """从落盘 JSON 还原样本；缺失字段保持 None，禁止回填。"""

        extra = dict(payload.get("extra") or {})
        generated = payload.get("generated_text")
        if not isinstance(generated, str):
            generated = str(payload.get("generated_text_preview") or "")
        return cls(
            request_id=str(payload.get("request_id") or ""),
            kind=payload.get("kind") or "measured",  # type: ignore[arg-type]
            cache_prompt=bool(payload.get("cache_prompt")),
            outcome=payload.get("outcome") or "missing_ttft",  # type: ignore[arg-type]
            ok=bool(payload.get("ok")),
            exclusion_reason=payload.get("exclusion_reason"),
            error=payload.get("error"),
            ttft_ms=payload.get("ttft_ms"),
            ttft_missing_reason=payload.get("ttft_missing_reason"),
            e2e_ms=payload.get("e2e_ms"),
            e2e_missing_reason=payload.get("e2e_missing_reason"),
            prefill_ms=payload.get("prefill_ms"),
            prefill_source=payload.get("prefill_source"),
            prefill_missing_reason=payload.get("prefill_missing_reason"),
            prompt_tokens=payload.get("prompt_tokens"),
            prompt_eval_tokens=payload.get("prompt_eval_tokens"),
            completion_tokens=payload.get("completion_tokens"),
            cached_input_tokens=payload.get("cached_input_tokens"),
            generated_text=generated,
            http_status=payload.get("http_status"),
            llama_task_id=payload.get("llama_task_id"),
            rounded_progress_100_ms=payload.get("rounded_progress_100_ms"),
            extra=extra,
        )


class SampleMismatchError(RuntimeError):
    """并发或日志错配时抛出，防止把 breaker/邻接请求的计时安到当前样本上。"""


__all__ = [
    "CLOCK_CLIENT_MONOTONIC",
    "CLOCK_SERVER_LLAMA_TIMING",
    "CallOutcome",
    "EXCLUSION_REASONS",
    "LatencySample",
    "METRIC_DEFINITIONS",
    "METRIC_E2E",
    "METRIC_PREFILL",
    "METRIC_TTFT",
    "PREFILL_MISSING_REASONS",
    "SampleKind",
    "SampleMismatchError",
    "TTFT_MISSING_REASONS",
    "metric_definitions_for_report",
]
