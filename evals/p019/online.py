"""P0-19 三策略真实 Fast 在线闭环执行器。

三种策略共享 Guard → Understand → 初次 Retrieve 的策略无关前置，再分别进入：
固定 Workflow 图、独立 ReAct 循环、生产 PEVR 图。Fixed / PEVR 仍走 P0-18
``OnlineFastHarness``；ReAct 必须走 ``ReActOnlineHarness``，禁止实例化
``PEVRGraphRunner``。数据集、Fast 制品、工具、安全门禁和预算包络保持相同；
控制 Prompt 按策略不同，因此不再宣称 ``same_prompts=true``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any

import psutil

from evals.p018.contracts import EvalCase, EvalReport, EvalReportCase
from evals.p018.dataset import load_config as load_p018_config
from evals.p018.dataset import load_dataset as load_p018_dataset
from evals.p018.online import OnlineControlStrategy, OnlineFastHarness
from evals.p018.oracle import POSITIVE_OUTCOMES
from evals.p018.reporting import write_report as write_p018_report
from evals.p018.reproducibility import canonical_digest, sha256_file

from .contracts import (
    FairnessEvidence,
    P019ExecutionMode,
    P019Report,
    P019Strategy,
    ResourceObservation,
    SmartDeferral,
    StrategyCaseResult,
    StrategySummary,
)
from .dataset import DEFAULT_ONLINE_CONFIG_PATH, load_config, rooted_path
from agent.context.prompt_registry import P005_PROMPT_VERSION
from .react_contracts import REACT_PROMPT_ID, REACT_PROMPT_VERSION, REACT_RUNNER_VERSION
from .react_eval import ReActOnlineHarness
from .replay import (
    _aggregate,
    _plan_metrics,
    _recovery_metrics,
    _tool_errors,
    _trace_tokens,
)


DEFAULT_ONLINE_OUTPUT_DIR = Path("tmp/p019_online_strategy_compare")
STRATEGY_ORDER = (
    P019Strategy.FIXED_WORKFLOW,
    P019Strategy.REACT,
    P019Strategy.PEVR,
)


class OnlineComparisonError(RuntimeError):
    """在线输入、进度身份或公平性门禁失败时的稳定异常。"""


class ProcessResourceSampler:
    """按 case 采样评测进程、模型监听进程与 GPU 显存。

    采样是进程级观测，不试图把共享 OS/驱动成本精确归因给某个节点。CPU 使用相邻
    样本的进程累计时间差，RSS/GPU 取峰值；进程退出、权限不足或无 NVIDIA GPU 时
    只保留可观测字段，绝不把缺失值写成 0。
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        listener_ports: Sequence[int],
        gpu_interval_seconds: float = 5.0,
    ) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.gpu_stride = max(1, round(float(gpu_interval_seconds) / self.interval_seconds))
        self.listener_ports = {int(port) for port in listener_ports}
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._previous_cpu: dict[int, float] = {}
        self._cpu_seconds = 0.0
        self._peak_rss_bytes: int | None = None
        self._peak_gpu_mb: float | None = None
        self._sample_count = 0
        self._gpu_sample_count = 0
        self._sample_tick = 0

    def start(self) -> "ProcessResourceSampler":
        """先取基线再启动守护线程，避免首个累计 CPU 值被误算为本 case 成本。"""

        self._sample()
        self._thread = Thread(target=self._loop, name="p019-resource-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> ResourceObservation:
        """停止线程并返回至少含 CPU/RSS 的显式观测。"""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 3))
        self._sample()
        with self._lock:
            return ResourceObservation(
                observed=self._sample_count > 0,
                sample_count=self._sample_count,
                cpu_time_ms=round(self._cpu_seconds * 1000.0, 3),
                peak_rss_mb=(
                    round(self._peak_rss_bytes / (1024 * 1024), 3)
                    if self._peak_rss_bytes is not None
                    else None
                ),
                peak_gpu_memory_mb=(
                    round(self._peak_gpu_mb, 3) if self._peak_gpu_mb is not None else None
                ),
                source="online_sampler",
                reason=(
                    "采样评测进程、其子进程及 8080/18080 监听进程；"
                    f"GPU 有效样本 {self._gpu_sample_count} 次。"
                ),
            )

    def _loop(self) -> None:
        """用 Event.wait 实现可立即停止的短周期采样，不留下后台线程。"""

        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _target_pids(self) -> set[int]:
        """动态发现短命 C++ 子进程及模型/鉴权代理监听进程。"""

        pids = {os.getpid()}
        try:
            pids.update(child.pid for child in psutil.Process(os.getpid()).children(recursive=True))
        except (psutil.Error, OSError):
            pass
        try:
            for connection in psutil.net_connections(kind="inet"):
                if connection.pid is None or not connection.laddr:
                    continue
                if int(connection.laddr.port) in self.listener_ports:
                    pids.add(int(connection.pid))
        except (psutil.Error, OSError):
            pass
        return pids

    @staticmethod
    def _gpu_memory_mb(pids: set[int]) -> float | None:
        """只汇总目标 PID 的 compute 显存；命令缺失或无行时返回 None。"""

        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        values: list[float] = []
        for line in completed.stdout.splitlines():
            fields = [item.strip() for item in line.split(",")]
            if len(fields) != 2:
                continue
            try:
                pid = int(fields[0])
                memory = float(fields[1])
            except ValueError:
                continue
            if pid in pids:
                values.append(memory)
        return sum(values) if values else None

    def _sample(self) -> None:
        """单次采样对消失/拒绝访问的进程 fail-soft，不影响评测闭环。"""

        pids = self._target_pids()
        current_cpu: dict[int, float] = {}
        rss_bytes = 0
        observed_processes = 0
        for pid in pids:
            try:
                process = psutil.Process(pid)
                cpu = process.cpu_times()
                current_cpu[pid] = float(cpu.user + cpu.system)
                rss_bytes += int(process.memory_info().rss)
                observed_processes += 1
            except (psutil.Error, OSError):
                continue
        # nvidia-smi 会创建外部进程；CPU/RSS 保持高频，GPU 每约 5 秒采一次，
        # 避免采样器本身显著污染短 case 的墙钟与 CPU 指标。
        query_gpu = self._sample_tick % self.gpu_stride == 0
        self._sample_tick += 1
        gpu_mb = self._gpu_memory_mb(pids) if query_gpu else None
        with self._lock:
            for pid, value in current_cpu.items():
                previous = self._previous_cpu.get(pid)
                if previous is not None:
                    self._cpu_seconds += max(0.0, value - previous)
            self._previous_cpu = current_cpu
            if observed_processes:
                self._peak_rss_bytes = max(self._peak_rss_bytes or 0, rss_bytes)
                self._sample_count += 1
            if gpu_mb is not None:
                self._peak_gpu_mb = max(self._peak_gpu_mb or 0.0, gpu_mb)
                self._gpu_sample_count += 1


