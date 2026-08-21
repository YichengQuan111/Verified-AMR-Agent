"""P0-19 策略对照实验的严格 JSON 契约。

P0-19 只对同一份 P0-18 逐例 Trace 做策略控制流投影，不把投影伪装成新的在线
模型运行。契约因此同时保存源 case、源 Trace、策略投影、可观测性标记和 Smart
延期状态。后续若新增在线执行模式，必须新增独立 ``execution_mode``，不能覆盖
当前 replay 结果或把缺失的 Token/资源样本填成猜测值。
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from evals.p018.contracts import EvalCategory, EvalOutcome, ZeroToleranceMetrics


class P019Contract(BaseModel):
    """所有 P0-19 对外对象都拒绝未知字段，避免统计口径静默漂移。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class P019ExecutionMode(str, Enum):
    """P0-19 允许的执行模式。

    ``offline_independent_oracle`` 是发布验收：三种策略各自执行同一 60 例。
    ``offline_trace_replay`` 仅保留为可视化/步数投影，不能再当作策略质量对照。
    """

    INDEPENDENT_ORACLE = "offline_independent_oracle"
    TRACE_REPLAY = "offline_trace_replay"


class P019Strategy(str, Enum):
    """三种固定对照策略；ReAct 只允许出现在评测层。"""

    FIXED_WORKFLOW = "fixed_workflow"
    REACT = "react"
    PEVR = "pevr"


class SmartComparisonStatus(str, Enum):
    """Smart 对照的事实状态，明确区分延期和已完成。"""

    DEFERRED = "deferred"


class ResourceObservation(P019Contract):
    """记录资源是否真的被采样；没有样本时所有数值保持 ``None``。

    P0-18 离线 Trace 没有 CPU、RSS 或 GPU 采样。用显式 ``observed=false`` 而不是
    填 0，避免把“没有测量”误读为“消耗为零”；未来在线 adapter 可复用同一字段。
    """

    observed: bool = False
    sample_count: int = Field(default=0, ge=0)
    cpu_time_ms: float | None = Field(default=None, ge=0)
    peak_rss_mb: float | None = Field(default=None, ge=0)
    peak_gpu_memory_mb: float | None = Field(default=None, ge=0)
    source: Literal["p018_trace", "online_sampler", "not_observed"] = "not_observed"
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_observation_state(self) -> "ResourceObservation":
        """没有采样时禁止出现看似真实的资源数值。"""

        values = (self.cpu_time_ms, self.peak_rss_mb, self.peak_gpu_memory_mb)
        if not self.observed and any(value is not None for value in values):
            raise ValueError("resource 数值存在时 observed 必须为 true")
        if self.observed and self.sample_count <= 0:
            raise ValueError("observed=true 必须至少有一个资源样本")
        return self


class LatencySummary(P019Contract):
    """按逐例总延迟计算的 P50/P95，并保留延迟来源和墙钟语义。"""

    observed: bool = True
    sample_count: int = Field(ge=0)
    total_ms: float = Field(ge=0)
    mean_case_ms: float = Field(ge=0)
    p50_case_ms: float = Field(ge=0)
    p95_case_ms: float = Field(ge=0)
    max_case_ms: float = Field(ge=0)
    source: Literal["p018_trace", "p018_independent_oracle"] = "p018_trace"
    wall_clock: bool = False
    note: str = Field(min_length=1, max_length=1000)


class TokenSummary(P019Contract):
    """Token 汇总和可观测性标记；离线源没有模型调用时不声称测得 Token。"""

    observed: bool = False
    model_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    source: Literal["p018_trace", "not_observed"] = "not_observed"
    note: str = Field(min_length=1, max_length=1000)


