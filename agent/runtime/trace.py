"""P0-17 运行 Trace 契约、采集器和结果适配器。

Trace 是运行期间的事实索引，不替代 ``RunState``、ToolResult 或模型输出正文。
每条事件都携带可重算的摘要、版本和证据引用：调用方可以只保存 Trace 和
Checkpoint，而不必把 Prompt、参数或大段日志重复写入审计表。采集器对序号和
``trace_id/run_id`` 做 fail-closed 校验；持久化 sink 失败时直接向上抛出，避免
把“业务成功但审计丢失”误报成完整运行。后续可在不改变事件 Schema 的前提下
增加 OTLP/SSE sink。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.tools.contracts import ToolResult, ToolResultStatus


TraceEventType = Literal["node", "model", "tool", "verification"]
TraceEventStatus = Literal["started", "completed", "failed", "timeout", "denied"]


class TraceContract(BaseModel):
    """Trace 公共严格配置；未知字段必须在运行时边界处失败。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class TraceError(TraceContract):
    """失败事件的稳定错误索引；原始异常正文不作为任意代码执行。"""

    category: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TraceEvent(TraceContract):
    """一条可持久化 Trace 事件。

    ``task_id`` 是项目既有命名；``task`` 属性提供面向报告的同义读取入口。
    参数和输入/输出正文只保存 SHA-256，证据通过 URI/行号等引用追溯，避免把
    敏感 Prompt 或大型工具结果无边界地复制到审计事件中。
    """

    schema_version: Literal["1.0"] = "1.0"
    trace_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1)
    event_type: TraceEventType
    status: TraceEventStatus
    node: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=128)
    tool_version: str | None = Field(default=None, min_length=1, max_length=128)
    model_version: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=128)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    parameters_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: TraceError | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def task(self) -> str | None:
        """返回 ``task_id`` 的报告层别名，避免重复存储同一事实。"""

        return self.task_id

    @property
    def model(self) -> str | None:
        """返回 ``model_version`` 的简短读取别名。"""

        return self.model_version

    @model_validator(mode="after")
    def validate_event_consistency(self) -> "TraceEvent":
        """校验时间、Token 汇总、失败错误和证据唯一性。"""

        if self.finished_at < self.started_at:
            raise ValueError("TraceEvent.finished_at 不能早于 started_at")
        expected_latency = max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))
        if self.latency_ms != expected_latency:
            raise ValueError("TraceEvent.latency_ms 必须由 started_at/finished_at 重算")
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("TraceEvent.total_tokens 必须等于 input_tokens+output_tokens")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("TraceEvent.evidence_refs 不能重复")
        if self.status in {"failed", "timeout", "denied"} and self.error is None:
            raise ValueError("失败、超时或拒绝事件必须携带 error")
        if self.event_type == "model" and self.prompt_id is None:
            raise ValueError("model TraceEvent 必须携带 prompt_id")
        if self.event_type == "tool" and self.tool_name is None:
            raise ValueError("tool TraceEvent 必须携带 tool_name")
        return self


class TraceSinkProtocol(Protocol):
    """Trace 持久化 sink 的最小接口；可由内存、PostgreSQL 或流式适配器实现。"""

    def append_trace_event(self, event: TraceEvent) -> None: ...


def new_trace_id(run_id: str) -> str:
    """生成不含业务参数的随机 Trace ID，并校验数据库关联的 run_id。"""

    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 64:
        raise ValueError("run_id 必须是 1～64 个字符的非空字符串")
    return f"trace-{uuid4().hex}"


def _canonical_digest(value: Any) -> str:
    """对参数/元数据做稳定摘要；不可序列化值直接失败而不是隐式转字符串。"""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _latency_ms(started_at: datetime, finished_at: datetime) -> int:
    """按事件时间计算毫秒延迟，统一截断小数避免报告浮点漂移。"""

    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _as_error(value: Any, *, fallback_category: str, fallback_code: str, fallback_message: str) -> TraceError:
    """把工具/节点异常收口为不依赖具体异常类的 TraceError。"""

    if isinstance(value, TraceError):
        return value
    category = getattr(value, "category", None)
    category_value = getattr(category, "value", category) or fallback_category
    code = str(getattr(value, "code", None) or fallback_code)
    message = str(getattr(value, "message", None) or fallback_message)
    retryable = getattr(value, "retryable", None)
    details = getattr(value, "details", {})
    if not isinstance(details, Mapping):
        details = {"raw_type": type(value).__name__}
    return TraceError(
        category=str(category_value),
        code=code[:128],
        message=message[:2000],
        retryable=retryable if isinstance(retryable, bool) else None,
        details=dict(details),
    )