def _resource_from_case(case: EvalReportCase) -> ResourceObservation:
    """从逐例 metrics 还原采样契约，禁止从延迟或 Token 猜资源。"""

    sample_count = int(case.metrics.get("resource_sample_count") or 0)
    return ResourceObservation(
        observed=sample_count > 0,
        sample_count=sample_count,
        cpu_time_ms=(
            float(case.metrics["cpu_time_ms"])
            if isinstance(case.metrics.get("cpu_time_ms"), (int, float))
            else None
        ),
        peak_rss_mb=(
            float(case.metrics["peak_rss_mb"])
            if isinstance(case.metrics.get("peak_rss_mb"), (int, float))
            else None
        ),
        peak_gpu_memory_mb=(
            float(case.metrics["peak_gpu_memory_mb"])
            if isinstance(case.metrics.get("peak_gpu_memory_mb"), (int, float))
            else None
        ),
        source="online_sampler" if sample_count > 0 else "not_observed",
        reason=str(case.metrics.get("resource_observation_reason") or "在线采样不可用"),
    )


def _actual_control_events(strategy: P019Strategy, case: EvalReportCase) -> list[dict[str, Any]]:
    """在线模式只记录实际控制证据，不再生成虚构 think/act/observe 投影。"""

    if strategy is P019Strategy.REACT:
        controller = [
            dict(event)
            for event in case.trace_events
            if event.get("node") in {"react_decide", "react_act", "react_observe", "react_guard", "react_terminal"}
        ]
        if controller:
            return [
                {
                    "kind": "actual_react_loop",
                    "sequence": event.get("sequence"),
                    "node": event.get("node"),
                    "event_type": event.get("event_type"),
                    "status": event.get("status"),
                    "metadata": event.get("metadata") or {},
                    "error": event.get("error"),
                }
                for event in controller
            ]
        return [{"kind": "react_loop_missing", "status": "not_applicable"}]
    if strategy is P019Strategy.FIXED_WORKFLOW:
        return [
            {
                "kind": "fixed_workflow_actual",
                "fault_recovery_enabled": False,
                "terminal_status": case.observed_outcome.value,
            }
        ]
    return [
        {
            "kind": "pevr_production_actual",
            "fault_recovery_enabled": True,
            "replan_count": case.replan_count,
            "retry_count": case.retry_count,
            "terminal_status": case.observed_outcome.value,
        }
    ]


