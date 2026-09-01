"""解析 OpenAI 兼容 SSE，只把真正的生成文本当作首 token。

llama.cpp / 代理可能先推 role 元数据、空 content、心跳注释或末尾 usage。
这些事件没有可展示的生成文本，不能当作 TTFT 终点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


DONE_MARKERS = ("[DONE]", "DONE")


def _content_to_text(content: Any) -> str | None:
    """把 delta.content 收成字符串；空字符串视为“本事件无生成文本”。"""

    if isinstance(content, str):
        return content if content != "" else None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {None, "text"} and item.get("text"):
                    parts.append(str(item.get("text")))
                elif isinstance(item.get("content"), str) and item["content"]:
                    parts.append(item["content"])
        joined = "".join(parts)
        return joined if joined else None
    return None


def generated_text_from_payload(payload: Any) -> str | None:
    """从单条 JSON 载荷提取生成文本；没有文本时返回 None。

    只承认 ``choices[0].delta.content`` 或非流式 ``choices[0].message.content``。
    ``role``、``reasoning_content``、usage 和空 delta 都忽略。
    """

    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = _content_to_text(delta.get("content"))
        if text is not None:
            return text
    message = choice.get("message")
    if isinstance(message, dict):
        return _content_to_text(message.get("content"))
    return None


def is_usage_only_payload(payload: Any) -> bool:
    """末尾 usage/timings 块：没有 choices 文本，不能当首 token。"""

    if not isinstance(payload, dict):
        return False
    if generated_text_from_payload(payload) is not None:
        return False
    return "usage" in payload or "timings" in payload


def parse_sse_data_payloads(event_text: str) -> list[Any]:
    """解析一个 SSE 事件中的 ``data:`` 行。注释（``:`` 开头）和空数据丢弃。"""

    payloads: list[Any] = []
    data_lines: list[str] = []
    for raw_line in event_text.splitlines():
        line = raw_line.strip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
            continue
    if not data_lines:
        return []
    blob = "\n".join(data_lines).strip()
    if not blob:
        return []
    if blob in DONE_MARKERS:
        return [blob]
    try:
        payloads.append(json.loads(blob))
    except json.JSONDecodeError:
        # 单行损坏不让整次统计崩溃；调用方把它当成无文本事件。
        return []
    return payloads


def split_complete_sse_events(buffer: str) -> tuple[list[str], str]:
    """按空行切出完整 SSE 事件，返回事件列表和未完成尾巴。"""

    events: list[str] = []
    normalized = buffer.replace("\r\n", "\n")
    while "\n\n" in normalized:
        event, normalized = normalized.split("\n\n", 1)
        if event.strip():
            events.append(event)
    return events, normalized


@dataclass
class StreamParseState:
    """增量解析流式字节；记录第一个非空生成 delta 与是否看到流结束标记。"""

    remainder: str = ""
    generated_parts: list[str] = field(default_factory=list)
    first_generated_text: str | None = None
    saw_done: bool = False
    saw_sse_data: bool = False
    saw_non_stream_json: bool = False
    usage: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None
    finish_reason: str | None = None
    payload_count: int = 0

    @property
    def generated_text(self) -> str:
        return "".join(self.generated_parts)


def _absorb_payload(state: StreamParseState, payload: Any) -> bool:
    """消化一条载荷。返回是否刚刚捕获到第一个生成文本。"""

    if payload in DONE_MARKERS:
        state.saw_done = True
        return False
    if not isinstance(payload, dict):
        return False
    state.payload_count += 1
    if isinstance(payload.get("usage"), dict):
        state.usage = payload["usage"]
    if isinstance(payload.get("timings"), dict):
        state.timings = payload["timings"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            state.finish_reason = reason
    text = generated_text_from_payload(payload)
    if text is None:
        return False
    captured_first = state.first_generated_text is None
    if captured_first:
        state.first_generated_text = text
    state.generated_parts.append(text)
    return captured_first


def feed_stream_bytes(state: StreamParseState, chunk: bytes | str) -> bool:
    """喂入一块流数据。若本块首次出现非空生成文本则返回 True。"""

    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
    state.remainder += text
    events, state.remainder = split_complete_sse_events(state.remainder)
    captured = False
    for event in events:
        payloads = parse_sse_data_payloads(event)
        if payloads:
            state.saw_sse_data = True
        for payload in payloads:
            if _absorb_payload(state, payload):
                captured = True
    return captured


def finalize_stream_state(state: StreamParseState) -> StreamParseState:
    """处理没有以空行结尾的尾巴，并识别整包非流式 JSON。"""

    leftover = state.remainder.strip()
    if leftover:
        payloads = parse_sse_data_payloads(leftover)
        if payloads:
            state.saw_sse_data = True
            for payload in payloads:
                _absorb_payload(state, payload)
        else:
            try:
                parsed = json.loads(leftover)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                state.saw_non_stream_json = True
                _absorb_payload(state, parsed)
        state.remainder = ""
    return state


def iter_first_token_chunks(chunks: Iterable[bytes | str]) -> Iterator[tuple[StreamParseState, bool]]:
    """测试辅助：逐步喂入并产出（状态, 是否刚刚捕获首 token）。"""

    state = StreamParseState()
    for chunk in chunks:
        captured = feed_stream_bytes(state, chunk)
        yield state, captured
    finalize_stream_state(state)
    yield state, False


__all__ = [
    "DONE_MARKERS",
    "StreamParseState",
    "feed_stream_bytes",
    "finalize_stream_state",
    "generated_text_from_payload",
    "is_usage_only_payload",
    "iter_first_token_chunks",
    "parse_sse_data_payloads",
    "split_complete_sse_events",
]
