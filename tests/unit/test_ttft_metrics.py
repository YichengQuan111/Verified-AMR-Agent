"""TTFT 流式解析、客户端时钟、百分位与生产非流式契约。"""

from __future__ import annotations

import inspect
import json
import time

import httpx
import pytest

from evals.perf.client import SerialRequestGuard, StreamingChatClient
from evals.perf.contracts import LatencySample, SampleMismatchError
from evals.perf.stats import percentile, speedup, summarize_samples
from evals.perf.stream_events import (
    StreamParseState,
    feed_stream_bytes,
    finalize_stream_state,
    generated_text_from_payload,
)
from services.model_gateway.provider import ModelProvider
from services.model_gateway.secure_proxy import create_proxy_app, request_wants_sse_stream
from tests.unit.fakes import FakeOpenAIClient
from tests.unit.test_model_provider import MESSAGES, TransportExtraction, make_settings


def _sse(*payloads: dict | str) -> bytes:
    chunks: list[str] = []
    for item in payloads:
        if item == "[DONE]":
            chunks.append("data: [DONE]\n\n")
        else:
            chunks.append("data: " + json.dumps(item, ensure_ascii=False) + "\n\n")
    return "".join(chunks).encode("utf-8")


def test_first_nonempty_text_delta_is_ttft_event() -> None:
    state = StreamParseState()
    assert generated_text_from_payload({"choices": [{"delta": {"role": "assistant"}}]}) is None
    assert not feed_stream_bytes(state, _sse({"choices": [{"delta": {"role": "assistant"}}]}))
    assert not feed_stream_bytes(state, _sse({"choices": [{"delta": {"content": ""}}]}))
    assert feed_stream_bytes(state, _sse({"choices": [{"delta": {"content": "好"}}]}))
    assert state.first_generated_text == "好"
    feed_stream_bytes(state, _sse("[DONE]"))
    assert state.saw_done is True


def test_role_heartbeat_and_usage_are_ignored() -> None:
    state = StreamParseState()
    feed_stream_bytes(state, b": keep-alive\n\n")
    feed_stream_bytes(state, _sse({"choices": [{"delta": {"role": "assistant"}}]}))
    feed_stream_bytes(state, _sse({"usage": {"prompt_tokens": 10}, "timings": {"prompt_ms": 12.3}}))
    assert state.first_generated_text is None
    finalize_stream_state(state)
    assert state.usage == {"prompt_tokens": 10}
    assert state.timings == {"prompt_ms": 12.3}


def test_stream_without_any_text_leaves_ttft_missing() -> None:
    state = StreamParseState()
    feed_stream_bytes(state, _sse({"choices": [{"delta": {"role": "assistant"}}]}))
    feed_stream_bytes(state, _sse({"usage": {"completion_tokens": 0}}))
    feed_stream_bytes(state, _sse("[DONE]"))
    assert state.first_generated_text is None
    assert state.generated_text == ""


def _mock_client(handler, **kwargs) -> StreamingChatClient:
    return StreamingChatClient(
        base_url="http://127.0.0.1:8080/v1",
        api_key="unit-test-key",
        model="qwen3.6-fast",
        transport=httpx.MockTransport(handler),
        llama_log_cursor=None,
        **kwargs,
    )


def test_streaming_client_records_first_text_not_role() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["stream"] = payload["stream"]
        seen["cache_prompt"] = payload["cache_prompt"]
        body = _sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": ""}}]},
            {"choices": [{"delta": {"content": "好"}}]},
            {"usage": {"prompt_tokens": 20, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 8}}, "timings": {"prompt_ms": 40.0, "prompt_n": 12}},
            "[DONE]",
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    with _mock_client(handler) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=True,
            kind="measured",
        )
    assert seen["stream"] is True
    assert seen["cache_prompt"] is True
    assert sample.ok is True
    assert sample.ttft_ms is not None
    assert sample.e2e_ms is not None
    assert sample.ttft_ms <= sample.e2e_ms
    assert sample.ttft_missing_reason is None
    assert sample.prefill_ms == 40.0
    assert sample.cached_input_tokens == 8
    assert sample.generated_text == "好"