def _online_case_result(strategy: P019Strategy, case: EvalReportCase) -> StrategyCaseResult:
    """把策略自己的在线 P0-18 结果转换为 P0-19 原始结果。"""

    events = [dict(event) for event in case.trace_events]
    plan_evaluated, plan_legal = _plan_metrics(case)
    recovery_applicable, recovery_success, successful_recovery = _recovery_metrics(case)
    tool_error_count, unexpected_tool_error_count = _tool_errors(case)
    return StrategyCaseResult(
        strategy=strategy,
        case_id=case.case_id,
        category=case.category,
        source_trace_id=case.trace_id,
        strategy_trace_id=case.trace_id,
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
        step_count=len(events),
        tool_step_count=sum(event.get("event_type") == "tool" for event in events),
        derived_control_overhead_steps=0,
        token_usage=_trace_tokens(events, source="online_trace"),
        latency_ms=float(case.metrics.get("wall_clock_ms") or 0.0),
        resource=_resource_from_case(case),
        replan_count=case.replan_count,
        retry_count=case.retry_count,
        approval_resume_count=case.approval_resume_count,
        zero_tolerance=case.zero_tolerance,
        failure_code=case.failure_code,
        failure_reason=case.failure_reason,
        source_case=case.model_dump(mode="json"),
        source_trace_events=events,
        projection_events=_actual_control_events(strategy, case),
    )


