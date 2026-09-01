"""解析 llama.cpp 服务端日志中的 Prefill，明确拒绝把 progress 当 TTFT。

已知陷阱（见 ``docs/LESSONS_LEARNED.md``）：

1. ``prompt processing, progress = 1.00`` 只保留两位小数，且打印发生在
   *下一批* Prompt token 处理之前。最后一小批（实测可能是 4 个 token）
   尚未计算时，``(N-4)/N`` 已经可能显示成 1.00。
2. ``prompt eval time`` 在整次生成结束后才打印，其毫秒值才是 Prefill 时长。
3. 没有 progress 行不等于 prefix KV 命中：llama.cpp 默认约 3 秒才打一条
   进度，短 Prefill 或高命中导致很快结束时都可能没有 progress。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_TS = r"(?P<ts>\d+\.\d+\.\d+(?:\.\d+)?)"
_LAUNCH_RE = re.compile(
    rf"^{_TS} I slot launch_slot_: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) ",
    re.MULTILINE,
)
_PROGRESS_RE = re.compile(
    rf"^{_TS} I slot print_timing: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
    r"prompt processing, n_tokens =\s+(?P<n>\d+), progress = (?P<prog>[\d.]+)",
    re.MULTILINE,
)
_PROMPT_EVAL_RE = re.compile(
    rf"^{_TS} I slot print_timing: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
    r"prompt eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<tokens>\d+) tokens",
    re.MULTILINE,
)
_EVAL_RE = re.compile(
    rf"^{_TS} I slot print_timing: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
    r"\s*eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<tokens>\d+) tokens",
    re.MULTILINE,
)
_TOTAL_RE = re.compile(
    rf"^{_TS} I slot print_timing: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
    r"\s*total time =\s+(?P<ms>[\d.]+) ms /\s+(?P<tokens>\d+) tokens",
    re.MULTILINE,
)
_N_PROMPT_RE = re.compile(r"n_prompt\s*=\s*(?P<n>\d+)")
_N_PAST_RE = re.compile(r"n_past\s*=\s*(?P<n>\d+)")
_RELEASE_RE = re.compile(
    rf"^{_TS} I slot\s+release: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
    r"stop processing: n_tokens = (?P<n>\d+)",
    re.MULTILINE,
)


def parse_llama_rel_seconds(stamp: str) -> float:
    """把 ``M.SS.mmm.uuu`` 相对时间戳换成秒。"""

    parts = stamp.split(".")
    minutes = int(parts[0])
    seconds = int(parts[1])
    millis = int(parts[2]) if len(parts) > 2 else 0
    micros = int(parts[3]) if len(parts) > 3 else 0
    return minutes * 60 + seconds + millis / 1000.0 + micros / 1_000_000.0


@dataclass
class LlamaSlotTiming:
    """一次 slot task 的服务端计时。``ttft_ms`` 故意不存在于本结构。"""

    task_id: int
    slot_id: int | None = None
    launch_rel_s: float | None = None
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    prompt_eval_ms: float | None = None
    prompt_eval_tokens: int | None = None
    eval_ms: float | None = None
    eval_tokens: int | None = None
    total_ms: float | None = None
    n_prompt: int | None = None
    n_past: int | None = None
    n_tokens_stop: int | None = None

    @property
    def rounded_progress_100_ms(self) -> float | None:
        """launch → 首次 progress=1.00 的间隔。这不是 TTFT，也不是 Prefill 结束。"""

        if self.launch_rel_s is None:
            return None
        for event in self.progress_events:
            if float(event["progress"]) >= 1.0 - 1e-12:
                return round((float(event["rel_s"]) - self.launch_rel_s) * 1000.0, 1)
        return None

    def cache_hit_from_progress_absence(self) -> None:
        """占位：禁止从“没有 progress”推断命中。调用方应使用 usage/timings。"""

        return None

    def to_prefill_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "slot_id": self.slot_id,
            "prompt_eval_ms": self.prompt_eval_ms,
            "prompt_eval_tokens": self.prompt_eval_tokens,
            "eval_ms": self.eval_ms,
            "eval_tokens": self.eval_tokens,
            "total_ms": self.total_ms,
            "n_prompt": self.n_prompt,
            "n_past": self.n_past,
            "n_tokens_stop": self.n_tokens_stop,
            "progress_event_count": len(self.progress_events),
            "rounded_progress_100_ms": self.rounded_progress_100_ms,
            "ttft_ms": None,
            "ttft_missing_reason": "rounded_progress_is_not_ttft"
            if self.rounded_progress_100_ms is not None
            else "legacy_log_only_no_client_clock",
            "missing_progress_means_cache_hit": False,
        }


def _slot(store: dict[int, LlamaSlotTiming], task_id: int, slot_id: str | None) -> LlamaSlotTiming:
    item = store.get(task_id)
    if item is None:
        item = LlamaSlotTiming(task_id=task_id)
        store[task_id] = item
    if slot_id is not None and item.slot_id is None:
        item.slot_id = int(slot_id)
    return item


def parse_llama_slot_timings(text: str) -> list[LlamaSlotTiming]:
    """从一段日志文本解析全部 slot task。结果按首次出现顺序排列。"""

    store: dict[int, LlamaSlotTiming] = {}
    order: list[int] = []

    def remember(task_id: int) -> None:
        if task_id not in order:
            order.append(task_id)

    for match in _LAUNCH_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.launch_rel_s = parse_llama_rel_seconds(match.group("ts"))
        remember(task_id)
        window = text[match.start() : match.start() + 800]
        prompt_match = _N_PROMPT_RE.search(window)
        if prompt_match:
            item.n_prompt = int(prompt_match.group("n"))
        past_match = _N_PAST_RE.search(window)
        if past_match:
            item.n_past = int(past_match.group("n"))

    for match in _PROGRESS_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.progress_events.append(
            {
                "rel_s": parse_llama_rel_seconds(match.group("ts")),
                "n_tokens": int(match.group("n")),
                "progress": float(match.group("prog")),
            }
        )
        remember(task_id)

    for match in _PROMPT_EVAL_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.prompt_eval_ms = float(match.group("ms"))
        item.prompt_eval_tokens = int(match.group("tokens"))
        remember(task_id)

    for match in _EVAL_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.eval_ms = float(match.group("ms"))
        item.eval_tokens = int(match.group("tokens"))
        remember(task_id)

    for match in _TOTAL_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.total_ms = float(match.group("ms"))
        remember(task_id)

    for match in _RELEASE_RE.finditer(text):
        task_id = int(match.group("task"))
        item = _slot(store, task_id, match.group("slot"))
        item.n_tokens_stop = int(match.group("n"))
        remember(task_id)

    return [store[task_id] for task_id in order]


def attach_prefill_from_log_delta(
    *,
    log_delta: str,
    allow_empty: bool = False,
) -> tuple[LlamaSlotTiming | None, str | None]:
    """把一次串行请求对应的日志增量关联到单条 Prefill。

    必须恰好一条 ``prompt eval time``；0 条或多条都视为错配，避免把
    breaker / 邻接请求的计时接到当前样本。
    """

    if not log_delta.strip():
        if allow_empty:
            return None, "no_prompt_eval_in_log"
        return None, "no_prompt_eval_in_log"

    timings = [item for item in parse_llama_slot_timings(log_delta) if item.prompt_eval_ms is not None]
    if len(timings) == 0:
        return None, "no_prompt_eval_in_log"
    if len(timings) > 1:
        return None, "mismatched_server_log"
    return timings[0], None


def _prompt_tokens_from_timing(timing: LlamaSlotTiming) -> tuple[int | None, str | None]:
    """优先 n_prompt；当前 llama.cpp 日志常不打该字段，退化为 release n_tokens − 生成 token。"""

    if timing.n_prompt is not None and timing.n_prompt > 0:
        return timing.n_prompt, "n_prompt"
    if (
        timing.n_tokens_stop is not None
        and timing.eval_tokens is not None
        and timing.n_tokens_stop >= timing.eval_tokens
    ):
        return timing.n_tokens_stop - timing.eval_tokens, "n_tokens_stop_minus_eval_tokens"
    return None, None


def cache_hit_inference_from_log(timing: LlamaSlotTiming) -> dict[str, Any]:
    """缓存命中只能来自 token 计数，不能来自 progress 是否出现。"""

    prompt_tokens, source = _prompt_tokens_from_timing(timing)
    if prompt_tokens is not None and timing.prompt_eval_tokens is not None and prompt_tokens > 0:
        cached = max(0, prompt_tokens - timing.prompt_eval_tokens)
        return {
            "cache_hit": cached > 0,
            "cached_tokens": cached,
            "prompt_tokens": prompt_tokens,
            "source": f"{source}_minus_prompt_eval_tokens",
            "missing_progress_used": False,
        }
    return {
        "cache_hit": None,
        "cached_tokens": None,
        "prompt_tokens": prompt_tokens,
        "source": "unknown",
        "reason": "missing_progress_is_not_cache_hit"
        if not timing.progress_events
        else "progress_not_used_for_cache",
        "missing_progress_used": False,
    }


class LlamaLogCursor:
    """按文件偏移读取一次请求期间新增的日志，避免用墙上时钟去猜 task。

    Benchmark 必须串行：每次请求前 ``mark()``，结束后 ``read_delta()``。
    若文件被截断或轮转，``truncated=True``，调用方应把 Prefill 标为缺失。
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._offset = 0
        self.truncated = False

    def mark(self) -> None:
        self.truncated = False
        if self.path is None or not self.path.is_file():
            self._offset = 0
            return
        self._offset = self.path.stat().st_size

    def read_delta(self) -> str:
        self.truncated = False
        if self.path is None or not self.path.is_file():
            return ""
        size = self.path.stat().st_size
        if size < self._offset:
            self.truncated = True
            self._offset = size
            return ""
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            text = handle.read()
        self._offset = size
        return text


__all__ = [
    "LlamaLogCursor",
    "LlamaSlotTiming",
    "attach_prefill_from_log_delta",
    "cache_hit_inference_from_log",
    "parse_llama_rel_seconds",
    "parse_llama_slot_timings",
]
