"""P0-19 同源 Trace Replay 实验器。

实验器先验证 P0-18 报告的 digest、60 个 case、Fast 模型指纹、Prompt/ToolSpec
版本和全部逐例 Trace，再对同一份证据做三种控制流投影。固定 Workflow 与 PEVR
保留源事件；ReAct 的 think/act/observe 只是可视化投影，**不能**代表独立 ReAct
Agent，也不能当作发布质量对照。所有业务终态、工具错误和安全计数直接来自源
事件。在线独立 ReAct 对照见 ``p0-19.online.v2``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any

from evals.p018.contracts import EvalCase, EvalDataset, EvalReport, EvalReportCase, EvalOutcome, ZeroToleranceMetrics
from evals.p018.dataset import load_config as load_p018_config
from evals.p018.dataset import load_dataset as load_p018_dataset
from evals.p018.reproducibility import canonical_digest, sha256_file

from .contracts import (
    FairnessEvidence,
    LatencySummary,
    P019ExecutionMode,
    P019Report,
    P019Strategy,
    ResourceObservation,
    SmartDeferral,
    StrategyCaseResult,
    StrategySummary,
    TokenSummary,
)
from .dataset import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOURCE_REPORT_PATH,
    PROJECT_ROOT,
    load_config,
    rooted_path,
)


POSITIVE_OUTCOMES = {
    EvalOutcome.COMPLETED,
    EvalOutcome.CHARGED,
    EvalOutcome.ANSWERED,
    EvalOutcome.VERIFIED,
}
ERROR_STATUSES = {"failed", "timeout", "denied"}
ACTION_EVENT_TYPES = {"tool", "verification"}
PLAN_NODES = {"plan", "validate"}
PLAN_TOOLS = {"validate_fleet_plan", "plan_multi_amr_routes"}
TRACE_DURATION_NOTE = "延迟来自 P0-18 TraceEvent 的确定性字段，不是在线服务墙钟测量。"
TOKEN_NOTE = "P0-18 为 offline_deterministic_oracle，源 Trace 没有模型调用/Token 样本。"
RESOURCE_NOTE = "P0-18 源 Trace 没有 CPU/RSS/GPU 采样，不能把缺失样本解释为零消耗。"


class SourceReportError(ValueError):
    """P0-18 源报告不能作为公平对照输入时抛出的稳定错误。"""


def _relative(path: Path) -> str:
    """把报告中的路径限制为仓库内相对路径，便于跨机器复核。"""

    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def _stable_source_reproducibility(value: Mapping[str, Any]) -> dict[str, Any]:
    """复现 P0-18 Harness 的 digest 口径，去掉唯一的生成时间字段。"""

    result = deepcopy(dict(value))
    runtime = result.get("runtime")
    if isinstance(runtime, dict):
        runtime.pop("generated_at", None)
    return result


def _expected_source_digest(report: EvalReport) -> str:
    """从源报告正文重算 digest，不信任 JSON 中自带的摘要字段。"""

    body = {
        "dataset_id": report.dataset_id,
        "dataset_version": report.dataset_version,
        "reproducibility": _stable_source_reproducibility(report.reproducibility),
        "metrics": report.metrics.model_dump(mode="json"),
        "failures": report.failures,
        "observed_negative_cases": report.observed_negative_cases,
        "cases": [item.model_dump(mode="json") for item in report.cases],
    }
    return canonical_digest(body)


def load_source_report(path: str | Path = DEFAULT_SOURCE_REPORT_PATH) -> EvalReport:
    """加载并完整验证 P0-18 源报告；损坏、缺例或意外失败均 fail closed。"""

    report_path = Path(path).resolve()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        report = EvalReport.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SourceReportError(f"P0-18 源报告不可读取或契约无效: {report_path}") from exc
    if report.dataset_id != "amr-p018-60" or report.dataset_version != "p0-18.v1":
        raise SourceReportError("P0-19 只接受 amr-p018-60/p0-18.v1 源报告")
    if report.status != "passed" or report.failures or report.metrics.evaluation_pass_count != 60:
        raise SourceReportError("P0-18 源报告存在意外失败，不能作为公平对照输入")
    if report.metrics.zero_tolerance.total() != 0:
        raise SourceReportError("P0-18 源报告零容忍指标非零，不能进入 P0-19")
    if _expected_source_digest(report) != report.report_digest:
        raise SourceReportError("P0-18 源报告 digest 与正文不一致")
    if len(report.cases) != 60 or len({item.case_id for item in report.cases}) != 60:
        raise SourceReportError("P0-18 源报告必须包含 60 个唯一逐例结果")
    if any(not item.trace_events for item in report.cases):
        raise SourceReportError("P0-18 每个 case 都必须保留非空 Trace")
    return report


def _sha256_mapping(value: Any) -> str:
    """对 JSON 映射做稳定摘要，供公平性和源轨迹身份绑定。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _event_latency(event: Mapping[str, Any]) -> float:
    """读取 TraceEvent 延迟；必要时用带时区时间戳做同源回退。"""

    value = event.get("latency_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    started = event.get("started_at")
    finished = event.get("finished_at")
    if isinstance(started, str) and isinstance(finished, str):
        try:
            delta = datetime.fromisoformat(finished.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))
            return max(0.0, delta.total_seconds() * 1000.0)
        except ValueError:
            return 0.0
    return 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    """用固定 inclusive 定义计算百分位，避免不同实现产生口径漂移。"""

    if not values:
        return 0.0
    if len(values) == 1:
        return round(float(values[0]), 6)
    return round(float(statistics.quantiles(list(values), n=100, method="inclusive")[int(percentile) - 1]), 6)