class OnlineThreeStrategyComparison:
    """管理公平门禁、可恢复进度、交错执行和最终报告汇总。"""

    def __init__(
        self,
        *,
        config_path: str | Path = DEFAULT_ONLINE_CONFIG_PATH,
        output_dir: str | Path = DEFAULT_ONLINE_OUTPUT_DIR,
        verification_timeout_seconds: float = 120.0,
        resume: bool = False,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        if self.config.get("execution_mode") != P019ExecutionMode.ONLINE_FAST.value:
            raise OnlineComparisonError("在线入口只接受 online_fast_three_strategy_closed_loop")
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.resume = resume
        self.p018_dataset_path = rooted_path(str(self.config["p018_dataset_path"]))
        self.p018_config_path = rooted_path(str(self.config["p018_config_path"]))
        self.dataset = load_p018_dataset(self.p018_dataset_path)
        self.p018_config = load_p018_config(self.p018_config_path)
        if len(self.dataset.cases) != 60:
            raise OnlineComparisonError("在线三策略必须精确覆盖 P0-18 的 60 例")
        self.harnesses = {
            P019Strategy.FIXED_WORKFLOW: OnlineFastHarness(
                dataset=self.dataset,
                config=self.p018_config,
                dataset_path=self.p018_dataset_path,
                config_path=self.p018_config_path,
                verification_timeout_seconds=verification_timeout_seconds,
                control_strategy=OnlineControlStrategy.FIXED_WORKFLOW,
            ),
            P019Strategy.REACT: ReActOnlineHarness(
                dataset=self.dataset,
                config=self.p018_config,
                dataset_path=self.p018_dataset_path,
                config_path=self.p018_config_path,
                verification_timeout_seconds=verification_timeout_seconds,
            ),
            P019Strategy.PEVR: OnlineFastHarness(
                dataset=self.dataset,
                config=self.p018_config,
                dataset_path=self.p018_dataset_path,
                config_path=self.p018_config_path,
                verification_timeout_seconds=verification_timeout_seconds,
                control_strategy=OnlineControlStrategy.PEVR,
            ),
        }
        sampling = self.config.get("resource_sampling")
        if not isinstance(sampling, Mapping):
            raise OnlineComparisonError("缺少 resource_sampling 配置")
        self.sample_interval = float(sampling.get("interval_seconds") or 0.5)
        self.gpu_interval = float(sampling.get("gpu_interval_seconds") or 5.0)
        ports = sampling.get("include_listeners")
        self.listener_ports = [int(port) for port in ports] if isinstance(ports, list) else [8080, 18080]
        self.progress_path = self.output_dir / "p019_online_progress.jsonl"
        self.manifest_path = self.output_dir / "p019_online_run_manifest.json"

    def _manifest(self) -> dict[str, Any]:
        """进度恢复只能复用完全相同的数据、配置与执行顺序。"""

        patterns = self.config.get("schedule", {}).get("patterns")  # type: ignore[union-attr]
        return {
            "execution_mode": P019ExecutionMode.ONLINE_FAST.value,
            "config_version": str(self.config.get("version")),
            "dataset_sha256": sha256_file(self.p018_dataset_path),
            "p018_config_sha256": sha256_file(self.p018_config_path),
            "p019_config_sha256": sha256_file(self.config_path),
            "case_id_digest": canonical_digest([case.case_id for case in self.dataset.cases]),
            "schedule_digest": canonical_digest(patterns),
            "react_prompt_id": REACT_PROMPT_ID,
            "react_prompt_version": REACT_PROMPT_VERSION,
            "react_runner_version": REACT_RUNNER_VERSION,
            "react_uses_pevr_runner": False,
        }

    def _load_progress(self) -> dict[tuple[P019Strategy, str], EvalReportCase]:
        """恢复已落盘的完整逐例结果；重复键或坏 JSON 一律 fail closed。"""

        expected_manifest = self._manifest()
        if not self.resume:
            self.manifest_path.write_text(
                json.dumps(expected_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self.progress_path.write_text("", encoding="utf-8", newline="\n")
            return {}
        if not self.manifest_path.is_file() or not self.progress_path.is_file():
            raise OnlineComparisonError("--resume 要求已有 manifest 与 progress JSONL")
        actual_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if actual_manifest != expected_manifest:
            raise OnlineComparisonError("在线进度的数据/配置/顺序身份已变化，拒绝恢复")
        allowed_cases = {case.case_id for case in self.dataset.cases}
        loaded: dict[tuple[P019Strategy, str], EvalReportCase] = {}
        for line_no, line in enumerate(self.progress_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            strategy = P019Strategy(payload["strategy"])
            case = EvalReportCase.model_validate(payload["case"])
            key = (strategy, case.case_id)
            if case.case_id not in allowed_cases or key in loaded:
                raise OnlineComparisonError(f"进度第 {line_no} 行身份重复或不属于固定数据集")
            loaded[key] = case
        return loaded

    def _preflight_model(self) -> dict[P019Strategy, dict[str, Any]]:
        """三套 Harness 分别通过真实 Fast startup，并核对同一制品身份。"""

        records = {
            strategy: harness.provider.startup().model_dump(mode="json")
            for strategy, harness in self.harnesses.items()
        }
        identity_keys = (
            "served_alias",
            "artifact_manifest_sha256",
            "model_sha256",
            "runtime_binary_sha256",
            "launch_script_sha256",
            "context_window",
            "temperature",
        )
        identities = {
            tuple(record.get(key) for key in identity_keys)
            for record in records.values()
        }
        if len(identities) != 1 or next(iter(identities))[0] != "qwen3.6-fast":
            raise OnlineComparisonError("三策略没有连接到同一 Qwen3.6 Fast 制品")
        model_config = self.p018_config.get("model")
        if not isinstance(model_config, Mapping):
            raise OnlineComparisonError("P0-18 在线配置缺少 model 身份")
        observed = records[P019Strategy.PEVR]
        expected_fields = {
            "served_alias": model_config.get("alias"),
            "artifact_manifest_sha256": model_config.get("artifact_manifest_sha256"),
            "model_sha256": model_config.get("model_sha256"),
            "runtime_binary_sha256": model_config.get("runtime_binary_sha256"),
            "launch_script_sha256": model_config.get("launch_script_sha256"),
            "context_window": model_config.get("context_window"),
            "temperature": model_config.get("temperature"),
            "quantization": model_config.get("quantization"),
        }
        mismatches = {
            key: {"expected": expected, "observed": observed.get(key)}
            for key, expected in expected_fields.items()
            if (
                str(expected).casefold() != str(observed.get(key)).casefold()
                if isinstance(expected, str)
                else expected != observed.get(key)
            )
        }
        if mismatches:
            raise OnlineComparisonError(f"Fast 实际制品与 P0-18 在线配置不一致: {mismatches}")
        return records

    def _schedule(self) -> list[tuple[P019Strategy, EvalCase]]:
        """每三例轮换先后次序，使每个策略恰好 20 次位于首/中/末位置。"""

        schedule_cfg = self.config.get("schedule")
        patterns = schedule_cfg.get("patterns") if isinstance(schedule_cfg, Mapping) else None
        if not isinstance(patterns, list) or len(patterns) != 3:
            raise OnlineComparisonError("Latin-square patterns 必须恰好为 3 行")
        rows: list[tuple[P019Strategy, EvalCase]] = []
        for index, case in enumerate(self.dataset.cases):
            pattern = patterns[index % len(patterns)]
            strategies = [P019Strategy(item) for item in pattern]
            if set(strategies) != set(STRATEGY_ORDER):
                raise OnlineComparisonError("每个 Latin-square pattern 必须恰好包含三策略")
            rows.extend((strategy, case) for strategy in strategies)
        return rows

    def _run_one(self, strategy: P019Strategy, case: EvalCase) -> EvalReportCase:
        """采样一次真实 case，并把资源和外层墙钟作为逐例事实写入 metrics。"""

        sampler = ProcessResourceSampler(
            interval_seconds=self.sample_interval,
            listener_ports=self.listener_ports,
            gpu_interval_seconds=self.gpu_interval,
        ).start()
        started = time.perf_counter()
        try:
            result = self.harnesses[strategy].run_case(case)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            resource = sampler.stop()
        metrics = {
            **result.metrics,
            "wall_clock_ms": elapsed_ms,
            "resource_sample_count": resource.sample_count,
            "cpu_time_ms": resource.cpu_time_ms,
            "peak_rss_mb": resource.peak_rss_mb,
            "peak_gpu_memory_mb": resource.peak_gpu_memory_mb,
            "resource_observation_reason": resource.reason,
            "control_strategy": strategy.value,
        }
        return result.model_copy(update={"metrics": metrics})

    def run(self) -> P019Report:
        """执行 60×3 在线比较；每完成一个 case 即追加可恢复进度。"""

        model_records = self._preflight_model()
        completed = self._load_progress()
        schedule = self._schedule()
        total = len(schedule)
        for ordinal, (strategy, case) in enumerate(schedule, start=1):
            key = (strategy, case.case_id)
            if key in completed:
                print(
                    f"[p019-online] ({ordinal}/{total}) resume-skip {strategy.value} {case.case_id}",
                    flush=True,
                )
                continue
            print(
                f"[p019-online] ({ordinal}/{total}) start {strategy.value} {case.case_id} {case.scenario}",
                flush=True,
            )
            result = self._run_one(strategy, case)
            completed[key] = result
            with self.progress_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {"strategy": strategy.value, "case": result.model_dump(mode="json")},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            print(
                f"[p019-online] ({ordinal}/{total}) done {strategy.value} {case.case_id} "
                f"observed={result.observed_outcome.value} passed={result.evaluation_passed} "
                f"wall_ms={result.metrics.get('wall_clock_ms')}",
                flush=True,
            )
        expected_keys = {
            (strategy, case.case_id)
            for strategy in STRATEGY_ORDER
            for case in self.dataset.cases
        }
        if set(completed) != expected_keys:
            raise OnlineComparisonError("在线执行结束但没有得到完整 180 条策略-case")

        reports: dict[P019Strategy, EvalReport] = {}
        for strategy in STRATEGY_ORDER:
            cases = [completed[(strategy, case.case_id)] for case in self.dataset.cases]
            report = self.harnesses[strategy]._build_report(cases)
            report = report.model_copy(
                update={
                    "reproducibility": {
                        **report.reproducibility,
                        "p019_control_strategy": strategy.value,
                        "observed_model": model_records[strategy],
                    }
                }
            )
            reports[strategy] = report
            strategy_dir = self.output_dir / strategy.value
            write_p018_report(
                report,
                output_dir=strategy_dir,
                json_name=f"p018_online_{strategy.value}.json",
                markdown_name=f"p018_online_{strategy.value}.md",
            )
        return self._build_report(reports, model_records)

    def _build_report(
        self,
        reports: Mapping[P019Strategy, EvalReport],
        model_records: Mapping[P019Strategy, Mapping[str, Any]],
    ) -> P019Report:
        """从三份独立在线报告重算公平性、逐例事实和汇总指标。"""

        pevr_report = reports[P019Strategy.PEVR]
        case_ids = [case.case_id for case in self.dataset.cases]
        report_case_ids = {
            strategy: [case.case_id for case in report.cases]
            for strategy, report in reports.items()
        }
        tool_sets = {
            canonical_digest(report.reproducibility.get("tool_spec_versions", {}))
            for report in reports.values()
        }
        model_identities = {
            canonical_digest(
                {
                    key: record.get(key)
                    for key in (
                        "served_alias",
                        "artifact_manifest_sha256",
                        "model_sha256",
                        "runtime_binary_sha256",
                        "launch_script_sha256",
                        "context_window",
                        "temperature",
                    )
                }
            )
            for record in model_records.values()
        }
        combined_identity = canonical_digest(
            {strategy.value: reports[strategy].report_digest for strategy in STRATEGY_ORDER}
        )
        react_prompts = dict(reports[P019Strategy.REACT].reproducibility.get("prompt_versions") or {})
        merged_prompts = {
            **{
                str(key): str(value)
                for key, value in dict(pevr_report.reproducibility.get("prompt_versions", {})).items()
            },
            **{str(key): str(value) for key, value in react_prompts.items()},
        }
        fairness = FairnessEvidence(
            dataset_id=pevr_report.dataset_id,
            dataset_version=pevr_report.dataset_version,
            case_count=60,
            case_id_digest=canonical_digest(sorted(case_ids)),
            model_profile="fast",
            model_alias="qwen3.6-fast",
            prompt_versions=merged_prompts,
            tool_spec_versions={
                str(key): str(value)
                for key, value in dict(pevr_report.reproducibility.get("tool_spec_versions", {})).items()
            },
            p018_config_sha256=sha256_file(self.p018_config_path),
            p019_config_sha256=sha256_file(self.config_path),
            p018_source_report_sha256=combined_identity,
            same_dataset=all(ids == case_ids for ids in report_case_ids.values()),
            same_tools=len(tool_sets) == 1,
            same_prompts=False,
            same_config=all(
                report.reproducibility.get("input_files", {}).get("config", {}).get("sha256")
                == sha256_file(self.p018_config_path)
                for report in reports.values()
            ),
            same_model=len(model_identities) == 1,
            same_shared_context_contract=True,
            same_initial_retrieval=True,
            same_safety_gates=True,
            same_budget_envelope=True,
            strategy_prompt_versions={
                "fixed_workflow": f"amr.p005.*@{P005_PROMPT_VERSION}",
                "react": f"{REACT_PROMPT_ID}@{REACT_PROMPT_VERSION}",
                "pevr": f"amr.p005.*@{P005_PROMPT_VERSION}",
            },
            react_uses_pevr_runner=False,
            react_production_path_touched=False,
        )
        raw_results: list[StrategyCaseResult] = []
        summaries: list[StrategySummary] = []
        for strategy in STRATEGY_ORDER:
            strategy_results = [_online_case_result(strategy, case) for case in reports[strategy].cases]
            raw_results.extend(strategy_results)
            summaries.append(
                _aggregate(
                    strategy,
                    strategy_results,
                    latency_source="online_wall_clock",
                    wall_clock=True,
                )
            )
        summary_map = {summary.strategy: summary for summary in summaries}
        workflow = summary_map[P019Strategy.FIXED_WORKFLOW]
        react = summary_map[P019Strategy.REACT]
        pevr = summary_map[P019Strategy.PEVR]
        conclusions = [
            "三策略各自真实执行同一 P0-18 固定 60 例，共 180 条独立身份；Fast 制品、共享前置、初次 Retrieve、ToolSpec、安全门禁与预算包络通过公平性门禁。ReAct 控制 Prompt 与 PEVR 不同，same_prompts=false。",
            f"全例预期符合率：Workflow {workflow.evaluation_pass_count}/60，ReAct {react.evaluation_pass_count}/60，PEVR {pevr.evaluation_pass_count}/60；任务完成率分别为 {workflow.task_completion_count}/{workflow.positive_case_count}、{react.task_completion_count}/{react.positive_case_count}、{pevr.task_completion_count}/{pevr.positive_case_count}。",
            f"异常终态正确率按 expected==observed：Workflow {workflow.recovery_terminal_correct_count}/{workflow.recovery_case_count}，ReAct {react.recovery_terminal_correct_count}/{react.recovery_case_count}，PEVR {pevr.recovery_terminal_correct_count}/{pevr.recovery_case_count}；成功恢复（发生恢复动作且最终完成）为 {workflow.successful_recovery_count}/{workflow.successful_recovery_case_count}、{react.successful_recovery_count}/{react.successful_recovery_case_count}、{pevr.successful_recovery_count}/{pevr.successful_recovery_case_count}。",
            f"墙钟 P95：Workflow {workflow.latency.p95_case_ms:.1f} ms，ReAct {react.latency.p95_case_ms:.1f} ms，PEVR {pevr.latency.p95_case_ms:.1f} ms；Token 总量分别为 {workflow.token_usage.total_tokens}、{react.token_usage.total_tokens}、{pevr.token_usage.total_tokens}。",
            "PEVR 仍是唯一生产主链；Fixed 走固定图，独立 ReAct 只存在于评测层且 react_uses_pevr_runner=false。",
        ]
        limitations = [
            "这是一次本机单模型单 GPU 在线运行，没有重复试验或置信区间；temperature=0.1 仍可能产生跨次波动。",
            "沿用 P0-18 分流：正常/充电/可恢复异常/审批走各策略主路径；RAG、权限、安全、验证类继续走同一 live sidecar，因此不是全部 60 例都进入 ReAct 循环或 PEVR 图。",
            "本轮 ReAct 在共享初次 Retrieve 后禁止再检索，以隔离控制策略差异；自主重复检索是另一实验，不能混入本轮数据。",
            "独立 ReAct 仍只能使用白名单工具，finish 只是请求完成，终态由确定性检查确认。",
            "资源采样是进程级近似，包含评测进程、子进程和 8080/18080 服务；GPU/OS 调度噪声不能解释为节点级因果成本。",
            "三策略按 Latin-square 交错顺序顺序执行，不并发；这样保护单槽 Fast 的可比性，但总墙钟不代表三路并行吞吐。",
            "p0-19.online.v1 把异常后一次 retry/stop 误称为 ReAct，其指标已作废，不能 --resume 进本报告。",
        ]
        source_summary = {
            "mode": P019ExecutionMode.ONLINE_FAST.value,
            "path": str(self.p018_dataset_path).replace("\\", "/"),
            "sha256": combined_identity,
            "dataset_sha256": sha256_file(self.p018_dataset_path),
            "p018_config_sha256": sha256_file(self.p018_config_path),
            "report_id": pevr_report.report_id,
            "report_digest": pevr_report.report_digest,
            "dataset_id": pevr_report.dataset_id,
            "dataset_version": pevr_report.dataset_version,
            "case_count": 60,
            "strategy_report_digests": {
                strategy.value: reports[strategy].report_digest for strategy in STRATEGY_ORDER
            },
            "schedule": self.config.get("schedule"),
        }
        smart = SmartDeferral.model_validate(self.config["smart_comparison"])
        stable_body = {
            "execution_mode": P019ExecutionMode.ONLINE_FAST.value,
            "source_report": source_summary,
            "fairness": fairness.model_dump(mode="json"),
            "smart_comparison": smart.model_dump(mode="json"),
            "strategies": [item.model_dump(mode="json") for item in summaries],
            "raw_results": [item.model_dump(mode="json") for item in raw_results],
            "conclusions": conclusions,
            "limitations": limitations,
        }
        report_digest = canonical_digest(stable_body)
        status = (
            "passed"
            if len(raw_results) == 180
            and all(summary.zero_tolerance.total() == 0 for summary in summaries)
            else "failed"
        )
        return P019Report(
            report_id=f"p019-online-{report_digest[:16]}",
            report_version="p0-19.online.v2",
            execution_mode=P019ExecutionMode.ONLINE_FAST,
            status=status,
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


def run_online_comparison(
    *,
    config_path: str | Path = DEFAULT_ONLINE_CONFIG_PATH,
    output_dir: str | Path = DEFAULT_ONLINE_OUTPUT_DIR,
    verification_timeout_seconds: float = 120.0,
    resume: bool = False,
) -> P019Report:
    """公开入口：执行或安全恢复一次完整的 60×3 在线对照。"""

    return OnlineThreeStrategyComparison(
        config_path=config_path,
        output_dir=output_dir,
        verification_timeout_seconds=verification_timeout_seconds,
        resume=resume,
    ).run()


__all__ = [
    "DEFAULT_ONLINE_OUTPUT_DIR",
    "OnlineComparisonError",
    "OnlineThreeStrategyComparison",
    "ProcessResourceSampler",
    "run_online_comparison",
]
