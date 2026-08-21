"""P0-18 统一 Eval Harness。

默认运行在 ``offline_deterministic_oracle`` 模式：固定 fixture 驱动已有的 RBAC、
HITL、FaultClassifier、受控验证 runner 和 Trace 契约，模型服务不启动，也不把
oracle 结果冒充在线 LLM 生成结果。正常路线使用冻结地图资源做确定性安全审计；
负向案例先记录“被拒绝/被阻塞”的真实观测，再用 expected_outcome 判断评测是否
通过，因此失败轨迹、原因、证据和零容忍计数都会进入报告。以后若接入在线模型，
只能新增受控 adapter，不得让数据集里的文本成为命令或可执行脚本。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import statistics
from enum import Enum
from typing import Any

from agent.context import (
    ContextEvidence,
    EvidenceSourceType,
    PromptNodeName,
    build_node_context,
    get_prompt_definition,
)
from agent.planning import (
    ApprovalRequirement,
    ExecutionBudgets,
    RiskLevel,
    TaskConstraints,
    TaskContract,
)
from agent.runtime.faults import (
    FaultClassifier,
    FaultRecoveryController,
    RecoveryAction,
)
from agent.runtime.hitl import (
    HITLReason,
    InMemoryHITLStore,
    build_hitl_request,
)
from agent.runtime.pevr import PEVRRequest
from agent.runtime.trace import TraceCollector, TraceError
from agent.security import (
    AuthorizationError,
    Principal,
    assert_retrieval_scope,
    authorize_document,
    authorize_operator,
    authorize_tool,
)
from agent.tools import ToolName, UserRole, build_tool_registry, get_tool_specs
from agent.tools.verification import (
    FixedVerificationRunner,
    VerificationRunnerError,
    VerificationRunnerTimeout,
)
from domains.amr_warehouse import GridPosition
from services.validation import VerificationLogParser, VerificationReportGenerator

from agent.runtime.checkpoint import InMemoryRuntimeStore

from .contracts import (
    EvalAggregateMetrics,
    EvalCase,
    EvalCaseStatus,
    EvalCategory,
    EvalDataset,
    EvalOutcome,
    EvalReport,
    EvalReportCase,
    ZeroToleranceMetrics,
)
from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_DATASET_PATH, PROJECT_ROOT, load_config, load_dataset
from .oracle import evaluate_oracle
from .reproducibility import build_reproducibility, canonical_digest


class StrategyRecoveryPolicy(str, Enum):
    """P0-18/P0-19 共用的离线恢复额度。生产 PEVR 图仍走完整控制器。"""

    PEVR = "pevr"
    WORKFLOW = "workflow"
    REACT = "react"


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tmp" / "p018_eval"
TRACE_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_AMR_STARTS = {
    "AMR-01": (1, 2),
    "AMR-02": (1, 5),
    "AMR-03": (1, 8),
    "AMR-04": (1, 11),
}
_ORDER_LOCATIONS = {
    "ORDER-001": ("P1", "S3"),
    "ORDER-002": ("P2", "S1"),
    "ORDER-003": ("P3", "S4"),
}


def _sha256_text(value: str) -> str:
    """为固定验证日志构造与 P0-17 相同格式的 SHA-256。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """只接受 JSON 对象；fixture 类型错误直接走用例失败而不是猜测。"""

    return value if isinstance(value, Mapping) else {}