def _trace_tokens(
    events: Sequence[Mapping[str, Any]],
    *,
    source: str = "p018_trace",
) -> TokenSummary:
    """从真实 model Trace 汇总 Token，并把 Schema 修复尝试计入调用数。"""

    model_events = [event for event in events if event.get("event_type") == "model"]
    input_tokens = sum(int(event.get("input_tokens") or 0) for event in model_events)
    output_tokens = sum(int(event.get("output_tokens") or 0) for event in model_events)
    total_tokens = sum(int(event.get("total_tokens") or 0) for event in model_events)
    observed = bool(model_events)
    model_call_count = 0
    for event in model_events:
        metadata = event.get("metadata")
        attempts = metadata.get("attempts") if isinstance(metadata, Mapping) else None
        model_call_count += int(attempts) if isinstance(attempts, int) and attempts > 0 else 1
    return TokenSummary(
        observed=observed,
        model_call_count=model_call_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        source=source if observed else "not_observed",  # type: ignore[arg-type]
        note="在线/源 model Trace 已提供 usage。" if observed else TOKEN_NOTE,
    )


def _trace_resource(events: Sequence[Mapping[str, Any]]) -> ResourceObservation:
    """只接受显式资源样本，不从 Token 或延迟反推资源消耗。"""

    samples: list[Mapping[str, Any]] = []
    for event in events:
        metadata = event.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        if any(key in metadata for key in ("cpu_time_ms", "peak_rss_mb", "peak_gpu_memory_mb")):
            samples.append(metadata)
    if not samples:
        return ResourceObservation(observed=False, sample_count=0, source="not_observed", reason=RESOURCE_NOTE)
    cpu = [float(item["cpu_time_ms"]) for item in samples if isinstance(item.get("cpu_time_ms"), (int, float))]
    rss = [float(item["peak_rss_mb"]) for item in samples if isinstance(item.get("peak_rss_mb"), (int, float))]
    gpu = [float(item["peak_gpu_memory_mb"]) for item in samples if isinstance(item.get("peak_gpu_memory_mb"), (int, float))]
    return ResourceObservation(
        observed=True,
        sample_count=len(samples),
        cpu_time_ms=sum(cpu) if cpu else None,
        peak_rss_mb=max(rss) if rss else None,
        peak_gpu_memory_mb=max(gpu) if gpu else None,
        source="p018_trace",
        reason="资源样本由源 Trace metadata 显式提供。",
    )