class StrategyCaseResult(P019Contract):
    """一个策略在一个 P0-18 case 上的可复核结果。

    ``source_case`` 和 ``source_trace_events`` 是原始证据的内嵌副本；
    ``projection_events`` 只描述策略如何消费这些事件，不改变观察终态、工具
    能力或安全事实。这样脱离临时目录仍可从 P0-19 JSON 逐例复核结论。
    """

    strategy: P019Strategy
    case_id: str = Field(min_length=1, max_length=128)
    category: EvalCategory
    source_trace_id: str = Field(min_length=1, max_length=128)
    strategy_trace_id: str = Field(min_length=1, max_length=160)
    expected_outcome: EvalOutcome
    observed_outcome: EvalOutcome
    evaluation_passed: bool
    task_completed: bool
    plan_evaluated: bool
    plan_legal: bool | None
    recovery_applicable: bool
    recovery_success: bool | None
    successful_recovery: bool | None
    tool_error_count: int = Field(ge=0)
    unexpected_tool_error_count: int = Field(ge=0)
    step_count: int = Field(ge=0)
    tool_step_count: int = Field(ge=0)
    derived_control_overhead_steps: int = Field(ge=0)
    token_usage: TokenSummary
    latency_ms: float = Field(ge=0)
    resource: ResourceObservation
    replan_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    approval_resume_count: int = Field(ge=0)
    zero_tolerance: ZeroToleranceMetrics
    failure_code: str | None = Field(default=None, max_length=128)
    failure_reason: str | None = Field(default=None, max_length=2000)
    source_case: dict[str, JsonValue]
    source_trace_events: list[dict[str, JsonValue]] = Field(min_length=1)
    projection_events: list[dict[str, JsonValue]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_projection(self) -> "StrategyCaseResult":
        """保证回放结果仍绑定原始身份，并且失败原因没有被丢弃。"""

        source_id = self.source_case.get("case_id")
        source_trace_id = self.source_case.get("trace_id")
        if source_id != self.case_id:
            raise ValueError("source_case.case_id 与策略结果不一致")
        if source_trace_id != self.source_trace_id:
            raise ValueError("source_case.trace_id 与策略结果不一致")
        if not self.evaluation_passed and not (self.failure_code and self.failure_reason):
            raise ValueError("策略失败必须保留 failure_code/failure_reason")
        if self.plan_evaluated != (self.plan_legal is not None):
            raise ValueError("plan_evaluated 与 plan_legal 的可用性必须一致")
        if self.recovery_applicable != (self.recovery_success is not None):
            raise ValueError("recovery_applicable 与 recovery_success 的可用性必须一致")
        if self.successful_recovery is not None and not self.recovery_applicable:
            raise ValueError("没有异常场景时不能填写 successful_recovery")
        if self.unexpected_tool_error_count > self.tool_error_count:
            raise ValueError("unexpected_tool_error_count 不能大于总工具错误")
        return self


class StrategySummary(P019Contract):
    """按策略从 60 条逐例结果重新汇总的对照表行。"""

    strategy: P019Strategy
    case_count: int = Field(ge=0)
    evaluation_pass_count: int = Field(ge=0)
    evaluation_accuracy_rate: float = Field(ge=0, le=1)
    positive_case_count: int = Field(ge=0)
    task_completion_count: int = Field(ge=0)
    task_completion_rate: float = Field(ge=0, le=1)
    plan_evaluated_count: int = Field(ge=0)
    plan_legal_count: int = Field(ge=0)
    plan_legal_rate: float = Field(ge=0, le=1)
    recovery_case_count: int = Field(ge=0)
    recovery_terminal_correct_count: int = Field(ge=0)
    recovery_rate: float = Field(ge=0, le=1)
    successful_recovery_case_count: int = Field(ge=0)
    successful_recovery_count: int = Field(ge=0)
    successful_recovery_rate: float = Field(ge=0, le=1)
    tool_error_count: int = Field(ge=0)
    unexpected_tool_error_count: int = Field(ge=0)
    step_count_total: int = Field(ge=0)
    step_count_mean: float = Field(ge=0)
    step_count_p50: float = Field(ge=0)
    step_count_p95: float = Field(ge=0)
    tool_step_count_total: int = Field(ge=0)
    token_usage: TokenSummary
    latency: LatencySummary
    resource: ResourceObservation
    zero_tolerance: ZeroToleranceMetrics

    @model_validator(mode="after")
    def validate_counts(self) -> "StrategySummary":
        """防止汇总表出现超过分母的人工填数。"""

        if self.evaluation_pass_count > self.case_count:
            raise ValueError("evaluation_pass_count 超过 case_count")
        if self.positive_case_count > self.case_count:
            raise ValueError("positive_case_count 超过 case_count")
        if self.task_completion_count > self.positive_case_count:
            raise ValueError("task_completion_count 超过正向 case 数")
        if self.plan_legal_count > self.plan_evaluated_count:
            raise ValueError("plan_legal_count 超过 plan_evaluated_count")
        if self.recovery_terminal_correct_count > self.recovery_case_count:
            raise ValueError("recovery_terminal_correct_count 超过 recovery_case_count")
        if self.successful_recovery_case_count > self.recovery_case_count:
            raise ValueError("successful_recovery_case_count 超过 recovery_case_count")
        if self.successful_recovery_count > self.successful_recovery_case_count:
            raise ValueError("successful_recovery_count 超过 successful_recovery_case_count")
        return self


class SmartDeferral(P019Contract):
    """Smart 模型对照的延期记录；所有字段固定为未启动/未完成。"""

    alias: Literal["qwen3.8-smart"] = "qwen3.8-smart"
    status: SmartComparisonStatus = SmartComparisonStatus.DEFERRED
    requested_case_count: int = Field(default=15, ge=0)
    started: Literal[False] = False
    completed: Literal[False] = False
    reason: str = Field(min_length=1, max_length=2000)
    backlog_key: str = Field(min_length=1, max_length=128)


class FairnessEvidence(P019Contract):
    """证明三种策略没有更换数据、Prompt、工具、配置或 Fast 身份。"""

    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=32)
    case_count: int = Field(ge=0)
    case_id_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_profile: Literal["fast"] = "fast"
    model_alias: Literal["qwen3.6-fast"] = "qwen3.6-fast"
    prompt_versions: dict[str, str] = Field(min_length=1)
    tool_spec_versions: dict[str, str] = Field(min_length=1)
    p018_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    p019_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    p018_source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    same_dataset: bool
    same_tools: bool
    same_prompts: bool
    same_config: bool
    same_model: bool
    react_production_path_touched: Literal[False] = False

    @model_validator(mode="after")
    def validate_all_gates(self) -> "FairnessEvidence":
        """公平性门禁任一失败都不能被报告成可比较实验。"""

        if not all((self.same_dataset, self.same_tools, self.same_prompts, self.same_config, self.same_model)):
            raise ValueError("P0-19 公平性门禁未全部通过")
        return self


