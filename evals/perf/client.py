"""专用 Benchmark 流式客户端：测 TTFT，不改生产 ``stream=False`` 契约。

生产路径 ``services.model_gateway.provider.ModelProvider`` 固定非流式。
本客户端直接打 OpenAI 兼容 ``/chat/completions`` 且 ``stream=true``，用
``time.perf_counter()`` 记录 request_start / first_nonempty_generated_delta /
response_end。同一时刻只允许一个请求在途，避免日志与样本错配。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

import httpx

from evals.perf.contracts import LatencySample, SampleKind, SampleMismatchError
from evals.perf.llama_log import LlamaLogCursor, attach_prefill_from_log_delta  # noqa: F401
from evals.perf.stream_events import StreamParseState, feed_stream_bytes, finalize_stream_state
from services.model_gateway.provider import extract_cached_input_tokens


def round_ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def _chat_url(base_url: str) -> str:
    root = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(root, "chat/completions")


def _int_field(payload: Mapping[str, Any] | None, *names: str) -> int | None:
    if not payload:
        return None
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0 and value.is_integer():
            return int(value)
    return None


def _float_field(payload: Mapping[str, Any] | None, *names: str) -> float | None:
    if not payload:
        return None
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def llama_extra_body(*, reasoning_enabled: bool, reasoning_budget: int, top_k: int, cache_prompt: bool) -> dict[str, Any]:
    """与生产网关白名单对齐的 extra_body；调用方只能改 cache_prompt 对照开关。"""

    return {
        "chat_template_kwargs": {"enable_thinking": reasoning_enabled},
        "reasoning_budget": reasoning_budget,
        "top_k": top_k,
        "cache_prompt": cache_prompt,
    }


class SerialRequestGuard:
    """进程内串行锁。重叠请求直接失败，而不是悄悄错配日志。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: str | None = None

    def acquire(self, request_id: str) -> None:
        with self._lock:
            if self._in_flight is not None:
                raise SampleMismatchError(
                    f"overlapping requests: {self._in_flight} still in flight when starting {request_id}"
                )
            self._in_flight = request_id

    def release(self, request_id: str) -> None:
        with self._lock:
            if self._in_flight not in {request_id, None}:
                leaked = self._in_flight
                self._in_flight = None
                raise SampleMismatchError(
                    f"request mismatch on release: expected {request_id}, in-flight was {leaked}"
                )
            self._in_flight = None