def _plan_metrics(case: EvalReportCase) -> tuple[bool, bool | None]:
    """从 plan/validate 节点或 validate_fleet_plan 工具事件推断计划是否评估过。"""

    events = case.trace_events
    evaluated = any(
        event.get("node") in PLAN_NODES or event.get("tool_name") in PLAN_TOOLS for event in events
    )
    if not evaluated:
        return False, None
    validate_events = [
        event
        for event in events
        if event.get("node") == "validate" or event.get("tool_name") == "validate_fleet_plan"
    ]
    legal = bool(validate_events) and all(event.get("status") == "completed" for event in validate_events)
    legal = legal and case.zero_tolerance.total() == 0
    return True, legal


def _recovery_metrics(case: EvalReportCase) -> tuple[bool, bool | None, bool | None]:
    """异常终态正确严格按 expected==observed；成功恢复必须发生恢复动作且最终完成。"""

    applicable = case.category.value == "exception_local_replan"
    if not applicable:
        return False, None, None
    terminal_correct = case.expected_outcome == case.observed_outcome
    recovery_action = (
        case.replan_count > 0
        or case.retry_count > 0
        or int(case.metrics.get("react_recovery_action_count") or 0) > 0
    )
    successful_recovery = recovery_action and case.observed_outcome in POSITIVE_OUTCOMES
    return True, terminal_correct, successful_recovery


def _tool_errors(case: EvalReportCase) -> tuple[int, int]:
    """统计工具/验证事件错误，并把符合 oracle 的负向错误排除在意外错误外。"""

    errors = [
        event
        for event in case.trace_events
        if event.get("event_type") in ACTION_EVENT_TYPES and event.get("status") in ERROR_STATUSES
    ]
    if not errors:
        return 0, 0
    expected_negative = case.evaluation_passed and case.observed_outcome in {
        EvalOutcome.DENIED,
        EvalOutcome.BLOCKED,
        EvalOutcome.FAILED,
    }
    return len(errors), 0 if expected_negative else len(errors)


def _projection(strategy: P019Strategy, case: EvalReportCase) -> tuple[int, int, list[dict[str, Any]]]:
    """生成策略控制步投影；不复制或改变源工具结果。"""

    events = case.trace_events
    projection: list[dict[str, Any]] = []
    if strategy is P019Strategy.REACT:
        step = 0
        for event in events:
            source_sequence = event.get("sequence")
            if event.get("node") == "guard" and step == 0:
                step += 1
                projection.append({"projection_sequence": step, "kind": "guard", "source_sequence": source_sequence})
                continue
            step += 1
            projection.append({"projection_sequence": step, "kind": "think", "source_sequence": source_sequence})
            if event.get("event_type") in ACTION_EVENT_TYPES:
                step += 1
                projection.append({"projection_sequence": step, "kind": "act", "source_sequence": source_sequence, "tool_name": event.get("tool_name")})
            step += 1
            projection.append({"projection_sequence": step, "kind": "observe", "source_sequence": source_sequence, "status": event.get("status")})
        return step, sum(event.get("event_type") in ACTION_EVENT_TYPES for event in events), projection

    for index, event in enumerate(events, start=1):
        stage = str(event.get("node") or event.get("event_type") or "unknown")
        projection.append(
            {
                "projection_sequence": index,
                "kind": "workflow_step" if strategy is P019Strategy.FIXED_WORKFLOW else "pevr_event",
                "stage": stage,
                "source_sequence": event.get("sequence"),
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
            }
        )
    return len(events), sum(event.get("event_type") in ACTION_EVENT_TYPES for event in events), projection