class P019Report(P019Contract):
    """P0-19 完整报告，包含原始 180 条策略轨迹和汇总结论。"""

    report_id: str = Field(min_length=1, max_length=128)
    report_version: Literal["p0-19.v1", "p0-19.v2"] = "p0-19.v1"
    execution_mode: P019ExecutionMode = P019ExecutionMode.INDEPENDENT_ORACLE
    status: Literal["passed", "failed"]
    generated_at: str = Field(min_length=1)
    source_report: dict[str, JsonValue]
    fairness: FairnessEvidence
    smart_comparison: SmartDeferral
    strategies: list[StrategySummary] = Field(min_length=3, max_length=3)
    raw_results: list[StrategyCaseResult] = Field(min_length=180, max_length=180)
    conclusions: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    EXPECTED_STRATEGIES: ClassVar[set[P019Strategy]] = {
        P019Strategy.FIXED_WORKFLOW,
        P019Strategy.REACT,
        P019Strategy.PEVR,
    }

    @model_validator(mode="after")
    def validate_report_composition(self) -> "P019Report":
        """强制三策略各覆盖同一组 60 例，防止选择性报告。"""

        strategy_names = {summary.strategy for summary in self.strategies}
        if strategy_names != self.EXPECTED_STRATEGIES:
            raise ValueError("P0-19 必须恰好包含 fixed_workflow/react/pevr")
        result_names = {strategy: [] for strategy in self.EXPECTED_STRATEGIES}
        for item in self.raw_results:
            result_names[item.strategy].append(item.case_id)
        if any(len(ids) != 60 or len(set(ids)) != 60 for ids in result_names.values()):
            raise ValueError("每个 P0-19 策略必须覆盖 60 个唯一 case")
        if self.status == "passed" and self.smart_comparison.completed:
            raise ValueError("Smart 已完成不能与延期状态同时存在")
        return self


__all__ = [
    "FairnessEvidence",
    "LatencySummary",
    "P019ExecutionMode",
    "P019Report",
    "P019Strategy",
    "ResourceObservation",
    "SmartComparisonStatus",
    "SmartDeferral",
    "StrategyCaseResult",
    "StrategySummary",
    "TokenSummary",
]