class StreamingChatClient:
    """串行流式调用。``kind=warmup|breaker`` 的样本会被统计层排除。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        top_p: float = 0.95,
        top_k: int = 20,
        reasoning_enabled: bool = False,
        reasoning_budget: int = 0,
        connect_timeout_seconds: float = 3.0,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        llama_log_cursor: LlamaLogCursor | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.reasoning_enabled = reasoning_enabled
        self.reasoning_budget = reasoning_budget
        self.connect_timeout_seconds = connect_timeout_seconds
        self.timeout_seconds = timeout_seconds
        self.llama_log_cursor = llama_log_cursor
        self.extra_headers = dict(extra_headers or {})
        self.guard = SerialRequestGuard()
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                timeout=timeout_seconds,
                connect=connect_timeout_seconds,
                read=timeout_seconds,
                write=timeout_seconds,
            ),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "StreamingChatClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def complete_stream(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        cache_prompt: bool,
        kind: SampleKind = "measured",
        max_tokens: int = 16,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LatencySample:
        request_id = request_id or str(uuid.uuid4())
        timeout_seconds = timeout_seconds or self.timeout_seconds
        self.guard.acquire(request_id)
        if self.llama_log_cursor is not None:
            self.llama_log_cursor.mark()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **llama_extra_body(
                reasoning_enabled=self.reasoning_enabled,
                reasoning_budget=self.reasoning_budget,
                top_k=self.top_k,
                cache_prompt=cache_prompt,
            ),
        }
        # PEVR 结构化生成必须带上与生产相同的 JSON Schema，否则探针测到的
        # 不是真实节点请求。生产 ModelProvider 仍走非流式 SDK。
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-AMR-Perf-Request-Id": request_id,
            "X-AMR-Perf-Kind": kind,
            **self.extra_headers,
        }
        state = StreamParseState()
        http_status: int | None = None
        error: str | None = None
        exclusion: str | None = None
        first_mono: float | None = None
        last_byte_mono: float | None = None
        request_start = time.perf_counter()
        try:
            with self._client.stream(
                "POST",
                _chat_url(self.base_url),
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(
                    timeout=timeout_seconds,
                    connect=self.connect_timeout_seconds,
                    read=timeout_seconds,
                    write=timeout_seconds,
                ),
            ) as response:
                http_status = response.status_code
                for chunk in response.iter_bytes():
                    last_byte_mono = time.perf_counter()
                    if feed_stream_bytes(state, chunk) and first_mono is None:
                        first_mono = last_byte_mono
                finalize_stream_state(state)
                # 最后一块 SSE 若缺少空行，文本只在 finalize 才可见。
                # 非流式整包 JSON 不会设置 saw_sse_data，因此不会在这里冒充 TTFT。
                if (
                    first_mono is None
                    and state.saw_sse_data
                    and state.first_generated_text
                    and last_byte_mono is not None
                ):
                    first_mono = last_byte_mono
            response_end = time.perf_counter()
        except httpx.TimeoutException as exc:
            response_end = time.perf_counter()
            error = f"TimeoutException: {exc}"
            exclusion = "timeout"
        except httpx.HTTPError as exc:
            response_end = time.perf_counter()
            error = f"{type(exc).__name__}: {exc}"
            exclusion = "request_failed"
        except SampleMismatchError:
            self.guard.release(request_id)
            raise
        except Exception as exc:  # noqa: BLE001 — Benchmark 必须把失败收成样本而不是炸掉整轮
            response_end = time.perf_counter()
            error = f"{type(exc).__name__}: {exc}"
            exclusion = "request_failed"
        finally:
            self.guard.release(request_id)

        e2e_ms = round_ms(response_end - request_start)
        ttft_ms = round_ms(first_mono - request_start) if first_mono is not None else None
        log_timing = None
        log_reason = None
        if self.llama_log_cursor is not None:
            delta = self.llama_log_cursor.read_delta()
            if self.llama_log_cursor.truncated:
                log_reason = "log_truncated"
            else:
                log_timing, log_reason = attach_prefill_from_log_delta(log_delta=delta)

        return self._to_sample(
            request_id=request_id,
            kind=kind,
            cache_prompt=cache_prompt,
            http_status=http_status,
            error=error,
            exclusion=exclusion,
            state=state,
            ttft_ms=ttft_ms,
            e2e_ms=e2e_ms,
            log_timing=log_timing,
            log_reason=log_reason,
        )

    def _to_sample(
        self,
        *,
        request_id: str,
        kind: SampleKind,
        cache_prompt: bool,
        http_status: int | None,
        error: str | None,
        exclusion: str | None,
        state: StreamParseState,
        ttft_ms: float | None,
        e2e_ms: float,
        log_timing: Any,
        log_reason: str | None,
    ) -> LatencySample:
        usage = state.usage if isinstance(state.usage, dict) else {}
        timings = state.timings if isinstance(state.timings, dict) else {}
        cached = extract_cached_input_tokens({"usage": usage, "timings": timings})
        prompt_tokens = _int_field(usage, "prompt_tokens")
        completion_tokens = _int_field(usage, "completion_tokens")
        prompt_eval_tokens = _int_field(timings, "prompt_n", "prompt_eval_n")
        prefill_ms = _float_field(timings, "prompt_ms", "prompt_eval_ms")
        if prefill_ms is not None:
            prefill_ms = round(prefill_ms, 1)
        prefill_source = "response_timings" if prefill_ms is not None else None
        rounded_progress_100_ms = None
        llama_task_id = None
        if log_timing is not None:
            llama_task_id = log_timing.task_id
            rounded_progress_100_ms = log_timing.rounded_progress_100_ms
            if prefill_ms is None and log_timing.prompt_eval_ms is not None:
                prefill_ms = round(float(log_timing.prompt_eval_ms), 1)
                prefill_source = "llama_log_prompt_eval"
            if prompt_eval_tokens is None:
                prompt_eval_tokens = log_timing.prompt_eval_tokens

        ttft_missing = None
        e2e_missing = None
        prefill_missing = None
        outcome: str
        ok = False

        if kind in {"warmup", "breaker"}:
            exclusion = kind
            ttft_missing = f"excluded_{kind}"
            prefill_missing = f"excluded_{kind}"
            if ttft_ms is not None:
                # 排除样本仍保留原始观测，百分位层会丢掉它们。
                ttft_missing = f"excluded_{kind}"
            outcome = "excluded"
        elif exclusion == "timeout":
            # 超时样本整次丢弃：即使超时前看到过首 token，也不得进入百分位。
            ttft_ms = None
            ttft_missing = "timeout"
            prefill_missing = "timeout"
            outcome = "failed"
        elif exclusion == "request_failed" or (http_status is not None and http_status >= 400):
            exclusion = exclusion or "request_failed"
            ttft_ms = None
            ttft_missing = "request_failed"
            prefill_missing = "request_failed"
            outcome = "failed"
        elif state.saw_non_stream_json and not state.saw_sse_data:
            ttft_ms = None
            ttft_missing = "non_streaming_response"
            outcome = "missing_ttft"
            exclusion = None
        elif not state.saw_done and error is None and http_status == 200 and not state.generated_text:
            ttft_ms = None
            ttft_missing = "incomplete_stream"
            exclusion = "incomplete_stream"
            outcome = "failed"
        elif ttft_ms is None:
            ttft_missing = "no_generated_text_delta"
            exclusion = None
            outcome = "missing_ttft"
        else:
            ok = True
            outcome = "valid"

        if prefill_ms is None and prefill_missing is None:
            prefill_missing = log_reason or "no_timings_in_response"
        if kind in {"warmup", "breaker"}:
            ok = False

        return LatencySample(
            request_id=request_id,
            kind=kind,
            cache_prompt=cache_prompt,
            outcome=outcome,  # type: ignore[arg-type]
            ok=ok,
            exclusion_reason=exclusion,
            error=error,
            ttft_ms=ttft_ms,
            ttft_missing_reason=ttft_missing,
            e2e_ms=e2e_ms,
            e2e_missing_reason=e2e_missing,
            prefill_ms=None if outcome == "failed" else prefill_ms,
            prefill_source=None if outcome == "failed" else prefill_source,
            prefill_missing_reason=prefill_missing if (outcome == "failed" or prefill_ms is None) else None,
            prompt_tokens=prompt_tokens,
            prompt_eval_tokens=prompt_eval_tokens,
            completion_tokens=completion_tokens,
            cached_input_tokens=cached,
            generated_text=state.generated_text,
            http_status=http_status,
            llama_task_id=llama_task_id,
            rounded_progress_100_ms=rounded_progress_100_ms,
            extra={
                "saw_sse_data": state.saw_sse_data,
                "saw_done": state.saw_done,
                "saw_non_stream_json": state.saw_non_stream_json,
                "finish_reason": state.finish_reason,
            },
        )


__all__ = [
    "LlamaLogCursor",
    "SerialRequestGuard",
    "StreamingChatClient",
    "llama_extra_body",
    "round_ms",
]