class TraceCollector:
    """线程安全的顺序 Trace 收集器。

    收集器只接受同一 run 的严格下一个序号；sink 在内部列表更新前调用，因而
    持久化失败不会留下“内存声称已记录、数据库实际缺失”的半状态。``events``
    返回深拷贝，调用方不能通过修改返回列表篡改审计事实。
    """

    def __init__(
        self,
        *,
        trace_id: str,
        run_id: str,
        sink: TraceSinkProtocol | Callable[[TraceEvent], None] | None = None,
        events: Sequence[TraceEvent | Mapping[str, Any]] = (),
    ) -> None:
        self.trace_id = trace_id
        self.run_id = run_id
        self._sink = sink
        self._lock = RLock()
        self._events: list[TraceEvent] = []
        for item in events:
            self.append(item)

    @property
    def events(self) -> list[TraceEvent]:
        """返回当前 Trace 的深拷贝快照。"""

        with self._lock:
            return [item.model_copy(deep=True) for item in self._events]

    def append(self, event: TraceEvent | Mapping[str, Any]) -> TraceEvent:
        """追加一条事件；run/trace 不一致或序号跳跃均直接拒绝。"""

        candidate = event if isinstance(event, TraceEvent) else TraceEvent.model_validate(event)
        if candidate.trace_id != self.trace_id or candidate.run_id != self.run_id:
            raise ValueError("TraceEvent 的 trace_id/run_id 与收集器不一致")
        with self._lock:
            expected = len(self._events) + 1
            if candidate.sequence != expected:
                raise ValueError(f"TraceEvent.sequence 必须为 {expected}")
            self._write_sink(candidate)
            self._events.append(candidate.model_copy(deep=True))
            return candidate.model_copy(deep=True)

    def emit(
        self,
        *,
        event_type: TraceEventType,
        status: TraceEventStatus,
        node: str,
        task_id: str | None = None,
        tool_name: str | None = None,
        tool_version: str | None = None,
        model_version: str | None = None,
        prompt_id: str | None = None,
        prompt_version: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        parameters_digest: str | None = None,
        input_digest: str | None = None,
        output_digest: str | None = None,
        error: TraceError | None = None,
        evidence_refs: Sequence[str] = (),
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> TraceEvent:
        """构造并追加事件；默认时间只取一次，避免毫秒延迟不一致。"""

        started = started_at or datetime.now(timezone.utc)
        finished = finished_at or started
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        with self._lock:
            event = TraceEvent(
                trace_id=self.trace_id,
                run_id=self.run_id,
                sequence=len(self._events) + 1,
                event_type=event_type,
                status=status,
                node=node,
                task_id=task_id,
                tool_name=tool_name,
                tool_version=tool_version,
                model_version=model_version,
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=_latency_ms(started, finished),
                started_at=started,
                finished_at=finished,
                parameters_digest=parameters_digest,
                input_digest=input_digest,
                output_digest=output_digest,
                error=error,
                evidence_refs=list(dict.fromkeys(evidence_refs)),
                metadata=dict(metadata or {}),
            )
            self._write_sink(event)
            self._events.append(event.model_copy(deep=True))
            return event.model_copy(deep=True)

    def record_model_result(
        self,
        result: Any,
        *,
        node: str | None = None,
        task_id: str | None = None,
        evidence_refs: Sequence[str] = (),
    ) -> TraceEvent:
        """记录 P0-05 ``NodeExecutionResult`` 的 Prompt、模型、Token 和路由。"""

        route = getattr(getattr(result, "route", None), "value", getattr(result, "route", "failed"))
        status: TraceEventStatus = "completed" if route == "success" else "failed"
        reason_code = getattr(result, "reason_code", None)
        error = None
        if status != "completed":
            category = "budget" if route == "fallback" else "human"
            error = _as_error(
                None,
                fallback_category=category,
                fallback_code=str(reason_code or "model_node_failed"),
                fallback_message=str(getattr(result, "reason", None) or "模型节点未成功完成"),
            )
        before = getattr(result, "usage_before", None)
        after = getattr(result, "usage_after", None)
        input_tokens = self._usage_delta(before, after, "input_tokens")
        output_tokens = self._usage_delta(before, after, "output_tokens")
        return self.emit(
            event_type="model",
            status=status,
            node=node or str(getattr(getattr(result, "node_name", None), "value", getattr(result, "node_name", "model"))),
            task_id=task_id,
            model_version=getattr(result, "model_alias", None),
            prompt_id=getattr(result, "prompt_id", None),
            prompt_version=getattr(result, "prompt_version", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None),
            started_at=getattr(result, "started_at", None),
            finished_at=getattr(result, "finished_at", None),
            parameters_digest=getattr(result, "context_digest", None),
            error=error,
            evidence_refs=evidence_refs,
            metadata={
                "route": route,
                "reason_code": reason_code,
                "estimated_input_tokens": getattr(result, "estimated_input_tokens", 0),
            },
        )

    def record_tool_result(
        self,
        result: ToolResult,
        *,
        node: str,
        task_id: str | None = None,
        parameters_digest: str | None = None,
    ) -> TraceEvent:
        """记录 ToolResult；错误、版本、输入输出摘要和 evidence_refs 原样索引。"""

        status_map: dict[ToolResultStatus, TraceEventStatus] = {
            ToolResultStatus.SUCCESS: "completed",
            ToolResultStatus.FAILED: "failed",
            ToolResultStatus.TIMEOUT: "timeout",
            ToolResultStatus.DENIED: "denied",
        }
        status = status_map[result.status]
        error = None
        if result.error is not None:
            error = _as_error(
                result.error,
                fallback_category="tool",
                fallback_code="tool_failed",
                fallback_message="工具调用失败",
            )
        return self.emit(
            event_type="tool",
            status=status,
            node=node,
            task_id=task_id,
            tool_name=result.tool_name.value,
            tool_version=result.tool_version,
            started_at=result.started_at,
            finished_at=result.finished_at,
            parameters_digest=parameters_digest,
            input_digest=result.input_digest,
            output_digest=result.output_digest,
            error=error,
            evidence_refs=result.evidence_refs,
            metadata={
                "call_id": result.call_id,
                "effect_id": result.effect_id,
                "idempotency_key": result.idempotency_key,
                **result.audit_metadata,
            },
        )

    def record_verification_case(
        self,
        case: Any,
        *,
        suite_id: str,
        tool_version: str | None = None,
        parameters_digest: str | None = None,
    ) -> TraceEvent:
        """记录受控验证 case，并把失败类型/任务/工具纳入同一 Trace。"""

        status_value = str(getattr(case, "status", "failed"))
        status: TraceEventStatus = {
            "passed": "completed",
            "timeout": "timeout",
            "failed": "failed",
        }.get(status_value, "failed")
        task_id = getattr(case, "task_id", None)
        tool_name = getattr(case, "tool_name", None)
        failure_type = getattr(case, "failure_type", "none")
        failure_value = getattr(failure_type, "value", failure_type)
        error = None
        if status != "completed":
            error = TraceError(
                category=str(failure_value),
                code=str(failure_value),
                message=str(getattr(case, "summary", None) or "验证 case 未通过"),
                details={"suite_id": suite_id},
            )
        evidence_refs = list(getattr(case, "evidence_refs", []) or [])
        duration_ms = getattr(case, "duration_ms", 0)
        finished_at = datetime.now(timezone.utc)
        started_at = finished_at - timedelta(milliseconds=max(0, int(duration_ms)))
        return self.emit(
            event_type="verification",
            status=status,
            node="run_verification_suite",
            task_id=task_id,
            tool_name=tool_name or "run_verification_suite",
            tool_version=tool_version,
            started_at=started_at,
            finished_at=finished_at,
            parameters_digest=parameters_digest or getattr(case, "parameters_digest", None),
            error=error,
            evidence_refs=evidence_refs,
            metadata={
                "suite_id": suite_id,
                "case_id": getattr(case, "case_id", None),
                "failure_type": str(failure_value),
                "evidence_locations": [
                    item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                    for item in (getattr(case, "evidence_locations", []) or [])
                ],
            },
        )

    def _write_sink(self, event: TraceEvent) -> None:
        """兼容对象方法和函数 sink；sink 异常不被吞掉。"""

        if self._sink is None:
            return
        writer = getattr(self._sink, "append_trace_event", None)
        if callable(writer):
            writer(event)
            return
        if callable(self._sink):
            self._sink(event)
            return
        raise TypeError("Trace sink 必须实现 append_trace_event 或是可调用对象")

    @staticmethod
    def _usage_delta(before: Any, after: Any, field: str) -> int | None:
        """从累计预算快照计算单次模型 Token；缺失旧字段时返回 None。"""

        before_value = getattr(before, field, None)
        after_value = getattr(after, field, None)
        if not isinstance(after_value, int):
            return None
        if not isinstance(before_value, int):
            return max(0, after_value)
        return max(0, after_value - before_value)


__all__ = [
    "TraceCollector",
    "TraceContract",
    "TraceError",
    "TraceEvent",
    "TraceEventStatus",
    "TraceEventType",
    "TraceSinkProtocol",
    "new_trace_id",
]