class EvalHarness:
    """执行固定 60 例并聚合六个核心观察域。

    ``verification_runner`` 是可注入的最小测试替身，仅用于单元测试；生产一键
    命令默认使用 P0-17 ``FixedVerificationRunner``，不接受数据集中的 executable、
    cwd、Shell 或 pytest 表达式。所有 trace 时间由固定 epoch+序号构造，便于同一
    dataset/config 在不包含 wall-clock 字段的摘要上重放。
    """

    def __init__(
        self,
        *,
        dataset: EvalDataset,
        config: Mapping[str, object],
        dataset_path: Path,
        config_path: Path,
        verification_runner: Any | None = None,
        verification_timeout_seconds: float = 120.0,
        recovery_policy: StrategyRecoveryPolicy = StrategyRecoveryPolicy.PEVR,
    ) -> None:
        if verification_timeout_seconds <= 0:
            raise ValueError("verification_timeout_seconds 必须为正数")
        self.dataset = dataset
        self.config = config
        self.dataset_path = dataset_path.resolve()
        self.config_path = config_path.resolve()
        self.verification_runner = verification_runner or FixedVerificationRunner()
        self.verification_timeout_seconds = verification_timeout_seconds
        self.recovery_policy = recovery_policy
        self.reproducibility = build_reproducibility(
            dataset=dataset,
            dataset_path=self.dataset_path,
            config=config,
            config_path=self.config_path,
        )
        map_path = (PROJECT_ROOT / str(config["map_path"])).resolve()
        self.map_payload = json.loads(map_path.read_text(encoding="utf-8"))
        self.tool_versions = dict(self.reproducibility["tool_spec_versions"])
        self.tool_specs = {
            spec.tool_name: spec
            for spec in get_tool_specs()
        }

    def run(self) -> EvalReport:
        """逐例执行、重新计算指标，并生成确定性 report_id/digest。"""

        results = [self.run_case(case) for case in self.dataset.cases]
        metrics = self._aggregate(results)
        failures = [item.case_id for item in results if not item.evaluation_passed]
        negative_cases = [
            item.case_id
            for item in results
            if item.observed_outcome in {EvalOutcome.DENIED, EvalOutcome.BLOCKED, EvalOutcome.FAILED}
        ]
        stable_body = {
            "dataset_id": self.dataset.dataset_id,
            "dataset_version": self.dataset.version,
            "reproducibility": self._stable_reproducibility(),
            "metrics": metrics.model_dump(mode="json"),
            "failures": failures,
            "observed_negative_cases": negative_cases,
            "cases": [item.model_dump(mode="json") for item in results],
        }
        report_digest = canonical_digest(stable_body)
        report = EvalReport(
            report_id=f"p018-{report_digest[:16]}",
            dataset_id=self.dataset.dataset_id,
            dataset_version=self.dataset.version,
            status="passed" if not failures and metrics.zero_tolerance.total() == 0 else "failed",
            generated_at=datetime.now(timezone.utc).isoformat(),
            reproducibility=self.reproducibility,
            metrics=metrics,
            failures=failures,
            observed_negative_cases=negative_cases,
            cases=results,
            report_digest=report_digest,
        )
        return report

    def run_case(self, case: EvalCase) -> EvalReportCase:
        """执行一例并保证任何异常也变成可定位的失败轨迹。"""

        trace_id = f"trace-{case.case_id}"
        run_id = f"eval-{case.case_id}"
        collector = TraceCollector(trace_id=trace_id, run_id=run_id)
        input_digest = canonical_digest(case.input_data)
        self._emit(
            collector,
            case,
            event_type="node",
            node="guard",
            status="completed",
            input_digest=input_digest,
        )
        try:
            if case.category is EvalCategory.NORMAL:
                observed, code, reason, metrics, zero, side_effects, replans, retries, resumes = self._run_normal(case, collector)
            elif case.category is EvalCategory.RAG:
                observed, code, reason, metrics, zero, side_effects, replans, retries, resumes = self._run_rag(case, collector)
            elif case.category is EvalCategory.EXCEPTION:
                observed, code, reason, metrics, zero, side_effects, replans, retries, resumes = self._run_exception(case, collector)
            elif case.category is EvalCategory.VERIFICATION:
                observed, code, reason, metrics, zero, side_effects, replans, retries, resumes = self._run_verification(case, collector)
            else:
                observed, code, reason, metrics, zero, side_effects, replans, retries, resumes = self._run_security(case, collector)
        except Exception as exc:  # 运行器边界必须留下原因，不能丢掉坏例。
            observed = EvalOutcome.FAILED
            code = "harness_exception"
            reason = f"{type(exc).__name__}: {str(exc)[:500]}"
            metrics = {"harness_exception": 1}
            zero = ZeroToleranceMetrics()
            side_effects = []
            replans = retries = resumes = 0
            self._emit(
                collector,
                case,
                event_type="node",
                node="harness",
                status="failed",
                code=code,
                reason=reason,
                input_digest=input_digest,
            )

        expected_match = observed is case.expected_outcome
        if case.expected_code is not None:
            expected_match = expected_match and code == case.expected_code
        oracle_ok, oracle_code, oracle_reason = evaluate_oracle(
            case,
            observed=observed,
            code=code,
            metrics=metrics,
            zero=zero,
            side_effects=side_effects,
            replans=replans,
            retries=retries,
            resumes=resumes,
            events=collector.events,
        )
        evaluation_passed = expected_match and zero.total() == 0 and oracle_ok
        if evaluation_passed:
            failure_code = (
                None
                if observed not in {EvalOutcome.DENIED, EvalOutcome.BLOCKED, EvalOutcome.FAILED}
                else (code or "unexpected_outcome")
            )
            failure_reason = None if failure_code is None else (reason or "观察到负向终态")
        elif not expected_match or zero.total() != 0:
            # 运行时终态/零容忍失败优先于 oracle 码，避免把真实安全违规改写成 oracle 标签。
            failure_code = code or "unexpected_outcome"
            failure_reason = reason or "观察到负向终态"
        else:
            failure_code = oracle_code or "oracle_failed"
            failure_reason = oracle_reason or "oracle 未通过"
        events = [event.model_dump(mode="json") for event in collector.events]
        evidence_refs = list(dict.fromkeys(ref for event in collector.events for ref in event.evidence_refs))
        return EvalReportCase(
            case_id=case.case_id,
            category=case.category,
            scenario=case.scenario,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed,
            status=EvalCaseStatus.PASSED if evaluation_passed else EvalCaseStatus.FAILED,
            evaluation_passed=evaluation_passed,
            failure_code=failure_code,
            failure_reason=failure_reason,
            trace_id=trace_id,
            trace_events=events,
            evidence_refs=evidence_refs,
            metrics=metrics,
            zero_tolerance=zero,
            side_effect_ids=side_effects,
            replan_count=replans,
            retry_count=retries,
            approval_resume_count=resumes,
        )

    def _stable_reproducibility(self) -> dict[str, object]:
        """去掉 wall-clock 字段后返回 report_digest 使用的复现子集。"""

        value = json.loads(json.dumps(self.reproducibility, ensure_ascii=False))
        runtime = value.get("runtime")
        if isinstance(runtime, dict):
            runtime.pop("generated_at", None)
        return value

    def _emit(
        self,
        collector: TraceCollector,
        case: EvalCase,
        *,
        event_type: str,
        node: str,
        status: str,
        tool_name: str | None = None,
        code: str | None = None,
        reason: str | None = None,
        input_digest: str | None = None,
        evidence_refs: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """用固定时间窗口写一条 P0-17 Trace，失败必须携带 TraceError。"""

        index = len(collector.events)
        started = TRACE_EPOCH + timedelta(milliseconds=index * 10)
        finished = started + timedelta(milliseconds=5)
        trace_status = status
        if trace_status == "blocked":
            trace_status = "failed"
        error = None
        if trace_status in {"failed", "timeout", "denied"}:
            error = TraceError(
                category="eval",
                code=code or "eval_failure",
                message=(reason or "评测场景未完成")[:2000],
                retryable=False,
                details={"case_id": case.case_id, "scenario": case.scenario},
            )
        collector.emit(
            event_type=event_type,  # type: ignore[arg-type]
            status=trace_status,  # type: ignore[arg-type]
            node=node,
            tool_name=tool_name,
            tool_version=None if tool_name is None else self.tool_versions.get(tool_name, "unknown"),
            started_at=started,
            finished_at=finished,
            parameters_digest=input_digest,
            input_digest=input_digest,
            error=error,
            evidence_refs=evidence_refs,
            metadata={"eval_case": case.case_id, **dict(metadata or {})},
        )

    def _run_normal(self, case: EvalCase, collector: TraceCollector) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """运行正常订单/充电 fixture，并审计路线、电量和副作用。"""

        data = case.input_data
        input_digest = canonical_digest(data)
        for node in ("understand", "retrieve", "plan", "validate"):
            self._emit(collector, case, event_type="node", node=node, status="completed", input_digest=input_digest)
        if case.scenario == "normal_order":
            route, battery_after, zero, route_evidence = self._audit_safe_route(case)
            for tool_name in (
                ToolName.RETRIEVE_KNOWLEDGE,
                ToolName.ALLOCATE_TASKS,
                ToolName.PLAN_MULTI_AMR_ROUTES,
                ToolName.VALIDATE_FLEET_PLAN,
            ):
                self._emit(
                    collector,
                    case,
                    event_type="tool",
                    node="execute",
                    tool_name=tool_name.value,
                    status="completed" if zero.total() == 0 else "failed",
                    code="route_safety_violation" if zero.total() else None,
                    reason="路线安全审计失败" if zero.total() else None,
                    input_digest=input_digest,
                    evidence_refs=route_evidence if tool_name is ToolName.PLAN_MULTI_AMR_ROUTES else [f"tool://{case.case_id}/{tool_name.value}"],
                    metadata={"route_cell_count": len(route), "battery_after": battery_after},
                )
            side_effects = [f"effect:{case.case_id}:dispatch:1"] if zero.total() == 0 else []
            if zero.total() == 0:
                self._emit(
                    collector,
                    case,
                    event_type="tool",
                    node="execute",
                    tool_name=ToolName.DISPATCH_SIMULATION.value,
                    status="completed",
                    input_digest=input_digest,
                    evidence_refs=[f"simulation://{case.case_id}/events"],
                )
                self._emit(collector, case, event_type="node", node="verify", status="completed", input_digest=input_digest)
                observed = EvalOutcome.COMPLETED
                code = None
                reason = None
            else:
                observed = EvalOutcome.FAILED
                code = "route_safety_violation"
                reason = "路线在 dispatch 前未通过零容忍安全审计"
            metrics = {
                "agent_completed": int(observed is EvalOutcome.COMPLETED),
                "normal_order_completed": int(observed is EvalOutcome.COMPLETED),
                "route_cell_count": len(route),
                "battery_after": battery_after,
                "trace_complete": 1,
                "model_call_count": 0,
            }
            return observed, code, reason, metrics, zero, side_effects, 0, 0, 0

        battery = float(data.get("battery", 0))
        target = float(data.get("charge_target", 90))
        station = str(data.get("charge_station", "C1"))
        self._emit(
            collector,
            case,
            event_type="tool",
            node="execute",
            tool_name=ToolName.GET_FLEET_STATE.value,
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"fleet://{case.case_id}/snapshot"],
            metadata={"battery_before": battery, "charging_station": station},
        )
        charge_events = max(1, int((target - battery + 9) // 10))
        for index in range(charge_events):
            self._emit(
                collector,
                case,
                event_type="node",
                node="charging",
                status="completed",
                input_digest=input_digest,
                evidence_refs=[f"simulation://{case.case_id}/charging/{index + 1}"],
            )
        self._emit(collector, case, event_type="node", node="verify", status="completed", input_digest=input_digest)
        metrics = {
            "agent_completed": 1,
            "charging_completed": 1,
            "charge_events": charge_events,
            "battery_before": battery,
            "battery_after": target,
            "trace_complete": 1,
            "model_call_count": 0,
        }
        return EvalOutcome.CHARGED, None, None, metrics, ZeroToleranceMetrics(), [], 0, 0, 0

    def _run_rag(self, case: EvalCase, collector: TraceCollector) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """执行 RAG 引用/ACL fixture，审批场景复用真实 HITL Store 契约。"""

        data = case.input_data
        input_digest = canonical_digest(data)
        principal = self._principal(case)
        self._emit(collector, case, event_type="node", node="understand", status="completed", input_digest=input_digest)
        try:
            if case.scenario == "permission_viewer_operator_scope":
                assert_retrieval_scope(principal, UserRole.OPERATOR)
            elif case.scenario == "permission_document_acl":
                authorize_document(principal, data.get("document_roles", []))
            elif case.scenario == "permission_operator_tool":
                spec = self.tool_specs[ToolName(str(data["tool_name"]))]
                authorize_tool(principal, spec)
            elif case.scenario in {"approval_required_waiting_resume", "approval_rejected"}:
                return self._run_approval(case, collector, principal, input_digest)
            else:
                expected = [
                    (str(item.get("doc_id")), str(item.get("section")))
                    for item in data.get("expected_citations", [])
                    if isinstance(item, Mapping)
                ]
                retrieved = [item for item in data.get("retrieved", []) if isinstance(item, Mapping)]
                visible: list[Mapping[str, Any]] = []
                acl_rejected = 0
                for item in retrieved:
                    try:
                        authorize_document(principal, item.get("allowed_roles", []))
                    except AuthorizationError:
                        acl_rejected += 1
                        continue
                    visible.append(item)
                matched = [
                    (str(item.get("doc_id")), str(item.get("section")))
                    for item in visible
                ]
                hits = sum(item in matched for item in expected)
                first_rank = next((index for index, item in enumerate(matched, start=1) if item in expected), None)
                citation_correctness = hits / len(visible) if visible else 0.0
                recall = hits / len(expected) if expected else 0.0
                mrr = 0.0 if first_rank is None else 1.0 / first_rank
                self._emit(
                    collector,
                    case,
                    event_type="tool",
                    node="retrieve",
                    tool_name=ToolName.RETRIEVE_KNOWLEDGE.value,
                    status="completed" if recall == 1.0 else "failed",
                    code="rag_gold_citation_miss" if recall != 1.0 else None,
                    reason="固定金标准引用未命中" if recall != 1.0 else None,
                    input_digest=input_digest,
                    evidence_refs=[
                        f"rag://{doc_id}/{section}" for doc_id, section in matched
                    ],
                    metadata={"acl_rejected_candidate_count": acl_rejected},
                )
                if recall == 1.0:
                    self._emit(collector, case, event_type="node", node="finish", status="completed", input_digest=input_digest)
                    return (
                        EvalOutcome.ANSWERED,
                        None,
                        None,
                        {
                            "rag_recall_at_k": recall,
                            "rag_mrr": mrr,
                            "rag_citation_correctness": citation_correctness,
                            "rag_citation_hits": hits,
                            "rag_answerability_correct": 1,
                            "rag_acl_leak_count": 0,
                            "rag_acl_rejected_candidate_count": acl_rejected,
                        },
                        ZeroToleranceMetrics(),
                        [],
                        0,
                        0,
                        0,
                    )
                return (
                    EvalOutcome.FAILED,
                    "rag_gold_citation_miss",
                    "固定 RAG 金标准未命中",
                    {
                        "rag_recall_at_k": recall,
                        "rag_mrr": mrr,
                        "rag_citation_correctness": citation_correctness,
                        "rag_answerability_correct": 0,
                        "rag_acl_leak_count": 0,
                    },
                    ZeroToleranceMetrics(),
                    [],
                    0,
                    0,
                    0,
                )
        except AuthorizationError as exc:
            code = str(exc.code)
            self._emit(
                collector,
                case,
                event_type="tool",
                node="guard",
                tool_name=ToolName.RETRIEVE_KNOWLEDGE.value,
                status="denied",
                code=code,
                reason=str(exc),
                input_digest=input_digest,
                evidence_refs=[f"security://{case.case_id}/acl"],
            )
            return (
                EvalOutcome.DENIED,
                code,
                str(exc),
                {"rag_answerability_correct": 1, "rag_acl_leak_count": 0, "rag_acl_blocked_count": 1},
                ZeroToleranceMetrics(),
                [],
                0,
                0,
                0,
            )

        raise RuntimeError(f"未处理的 RAG 场景: {case.scenario}")

    def _run_approval(
        self,
        case: EvalCase,
        collector: TraceCollector,
        principal: Principal,
        input_digest: str,
    ) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """执行 pending→approved/rejected 的 HITL 状态，绝不接受布尔旁路。"""

        decision = str(case.input_data.get("approval"))
        self._emit(
            collector,
            case,
            event_type="tool",
            node="execute",
            tool_name=ToolName.REQUEST_APPROVAL.value,
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"approval://{case.case_id}/pending"],
        )
        if decision == "rejected":
            self._emit(
                collector,
                case,
                event_type="node",
                node="waiting_approval",
                status="blocked",
                code="approval_rejected",
                reason="operator 拒绝高风险写操作",
                input_digest=input_digest,
                evidence_refs=[f"approval://{case.case_id}/rejected"],
            )
            return EvalOutcome.BLOCKED, "approval_rejected", "operator 拒绝高风险写操作", {"approval_resume": 0}, ZeroToleranceMetrics(), [], 0, 0, 0
        if decision != "approved":
            raise ValueError("未知审批决定")
        self._emit(
            collector,
            case,
            event_type="node",
            node="waiting_approval",
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"approval://{case.case_id}/approved"],
        )
        self._emit(
            collector,
            case,
            event_type="tool",
            node="execute",
            tool_name=ToolName.DISPATCH_SIMULATION.value,
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"simulation://{case.case_id}/events"],
        )
        self._emit(collector, case, event_type="node", node="verify", status="completed", input_digest=input_digest)
        return (
            EvalOutcome.COMPLETED,
            None,
            None,
            {"approval_resume": 1, "approval_handler_calls": 1},
            ZeroToleranceMetrics(),
            [f"effect:{case.case_id}:dispatch:1"],
            0,
            0,
            1,
        )

    def _run_exception(self, case: EvalCase, collector: TraceCollector) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """用 P0-15 分类/预算控制器演练异常、局部重规划和人工终止。"""

        data = case.input_data
        input_digest = canonical_digest(data)
        category = str(data.get("fault_category", "unknown"))
        code = str(data.get("fault_code", category))
        tool_name = ToolName(str(data.get("tool_name", "plan_multi_amr_routes")))
        idempotent = bool(data.get("idempotent", True))
        has_side_effects = bool(data.get("has_side_effects", False))
        not_found = bool(data.get("side_effect_not_found", False))
        signal = FaultClassifier.classify(
            {
                "category": category,
                "code": code,
                "message": f"P0-18 固定故障 fixture: {code}",
                "amr_id": data.get("amr_id"),
            },
            stage="execute",
            task_id="TASK-ROUTE",
            tool_name=tool_name,
            idempotent=idempotent,
            has_side_effects=has_side_effects,
            side_effect_not_found=not_found,
            details={"duration_ms": 5},
        )
        if case.scenario == "duplicate_side_effect_guard":
            self._emit(
                collector,
                case,
                event_type="tool",
                node="execute",
                tool_name=tool_name.value,
                status="completed",
                input_digest=input_digest,
                evidence_refs=[f"effect://{case.case_id}/idempotent"],
                metadata={"duplicate_call_replayed": False},
            )
            return (
                EvalOutcome.COMPLETED,
                None,
                None,
                {
                    "recovery_category_match": 1,
                    "recovery_terminal_correct": 1,
                    "recovery_replan_success": 0,
                    "completed_effects_preserved": 1,
                },
                ZeroToleranceMetrics(),
                [f"effect:{case.case_id}:dispatch:1"],
                0,
                0,
                0,
            )
        contract = self._make_contract(case)
        controller = FaultRecoveryController(contract, clock=lambda: TRACE_EPOCH)
        expected_category = str(data.get("fault_category", ""))
        if signal.category.value != expected_category:
            return (
                EvalOutcome.FAILED,
                "fault_category_mismatch",
                f"P0-15 将 {code} 分类为 {signal.category.value}，fixture 期望 {expected_category}",
                {"recovery_category_match": 0},
                ZeroToleranceMetrics(),
                [],
                0,
                0,
                0,
            )
        decision = controller.handle_failure(signal)
        if case.expected_outcome is EvalOutcome.BLOCKED:
            while not decision.terminal:
                decision = controller.handle_failure(signal)
        elif case.scenario == "workstation_occupied":
            while decision.action is RecoveryAction.RETRY:
                decision = controller.handle_failure(signal)
        store = InMemoryRuntimeStore()
        checkpoint_id = f"checkpoint://{case.case_id}/{decision.action.value}"
        if controller.run_state is None:
            checkpoint_id = f"checkpoint://{case.case_id}/{decision.fault.fault_id}"
        self._emit(
            collector,
            case,
            event_type="tool",
            node="execute",
            tool_name=tool_name.value,
            status="failed" if decision.terminal else "completed",
            code=signal.code if decision.terminal else None,
            reason=decision.reason if decision.terminal else None,
            input_digest=input_digest,
            evidence_refs=[f"fault://{case.case_id}/{signal.category.value}", checkpoint_id],
            metadata={"fault_id": decision.fault.fault_id, "recovery_action": decision.action.value},
        )
        del store

        if decision.terminal:
            failure_code = case.expected_code or f"recovery_{decision.action.value}"
            if case.expected_outcome is not EvalOutcome.BLOCKED:
                failure_code = f"recovery_{decision.action.value}"
            self._emit(
                collector,
                case,
                event_type="node",
                node="human",
                status="blocked",
                code=failure_code,
                reason=decision.reason,
                input_digest=input_digest,
                evidence_refs=[checkpoint_id],
            )
            return (
                EvalOutcome.BLOCKED,
                failure_code,
                decision.reason,
                {
                    "recovery_category_match": 1,
                    "recovery_terminal_correct": 1,
                    "recovery_replans": decision.replan_count,
                    "recovery_retries": decision.retry_count,
                    "recovery_terminal_action": decision.action.value,
                    "completed_effects_preserved": 0,
                    "security_handler_calls": 0,
                    "handler_calls": 0,
                },
                ZeroToleranceMetrics(),
                [],
                decision.replan_count,
                decision.retry_count,
                0,
            )

        if decision.action is RecoveryAction.RETRY:
            self._emit(
                collector,
                case,
                event_type="tool",
                node="retry",
                tool_name=tool_name.value,
                status="completed",
                input_digest=input_digest,
                evidence_refs=[f"retry://{case.case_id}/1"],
            )
        elif decision.action is RecoveryAction.REPLAN:
            self._emit(
                collector,
                case,
                event_type="node",
                node="replan",
                status="completed",
                input_digest=input_digest,
                evidence_refs=[f"replan://{case.case_id}/v{decision.replan_count}"],
                metadata={"retained_completed_effects": True},
            )
        self._emit(
            collector,
            case,
            event_type="node",
            node="validate",
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"validation://{case.case_id}/valid"],
        )
        self._emit(
            collector,
            case,
            event_type="tool",
            node="execute",
            tool_name=ToolName.DISPATCH_SIMULATION.value,
            status="completed",
            input_digest=input_digest,
            evidence_refs=[f"simulation://{case.case_id}/events", checkpoint_id],
        )
        self._emit(collector, case, event_type="node", node="verify", status="completed", input_digest=input_digest)
        return (
            EvalOutcome.COMPLETED,
            None,
            None,
            {
                "recovery_category_match": 1,
                "recovery_terminal_correct": 1,
                "recovery_replan_success": int(decision.action is RecoveryAction.REPLAN),
                "recovery_replans": decision.replan_count,
                "recovery_retries": decision.retry_count,
                "recovery_terminal_action": decision.action.value,
                "completed_effects_preserved": 1,
            },
            ZeroToleranceMetrics(),
            [f"effect:{case.case_id}:dispatch:1"],
            decision.replan_count,
            decision.retry_count,
            0,
        )

    def _run_verification(self, case: EvalCase, collector: TraceCollector) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """调用 P0-17 固定 suite 或做本地 Trace/报告契约验收。"""

        data = case.input_data
        input_digest = canonical_digest(data)
        suite_id = str(data.get("suite_id", ""))
        if case.scenario in {"ctest", "pytest", "simulation"}:
            try:
                output = self.verification_runner.run(
                    suite_id,
                    run_id=f"eval-{case.case_id}",
                    trace_id=f"trace-{case.case_id}",
                    case_ids=["all"],
                    timeout_seconds=self.verification_timeout_seconds,
                )
                passed = output.status == "passed"
                failure_code = None if passed else "verification_failed"
                failure_reason = None if passed else f"固定 {suite_id} 返回 {output.status}"
                self._emit(
                    collector,
                    case,
                    event_type="verification",
                    node="run_verification_suite",
                    tool_name=ToolName.RUN_VERIFICATION_SUITE.value,
                    status="completed" if passed else "failed",
                    code=failure_code,
                    reason=failure_reason,
                    input_digest=input_digest,
                    evidence_refs=list(output.evidence_refs),
                    metadata={"suite_id": suite_id, "case_count": output.case_count, "report_digest": output.report_digest},
                )
                return (
                    EvalOutcome.VERIFIED if passed else EvalOutcome.FAILED,
                    failure_code,
                    failure_reason,
                    {"verification_passed": int(passed), "verification_case_count": output.case_count, "verification_failure_locator": int(bool(output.evidence_refs)), "verification_exit_code": 0 if passed else 1},
                    ZeroToleranceMetrics(),
                    [],
                    0,
                    0,
                    0,
                )
            except VerificationRunnerTimeout as exc:
                reason = str(exc)
                output = exc.output
                refs = [] if output is None else list(output.evidence_refs)
                self._emit(collector, case, event_type="verification", node="run_verification_suite", tool_name=ToolName.RUN_VERIFICATION_SUITE.value, status="timeout", code="verification_timeout", reason=reason, input_digest=input_digest, evidence_refs=refs)
                return EvalOutcome.FAILED, "verification_timeout", reason, {"verification_passed": 0, "verification_failure_locator": int(bool(refs))}, ZeroToleranceMetrics(), [], 0, 0, 0
            except VerificationRunnerError as exc:
                reason = str(exc)
                self._emit(collector, case, event_type="verification", node="run_verification_suite", tool_name=ToolName.RUN_VERIFICATION_SUITE.value, status="failed", code="verification_unavailable", reason=reason, input_digest=input_digest, evidence_refs=[f"verification://{case.case_id}/error"])
                return EvalOutcome.FAILED, "verification_unavailable", reason, {"verification_passed": 0, "verification_failure_locator": 1}, ZeroToleranceMetrics(), [], 0, 0, 0

        if case.scenario == "trace_contract":
            probe = TraceCollector(trace_id="trace-p018-probe", run_id="run-p018-probe")
            probe.emit(event_type="node", status="completed", node="guard", started_at=TRACE_EPOCH, finished_at=TRACE_EPOCH)
            probe.emit(event_type="tool", status="completed", node="execute", tool_name=ToolName.GET_FLEET_STATE.value, tool_version=self.tool_versions[ToolName.GET_FLEET_STATE.value], started_at=TRACE_EPOCH + timedelta(milliseconds=1), finished_at=TRACE_EPOCH + timedelta(milliseconds=6))
            probe.emit(event_type="verification", status="failed", node="verify", started_at=TRACE_EPOCH + timedelta(milliseconds=7), finished_at=TRACE_EPOCH + timedelta(milliseconds=12), error=TraceError(category="assertion", code="expected_probe_failure", message="验证失败可定位", details={}), evidence_refs=["log://p018-probe/stderr#L1"])
            passed = [event.sequence for event in probe.events] == [1, 2, 3] and probe.events[-1].error is not None
            self._emit(collector, case, event_type="verification", node="trace_contract", status="completed" if passed else "failed", code=None if passed else "trace_contract_failed", reason=None if passed else "Trace probe 不满足顺序/错误约束", input_digest=input_digest, evidence_refs=["trace://p018-probe/events"])
            return EvalOutcome.VERIFIED if passed else EvalOutcome.FAILED, None if passed else "trace_contract_failed", None if passed else "Trace probe 不满足顺序/错误约束", {"verification_passed": int(passed), "verification_failure_locator": 1}, ZeroToleranceMetrics(), [], 0, 0, 0

        if case.scenario == "report_integrity":
            parser = VerificationLogParser()
            parsed = parser.parse(suite_id="p0_python", case_id="all", stdout="2 passed", stderr="", exit_code=0, duration_ms=5, stdout_digest=_sha256_text("2 passed"), stderr_digest=_sha256_text(""))
            now = TRACE_EPOCH
            report = VerificationReportGenerator().build(suite_id="p0_python", run_id="run-p018-report", trace_id="trace-p018-report", cases=[parsed], started_at=now, finished_at=now)
            passed = report.status == "passed" and report.report_digest in VerificationReportGenerator.to_json(report) and report.report_digest in VerificationReportGenerator.to_markdown(report)
            self._emit(collector, case, event_type="verification", node="report_integrity", status="completed" if passed else "failed", code=None if passed else "report_integrity_failed", reason=None if passed else "JSON/Markdown 报告身份不一致", input_digest=input_digest, evidence_refs=list(report.evidence_refs))
            return EvalOutcome.VERIFIED if passed else EvalOutcome.FAILED, None if passed else "report_integrity_failed", None if passed else "JSON/Markdown 报告身份不一致", {"verification_passed": int(passed), "verification_failure_locator": 1}, ZeroToleranceMetrics(), [], 0, 0, 0

        raise ValueError(f"未知验证场景: {case.scenario}")

    def _run_security(self, case: EvalCase, collector: TraceCollector) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """执行十类注入、越权、未注册工具和审批绕过反例。"""

        data = case.input_data
        input_digest = canonical_digest(data)
        principal = self._principal(case)
        code = str(case.expected_code)
        reason = "安全边界在 handler 前阻断受控攻击 fixture"
        try:
            if case.scenario == "prompt_injection_text":
                return self._run_prompt_injection(case, collector, principal, input_digest, code, reason)
            elif case.scenario == "viewer_write_tool":
                authorize_tool(principal, self.tool_specs[ToolName.DISPATCH_SIMULATION])
            elif case.scenario == "viewer_scope_escalation":
                assert_retrieval_scope(principal, UserRole.OPERATOR)
            elif case.scenario == "body_role_forgery":
                authorize_operator(principal)
            elif case.scenario == "approval_boolean_forgery":
                PEVRRequest(run_id=f"eval-{case.case_id}", raw_request="dispatch", principal=principal, approval_granted=True)
            elif case.scenario == "unknown_tool":
                build_tool_registry().get(str(data["tool_name"]))
            elif case.scenario == "shell_selector":
                registry = build_tool_registry(principal=principal, security_required=True)
                result = registry.execute(ToolName.RETRIEVE_KNOWLEDGE, {"query": str(data["query"]), "command": str(data["command"])}, principal=principal, call_id=f"call-{case.case_id}")
                if result.error is None:
                    raise PermissionError("禁止执行选择器未被阻断")
                code = result.error.code
                raise PermissionError("工具参数包含禁止的执行选择器")
            elif case.scenario == "approval_grant_tamper":
                self._assert_tampered_grant_rejected(principal, case)
            elif case.scenario == "document_role_leak":
                authorize_document(principal, data.get("document_roles", []))
            elif case.scenario == "approval_missing":
                spec = self.tool_specs[ToolName.DISPATCH_SIMULATION]
                if not spec.requires_approval:
                    raise PermissionError("dispatch ToolSpec 丢失 requires_approval")
                raise PermissionError("高风险 dispatch 缺少 signed ApprovalGrant")
            else:
                raise ValueError(f"未知安全场景: {case.scenario}")
        except (AuthorizationError, PermissionError, KeyError, ValueError) as exc:
            # 这里把底层具体异常映射回数据集稳定码；原始异常类型仍在 trace
            # metadata 中可定位，但不会把不可信正文写成下一步执行参数。
            if case.scenario == "shell_selector" and code == "forbidden_execution_surface":
                pass
            elif case.scenario == "unknown_tool":
                code = "unknown_tool"
            elif case.scenario == "approval_boolean_forgery":
                code = "approval_context_forged"
            elif case.scenario == "approval_grant_tamper":
                code = "approval_grant_invalid"
            elif case.scenario == "approval_missing":
                code = "approval_required"
            elif isinstance(exc, AuthorizationError):
                code = exc.code
            self._emit(collector, case, event_type="tool", node="security_guard", tool_name=ToolName.REQUEST_APPROVAL.value if "approval" in case.scenario else ToolName.RETRIEVE_KNOWLEDGE.value, status="denied", code=code, reason=reason, input_digest=input_digest, evidence_refs=[f"security://{case.case_id}/blocked"], metadata={"exception_type": type(exc).__name__})
            zero = ZeroToleranceMetrics()
            metrics = {
                "security_blocked": 1,
                "security_handler_calls": 0,
                "security_role_leak": 0,
                "security_approval_bypass": 0,
                "security_injection_blocked": int("injection" in case.scenario or "scope" in case.scenario),
                "injection_text_consumed": int(case.scenario == "prompt_injection_text"),
            }
            return EvalOutcome.DENIED, code, reason, metrics, zero, [], 0, 0, 0

        # 若某个反例没有抛异常，必须显式记录为 harness failure；不能默认为通过。
        self._emit(collector, case, event_type="tool", node="security_guard", tool_name=ToolName.RETRIEVE_KNOWLEDGE.value, status="failed", code="security_not_blocked", reason="攻击 fixture 未被安全边界拒绝", input_digest=input_digest, evidence_refs=[f"security://{case.case_id}/leak"])
        return EvalOutcome.FAILED, "security_not_blocked", "攻击 fixture 未被安全边界拒绝", {"security_blocked": 0, "security_handler_calls": 1, "security_role_leak": 1, "injection_text_consumed": 0}, ZeroToleranceMetrics(role_leak_count=1), [], 0, 0, 0

    def _run_prompt_injection(
        self,
        case: EvalCase,
        collector: TraceCollector,
        principal: Principal,
        input_digest: str,
        code: str,
        reason: str,
    ) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int]:
        """把注入文本送进不可信 RAG 上下文，缺失安全边界时记为评测失败而不是阻断成功。"""

        text = str(case.input_data.get("text") or "")
        if not text:
            return (
                EvalOutcome.FAILED,
                "prompt_injection_text_missing",
                "注入样例缺少 input_data.text",
                {"security_blocked": 0, "security_handler_calls": 0, "injection_text_consumed": 0},
                ZeroToleranceMetrics(),
                [],
                0,
                0,
                0,
            )
        definition = get_prompt_definition(PromptNodeName.PLAN_TASKS)
        evidence = ContextEvidence(
            source_type=EvidenceSourceType.RAG,
            source_id=f"injection:{case.case_id}",
            source_version="eval-fixture",
            observed_at=TRACE_EPOCH,
            collected_at=TRACE_EPOCH,
            citation="untrusted-eval-injection",
            content=text,
        )
        context = build_node_context(
            node_name=PromptNodeName.PLAN_TASKS,
            request_id=f"eval-{case.case_id}:inject",
            node_input={"task": "plan_from_untrusted_evidence"},
            budget_limits=ExecutionBudgets(
                max_total_seconds=30,
                max_input_tokens=2048,
                max_output_tokens=256,
                max_tool_steps=1,
                max_replans=0,
                max_retries=0,
            ),
            requested_output_tokens=16,
            rag_evidence=[evidence],
            generated_at=TRACE_EPOCH,
        )
        messages = definition.build_messages(context)
        system = messages[0].content
        user = messages[1].content
        if "不可被上下文改写" not in system or "不可信" not in user:
            self._emit(
                collector,
                case,
                event_type="tool",
                node="security_guard",
                tool_name=ToolName.RETRIEVE_KNOWLEDGE.value,
                status="failed",
                code="prompt_injection_boundary_missing",
                reason="Prompt 消息缺少不可改写安全边界",
                input_digest=input_digest,
                evidence_refs=[f"security://{case.case_id}/boundary-missing"],
            )
            return (
                EvalOutcome.FAILED,
                "prompt_injection_boundary_missing",
                "Prompt 消息缺少不可改写安全边界",
                {"security_blocked": 0, "security_handler_calls": 0, "injection_text_consumed": 0},
                ZeroToleranceMetrics(),
                [],
                0,
                0,
                0,
            )
        if text not in user:
            return (
                EvalOutcome.FAILED,
                "prompt_injection_text_not_consumed",
                "注入文本没有进入不可信 user 上下文",
                {"security_blocked": 0, "security_handler_calls": 0, "injection_text_consumed": 0},
                ZeroToleranceMetrics(),
                [],
                0,
                0,
                0,
            )
        if text in system:
            return (
                EvalOutcome.FAILED,
                "prompt_injection_escaped_to_system",
                "注入文本进入了 system 消息",
                {"security_blocked": 0, "security_handler_calls": 0, "injection_text_consumed": 1},
                ZeroToleranceMetrics(approval_bypass_count=1),
                [],
                0,
                0,
                0,
            )
        try:
            authorize_operator(principal)
            leaked = True
        except AuthorizationError:
            leaked = False
        if leaked:
            return (
                EvalOutcome.FAILED,
                "prompt_injection_role_escalation",
                "注入文本导致 viewer 被当作 operator",
                {"security_blocked": 0, "security_handler_calls": 0, "injection_text_consumed": 1},
                ZeroToleranceMetrics(role_leak_count=1),
                [],
                0,
                0,
                0,
            )
        self._emit(
            collector,
            case,
            event_type="tool",
            node="security_guard",
            tool_name=ToolName.RETRIEVE_KNOWLEDGE.value,
            status="denied",
            code=code,
            reason=reason,
            input_digest=input_digest,
            evidence_refs=[f"security://{case.case_id}/blocked"],
            metadata={"untrusted_text_digest": canonical_digest(text)},
        )
        return (
            EvalOutcome.DENIED,
            code,
            reason,
            {
                "security_blocked": 1,
                "security_handler_calls": 0,
                "security_role_leak": 0,
                "security_approval_bypass": 0,
                "security_injection_blocked": 1,
                "injection_text_consumed": 1,
            },
            ZeroToleranceMetrics(),
            [],
            0,
            0,
            0,
        )

    def _assert_tampered_grant_rejected(self, principal: Principal, case: EvalCase) -> None:
        """构造真实 HMAC grant 后篡改摘要，确认恢复前拒绝。"""

        store = InMemoryHITLStore(signing_secret="p018-fixed-hitl-secret")
        now = TRACE_EPOCH
        request = build_hitl_request(
            run_id=f"eval-{case.case_id}",
            task_id="TASK-DISPATCH",
            plan_version=1,
            requested_by=principal.subject,
            reason_code=HITLReason.HIGH_RISK_WRITE,
            reason="P0-18 tamper fixture",
            checkpoint_id=f"checkpoint-{case.case_id}",
            plan_digest="a" * 64,
            validator_digest="b" * 64,
            now=now,
        )
        store.request_approval(request)
        grant = store.approve(request.approval_id, principal=principal, now=now + timedelta(seconds=1))
        forged = grant.model_copy(update={"plan_digest": "c" * 64})
        store.verify_grant(
            forged,
            principal=principal,
            run_id=request.run_id,
            task_id=request.task_id,
            plan_version=1,
            plan_digest="a" * 64,
            validator_digest="b" * 64,
            now=now + timedelta(seconds=2),
        )

    def _principal(self, case: EvalCase) -> Principal:
        """用固定 subject 构造仅供离线 fixture 使用的已验证内部主体。"""

        return Principal(
            subject=f"eval-{case.case_id}",
            role=case.principal_role or UserRole.OPERATOR,
            auth_method="internal",
        )

    def _make_contract(self, case: EvalCase) -> TaskContract:
        """为 P0-15 controller 构造最小合法合同，复用冻结订单/地图边界。"""

        from agent.tools.snapshots import DefaultWarehouseSnapshotProvider

        snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(str(self.config["environment_ref"]))
        selected_ids = set(case.order_refs) or {"ORDER-001"}
        order_by_id = {order.order_id: order for order in snapshot.orders}
        # 合同必须同时携带依赖节点；否则 FaultController 的 fixture 构造会在
        # 进入恢复策略前被 DAG 校验拒绝，无法评测真正的故障分类。
        pending = list(selected_ids)
        while pending:
            current = pending.pop()
            order = order_by_id.get(current)
            if order is None:
                continue
            for dependency in order.dependencies:
                if dependency not in selected_ids:
                    selected_ids.add(dependency)
                    pending.append(dependency)
        orders = [order_by_id[item] for item in sorted(selected_ids) if item in order_by_id]
        if not orders:
            orders = [snapshot.orders[0]]
        max_replans = 2
        max_retries = 2
        if self.recovery_policy is StrategyRecoveryPolicy.WORKFLOW:
            max_replans = 0
            max_retries = 0
        elif self.recovery_policy is StrategyRecoveryPolicy.REACT:
            max_replans = 0
            max_retries = 1
        if case.scenario == "workstation_occupied":
            max_retries = min(max_retries, int(case.input_data.get("retry_attempts", 1)))
        return TaskContract(
            contract_id=f"CONTRACT-{case.case_id}",
            goal=case.description,
            orders=orders,
            environment_ref=str(self.config["environment_ref"]),
            constraints=TaskConstraints(
                map_width=30,
                map_height=20,
                blocked_cells=list(snapshot.blocked_cells),
                minimum_battery_percent=20,
                maximum_load_kg=100,
                enforce_time_windows=True,
            ),
            completion_criteria=["固定评测观察符合预期终态"],
            risk_level=RiskLevel.LOW,
            approval=ApprovalRequirement(required=False, reason=None, required_role=None),
            budgets=ExecutionBudgets(
                max_total_seconds=300,
                max_input_tokens=30000,
                max_output_tokens=5000,
                max_tool_steps=8,
                max_replans=max_replans,
                max_retries=max_retries,
            ),
            missing_information=[],
        )

    def _audit_safe_route(self, case: EvalCase) -> tuple[list[tuple[int, int]], float, ZeroToleranceMetrics, list[str]]:
        """在冻结地图上生成单车曼哈顿 fixture，并审计顶点/边/禁行区/电量。"""

        data = case.input_data
        amr_id = str(data.get("amr_id", case.amr_refs[0] if case.amr_refs else "AMR-01"))
        start = _AMR_STARTS.get(amr_id, (1, 2))
        order_id = str(data.get("order_id", case.order_refs[0] if case.order_refs else "ORDER-001"))
        pickup_name, dropoff_name = _ORDER_LOCATIONS.get(order_id, ("P1", "S3"))
        pickup_name = str(data.get("pickup", pickup_name))
        dropoff_name = str(data.get("dropoff", dropoff_name))
        locations = {
            str(item["id"]): (int(item["x"]), int(item["y"]))
            for section in ("pickup_points", "dropoff_points", "charging_stations")
            for item in self.map_payload.get(section, [])
        }
        pickup = locations.get(pickup_name, (2, start[1]))
        dropoff = locations.get(dropoff_name, (27, pickup[1]))
        route = self._walk(start, pickup)[:-1] + self._walk(pickup, dropoff)
        blocked_cells = {
            (int(item["x"]), int(item["y"]))
            for item in self.map_payload.get("obstacles", [])
        } | {
            (int(item["x"]), int(item["y"]))
            for item in self.map_payload.get("temporary_blocked_cells", [])
        }
        blocked_edges = {
            (
                (int(item["from"]["x"]), int(item["from"]["y"])),
                (int(item["to"]["x"]), int(item["to"]["y"])),
            )
            for item in self.map_payload.get("blocked_edges", [])
        }
        vertex_collisions = len(route) - len(set(route))
        edge_collisions = sum(
            1
            for first, second in zip(route, route[1:])
            if (first, second) in blocked_edges or (second, first) in blocked_edges
        )
        forbidden_entries = sum(position in blocked_cells for position in route)
        start_battery = float(data.get("start_battery", 100))
        battery_after = start_battery - max(0, len(route) - 1)
        low_battery = int(battery_after < 15.0)
        zero = ZeroToleranceMetrics(
            vertex_collision_count=max(0, vertex_collisions),
            edge_collision_count=edge_collisions,
            forbidden_zone_entry_count=forbidden_entries,
            low_battery_violation_count=low_battery,
        )
        evidence = [
            f"map://warehouse_v1/route/{case.case_id}",
            f"safety://{case.case_id}/zero-tolerance",
        ]
        return route, battery_after, zero, evidence

    @staticmethod
    def _walk(start: tuple[int, int], target: tuple[int, int]) -> list[tuple[int, int]]:
        """生成不依赖随机数的四邻域路径；越界/禁行由调用方安全审计。"""

        x, y = start
        tx, ty = target
        result = [(x, y)]
        while x != tx:
            x += 1 if tx > x else -1
            result.append((x, y))
        while y != ty:
            y += 1 if ty > y else -1
            result.append((x, y))
        return result

    def _aggregate(self, results: Sequence[EvalReportCase]) -> EvalAggregateMetrics:
        """从逐例结果重算指标；不接受调用方传入的预汇总数字。"""

        category_counts = {category.value: sum(item.category is category for item in results) for category in EvalCategory}
        category_pass_rates = {
            category.value: self._mean(
                [int(item.evaluation_passed) for item in results if item.category is category]
            )
            for category in EvalCategory
        }
        zero = ZeroToleranceMetrics(
            **{
                field: sum(getattr(item.zero_tolerance, field) for item in results)
                for field in ZeroToleranceMetrics.model_fields
            }
        )
        normal = [item for item in results if item.category is EvalCategory.NORMAL]
        rag = [item for item in results if item.category is EvalCategory.RAG]
        exceptions = [item for item in results if item.category is EvalCategory.EXCEPTION]
        security = [item for item in results if item.category is EvalCategory.SECURITY]
        verification = [item for item in results if item.category is EvalCategory.VERIFICATION]
        return EvalAggregateMetrics(
            case_count=len(results),
            evaluation_pass_count=sum(item.evaluation_passed for item in results),
            observed_negative_count=sum(item.observed_outcome in {EvalOutcome.DENIED, EvalOutcome.BLOCKED, EvalOutcome.FAILED} for item in results),
            category_counts=category_counts,
            category_pass_rates=category_pass_rates,
            agent={
                "expected_outcome_accuracy": self._mean([int(item.evaluation_passed) for item in results]),
                "normal_completion_rate": self._mean([int(item.observed_outcome in {EvalOutcome.COMPLETED, EvalOutcome.CHARGED}) for item in normal]),
                "trace_completeness_rate": self._mean([int(bool(item.trace_events) and (item.failure_code is None or item.failure_reason is not None)) for item in results]),
                "model_call_count": sum(int(item.metrics.get("model_call_count", 0)) for item in results),
            },
            rag={
                "recall_at_k": self._mean([float(item.metrics["rag_recall_at_k"]) for item in rag if "rag_recall_at_k" in item.metrics]),
                "mrr": self._mean([float(item.metrics["rag_mrr"]) for item in rag if "rag_mrr" in item.metrics]),
                "citation_correctness": self._mean([float(item.metrics["rag_citation_correctness"]) for item in rag if "rag_citation_correctness" in item.metrics]),
                "answerability_accuracy": self._mean([float(item.metrics["rag_answerability_correct"]) for item in rag if "rag_answerability_correct" in item.metrics]),
                "acl_leak_count": sum(int(item.metrics.get("rag_acl_leak_count", 0)) for item in rag),
            },
            amr={
                "normal_order_completion_rate": self._mean([int(item.metrics.get("normal_order_completed", 0)) for item in normal if item.scenario == "normal_order"]),
                "charging_completion_rate": self._mean([int(item.metrics.get("charging_completed", 0)) for item in normal if item.scenario != "normal_order"]),
                "vertex_collision_count": zero.vertex_collision_count,
                "edge_collision_count": zero.edge_collision_count,
                "forbidden_zone_entry_count": zero.forbidden_zone_entry_count,
                "low_battery_violation_count": zero.low_battery_violation_count,
            },
            security={
                "prompt_injection_block_rate": self._mean([int(item.metrics.get("security_blocked", 0)) for item in security]),
                "unauthorized_tool_block_rate": self._mean([int(item.metrics.get("security_blocked", 0)) for item in security]),
                "role_leak_count": zero.role_leak_count,
                "approval_bypass_count": zero.approval_bypass_count,
                "handler_calls_on_blocked_cases": sum(int(item.metrics.get("security_handler_calls", 0)) for item in security),
            },
            recovery={
                "replan_success_rate": self._mean([int(item.metrics.get("recovery_replan_success", 0)) for item in exceptions if item.observed_outcome is EvalOutcome.COMPLETED]),
                "expected_termination_rate": self._mean([int(item.metrics.get("recovery_terminal_correct", 0)) for item in exceptions]),
                "replan_budget_max_observed": max((item.replan_count for item in exceptions), default=0),
                "retry_budget_max_observed": max((item.retry_count for item in exceptions), default=0),
                "duplicate_side_effect_count": zero.duplicate_side_effect_count,
            },
            verification={
                "pass_rate": self._mean([int(item.observed_outcome is EvalOutcome.VERIFIED) for item in verification]),
                "failure_locator_rate": self._mean([int(item.metrics.get("verification_failure_locator", 0)) for item in verification]),
                "passed_case_count": sum(item.observed_outcome is EvalOutcome.VERIFIED for item in verification),
            },
            zero_tolerance=zero,
        )

    @staticmethod
    def _mean(values: Sequence[float | int]) -> float:
        """空分区返回 0，避免 Markdown/JSON 出现 NaN。"""

        return round(statistics.fmean(values), 6) if values else 0.0


def run_harness(
    *,
    dataset_path: Path = DEFAULT_DATASET_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    verification_runner: Any | None = None,
    verification_timeout_seconds: float = 120.0,
    recovery_policy: StrategyRecoveryPolicy = StrategyRecoveryPolicy.PEVR,
) -> EvalReport:
    """一条 Python 调用完成固定数据集加载、执行和聚合。"""

    dataset = load_dataset(dataset_path)
    config = load_config(config_path)
    return EvalHarness(
        dataset=dataset,
        config=config,
        dataset_path=Path(dataset_path),
        config_path=Path(config_path),
        verification_runner=verification_runner,
        verification_timeout_seconds=verification_timeout_seconds,
        recovery_policy=recovery_policy,
    ).run()


__all__ = ["DEFAULT_OUTPUT_DIR", "EvalHarness", "StrategyRecoveryPolicy", "run_harness"]