def test_streaming_client_forwards_response_format() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["stream"] = payload["stream"]
        seen["response_format"] = payload.get("response_format")
        body = _sse(
            {"choices": [{"delta": {"content": "{}"}}]},
            "[DONE]",
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    with _mock_client(handler) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=True,
            response_format={"type": "json_object", "schema": {"type": "object"}},
        )
    assert seen["stream"] is True
    assert seen["response_format"]["type"] == "json_object"
    assert sample.ok is True


def test_streaming_client_ttft_is_before_e2e_when_later_chunks_arrive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        def generate():
            yield _sse({"choices": [{"delta": {"content": "A"}}]})
            time.sleep(0.12)
            yield _sse({"choices": [{"delta": {"content": "B"}}]}, "[DONE]")

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=generate())

    with _mock_client(handler) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=False,
        )
    assert sample.ttft_ms is not None
    assert sample.e2e_ms is not None
    assert sample.ttft_ms + 80 <= sample.e2e_ms
    assert sample.generated_text == "AB"


def test_non_streaming_json_does_not_become_ttft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "整包"}}]},
        )

    with _mock_client(handler) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=True,
        )
    assert sample.ttft_ms is None
    assert sample.ttft_missing_reason == "non_streaming_response"
    assert sample.e2e_ms is not None
    assert sample.prefill_ms is None or sample.prefill_ms != sample.e2e_ms


def test_timeout_is_failed_and_does_not_fill_ttft_from_prefill() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("prefill still running", request=request)

    with _mock_client(handler, timeout_seconds=0.05, connect_timeout_seconds=0.05) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=True,
        )
    assert sample.ok is False
    assert sample.exclusion_reason == "timeout"
    assert sample.ttft_ms is None
    assert sample.ttft_missing_reason == "timeout"
    assert sample.prefill_ms is None
    assert sample.prefill_missing_reason == "timeout"


def test_http_error_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with _mock_client(handler) as client:
        sample = client.complete_stream(
            [{"role": "user", "content": "hi"}],
            cache_prompt=True,
        )
    assert sample.outcome == "failed"
    assert sample.ttft_ms is None
    assert sample.ttft_missing_reason == "request_failed"


def test_serial_guard_rejects_overlapping_requests() -> None:
    guard = SerialRequestGuard()
    guard.acquire("a")
    with pytest.raises(SampleMismatchError, match="overlapping"):
        guard.acquire("b")
    guard.release("a")
    guard.acquire("b")
    guard.release("b")


def test_summarize_excludes_warmup_breaker_timeout_and_never_fills_ttft() -> None:
    samples = [
        LatencySample(
            request_id="w",
            kind="warmup",
            cache_prompt=True,
            outcome="excluded",
            ok=False,
            exclusion_reason="warmup",
            ttft_ms=1.0,
            e2e_ms=2.0,
            prefill_ms=1.5,
        ),
        LatencySample(
            request_id="b",
            kind="breaker",
            cache_prompt=False,
            outcome="excluded",
            ok=False,
            exclusion_reason="breaker",
            ttft_ms=3.0,
            e2e_ms=4.0,
            prefill_ms=3.5,
        ),
        LatencySample(
            request_id="t",
            kind="measured",
            cache_prompt=True,
            outcome="failed",
            ok=False,
            exclusion_reason="timeout",
            ttft_ms=None,
            ttft_missing_reason="timeout",
            e2e_ms=120000.0,
            prefill_ms=None,
        ),
        LatencySample(
            request_id="m1",
            kind="measured",
            cache_prompt=True,
            outcome="valid",
            ok=True,
            ttft_ms=10.0,
            e2e_ms=20.0,
            prefill_ms=12.0,
            prompt_tokens=100,
            cached_input_tokens=40,
        ),
        LatencySample(
            request_id="m2",
            kind="measured",
            cache_prompt=True,
            outcome="valid",
            ok=True,
            ttft_ms=30.0,
            e2e_ms=40.0,
            prefill_ms=32.0,
            prompt_tokens=100,
            cached_input_tokens=40,
        ),
        LatencySample(
            request_id="missing",
            kind="measured",
            cache_prompt=True,
            outcome="missing_ttft",
            ok=False,
            ttft_ms=None,
            ttft_missing_reason="no_generated_text_delta",
            e2e_ms=50.0,
            prefill_ms=33.0,
        ),
    ]
    summary = summarize_samples(samples)
    assert summary["sample_counts"]["valid"] == 2
    assert summary["sample_counts"]["failed"] == 1
    assert summary["sample_counts"]["missing_ttft"] == 1
    assert summary["sample_counts"]["excluded"]["warmup"] == 1
    assert summary["sample_counts"]["excluded"]["breaker"] == 1
    assert summary["ttft_ms"]["p50"] == 20.0
    assert summary["ttft_ms"]["filled_from_prefill"] is False
    assert summary["ttft_ms"]["missing"] == 1
    assert summary["prefill_ms"]["n"] == 3
    assert summary["prefill_ms"]["p50"] == 32.0
    assert summary["invariants"]["pseudo_ttft_from_progress_100"] is False
    assert summary["cache"]["hit_ratio"] == 0.4