def _case_result(strategy: P019Strategy, case: EvalReportCase) -> StrategyCaseResult:
    """把一个 P0-18 结果投影为一个策略结果，保留所有源证据。"""

    events = [dict(event) for event in case.trace_events]
    step_count, tool_step_count, projection = _projection(strategy, case)
    plan_evaluated, plan_legal = _plan_metrics(case)
    recovery_applicable, recovery_success, successful_recovery = _recovery_metrics(case)
    tool_error_count, unexpected_tool_error_count = _tool_errors(case)
    token_usage = _trace_tokens(events)
    latency = sum(_event_latency(event) for event in events)
    resource = _trace_resource(events)
    source_case = case.model_dump(mode="json")
    return StrategyCaseResult(
        strategy=strategy,
        case_id=case.case_id,
        category=case.category,
        source_trace_id=case.trace_id,
        strategy_trace_id=f"trace-p019-{strategy.value}-{case.case_id}",
        expected_outcome=case.expected_outcome,
        observed_outcome=case.observed_outcome,
        evaluation_passed=case.evaluation_passed,
        task_completed=case.observed_outcome in POSITIVE_OUTCOMES,
        plan_evaluated=plan_evaluated,
        plan_legal=plan_legal,
        recovery_applicable=recovery_applicable,
        recovery_success=recovery_success,
        successful_recovery=successful_recovery,
        tool_error_count=tool_error_count,
        unexpected_tool_error_count=unexpected_tool_error_count,
        step_count=step_count,
        tool_step_count=tool_step_count,
        derived_control_overhead_steps=step_count - len(events),
        token_usage=token_usage,
        latency_ms=latency,
        resource=resource,
        replan_count=case.replan_count,
        retry_count=case.retry_count,
        approval_resume_count=case.approval_resume_count,
        zero_tolerance=case.zero_tolerance,
        failure_code=case.failure_code,
        failure_reason=case.failure_reason,
        source_case=source_case,
        source_trace_events=events,
        projection_events=projection,
    )


def _mean(values: Sequence[float]) -> float:
    """空分母返回 0，避免统计报告出现 NaN。"""

    return round(float(statistics.fmean(values)), 6) if values else 0.0


def _aggregate(
    strategy: P019Strategy,
    results: Sequence[StrategyCaseResult],
    *,
    latency_source: str = "p018_trace",
    wall_clock: bool = False,
) -> StrategySummary:
    """完全从逐例策略结果重算汇总表，不接受外部预填数字。"""

    case_count = len(results)
    passed = sum(item.evaluation_passed for item in results)
    positive = [item for item in results if item.expected_outcome in POSITIVE_OUTCOMES]
    plan = [item for item in results if item.plan_evaluated]
    legal = [item for item in plan if item.plan_legal]
    recovery = [item for item in results if item.recovery_applicable]
    terminal_correct = [item for item in recovery if item.recovery_success]
    successful_cases = [item for item in recovery if item.observed_outcome in POSITIVE_OUTCOMES]
    successful = [item for item in successful_cases if item.successful_recovery]
    latencies = [item.latency_ms for item in results]
    steps = [float(item.step_count) for item in results]
    tokens = [item.token_usage for item in results]
    token_observed = any(item.observed for item in tokens)
    observed_token_source = next(
        (item.source for item in tokens if item.observed),
        "not_observed",
    )
    token_usage = TokenSummary(
        observed=token_observed,
        model_call_count=sum(item.model_call_count for item in tokens),
        input_tokens=sum(item.input_tokens for item in tokens),
        output_tokens=sum(item.output_tokens for item in tokens),
        total_tokens=sum(item.total_tokens for item in tokens),
        source=observed_token_source,
        note="在线/源 model Trace 已提供 usage。" if token_observed else TOKEN_NOTE,
    )
    resources = [item.resource for item in results if item.resource.observed]
    if resources:
        resource = ResourceObservation(
            observed=True,
            sample_count=sum(item.sample_count for item in resources),
            cpu_time_ms=sum(item.cpu_time_ms or 0 for item in resources),
            peak_rss_mb=max((item.peak_rss_mb or 0 for item in resources), default=None),
            peak_gpu_memory_mb=max((item.peak_gpu_memory_mb or 0 for item in resources), default=None),
            source=resources[0].source,
            reason="资源样本由逐例显式采样结果汇总。",
        )
    else:
        resource = ResourceObservation(observed=False, sample_count=0, source="not_observed", reason=RESOURCE_NOTE)
    zero = {
        field: sum(getattr(item.zero_tolerance, field) for item in results)
        for field in ZeroToleranceMetrics.model_fields
    }
    return StrategySummary(
        strategy=strategy,
        case_count=case_count,
        evaluation_pass_count=passed,
        evaluation_accuracy_rate=round(passed / case_count, 6) if case_count else 0.0,
        positive_case_count=len(positive),
        task_completion_count=sum(item.task_completed for item in positive),
        task_completion_rate=round(sum(item.task_completed for item in positive) / len(positive), 6) if positive else 0.0,
        plan_evaluated_count=len(plan),
        plan_legal_count=len(legal),
        plan_legal_rate=round(len(legal) / len(plan), 6) if plan else 0.0,
        recovery_case_count=len(recovery),
        recovery_terminal_correct_count=len(terminal_correct),
        recovery_rate=round(len(terminal_correct) / len(recovery), 6) if recovery else 0.0,
        successful_recovery_case_count=len(successful_cases),
        successful_recovery_count=len(successful),
        successful_recovery_rate=round(len(successful) / len(successful_cases), 6) if successful_cases else 0.0,
        tool_error_count=sum(item.tool_error_count for item in results),
        unexpected_tool_error_count=sum(item.unexpected_tool_error_count for item in results),
        step_count_total=sum(item.step_count for item in results),
        step_count_mean=_mean(steps),
        step_count_p50=_percentile(steps, 50),
        step_count_p95=_percentile(steps, 95),
        tool_step_count_total=sum(item.tool_step_count for item in results),
        token_usage=token_usage,
        latency=LatencySummary(
            observed=all(event_count > 0 for event_count in (len(item.source_trace_events) for item in results)),
            sample_count=len(latencies),
            total_ms=round(sum(latencies), 6),
            mean_case_ms=_mean(latencies),
            p50_case_ms=_percentile(latencies, 50),
            p95_case_ms=_percentile(latencies, 95),
            max_case_ms=round(max(latencies, default=0.0), 6),
            source=latency_source,  # type: ignore[arg-type]
            wall_clock=wall_clock,
            note=(
                "逐例延迟来自真实在线执行墙钟，包含模型、工具、HITL 与评测控制器。"
                if wall_clock
                else (
                    TRACE_DURATION_NOTE
                    if latency_source == "p018_trace"
                    else "延迟来自各策略独立离线 Trace，不是在线服务墙钟测量。"
                )
            ),
        ),
        resource=resource,
        zero_tolerance=ZeroToleranceMetrics(**zero),
    )