def test_percentile_and_speedup_none_when_empty() -> None:
    assert percentile([], 50) is None
    assert percentile([10.0], 95) == 10.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 95) == 38.5
    assert speedup(None, 10.0) is None
    assert speedup(20.0, 10.0) == 2.0


def test_production_provider_stays_non_streaming() -> None:
    source = inspect.getsource(ModelProvider._request_completion)
    assert '"stream": False' in source
    client = FakeOpenAIClient(["qwen3.6-fast"], ['{"pickup":"P1","dropoff":"S3","quantity":2}'])
    provider = ModelProvider(make_settings(), client=client)
    provider.generate_structured(MESSAGES, TransportExtraction)
    assert client.completions.calls[0]["stream"] is False


def test_proxy_detects_stream_flag() -> None:
    assert request_wants_sse_stream(b'{"stream": true}') is True
    assert request_wants_sse_stream(b'{"stream": false}') is False
    assert request_wants_sse_stream(b"not-json") is False


def test_proxy_stream_true_takes_sse_passthrough_path() -> None:
    source = inspect.getsource(create_proxy_app)
    assert "StreamingResponse" in source
    assert "stream=True" in source
    assert "request_wants_sse_stream(body)" in source


def test_proxy_streams_sse_without_waiting_for_complete_body() -> None:
    """必须走真实 TCP：Starlette TestClient / httpx ASGITransport 会先收齐 body。"""

    import socket
    import threading

    import uvicorn

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    backend.bind(("127.0.0.1", 0))
    backend.listen(1)
    backend_port = int(backend.getsockname()[1])

    def serve_backend() -> None:
        conn = None
        try:
            backend.settimeout(8)
            conn, _addr = backend.accept()
            conn.settimeout(8)
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(4096)
                if not chunk:
                    return

                received += chunk

            def write_chunk(payload: bytes) -> None:
                conn.sendall(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")

            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            write_chunk(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
            time.sleep(0.2)
            write_chunk(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            write_chunk(b"data: [DONE]\n\n")
            conn.sendall(b"0\r\n\r\n")
            time.sleep(0.3)
        finally:
            if conn is not None:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            backend.close()

    threading.Thread(target=serve_backend, daemon=True).start()
    proxy_port = _free_port()
    app = create_proxy_app(
        api_key="proxy-unit-secret-0123456789-0123456789",
        backend_url=f"http://127.0.0.1:{backend_port}",
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=proxy_port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.perf_counter() + 5
    while not server.started and time.perf_counter() < deadline:
        time.sleep(0.02)
    assert server.started, "proxy uvicorn failed to start"

    first_delay = None
    total = None
    try:
        started = time.perf_counter()
        with httpx.Client(timeout=8.0) as client:
            try:
                with client.stream(
                    "POST",
                    f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                    headers={"Authorization": "Bearer proxy-unit-secret-0123456789-0123456789"},
                    json={"stream": True, "messages": []},
                ) as response:
                    for chunk in response.iter_bytes():
                        if first_delay is None and chunk:
                            first_delay = time.perf_counter() - started
            except httpx.RemoteProtocolError:
                # 后端在收尾时关连接；只要已经看到首块，仍能证明没有整包缓冲。
                if first_delay is None:
                    raise
        total = time.perf_counter() - started
    finally:
        server.should_exit = True
        thread.join(timeout=3)

    assert first_delay is not None
    assert first_delay < 0.12, first_delay
    assert total is not None and total >= 0.18, total