def _source_case_map(dataset: EvalDataset, report: EvalReport) -> dict[str, EvalCase]:
    """检查源报告和冻结数据集的 case 身份完全一致。"""

    dataset_map = {case.case_id: case for case in dataset.cases}
    report_ids = {case.case_id for case in report.cases}
    if set(dataset_map) != report_ids or len(report_ids) != 60:
        raise SourceReportError("P0-18 数据集与源报告 case_id 集合不一致")
    if any(case.model_alias != "qwen3.6-fast" or case.model_profile != "fast" for case in dataset.cases):
        raise SourceReportError("P0-19 要求 60 例全部绑定 Qwen3.6 Fast")
    return dataset_map


def compare_source_report(
    *,
    source_report: EvalReport,
    source_report_path: str | Path,
    p019_config: Mapping[str, object],
    p019_config_path: str | Path,
    p018_dataset: EvalDataset,
    p018_config: Mapping[str, object],
    p018_config_path: str | Path,
) -> P019Report:
    """执行三策略同源回放并生成完整 P0-19 报告。"""

    dataset_map = _source_case_map(p018_dataset, source_report)
    source_cases = [source_report.cases[index] for index in range(len(source_report.cases))]
    source_model = source_report.reproducibility.get("model", {})
    source_prompt_versions = source_report.reproducibility.get("prompt_versions", {})
    source_tool_versions = source_report.reproducibility.get("tool_spec_versions", {})
    model_cfg = p019_config.get("model")
    p018_model_cfg = p018_config.get("model")
    p018_prompts = p018_config.get("prompts")
    p018_tools = p018_config.get("tools")
    source_inputs = source_report.reproducibility.get("input_files", {})
    source_config_fingerprint = source_inputs.get("config", {}) if isinstance(source_inputs, Mapping) else {}
    source_dataset_fingerprint = source_inputs.get("dataset", {}) if isinstance(source_inputs, Mapping) else {}
    p018_dataset_path = rooted_path(str(p019_config["p018_dataset_path"]))
    same_dataset = (
        source_report.dataset_id == "amr-p018-60"
        and source_report.dataset_version == "p0-18.v1"
        and canonical_digest(sorted(dataset_map)) == canonical_digest(sorted(item.case_id for item in source_cases))
        and isinstance(source_dataset_fingerprint, Mapping)
        and sha256_file(p018_dataset_path) == source_dataset_fingerprint.get("sha256")
    )
    same_tools = (
        isinstance(p018_tools, Mapping)
        and sorted(str(item) for item in p018_tools.get("names", [])) == sorted(str(key) for key in source_tool_versions)
        and source_tool_versions == source_report.reproducibility.get("tool_spec_versions")
    )
    expected_prompt_versions: dict[str, str] = {}
    if isinstance(p018_prompts, Mapping):
        expected_prompt_versions = {
            str(key): str(value.get("version"))
            for key, value in p018_prompts.items()
            if isinstance(value, Mapping)
        }
    same_prompts = expected_prompt_versions == source_prompt_versions
    same_model = (
        isinstance(model_cfg, Mapping)
        and isinstance(p018_model_cfg, Mapping)
        and model_cfg.get("profile") == "fast"
        and model_cfg.get("alias") == "qwen3.6-fast"
        and p018_model_cfg.get("profile") == "fast"
        and p018_model_cfg.get("alias") == "qwen3.6-fast"
        and isinstance(source_model, Mapping)
        and source_model.get("profile") == "fast"
        and source_model.get("alias") == "qwen3.6-fast"
    )
    p018_config_path = Path(p018_config_path).resolve()
    same_config = (
        isinstance(source_config_fingerprint, Mapping)
        and source_config_fingerprint.get("sha256") == sha256_file(p018_config_path)
        and str(p019_config.get("p018_config_path")) == _relative(p018_config_path)
    )
    fairness = FairnessEvidence(
        dataset_id=source_report.dataset_id,
        dataset_version=source_report.dataset_version,
        case_count=len(source_cases),
        case_id_digest=canonical_digest(sorted(dataset_map)),
        prompt_versions={str(key): str(value) for key, value in source_prompt_versions.items()},
        tool_spec_versions={str(key): str(value) for key, value in source_tool_versions.items()},
        p018_config_sha256=sha256_file(p018_config_path),
        p019_config_sha256=sha256_file(Path(p019_config_path).resolve()),
        p018_source_report_sha256=sha256_file(Path(source_report_path).resolve()),
        same_dataset=same_dataset,
        same_tools=same_tools,
        same_prompts=same_prompts,
        same_config=same_config,
        same_model=same_model,
        react_production_path_touched=False,
    )
    # FairnessEvidence 自身会 fail closed；下面的策略配置也必须只包含三个固定 ID。
    strategy_ids = [item.get("id") for item in p019_config.get("strategies", []) if isinstance(item, Mapping)]
    if strategy_ids != [strategy.value for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR)]:
        raise SourceReportError("P0-19 策略配置顺序或 ID 被修改")
    raw_results: list[StrategyCaseResult] = []
    summaries: list[StrategySummary] = []
    for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR):
        strategy_results = [_case_result(strategy, case) for case in source_cases]
        raw_results.extend(strategy_results)
        summaries.append(_aggregate(strategy, strategy_results))
    smart_payload = p019_config["smart_comparison"]
    if not isinstance(smart_payload, Mapping):
        raise SourceReportError("P0-19 缺少 Smart 延期记录")
    smart = SmartDeferral.model_validate(smart_payload)
    react = next(item for item in summaries if item.strategy is P019Strategy.REACT)
    pevr = next(item for item in summaries if item.strategy is P019Strategy.PEVR)
    conclusions = [
        f"三种策略共用同一 P0-18 60 例源报告和 Fast 指纹；任务完成率 {pevr.task_completion_count}/{pevr.positive_case_count}，全例预期符合率 {pevr.evaluation_pass_count}/{pevr.case_count}。",
        f"计划合法率为 {pevr.plan_legal_count}/{pevr.plan_evaluated_count}，异常终止/恢复正确率为 {pevr.recovery_terminal_correct_count}/{pevr.recovery_case_count}，成功重规划率为 {pevr.successful_recovery_count}/{pevr.successful_recovery_case_count}（仅统计最终完成的异常路径）；三种策略结果相同，因为均复用同一真实源 Trace。",
        f"工具错误共 {pevr.tool_error_count} 次，其中意外工具错误 {pevr.unexpected_tool_error_count} 次；这些错误保留了预期拒绝/阻塞轨迹，没有从数据中删除。",
        f"ReAct 的派生控制步均值/P95 为 {react.step_count_mean:.2f}/{react.step_count_p95:.2f}，PEVR 为 {pevr.step_count_mean:.2f}/{pevr.step_count_p95:.2f}；该差异只是 think-act-observe 可视化投影开销，不是独立 ReAct Agent，也不是在线墙钟。",
        "PEVR 是生产主链；本 Replay 的 ReAct 槽位只作可视化。发布质量对照必须使用 p0-19.online.v2 独立 ReAct 循环。",
        "Qwen3.8 Smart 对照已延期：本步未启动、未测试、未完成，不阻塞 P0-19；延期项已写入 Backlog。",
    ]
    limitations = [
        "P0-18 源执行模式为 offline_deterministic_oracle，三策略本次没有新增在线 Qwen3.6 Fast 模型调用；报告中的 Fast 是同源配置/身份指纹，不是 60 例在线 LLM 质量结论。Replay 不得被引用为独立 ReAct 结果。",
        "Token 与 CPU/RSS/GPU 资源在源 Trace 中未观测，报告用 observed=false 和说明保留缺失事实，不能据此宣称零消耗或比较吞吐。",
        "P0-18 Trace 的 latency_ms/时间戳是确定性回放字段；P95 只表示源 Trace 的逐例延迟分布，不代表模型服务墙钟 P95。",
        "要获得在线三策略质量/Token/资源对照，后续必须新增独立 online_fast execution_mode、同一 60 例的受控 adapter、采样器和新验收门槛；不能改写本报告。",
    ]
    stable_body = {
        "execution_mode": P019ExecutionMode.TRACE_REPLAY.value,
        "source_report": {
            "report_id": source_report.report_id,
            "report_digest": source_report.report_digest,
            "sha256": fairness.p018_source_report_sha256,
        },
        "fairness": fairness.model_dump(mode="json"),
        "smart_comparison": smart.model_dump(mode="json"),
        "strategies": [item.model_dump(mode="json") for item in summaries],
        "raw_results": [item.model_dump(mode="json") for item in raw_results],
        "conclusions": conclusions,
        "limitations": limitations,
    }
    report_digest = canonical_digest(stable_body)
    source_summary = {
        "path": _relative(Path(source_report_path)),
        "sha256": fairness.p018_source_report_sha256,
        "report_id": source_report.report_id,
        "report_digest": source_report.report_digest,
        "dataset_id": source_report.dataset_id,
        "dataset_version": source_report.dataset_version,
        "case_count": len(source_cases),
    }
    return P019Report(
        report_id=f"p019-{report_digest[:16]}",
        execution_mode=P019ExecutionMode.TRACE_REPLAY,
        status="passed",
        generated_at=datetime.now().astimezone().isoformat(),
        source_report=source_summary,
        fairness=fairness,
        smart_comparison=smart,
        strategies=summaries,
        raw_results=raw_results,
        conclusions=conclusions,
        limitations=limitations,
        report_digest=report_digest,
    )


def run_comparison(
    *,
    source_report_path: str | Path = DEFAULT_SOURCE_REPORT_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> P019Report:
    """加载固定 P0-18 输入并执行 P0-19 三策略回放。"""

    p019_config = load_config(config_path)
    p018_dataset_path = rooted_path(str(p019_config["p018_dataset_path"]))
    p018_config_path = rooted_path(str(p019_config["p018_config_path"]))
    p018_dataset = load_p018_dataset(p018_dataset_path)
    p018_config = load_p018_config(p018_config_path)
    source_report = load_source_report(source_report_path)
    return compare_source_report(
        source_report=source_report,
        source_report_path=source_report_path,
        p019_config=p019_config,
        p019_config_path=config_path,
        p018_dataset=p018_dataset,
        p018_config=p018_config,
        p018_config_path=p018_config_path,
    )


__all__ = [
    "SourceReportError",
    "compare_source_report",
    "load_source_report",
    "run_comparison",
]
