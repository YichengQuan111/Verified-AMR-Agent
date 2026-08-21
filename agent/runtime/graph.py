"""P0-13 LangGraph PEVR 正常闭环与 P0-14 可恢复执行。

状态图只负责受控编排，不重写 P0-08～P0-12 的算法。``understand_goal``、
``plan_tasks``、``verify_observation`` 和 ``compose_report`` 直接复用 P0-05
具名节点；RAG、Hungarian、A*、P0-10 Validator 和仿真全部通过 P0-12
``ToolRegistry`` 进入。计划门禁在 ``validate`` 节点完成，因而 Planner 产生的
任何未授权 DAG 都不能到达 Executor。

传入 P0-14 ``checkpoint_store`` 后，固定图仍不允许动态添加节点，但每个完成阶段
和任务会保存 JSON Checkpoint；带副作用任务先写 Effect Ledger，再按真实仿真/工具
状态核对恢复。这样重启只能继续安全未开始的任务、复用已核对结果或停在补偿/重规划
边界，不能把旧快照当成外部事实。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
import hashlib
import inspect
import json
from typing import Any, Callable, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from agent.context import (
    BudgetUsage,
    ContextEvidence,
    EvidenceSourceType,
    FinalReport,
    NodeRoute,
    PlanTasksOutput,
    PromptNodeName,
    build_node_context,
    compose_report,
    plan_tasks,
    understand_goal,
    verify_observation,
)
from agent.context.contracts import FinalReportStatus, ObservationVerification, VerificationDecision
from agent.planning import PlanTask, PlanTaskStatus, TaskContract
from agent.planning.replanner import (
    TaskResourceProvenance,
    build_task_resource_provenance,
)
from agent.planning.validator import (
    NORMAL_PEVR_TOOL_CHAIN,
    PlanValidationResult,
    canonicalize_normal_pevr_plan,
    validate_normal_pevr_plan,
    validate_replanned_pevr_plan,
)
from agent.runtime.pevr import (
    PEVRGraphState,
    PEVRMetrics,
    PEVRRequest,
    PEVRRunReport,
    PEVRRunResult,
    PEVRStage,
    PEVRToolEvidence,
    PEVRTraceEvent,
    PEVR_STAGE_ORDER,
)
from agent.runtime.hitl import (
    ApprovalGrant,
    HITLInterrupt,
    HITLInterruptError,
    HITLReason,
    HITLStatus,
    HITLStoreProtocol,
    InMemoryHITLStore,
    build_hitl_request,
)
from agent.security.contracts import Principal
from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    EffectLedgerStatus,
    ExternalExecutionSnapshot,
    ExternalExecutionStatus,
    RecoveryCoordinator,
    RecoveryDecision,
    RuntimePersistenceProtocol,
    canonical_json_digest,
    make_external_execution_id,
    make_effect_idempotency_key,
    to_jsonable,
)
from agent.runtime.state import (
    Observation,
    ObservationSource,
    ObservationStatus,
    RunState,
    RunStatus,
)
from agent.runtime.faults import (
    FaultClassifier,
    FaultRecoveryController,
    FaultSignal,
    RecoveryAction,
)
from agent.runtime.trace import TraceError, TraceEvent, new_trace_id
from agent.tools import (
    ToolName,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    UserRole,
    build_tool_registry,
)
from agent.tools.contracts import TOOL_ARGUMENT_POLICIES
from agent.tools.schemas import (
    AllocationResponse,
    RoutePlanResponse,
    ValidationResponse,
)
from agent.tools.snapshots import (
    DefaultWarehouseSnapshotProvider,
    EnvironmentSnapshot,
    SnapshotProviderProtocol,
)
from domains.amr_warehouse import AMRState, TransportOrder
from services.amr_simulator.contracts import (
    FleetPlanRoute,
    SimulationPlan,
    SimulationResult,
    ValidatorConfig,
)
from services.retrieval.contracts import RetrievalResponse, RetrievalStatus
from services.model_gateway.contracts import ModelVersionRecord
from services.model_gateway.protocols import ModelProviderProtocol


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """提供带时区时间；时钟集中后，状态图单测可以稳定注入时间。"""

    return datetime.now(timezone.utc)


class _RegistryExternalStateReconciler:
    """通过只读 ``query_execution_state`` 查询 P0-12 仿真真实快照。"""

    def __init__(self, registry: Any, clock: Clock) -> None:
        self._registry = registry
        self._clock = clock

    def inspect(self, *, entry: Any) -> ExternalExecutionSnapshot:
        """只把可核验的 SimulationResult 转成 completed，否则返回 unknown。"""

        if entry.tool_name is not ToolName.DISPATCH_SIMULATION:
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.UNKNOWN,
                source="registry_query_unsupported",
                observed_at=self._clock(),
            )
        simulation_id = entry.external_effect_id or self._simulation_id(entry)
        if not simulation_id:
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.UNKNOWN,
                source="registry_query_missing_reference",
                observed_at=self._clock(),
            )
        try:
            parameters: dict[str, Any] = {
                "run_id": simulation_id,
            }
            query_call_id = f"recovery:{entry.idempotency_key}"
            kwargs: dict[str, Any] = {"role": UserRole.OPERATOR, "call_id": query_call_id}
            if "idempotency_key" in inspect.signature(self._registry.execute).parameters:
                kwargs["idempotency_key"] = f"recovery:{entry.idempotency_key}"
            query_result = self._registry.execute(
                ToolName.QUERY_EXECUTION_STATE,
                parameters,
                **kwargs,
            )
        except Exception:
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.UNKNOWN,
                source="registry_query_failed",
                observed_at=self._clock(),
            )
        if query_result.status is not ToolResultStatus.SUCCESS or not isinstance(query_result.output, Mapping):
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.UNKNOWN,
                source="registry_query_unavailable",
                observed_at=self._clock(),
            )
        output = query_result.output
        snapshot = output.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.UNKNOWN,
                source="registry_query_invalid_snapshot",
                observed_at=self._clock(),
            )
        raw_status = str(snapshot.get("status", output.get("status", "unknown")))
        if raw_status == "completed":
            now = self._clock()
            result = ToolResult(
                tool_name=ToolName.DISPATCH_SIMULATION,
                call_id=entry.call_id,
                status=ToolResultStatus.SUCCESS,
                output=to_jsonable(dict(snapshot)),
                error=None,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                evidence_refs=[f"simulation://{simulation_id}", f"simulation://{simulation_id}/events"],
                effect_id=simulation_id,
                tool_version=self._registry.get(ToolName.DISPATCH_SIMULATION).spec.version,
                principal_role=UserRole.OPERATOR,
                input_digest=entry.input_digest,
                output_digest=canonical_json_digest(snapshot),
                idempotency_key=entry.idempotency_key,
                audit_metadata={"reconciled": True, "source": "query_execution_state"},
            )
            return ExternalExecutionSnapshot(
                status=ExternalExecutionStatus.COMPLETED,
                source="query_execution_state",
                observed_at=now,
                external_effect_id=simulation_id,
                result=result,
                evidence_refs=list(result.evidence_refs),
            )
        if raw_status in {"blocked", "timeout", "failed"}:
            status = ExternalExecutionStatus.FAILED
        elif raw_status in {"running", "in_progress"}:
            status = ExternalExecutionStatus.IN_PROGRESS
        else:
            status = ExternalExecutionStatus.UNKNOWN
        return ExternalExecutionSnapshot(
            status=status,
            source="query_execution_state",
            observed_at=self._clock(),
            external_effect_id=simulation_id,
            details={"status": raw_status},
        )

    @staticmethod
    def _simulation_id(entry: Any) -> str | None:
        """按 Effect 身份推导新式外部 ID；历史 ID 只从账本原值读取。"""

        try:
            return make_external_execution_id(entry.idempotency_key, entry.input_digest)
        except (AttributeError, TypeError, ValueError):
            return None


class PEVRExecutionError(RuntimeError):
    """主图在任一确定性门禁失败时抛出的可定位错误。"""

    def __init__(
        self,
        stage: PEVRStage,
        code: str,
        message: str,
        *,
        fault: FaultSignal | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        # P0-15 只附加结构化分类，不改变 P0-13 既有 code/message 异常契约；
        # 未显式绑定的异常也统一进入 fail-closed 分类，避免上层漏掉终止门禁。
        self.fault = fault or FaultClassifier.classify(
            {"code": code, "message": message},
            stage=stage.value,
        )


class PEVRInterrupt(PEVRExecutionError):
    """PEVR 已保存 waiting_approval Checkpoint，必须由同一 run 恢复。"""

    def __init__(self, interrupt: HITLInterrupt) -> None:
        super().__init__(
            PEVRStage.EXECUTE,
            "hitl_interrupt",
            (
                f"run {interrupt.run_id} 在 task {interrupt.task_id} 等待审批 "
                f"{interrupt.approval_id}"
            ),
        )
        self.interrupt = interrupt


class PEVRGraphRunner:
    """构造并运行固定八节点 PEVR 图。

    默认构造仍保持 P0-13 的纯内存行为；传入 P0-14 ``checkpoint_store`` 后，图节点
    完成和每个任务完成都会写 PostgreSQL/测试适配器。恢复时图会跳过已完成节点，
    但带副作用任务必须先经过 Effect Ledger 和外部状态核对，不能因为旧快照标记
    completed 就直接再次派发。
    """

    ENTRY_BUDGETS = {
        # P0-13 会调用 understand、plan、verify、compose_report 四个独立
        # Prompt；预算是整次运行的累计上限，而不是单次 llama.cpp 上下文窗口。
        # Fast 的单次 context_window 由网关固定为 16384，这里只避免把多个
        # 合法节点的输入相加后误判为 fallback。
        "max_total_seconds": 300,
        "max_input_tokens": 30000,
        "max_output_tokens": 5000,
        "max_tool_steps": 8,
        # P0-15 的默认自动恢复额度；合同仍可显式收紧到 0～2。
        "max_replans": 2,
        "max_retries": 2,
    }
    DEFAULT_PAYLOAD_KG = 1.0

    def __init__(
        self,
        provider: ModelProviderProtocol,
        *,
        registry: ToolRegistry | None = None,
        snapshot_provider: SnapshotProviderProtocol | None = None,
        checkpoint_store: RuntimePersistenceProtocol | None = None,
        external_state_reconciler: Any | None = None,
        clock: Clock = _utc_now,
        hitl_store: HITLStoreProtocol | None = None,
        security_required: bool = False,
        hitl_ttl_seconds: int = 900,
        fault_recovery_enabled: bool | None = None,
    ) -> None:
        self.provider = provider
        self.security_required = security_required
        self.hitl_ttl_seconds = hitl_ttl_seconds
        self.hitl_store = hitl_store or (InMemoryHITLStore() if security_required else None)
        self._active_approval_grant: ApprovalGrant | None = None
        self.registry = registry or build_tool_registry(
            security_required=security_required,
            approval_verifier=self._registry_approval_verifier if security_required else None,
        )
        # 调用方注入的正式 ToolRegistry 也必须切到同一安全模式；fake registry
        # 没有这些属性时仍由 _registry_execute 的签名兼容逻辑保护旧测试。
        if security_required and isinstance(self.registry, ToolRegistry):
            self.registry.security_required = True
            self.registry.approval_verifier = self._registry_approval_verifier
        self.snapshot_provider = snapshot_provider or DefaultWarehouseSnapshotProvider()
        self.checkpoint_store = checkpoint_store
        # 自动恢复必须有可重启事实；无 Store 的纯单测继续暴露原异常，生产组装
        # 只要注入 Checkpoint Store 就默认启用 P0-15 有界控制器。
        self.fault_recovery_enabled = (
            checkpoint_store is not None
            if fault_recovery_enabled is None
            else fault_recovery_enabled
        )
        if self.fault_recovery_enabled and checkpoint_store is None:
            raise ValueError("启用 P0-15 自动恢复必须配置 checkpoint_store")
        # 默认使用当前注册表的只读状态查询；若调用方提供真实仿真/工具适配器，
        # RecoveryCoordinator 会优先使用它。没有适配器时仍返回 unknown 并转安全
        # 分支，绝不把旧 Checkpoint 当作外部真相。
        # PostgreSQL Store 能按唯一幂等键读取本行的外部快照，优先于全局按
        # simulation_id 扫描；这同时为旧版本的跨 run 冲突记录提供安全兼容读取。
        store_reconciler = (
            checkpoint_store
            if checkpoint_store is not None and callable(getattr(checkpoint_store, "inspect", None))
            else None
        )
        self._recovery = RecoveryCoordinator(
            external_state_reconciler
            or store_reconciler
            or _RegistryExternalStateReconciler(self.registry, clock)
        )
        self._clock = clock
        self.graph = self._build_graph()

    def _registry_approval_verifier(
        self,
        grant: ApprovalGrant,
        spec: Any,
        arguments: Mapping[str, Any],
    ) -> None:
        """把 Registry 的最后一道审批门禁绑定到当前已核对票据。"""

        del spec, arguments
        if self._active_approval_grant is None or grant != self._active_approval_grant:
            raise PermissionError("工具调用未绑定当前 PEVR 已核对的审批票据")

    @staticmethod
    def classify_failure(
        error: Any,
        *,
        stage: str | PEVRStage | None = None,
        task_id: str | None = None,
        tool_name: ToolName | str | None = None,
        idempotent: bool = True,
        has_side_effects: bool = False,
        side_effect_not_found: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> FaultSignal:
        """把 PEVR 异常交给 P0-15；已绑定信号原样保留影响实体和证据。"""

        attached_fault = getattr(error, "fault", None)
        if isinstance(error, FaultSignal):
            return error.model_copy(deep=True)
        if isinstance(attached_fault, FaultSignal):
            return attached_fault.model_copy(deep=True)
        resolved_stage = stage.value if isinstance(stage, PEVRStage) else stage
        return FaultClassifier.classify(
            error,
            stage=resolved_stage,
            task_id=task_id,
            tool_name=tool_name,
            idempotent=idempotent,
            has_side_effects=has_side_effects,
            side_effect_not_found=side_effect_not_found,
            details=details,
        )

    def _build_graph(self):
        """编译只含固定节点和固定边的状态图，不允许模型动态添加节点。"""

        builder = StateGraph(PEVRGraphState)
        builder.add_node(PEVRStage.GUARD.value, self._checkpointed_node(PEVRStage.GUARD, self._guard_node))
        builder.add_node(
            PEVRStage.UNDERSTAND.value,
            self._checkpointed_node(PEVRStage.UNDERSTAND, self._understand_node),
        )
        builder.add_node(PEVRStage.RETRIEVE.value, self._checkpointed_node(PEVRStage.RETRIEVE, self._retrieve_node))
        builder.add_node(PEVRStage.PLAN.value, self._checkpointed_node(PEVRStage.PLAN, self._plan_node))
        builder.add_node(PEVRStage.VALIDATE.value, self._checkpointed_node(PEVRStage.VALIDATE, self._validate_node))
        builder.add_node(PEVRStage.EXECUTE.value, self._checkpointed_node(PEVRStage.EXECUTE, self._execute_node))
        builder.add_node(PEVRStage.VERIFY.value, self._checkpointed_node(PEVRStage.VERIFY, self._verify_node))
        builder.add_node(PEVRStage.FINISH.value, self._checkpointed_node(PEVRStage.FINISH, self._finish_node))
        builder.add_edge(START, PEVRStage.GUARD.value)
        for previous, following in zip(PEVR_STAGE_ORDER, PEVR_STAGE_ORDER[1:]):
            builder.add_edge(previous.value, following.value)
        builder.add_edge(PEVRStage.FINISH.value, END)
        return builder.compile()

    def run(self, request: PEVRRequest | Mapping[str, Any]) -> PEVRRunResult:
        """执行或恢复一次本地模型驱动闭环，并返回完整可审计结果。"""

        resolved_request = PEVRRequest.model_validate(request)
        checkpoint = self._load_checkpoint(resolved_request.run_id)
        if checkpoint is not None:
            self._validate_resume_request(checkpoint, resolved_request)
            recovered_state: PEVRGraphState | None = None
            try:
                self._reconcile_checkpoint(checkpoint)
            except Exception as exc:
                if not self.fault_recovery_enabled:
                    raise
                recovered_state = self._recover_graph_failure(resolved_request, exc)
            if recovered_state is not None:
                restored = recovered_state
            else:
                try:
                    restored = self._restore_graph_state(checkpoint.graph_state)
                    self._merge_persisted_trace_events(restored, resolved_request.run_id)
                except (TypeError, ValueError, ValidationError) as exc:
                    # 损坏 JSONB 不允许通过过滤坏项“自愈”；恢复必须停在确定性边界，
                    # 同时不给调用方泄漏整份持久化载荷。
                    raise PEVRExecutionError(
                        PEVRStage.GUARD,
                        "checkpoint_corrupt",
                        f"Checkpoint 无法通过当前图状态契约: {type(exc).__name__}",
                    ) from exc
            trace_id = str(restored.get("trace_id") or resolved_request.trace_id or new_trace_id(resolved_request.run_id))
            resolved_request = resolved_request.model_copy(update={"trace_id": trace_id})
            restored["trace_id"] = trace_id
            restored["request"] = resolved_request
            if self._is_terminal_graph_state(restored):
                return self._result_from_graph_state(restored, resolved_request)
        else:
            restored = None
            trace_id = resolved_request.trace_id or new_trace_id(resolved_request.run_id)
            resolved_request = resolved_request.model_copy(update={"trace_id": trace_id})
        # 运行前先确认 alias；若 Fast 服务不在或暴露了错误模型，图不会开始执行。
        model_version = self.provider.startup()
        initial: PEVRGraphState = restored or {
            "request": resolved_request,
            "trace_id": trace_id,
            "trace_events": [],
            "stage_trace": [],
            "run_state": None,
            "contract": None,
            "retrieval_result": None,
            "rag_evidence": [],
            "plan": None,
            "plan_normalization_notes": [],
            "plan_validation": None,
            "derived_plan": None,
            "tool_results": [],
            "tool_task_ids": [],
            "resource_provenance": [],
            "observations": [],
            "verification": None,
            "final_report": None,
            "model_version": model_version,
            "budget_usage": BudgetUsage(),
            "model_call_count": 0,
            "hitl_interrupt": None,
            "approval_grant": resolved_request.approval_grant,
            "approval_id": None,
            "approval_checkpoint_id": None,
        }
        # 当前进程仍需重新确认模型身份；这不会覆盖已持久化的工具/状态事实。
        initial["request"] = resolved_request
        initial["trace_id"] = trace_id
        initial.setdefault("trace_events", [])
        initial["model_version"] = model_version
        initial["approval_grant"] = resolved_request.approval_grant or initial.get("approval_grant")
        state = self._invoke_graph_with_recovery(initial, resolved_request)
        report = state.get("final_report")
        run_state = state.get("run_state")
        verification = state.get("verification")
        if not isinstance(report, PEVRRunReport) or not isinstance(run_state, RunState):
            raise PEVRExecutionError(PEVRStage.FINISH, "report_missing", "PEVR 图未产生完整报告")
        if not isinstance(verification, ObservationVerification):
            raise PEVRExecutionError(PEVRStage.VERIFY, "verification_missing", "PEVR 图未产生 Observation 验证结果")
        return PEVRRunResult(
            request=resolved_request,
            trace_id=trace_id,
            report=report,
            run_state=run_state,
            stage_trace=list(state.get("stage_trace", [])),
            tool_results=list(state.get("tool_results", [])),
            observations=list(state.get("observations", [])),
            verification=verification,
            resource_provenance=list(state.get("resource_provenance", [])),
            trace_events=list(state.get("trace_events", [])),
        )

    def _invoke_graph_with_recovery(
        self,
        initial: PEVRGraphState,
        request: PEVRRequest,
    ) -> PEVRGraphState:
        """执行固定图，并把失败交给唯一的 P0-15 有界控制器。

        LangGraph 节点异常不会返回半成品 state，因此恢复只从最后一次已提交的
        Checkpoint 和独立 Trace 流重建事实。retry/replan 会生成新 Checkpoint 后
        重新进入同一固定八节点图；human/fallback/fatal 则先把 RunState 持久化为
        failed 再向调用方抛出稳定异常，数据库不会永久停在 planning。
        """

        current = initial
        recovery_cycles = 0
        while True:
            try:
                return cast(
                    PEVRGraphState,
                    self.graph.invoke(current, config={"recursion_limit": 32}),
                )
            except PEVRInterrupt:
                raise
            except Exception as exc:
                if not self.fault_recovery_enabled:
                    raise
                recovery_cycles += 1
                if recovery_cycles > 16:
                    # 该硬保险不替代合同预算；理论上 retry+replan 最多 4 次便会
                    # 终止。若状态机未来改动形成环，unknown/fatal 仍会落终态。
                    exc = PEVRExecutionError(
                        PEVRStage.EXECUTE,
                        "recovery_loop_guard",
                        "恢复循环超过硬上限，强制 fail closed",
                    )
                current = self._recover_graph_failure(request, exc)

    def _recover_graph_failure(
        self,
        request: PEVRRequest,
        error: Exception,
    ) -> PEVRGraphState:
        """从最近 Checkpoint 分类一次失败并返回 retry/replan 状态或持久化终态。"""

        checkpoint = self._load_checkpoint(request.run_id)
        if checkpoint is None:
            # understand 之前尚无 TaskContract，无法合法构造恢复预算或 RunState；
            # 保留原始异常，不能伪造一份“已恢复”合同。
            raise error
        try:
            restored = self._restore_graph_state(checkpoint.graph_state)
            self._merge_persisted_trace_events(restored, request.run_id)
        except Exception as restore_error:
            raise PEVRExecutionError(
                PEVRStage.GUARD,
                "recovery_checkpoint_corrupt",
                "失败恢复时 Checkpoint 无法通过当前契约",
            ) from restore_error
        contract = restored.get("contract")
        run_state = restored.get("run_state")
        plan = restored.get("plan")
        if not isinstance(contract, TaskContract) or not isinstance(run_state, RunState):
            raise error
        if run_state.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            # 业务终态不能因迟到的报告/Trace 错误被重新打开；此时数据库已经
            # 明确不在 planning，保留原异常供调用方修复报告边界。
            raise error
        controller = FaultRecoveryController(
            contract,
            usage=restored.get("budget_usage"),
            run_state=run_state,
            clock=self._clock,
        )
        decision = controller.handle_failure(error)
        if decision.action is RecoveryAction.REPLAN and isinstance(plan, PlanTasksOutput):
            try:
                return self._apply_production_replan(
                    restored,
                    controller=controller,
                    decision=decision,
                    run_state=run_state,
                    plan=plan,
                    request=request,
                )
            except Exception as replan_error:
                # LocalReplanner/完整门禁拒绝候选时不能沿用旧计划继续；把原始
                # replan 决策和新的 deterministic failure 一并写入 fatal 终态。
                preparing = controller.record_on_run_state(run_state, decision)
                terminal_controller = FaultRecoveryController(
                    contract,
                    usage=decision.budget_usage,
                    run_state=preparing,
                    clock=self._clock,
                )
                terminal = terminal_controller.handle_failure(
                    {
                        "code": "local_replan_invalid",
                        "message": f"局部重规划未通过完整门禁: {type(replan_error).__name__}",
                    },
                    stage=PEVRStage.EXECUTE.value,
                )
                terminal_state = terminal_controller.record_on_run_state(preparing, terminal)
                self._persist_recovery_decision(
                    restored,
                    run_state=terminal_state,
                    decision=terminal,
                    stage=PEVRStage.EXECUTE,
                )
                raise PEVRExecutionError(
                    PEVRStage.EXECUTE,
                    "recovery_fatal",
                    terminal.reason,
                    fault=terminal.fault,
                ) from replan_error

        recorded = controller.record_on_run_state(run_state, decision)
        stage = self._fault_stage(decision.fault)
        self._persist_recovery_decision(
            restored,
            run_state=recorded,
            decision=decision,
            stage=stage,
        )
        if decision.action is RecoveryAction.RETRY:
            restored["run_state"] = recorded
            restored["budget_usage"] = decision.budget_usage.to_budget_usage()
            restored["stage"] = stage
            return restored
        raise PEVRExecutionError(
            stage,
            f"recovery_{decision.action.value}",
            decision.reason,
            fault=decision.fault,
        ) from error

    def _apply_production_replan(
        self,
        restored: PEVRGraphState,
        *,
        controller: FaultRecoveryController,
        decision: Any,
        run_state: RunState,
        plan: PlanTasksOutput,
        request: PEVRRequest,
    ) -> PEVRGraphState:
        """克隆唯一受影响未完成子图，经 LocalReplanner 完整复验后写新版本。"""

        state_tasks = {task.task_id: task for task in run_state.plan_tasks}
        synchronized_plan = plan.model_copy(
            update={
                "tasks": [state_tasks.get(task.task_id, task) for task in plan.tasks]
            }
        )
        analysis = controller.replanner.analyze(
            synchronized_plan,
            completed_task_ids=run_state.completed_task_ids,
            affected_entities=decision.fault.affected_entities,
            failed_task_id=decision.fault.task_id,
            failed_tool_name=decision.fault.tool_name,
            runtime_resources=restored.get("resource_provenance", []),
        )
        if not analysis.invalidated_task_ids:
            raise ValueError("故障没有定位到可替换的未完成任务")
        replacements = self._clone_replan_subgraph(
            synchronized_plan,
            invalidated_task_ids=analysis.invalidated_task_ids,
            new_plan_version=synchronized_plan.plan_version + 1,
        )
        recovery = controller.apply_replan(
            run_state,
            synchronized_plan,
            decision,
            replacements,
            tool_specs=self.registry.specs(),
            expected_seed=request.seed,
            runtime_resources=restored.get("resource_provenance", []),
        )
        completed = set(recovery.state.completed_task_ids)
        kept_results: list[ToolResult] = []
        kept_task_ids: list[str | None] = []
        for task_id, result in zip(
            restored.get("tool_task_ids", []),
            restored.get("tool_results", []),
        ):
            if task_id is None or task_id in completed:
                kept_task_ids.append(task_id)
                kept_results.append(result)
        route_retained = any(
            task.task_id in completed and task.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES
            for task in recovery.replan_result.plan.tasks
        )
        # 计划版本变化后旧审批摘要必然失效；即使故障发生在审批之后，也必须
        # 回到新的 waiting checkpoint，不能把旧 grant 带到新计划。
        stored_request = restored["request"].model_copy(
            update={
                "approval_grant": None,
                "approval_granted": (
                    restored["request"].approval_granted
                    if not self.security_required and restored["request"].principal is None
                    else False
                ),
            }
        )
        kept_stages = {
            PEVRStage.GUARD,
            PEVRStage.UNDERSTAND,
            PEVRStage.RETRIEVE,
            PEVRStage.PLAN,
        }
        restored.update(
            {
                "request": stored_request,
                "stage": PEVRStage.VALIDATE,
                "stage_trace": [
                    item
                    for item in restored.get("stage_trace", [])
                    if item.stage in kept_stages
                ],
                "run_state": recovery.state,
                "plan": recovery.replan_result.plan,
                "plan_validation": recovery.replan_result.plan_validation,
                "plan_normalization_notes": [
                    *restored.get("plan_normalization_notes", []),
                    f"p015_replan:{recovery.replan_result.new_plan_version}",
                ],
                "derived_plan": restored.get("derived_plan") if route_retained else None,
                "tool_results": kept_results,
                "tool_task_ids": kept_task_ids,
                "resource_provenance": [
                    item
                    for item in restored.get("resource_provenance", [])
                    if item.task_id in completed
                ],
                "observations": [
                    item
                    for item in restored.get("observations", [])
                    if item.task_id is None or item.task_id in completed
                ],
                "budget_usage": decision.budget_usage.to_budget_usage(),
                "hitl_interrupt": None,
                "approval_grant": None,
                "approval_id": None,
                "approval_checkpoint_id": None,
                "verification": None,
                "final_report": None,
            }
        )
        self._persist_recovery_decision(
            restored,
            run_state=recovery.state,
            decision=decision,
            stage=PEVRStage.EXECUTE,
        )
        return restored

    @staticmethod
    def _clone_replan_subgraph(
        plan: PlanTasksOutput,
        *,
        invalidated_task_ids: list[str],
        new_plan_version: int,
    ) -> list[PlanTask]:
        """确定性克隆失效子图，替换 ID/依赖/$ref 并清除旧执行证据。"""

        invalidated = set(invalidated_task_ids)
        id_map: dict[str, str] = {}
        for task_id in invalidated_task_ids:
            candidate = f"{task_id}-R{new_plan_version}"
            if len(candidate) > 128:
                digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
                candidate = f"TASK-REPLAN-{new_plan_version}-{digest}"
            id_map[task_id] = candidate

        def replace_refs(value: Any) -> Any:
            """只替换结构化 task 引用字符串，不解释或执行其余文本。"""

            if isinstance(value, Mapping):
                return {str(key): replace_refs(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_refs(item) for item in value]
            if isinstance(value, str):
                for old, new in id_map.items():
                    value = value.replace(f"task:{old}/", f"task:{new}/")
                return value
            return value

        replacements: list[PlanTask] = []
        for task in plan.tasks:
            if task.task_id not in invalidated:
                continue
            replacements.append(
                task.model_copy(
                    update={
                        "task_id": id_map[task.task_id],
                        "dependencies": [id_map.get(item, item) for item in task.dependencies],
                        "tool_arguments": replace_refs(task.tool_arguments),
                        "status": PlanTaskStatus.PENDING,
                        "evidence_refs": [],
                        "effect_id": None,
                    }
                )
            )
        return replacements

    def _persist_recovery_decision(
        self,
        state: PEVRGraphState,
        *,
        run_state: RunState,
        decision: Any,
        stage: PEVRStage,
    ) -> None:
        """写恢复 Trace 与 Checkpoint；继续动作和终态使用同一审计格式。"""

        now = self._clock()
        terminal = bool(decision.terminal)
        error = (
            TraceError(
                category=decision.fault.category.value,
                code=f"recovery_{decision.action.value}",
                message=decision.reason,
                retryable=False,
                details={"fault_id": decision.fault.fault_id},
            )
            if terminal
            else None
        )
        event = TraceEvent(
            trace_id=str(state.get("trace_id") or state["request"].trace_id or ""),
            run_id=run_state.run_id,
            sequence=len(state.get("trace_events", [])) + 1,
            event_type="node",
            status="failed" if terminal else "completed",
            node="recovery",
            task_id=decision.fault.task_id,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            error=error,
            evidence_refs=list(decision.fault.evidence_refs),
            metadata={
                "fault_id": decision.fault.fault_id,
                "fault_category": decision.fault.category.value,
                "recovery_action": decision.action.value,
                "retry_count": decision.retry_count,
                "replan_count": decision.replan_count,
                "terminal": terminal,
            },
        )
        state["run_state"] = run_state
        state["budget_usage"] = decision.budget_usage.to_budget_usage()
        state["trace_events"] = self._append_trace_events(state, [event])
        state["stage"] = stage
        self._persist_checkpoint(state, stage=stage)

    @staticmethod
    def _fault_stage(fault: FaultSignal) -> PEVRStage:
        """未知或旧版 stage 统一落 execute，确保终态仍可持久化。"""

        try:
            return PEVRStage(fault.stage) if fault.stage is not None else PEVRStage.EXECUTE
        except ValueError:
            return PEVRStage.EXECUTE

    def _checkpointed_node(
        self,
        stage: PEVRStage,
        handler: Callable[[PEVRGraphState], dict[str, Any]],
    ) -> Callable[[PEVRGraphState], dict[str, Any]]:
        """为固定图节点加恢复跳过和完成后 Checkpoint。"""

        def wrapped(state: PEVRGraphState) -> dict[str, Any]:
            if self._stage_completed(state, stage):
                return {}
            started_at = self._clock()
            try:
                result = handler(state)
                merged = dict(state)
                merged.update(result)
                node_event = self._make_node_trace_event(
                    merged,
                    stage=stage,
                    status="completed",
                    started_at=started_at,
                    finished_at=self._clock(),
                )
                trace_events = self._append_trace_events(merged, [node_event])
                merged["trace_events"] = trace_events
                self._persist_checkpoint(merged, stage=stage)
                return {**result, "trace_events": trace_events}
            except Exception as exc:
                # 失败节点没有正常返回 state，因此必须直接写入持久化 Trace；下一次
                # 恢复会从 Checkpoint 的已有事件继续，而不会伪造成功的 stage_trace。
                failure_event = self._make_node_trace_event(
                    state,
                    stage=stage,
                    status=self._trace_status_for_exception(exc),
                    started_at=started_at,
                    finished_at=self._clock(),
                    error=self._trace_error_for_exception(exc, stage=stage),
                )
                self._persist_trace_event(failure_event)
                raise

        return wrapped

    @staticmethod
    def _stage_completed(state: PEVRGraphState, stage: PEVRStage) -> bool:
        """只按已持久化完成轨迹跳过节点；当前执行中的 stage 不算完成。"""

        return any(item.stage is stage for item in state.get("stage_trace", []))

    def _persist_checkpoint(self, state: Mapping[str, Any], *, stage: PEVRStage) -> None:
        """将图状态 JSON 化并交给持久化适配器，失败即停止而不继续执行。"""

        if self.checkpoint_store is None:
            return
        request = state.get("request")
        run_state = state.get("run_state")
        if not isinstance(request, PEVRRequest) or not isinstance(run_state, RunState):
            # guard 尚未生成合同/RunState 时没有足够事实创建 Checkpoint；后续
            # understand 节点会第一次持久化，避免保存一个不可恢复的半信封。
            return
        graph_state = cast(dict[str, Any], to_jsonable(dict(state)))
        interrupt = state.get("hitl_interrupt")
        checkpoint_id = (
            interrupt.checkpoint_id
            if isinstance(interrupt, HITLInterrupt)
            else f"cp_{uuid4().hex}"
        )
        snapshot = CheckpointSnapshot(
            checkpoint_id=checkpoint_id,
            run_id=request.run_id,
            stage=stage.value,
            status=run_state.status.value,
            plan_version=run_state.plan_version,
            current_task_id=run_state.current_task_id,
            graph_state=graph_state,
            saved_at=self._clock(),
        )
        self.checkpoint_store.save_checkpoint(snapshot)

    def _persist_trace_event(self, event: TraceEvent) -> None:
        """将 Trace 事件写入可选持久化层；没有 sink 时保持纯内存运行兼容。"""

        if self.checkpoint_store is None:
            return
        append = getattr(self.checkpoint_store, "append_trace_event", None)
        if callable(append):
            append(event)

    def _append_trace_events(
        self,
        state: Mapping[str, Any],
        events: list[TraceEvent],
    ) -> list[TraceEvent]:
        """按当前状态继续分配 Trace 序号，并在同一时刻持久化新增事件。"""

        request = state.get("request")
        if not isinstance(request, PEVRRequest):
            raise PEVRExecutionError(PEVRStage.GUARD, "trace_request_missing", "Trace 缺少 PEVRRequest")
        trace_id = str(state.get("trace_id") or request.trace_id or "")
        if not trace_id:
            raise PEVRExecutionError(PEVRStage.GUARD, "trace_id_missing", "Trace 缺少 trace_id")
        existing_payload = state.get("trace_events", [])
        if not isinstance(existing_payload, list):
            raise PEVRExecutionError(PEVRStage.GUARD, "trace_state_invalid", "Trace events 必须是数组")
        existing = [
            item if isinstance(item, TraceEvent) else TraceEvent.model_validate(item)
            for item in existing_payload
        ]
        output = [item.model_copy(deep=True) for item in existing]
        for event in events:
            if event.trace_id != trace_id or event.run_id != request.run_id:
                raise PEVRExecutionError(PEVRStage.GUARD, "trace_identity_mismatch", "Trace 事件身份不一致")
            expected = len(output) + 1
            if event.sequence != expected:
                raise PEVRExecutionError(
                    PEVRStage.GUARD,
                    "trace_sequence_gap",
                    f"Trace 序号不连续，期待 {expected}，收到 {event.sequence}",
                )
            self._persist_trace_event(event)
            output.append(event.model_copy(deep=True))
        return output

    def _make_node_trace_event(
        self,
        state: Mapping[str, Any],
        *,
        stage: PEVRStage,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        error: TraceError | None = None,
    ) -> TraceEvent:
        """为节点成功/失败统一生成事件，失败仍保留当前任务和业务证据。"""

        request = state.get("request")
        if not isinstance(request, PEVRRequest):
            raise PEVRExecutionError(PEVRStage.GUARD, "trace_request_missing", "Trace 缺少 PEVRRequest")
        run_state = state.get("run_state")
        task_id = run_state.current_task_id if isinstance(run_state, RunState) else None
        evidence_refs = (
            [ref for observation in run_state.observations for ref in observation.evidence_refs]
            if isinstance(run_state, RunState)
            else []
        )
        error_details = error.details if error is not None else {}
        if isinstance(error_details, Mapping):
            detail_task_id = error_details.get("task_id")
            if task_id is None and isinstance(detail_task_id, str):
                task_id = detail_task_id
            detail_evidence = error_details.get("evidence_refs")
            if isinstance(detail_evidence, list):
                evidence_refs.extend(item for item in detail_evidence if isinstance(item, str))
        detail_tool_name = (
            error_details.get("tool_name")
            if isinstance(error_details, Mapping)
            else None
        )
        if isinstance(detail_tool_name, Enum):
            detail_tool_name = detail_tool_name.value
        parameters_digest = (
            error_details.get("parameters_digest")
            if isinstance(error_details, Mapping)
            else None
        )
        if not isinstance(parameters_digest, str):
            parameters_digest = (
                error_details.get("input_digest")
                if isinstance(error_details, Mapping)
                else None
            )
        trace_id = str(state.get("trace_id") or request.trace_id or "")
        if not trace_id:
            raise PEVRExecutionError(PEVRStage.GUARD, "trace_id_missing", "Trace 缺少 trace_id")
        latency_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        return TraceEvent(
            trace_id=trace_id,
            run_id=request.run_id,
            sequence=len(state.get("trace_events", [])) + 1,
            event_type="node",
            status=status,  # type: ignore[arg-type]
            node=stage.value,
            task_id=task_id,
            tool_name=detail_tool_name if isinstance(detail_tool_name, str) else None,
            latency_ms=latency_ms,
            started_at=started_at,
            finished_at=finished_at,
            parameters_digest=parameters_digest if isinstance(parameters_digest, str) else None,
            error=error,
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            metadata={"stage": stage.value},
        )

    @staticmethod
    def _trace_status_for_exception(error: Exception) -> str:
        """把 P0-15/工具错误映射到有限 Trace 状态，不把异常字符串当命令。"""

        code = str(getattr(error, "code", "")).lower()
        fault = getattr(error, "fault", None)
        category = str(getattr(getattr(fault, "category", None), "value", "")).lower()
        if "timeout" in code or "timeout" in category:
            return "timeout"
        if any(token in code for token in ("permission", "approval", "principal", "role", "denied")):
            return "denied"
        return "failed"

    @staticmethod
    def _trace_error_for_exception(error: Exception, *, stage: PEVRStage) -> TraceError:
        """把节点异常固定为安全、可搜索的 Trace 错误载荷。"""

        fault = getattr(error, "fault", None)
        category = getattr(getattr(fault, "category", None), "value", None) or "runtime"
        code = str(getattr(error, "code", None) or type(error).__name__)
        message = str(error) or code
        details: dict[str, Any] = {"stage": stage.value, "exception_type": type(error).__name__}
        if isinstance(fault, BaseModel):
            fault_payload = fault.model_dump(mode="json")
            if isinstance(fault_payload, Mapping):
                details.update(dict(fault_payload))
        return TraceError(
            category=str(category),
            code=code[:128],
            message=message[:2000],
            retryable=getattr(fault, "retryable", None) if isinstance(getattr(fault, "retryable", None), bool) else None,
            details=details,
        )

    def _load_checkpoint(self, run_id: str) -> CheckpointSnapshot | None:
        """读取恢复点；新运行没有 PostgreSQL 行时仍允许先走 understand。"""

        if self.checkpoint_store is None:
            return None
        try:
            return self.checkpoint_store.load_checkpoint(run_id)
        except Exception as exc:
            # P0-06 的 ResourceNotFoundError 代表还未调用 ensure_run，不是损坏快照；
            # 其他异常继续抛出，不能把数据库不可用伪装成新运行。
            if type(exc).__name__ == "ResourceNotFoundError":
                return None
            raise

    def _merge_persisted_trace_events(
        self,
        state: PEVRGraphState,
        run_id: str,
    ) -> None:
        """恢复时用独立 Trace 事件流补齐短快照，分叉则 fail-closed。"""

        if self.checkpoint_store is None:
            return
        loader = getattr(self.checkpoint_store, "list_trace_events", None)
        if not callable(loader):
            return
        persisted = list(loader(run_id) or [])
        if not persisted:
            return
        if state.get("trace_id") not in {None, persisted[0].trace_id}:
            raise ValueError("Checkpoint trace_id 与独立 Trace 事件流不一致")
        current_payload = state.get("trace_events", [])
        current = [
            item if isinstance(item, TraceEvent) else TraceEvent.model_validate(item)
            for item in current_payload
        ]
        for left, right in zip(current, persisted):
            if left.model_dump(mode="json") != right.model_dump(mode="json"):
                raise ValueError("Checkpoint trace_events 与独立 Trace 事件流发生分叉")
        if len(persisted) < len(current):
            raise ValueError("Checkpoint trace_events 超过独立持久化 Trace")
        for expected, event in enumerate(persisted, start=1):
            if event.sequence != expected or event.run_id != run_id:
                raise ValueError("独立 Trace 事件流序号或 run_id 非法")
        state["trace_id"] = persisted[0].trace_id
        state["trace_events"] = [item.model_copy(deep=True) for item in persisted]

    @staticmethod
    def _validate_resume_request(checkpoint: CheckpointSnapshot, request: PEVRRequest) -> None:
        """同一 run_id 恢复时只接受同一环境和原始请求，避免串用 Checkpoint。"""

        payload = checkpoint.graph_state.get("request")
        if not isinstance(payload, Mapping):
            raise PEVRExecutionError(PEVRStage.GUARD, "checkpoint_request_missing", "Checkpoint 缺少原始请求")
        stored_principal = payload.get("principal")
        principal_identity = (
            {
                "subject": request.principal.subject,
                "role": request.principal.role.value,
                "issuer": request.principal.issuer,
                "audience": request.principal.audience,
            }
            if request.principal is not None
            else None
        )
        stored_identity = (
            {
                "subject": stored_principal.get("subject"),
                "role": stored_principal.get("role"),
                "issuer": stored_principal.get("issuer"),
                "audience": stored_principal.get("audience"),
            }
            if isinstance(stored_principal, Mapping)
            else None
        )
        if (
            payload.get("raw_request") != request.raw_request
            or payload.get("environment_ref") != request.environment_ref
            or payload.get("seed") != request.seed
            or stored_identity != principal_identity
            or (
                request.trace_id is not None
                and payload.get("trace_id") is not None
                and payload.get("trace_id") != request.trace_id
            )
        ):
            raise PEVRExecutionError(
                PEVRStage.GUARD,
                "checkpoint_request_mismatch",
                "恢复请求与持久化 run_id 的原始请求、环境或 seed 不一致",
            )

    def _reconcile_checkpoint(self, checkpoint: CheckpointSnapshot) -> None:
        """恢复前逐条核对未完成/已完成副作用，未知状态不自动重放。"""

        if self.checkpoint_store is None:
            return
        list_effects = getattr(self.checkpoint_store, "list_effects", None)
        if not callable(list_effects):
            return
        for entry in list_effects(checkpoint.run_id):
            if entry.status not in {
                EffectLedgerStatus.RESERVED,
                EffectLedgerStatus.COMPLETED,
                EffectLedgerStatus.RECONCILED,
            }:
                continue
            assessment = self._recovery.assess(entry)
            if assessment.decision is RecoveryDecision.SKIP_COMPLETED:
                if entry.result is None and assessment.external.result is not None:
                    self.checkpoint_store.complete_effect(
                        entry.idempotency_key,
                        assessment.external.result,
                        external_effect_id=assessment.external.external_effect_id,
                        reconciled=True,
                        recovery_note=assessment.reason,
                    )
                continue
            if assessment.decision is RecoveryDecision.CONTINUE:
                continue
            if assessment.decision is RecoveryDecision.COMPENSATE:
                fail_effect = getattr(self.checkpoint_store, "fail_effect", None)
                if callable(fail_effect):
                    # P0-14 没有擅自创造补偿工具；先把“必须补偿”的事实落账，
                    # 由 P0-15/人工流程执行受控补偿，防止下次恢复误当成可重放。
                    fail_effect(
                        entry.idempotency_key,
                        note=assessment.reason,
                        compensation_required=True,
                    )
            recovery_fault = FaultClassifier.classify(
                {
                    "code": "state_conflict",
                    "message": assessment.reason,
                    "details": {
                        "recovery_decision": assessment.decision.value,
                        "idempotency_key": assessment.idempotency_key,
                    },
                },
                stage=PEVRStage.EXECUTE.value,
                task_id=entry.task_id,
                tool_name=entry.tool_name,
                idempotent=True,
                has_side_effects=True,
            )
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                f"recovery_{assessment.decision.value}",
                assessment.reason,
                fault=recovery_fault,
            )

    @staticmethod
    def _restore_graph_state(payload: Mapping[str, Any]) -> PEVRGraphState:
        """把 JSONB 快照重新验证为 PEVR 各层 Pydantic 契约。"""

        allowed_keys = set(PEVRGraphState.__annotations__)
        unknown_keys = set(payload) - allowed_keys
        if unknown_keys:
            raise ValueError(f"Checkpoint graph_state 包含未知字段: {', '.join(sorted(unknown_keys))}")
        restored: PEVRGraphState = dict(payload)
        request_payload = payload.get("request")
        if not isinstance(request_payload, Mapping):
            raise ValueError("Checkpoint request 必须是对象")
        restored["request"] = PEVRRequest.model_validate(request_payload)
        raw_trace_id = payload.get("trace_id") or restored["request"].trace_id
        if raw_trace_id is None:
            # P0-14 早期 Checkpoint 没有 Trace 字段；用 run_id 的稳定摘要补齐，
            # 这样旧快照恢复后仍能在同一条可追溯链上继续，而不是随机切换身份。
            raw_trace_id = f"trace-restored-{hashlib.sha256(restored['request'].run_id.encode('utf-8')).hexdigest()[:24]}"
        if not isinstance(raw_trace_id, str) or not raw_trace_id.strip():
            raise ValueError("Checkpoint trace_id 必须是非空字符串")
        restored["trace_id"] = raw_trace_id
        trace_events_payload = payload.get("trace_events", [])
        if not isinstance(trace_events_payload, list) or not all(
            isinstance(item, Mapping) for item in trace_events_payload
        ):
            raise ValueError("Checkpoint trace_events 必须是对象数组")
        restored_trace_events = [TraceEvent.model_validate(item) for item in trace_events_payload]
        for expected, event in enumerate(restored_trace_events, start=1):
            if event.sequence != expected:
                raise ValueError("Checkpoint trace_events 序号必须连续")
            if event.trace_id != raw_trace_id or event.run_id != restored["request"].run_id:
                raise ValueError("Checkpoint trace_events 的 run/trace 身份不一致")
        restored["trace_events"] = restored_trace_events
        if isinstance(payload.get("stage"), str):
            restored["stage"] = PEVRStage(payload["stage"])
        elif payload.get("stage") is not None:
            raise ValueError("Checkpoint stage 必须是字符串或 null")

        def require_list(key: str) -> list[Any]:
            """严格读取列表字段；字符串或坏项不能被 list()/过滤静默改写。"""

            value = payload.get(key)
            if not isinstance(value, list):
                raise ValueError(f"Checkpoint {key} 必须是数组")
            return value

        trace_payload = require_list("stage_trace")
        if not all(isinstance(item, Mapping) for item in trace_payload):
            raise ValueError("Checkpoint stage_trace 包含非对象项")
        restored["stage_trace"] = [PEVRTraceEvent.model_validate(item) for item in trace_payload]
        trace_stages = [item.stage for item in restored["stage_trace"]]
        if len(trace_stages) != len(set(trace_stages)):
            raise ValueError("Checkpoint stage_trace 包含重复阶段")
        trace_positions = [PEVR_STAGE_ORDER.index(stage) for stage in trace_stages]
        if trace_positions != sorted(trace_positions):
            raise ValueError("Checkpoint stage_trace 顺序非法")
        model_types: dict[str, type[BaseModel]] = {
            "contract": TaskContract,
            "retrieval_result": ToolResult,
            "plan": PlanTasksOutput,
            "plan_validation": PlanValidationResult,
            "derived_plan": SimulationPlan,
            "verification": ObservationVerification,
            "final_report": PEVRRunReport,
            "model_version": ModelVersionRecord,
        }
        for key, model_type in model_types.items():
            value = payload.get(key)
            if isinstance(value, Mapping):
                restored[key] = model_type.model_validate(value)
            elif value is None:
                restored[key] = None
            else:
                raise ValueError(f"Checkpoint {key} 必须是对象或 null")

        hitl_payload = payload.get("hitl_interrupt")
        if isinstance(hitl_payload, Mapping):
            restored["hitl_interrupt"] = HITLInterrupt.model_validate(hitl_payload)
        elif hitl_payload is None:
            restored["hitl_interrupt"] = None
        else:
            raise ValueError("Checkpoint hitl_interrupt 必须是对象或 null")
        grant_payload = payload.get("approval_grant")
        if isinstance(grant_payload, Mapping):
            restored["approval_grant"] = ApprovalGrant.model_validate(grant_payload)
        elif grant_payload is None:
            restored["approval_grant"] = None
        else:
            raise ValueError("Checkpoint approval_grant 必须是对象或 null")
        for key in ("approval_id", "approval_checkpoint_id"):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"Checkpoint {key} 必须是非空字符串或 null")
            restored[key] = value

        rag_payload = require_list("rag_evidence")
        result_payload = require_list("tool_results")
        task_id_payload = require_list("tool_task_ids")
        observation_payload = require_list("observations")
        provenance_payload = require_list("resource_provenance")
        for key, values in (
            ("rag_evidence", rag_payload),
            ("tool_results", result_payload),
            ("observations", observation_payload),
            ("resource_provenance", provenance_payload),
        ):
            if not all(isinstance(item, Mapping) for item in values):
                raise ValueError(f"Checkpoint {key} 包含非对象项")
        if not all(item is None or isinstance(item, str) for item in task_id_payload):
            raise ValueError("Checkpoint tool_task_ids 只能包含字符串或 null")
        if len(result_payload) != len(task_id_payload):
            raise ValueError("Checkpoint tool_results 与 tool_task_ids 数量不一致")
        restored["rag_evidence"] = [ContextEvidence.model_validate(item) for item in rag_payload]
        restored["tool_results"] = [ToolResult.model_validate(item) for item in result_payload]
        restored["tool_task_ids"] = list(task_id_payload)
        restored["observations"] = [Observation.model_validate(item) for item in observation_payload]
        restored["resource_provenance"] = [
            TaskResourceProvenance.model_validate(item) for item in provenance_payload
        ]
        notes = require_list("plan_normalization_notes")
        if not all(isinstance(item, str) for item in notes):
            raise ValueError("Checkpoint plan_normalization_notes 只能包含字符串")
        restored["plan_normalization_notes"] = list(notes)
        run_state_payload = payload.get("run_state")
        if not isinstance(run_state_payload, Mapping):
            raise ValueError("Checkpoint run_state 必须是对象")
        restored["run_state"] = RunState.model_validate(run_state_payload)
        budget_payload = payload.get("budget_usage")
        if not isinstance(budget_payload, Mapping):
            raise ValueError("Checkpoint budget_usage 必须是对象")
        restored["budget_usage"] = BudgetUsage.model_validate(budget_payload)
        model_call_count = payload.get("model_call_count")
        if (
            isinstance(model_call_count, bool)
            or not isinstance(model_call_count, int)
            or model_call_count < 0
        ):
            raise ValueError("Checkpoint model_call_count 必须是非负整数")
        restored["model_call_count"] = model_call_count
        return restored

    @staticmethod
    def _is_terminal_graph_state(state: PEVRGraphState) -> bool:
        """仅当最终报告和验证都已保存时才可无模型/无工具直接返回。"""

        return (
            isinstance(state.get("final_report"), PEVRRunReport)
            and isinstance(state.get("run_state"), RunState)
            and state["run_state"].status is RunStatus.COMPLETED
            and isinstance(state.get("verification"), ObservationVerification)
        )

    @staticmethod
    def _result_from_graph_state(state: PEVRGraphState, request: PEVRRequest) -> PEVRRunResult:
        """从终态快照恢复完整结果，重复调用不重新派发副作用。"""

        run_state = state.get("run_state")
        report = state.get("final_report")
        verification = state.get("verification")
        if not isinstance(run_state, RunState) or not isinstance(report, PEVRRunReport) or not isinstance(verification, ObservationVerification):
            raise PEVRExecutionError(PEVRStage.FINISH, "checkpoint_terminal_state_invalid", "终态 Checkpoint 不完整")
        return PEVRRunResult(
            request=request,
            trace_id=str(state.get("trace_id") or request.trace_id or ""),
            report=report,
            run_state=run_state,
            stage_trace=list(state.get("stage_trace", [])),
            tool_results=list(state.get("tool_results", [])),
            observations=list(state.get("observations", [])),
            verification=verification,
            resource_provenance=list(state.get("resource_provenance", [])),
            trace_events=list(state.get("trace_events", [])),
        )

    def _mark_stage(self, state: PEVRGraphState, stage: PEVRStage) -> list[PEVRTraceEvent]:
        """追加节点完成事件；节点顺序由图边保证，序号只用于报告审计。"""

        now = self._clock()
        trace = list(state.get("stage_trace", []))
        trace.append(
            PEVRTraceEvent(
                sequence=len(trace) + 1,
                stage=stage,
                started_at=now,
                finished_at=now,
            )
        )
        return trace

    @staticmethod
    def _budget_limits(contract: TaskContract | None):
        """在理解节点前使用入口预算，之后严格使用 LLM 输出的合同预算。"""

        from agent.planning import ExecutionBudgets

        return contract.budgets if contract is not None else ExecutionBudgets(**PEVRGraphRunner.ENTRY_BUDGETS)

    @staticmethod
    def _requested_output_tokens(
        request: PEVRRequest,
        limits: Any,
        usage: BudgetUsage,
    ) -> int:
        """把单节点输出上限收紧到累计预算剩余值，避免最后一个节点自拒绝。"""

        remaining = max(1, limits.max_output_tokens - usage.output_tokens)
        return min(request.requested_output_tokens, limits.max_output_tokens, remaining)

    @staticmethod
    def _node_output_or_fail(result: Any, stage: PEVRStage, label: str) -> Any:
        """把 P0-05 节点的 route 统一收口，禁止继续使用空输出。"""

        if result.route is not NodeRoute.SUCCESS or result.output is None:
            raise PEVRExecutionError(
                stage,
                result.reason_code or f"{label}_failed",
                result.reason or f"{label} 节点没有成功输出",
            )
        return result.output

    def _append_model_trace(
        self,
        state: PEVRGraphState,
        result: Any,
        *,
        node: str | None = None,
        task_id: str | None = None,
    ) -> TraceEvent:
        """把一次 P0-05 模型节点结果即时写入图状态和持久化 Trace。"""

        request = state["request"]
        route = str(getattr(getattr(result, "route", None), "value", getattr(result, "route", "failed")))
        status = "completed" if route == "success" else "failed"
        before = getattr(result, "usage_before", None)
        after = getattr(result, "usage_after", None)
        input_before = getattr(before, "input_tokens", None)
        input_after = getattr(after, "input_tokens", None)
        output_before = getattr(before, "output_tokens", None)
        output_after = getattr(after, "output_tokens", None)
        input_tokens = max(0, input_after - input_before) if isinstance(input_after, int) and isinstance(input_before, int) else None
        output_tokens = max(0, output_after - output_before) if isinstance(output_after, int) and isinstance(output_before, int) else None
        error = None
        if status != "completed":
            error = TraceError(
                category="budget" if route == "fallback" else "model",
                code=str(getattr(result, "reason_code", None) or "model_node_failed"),
                message=str(getattr(result, "reason", None) or "模型节点没有成功输出"),
                details={"route": route},
            )
        started_at = getattr(result, "started_at", self._clock())
        finished_at = getattr(result, "finished_at", started_at)
        event = TraceEvent(
            trace_id=state.get("trace_id") or request.trace_id or "",
            run_id=request.run_id,
            sequence=len(state.get("trace_events", [])) + 1,
            event_type="model",
            status=status,  # type: ignore[arg-type]
            node=node or str(getattr(getattr(result, "node_name", None), "value", getattr(result, "node_name", "model"))),
            task_id=task_id,
            model_version=getattr(result, "model_alias", None),
            prompt_id=getattr(result, "prompt_id", None),
            prompt_version=getattr(result, "prompt_version", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None),
            latency_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            started_at=started_at,
            finished_at=finished_at,
            parameters_digest=getattr(result, "context_digest", None),
            error=error,
            metadata={
                "route": route,
                "reason_code": getattr(result, "reason_code", None),
                "estimated_input_tokens": getattr(result, "estimated_input_tokens", 0),
            },
        )
        state["trace_events"] = self._append_trace_events(state, [event])
        return event

    def _append_tool_trace(
        self,
        state: PEVRGraphState,
        result: ToolResult,
        *,
        node: PEVRStage,
        task_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        recovered: bool = False,
    ) -> TraceEvent:
        """把 ToolResult 的版本、参数摘要、错误和证据即时写入 Trace。"""

        request = state["request"]
        status_map = {
            ToolResultStatus.SUCCESS: "completed",
            ToolResultStatus.FAILED: "failed",
            ToolResultStatus.TIMEOUT: "timeout",
            ToolResultStatus.DENIED: "denied",
        }
        status = status_map[result.status]
        error = None
        if result.error is not None:
            category = getattr(result.error.category, "value", result.error.category)
            error = TraceError(
                category=str(category),
                code=result.error.code,
                message=result.error.message,
                retryable=result.error.retryable,
                details=dict(result.error.details),
            )
        event = TraceEvent(
            trace_id=state.get("trace_id") or request.trace_id or "",
            run_id=request.run_id,
            sequence=len(state.get("trace_events", [])) + 1,
            event_type="tool",
            status=status,  # type: ignore[arg-type]
            node=node.value,
            task_id=task_id,
            tool_name=result.tool_name.value,
            tool_version=result.tool_version,
            latency_ms=max(0, int((result.finished_at - result.started_at).total_seconds() * 1000)),
            started_at=result.started_at,
            finished_at=result.finished_at,
            parameters_digest=canonical_json_digest(parameters) if parameters is not None else None,
            input_digest=result.input_digest,
            output_digest=result.output_digest,
            error=error,
            evidence_refs=list(result.evidence_refs),
            metadata={
                "call_id": result.call_id,
                "effect_id": result.effect_id,
                "idempotency_key": result.idempotency_key,
                "recovered": recovered,
                **result.audit_metadata,
            },
        )
        state["trace_events"] = self._append_trace_events(state, [event])
        return event

    def _guard_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """执行入口长度、角色、环境和工具审批声明检查。"""

        request = state["request"]
        if request.principal_role is not UserRole.OPERATOR:
            raise PEVRExecutionError(PEVRStage.GUARD, "role_not_allowed", "正常执行必须使用 operator")
        if self.security_required and request.principal is None:
            raise PEVRExecutionError(
                PEVRStage.GUARD,
                "principal_required",
                "安全 PEVR 必须使用已验签 Principal",
            )
        dispatch_spec = self.registry.get(ToolName.DISPATCH_SIMULATION).spec
        if not dispatch_spec.requires_approval:
            raise PEVRExecutionError(
                PEVRStage.GUARD,
                "dispatch_approval_contract_missing",
                "dispatch_simulation 的 requires_approval 声明缺失",
            )
        # 这里不自动批准；只验证调用上下文能否表达可信审批。真正的拒绝在
        # Executor 即将调用副作用工具前再次检查，避免 Planner 改写审批事实。
        return {"stage": PEVRStage.GUARD, "stage_trace": self._mark_stage(state, PEVRStage.GUARD)}

    def _understand_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """用 Fast/Smart 网关把自然语言订单冻结为 TaskContract。"""

        request = state["request"]
        snapshot = self.snapshot_provider.get_snapshot(request.environment_ref)
        context = build_node_context(
            node_name=PromptNodeName.UNDERSTAND_GOAL,
            request_id=f"{request.run_id}:understand",
            node_input={
                "raw_request": request.raw_request,
                "environment_ref": snapshot.environment_ref,
                "environment_snapshot": {
                    "state_version": snapshot.state_version,
                    "map_width": snapshot.map_width,
                    "map_height": snapshot.map_height,
                    "location_ids": sorted(snapshot.location_positions),
                    "blocked_cells": [item.model_dump(mode="json") for item in snapshot.blocked_cells],
                },
                "available_orders": [item.model_dump(mode="json") for item in snapshot.orders],
                "fixed_execution_defaults": {
                    "max_total_seconds": self.ENTRY_BUDGETS["max_total_seconds"],
                    "max_input_tokens": self.ENTRY_BUDGETS["max_input_tokens"],
                    "max_output_tokens": self.ENTRY_BUDGETS["max_output_tokens"],
                    "minimum_battery_percent": 20,
                    "maximum_load_kg": 100,
                    "enforce_time_windows": True,
                    "max_tool_steps": 8,
                    "max_replans": 2,
                    "max_retries": 2,
                },
            },
            budget_limits=self._budget_limits(None),
            budget_usage=state["budget_usage"],
            requested_output_tokens=self._requested_output_tokens(
                request,
                self._budget_limits(None),
                state["budget_usage"],
            ),
            generated_at=self._clock(),
        )
        result = understand_goal(self.provider, context)
        self._append_model_trace(state, result, node=PromptNodeName.UNDERSTAND_GOAL.value)
        contract = cast(TaskContract, self._node_output_or_fail(result, PEVRStage.UNDERSTAND, "understand_goal"))
        self._validate_contract_against_snapshot(contract, snapshot)
        now = self._clock()
        run_state = RunState(
            run_id=request.run_id,
            status=RunStatus.PLANNING,
            plan_version=1,
            task_contract=contract,
            plan_tasks=[],
            amr_states=[item.model_copy(deep=True) for item in snapshot.amrs],
            orders=[item.model_copy(deep=True) for item in contract.orders],
            observations=[],
            current_task_id=None,
            completed_task_ids=[],
            failed_task_ids=[],
            created_at=now,
            updated_at=now,
            replan_count=0,
        )
        ensure_run = getattr(self.checkpoint_store, "ensure_run", None)
        if callable(ensure_run):
            # 只有合同和初始 RunState 都通过 Pydantic 后才创建/绑定 PostgreSQL
            # 运行行，避免数据库留下无法恢复的自然语言半成品。
            ensure_run(request.run_id, contract)
        return {
            "stage": PEVRStage.UNDERSTAND,
            "stage_trace": self._mark_stage(state, PEVRStage.UNDERSTAND),
            "contract": contract,
            "run_state": run_state,
            "budget_usage": result.usage_after,
            "model_call_count": state.get("model_call_count", 0) + 1,
        }

    @staticmethod
    def _validate_contract_against_snapshot(contract: TaskContract, snapshot: EnvironmentSnapshot) -> None:
        """确认 LLM 没有篡改固定 seed 的订单、地点和环境身份。"""

        if contract.environment_ref != snapshot.environment_ref:
            raise PEVRExecutionError(PEVRStage.UNDERSTAND, "environment_ref_mismatch", "合同环境与固定快照不一致")
        snapshot_orders = {item.order_id: item for item in snapshot.orders}
        for order in contract.orders:
            if order.order_id not in snapshot_orders or order != snapshot_orders[order.order_id]:
                raise PEVRExecutionError(
                    PEVRStage.UNDERSTAND,
                    "order_snapshot_mismatch",
                    f"合同订单不是固定快照中的原始订单: {order.order_id}",
                )
            for location_id in (order.pickup, order.dropoff):
                if location_id not in snapshot.location_positions:
                    raise PEVRExecutionError(
                        PEVRStage.UNDERSTAND,
                        "location_not_found",
                        f"订单 {order.order_id} 引用了未知工位: {location_id}",
                    )
        if contract.constraints.map_width != snapshot.map_width or contract.constraints.map_height != snapshot.map_height:
            raise PEVRExecutionError(PEVRStage.UNDERSTAND, "map_size_mismatch", "合同地图尺寸与固定快照不一致")
        if contract.constraints.blocked_cells != snapshot.blocked_cells:
            raise PEVRExecutionError(PEVRStage.UNDERSTAND, "blocked_cells_mismatch", "合同封路与固定环境快照不一致")
        if contract.missing_information:
            raise PEVRExecutionError(PEVRStage.UNDERSTAND, "missing_information", "正常闭环不能带未解决的执行必需信息")

    def _retrieve_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """通过真实 P0-12 RAG 工具取得 ACL 过滤后的冻结证据。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        run_state = cast(RunState, state["run_state"])
        query = (
            f"{contract.goal}；请参考仓储运输 SOP、交通冲突、电量安全余量、"
            "Validator 和运输完成条件。"
        )
        retrieve_arguments = {
            "query": query,
            "top_k": 5,
            "role_scope": request.principal_role,
        }
        result = self.registry.execute(
            ToolName.RETRIEVE_KNOWLEDGE,
            retrieve_arguments,
            role=request.principal_role,
            call_id=f"{request.run_id}:retrieve",
        )
        self._append_tool_trace(
            state,
            result,
            node=PEVRStage.RETRIEVE,
            parameters=retrieve_arguments,
        )
        if result.status is not ToolResultStatus.SUCCESS:
            raise PEVRExecutionError(
                PEVRStage.RETRIEVE,
                result.error.code if result.error is not None else "retrieve_failed",
                result.error.message if result.error is not None else "RAG 检索失败",
            )
        response = RetrievalResponse.model_validate(result.output)
        if response.status is not RetrievalStatus.ANSWERABLE:
            raise PEVRExecutionError(PEVRStage.RETRIEVE, "insufficient_evidence", response.reason)
        rag_evidence = response.to_context_evidence(collected_at=result.finished_at)
        observation = self._observation_from_tool(result, task_id=None)
        observations = [*state.get("observations", []), observation]
        updated_state = self._replace_run_state(run_state, observations=observations, status=RunStatus.PLANNING)
        tool_results = [*state.get("tool_results", []), result]
        return {
            "stage": PEVRStage.RETRIEVE,
            "stage_trace": self._mark_stage(state, PEVRStage.RETRIEVE),
            "retrieval_result": result,
            "rag_evidence": rag_evidence,
            "run_state": updated_state,
            "observations": observations,
            "tool_results": tool_results,
            "tool_task_ids": [*state.get("tool_task_ids", []), None],
            "budget_usage": self._add_tool_usage(state["budget_usage"], result),
        }

    def _plan_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """让模型只生成 DAG；执行权仍留在下一节点的确定性门禁之后。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        run_state = cast(RunState, state["run_state"])
        specs = [
            {
                "tool_name": spec.tool_name.value,
                "requires_approval": spec.requires_approval,
                "allowed_roles": [role.value for role in spec.allowed_roles],
                # Planner 已经收到实时 PlanTasksOutput Schema；这里不重复嵌入
                # 巨大的嵌套 JSON Schema，只提供封闭参数名和跨任务数据流规则，
                # 以免在 8K Fast 上下文中把确定性预算门禁推入 fallback。
                "required_parameters": sorted(TOOL_ARGUMENT_POLICIES[spec.tool_name].required),
                "optional_parameters": sorted(TOOL_ARGUMENT_POLICIES[spec.tool_name].optional),
            }
            for spec in self.registry.specs()
            if spec.tool_name in {*NORMAL_PEVR_TOOL_CHAIN}
        ]
        plan_input: dict[str, Any] = {
            "run_id": request.run_id,
            "simulation_seed": request.seed,
            "task_contract": contract.model_dump(mode="json"),
            "order_ids": [order.order_id for order in contract.orders],
            "fixed_execution_facts": {
                "environment_ref": contract.environment_ref,
                "order_ids": [order.order_id for order in contract.orders],
                "blocked_cells": [
                    cell.model_dump(mode="json")
                    for cell in contract.constraints.blocked_cells
                ],
                "latest_deadline": max(order.deadline for order in contract.orders),
                "ruleset_version": "p0-10.v1",
                "simulation_seed": request.seed,
            },
            "available_tool_contracts": specs,
            "required_normal_chain": [tool.value for tool in NORMAL_PEVR_TOOL_CHAIN],
            "dataflow_rules": {
                "route_assignments": "{\"$ref\": \"task:<allocate_task_id>/output/assignments\"}",
                "validate_plan": "{\"$ref\": \"derived:simulation_plan\"}",
                "dispatch_plan": "{\"$ref\": \"derived:simulation_plan\"}",
            },
            "normal_path_rule": "只生成四个工具任务；retrieve 已在本节点之前完成；不要生成 approval、query 或任何额外工具任务。",
        }
        context = build_node_context(
            node_name=PromptNodeName.PLAN_TASKS,
            request_id=f"{request.run_id}:plan",
            node_input=plan_input,
            budget_limits=contract.budgets,
            budget_usage=state["budget_usage"],
            requested_output_tokens=self._requested_output_tokens(
                request,
                contract.budgets,
                state["budget_usage"],
            ),
            run_state=run_state,
            rag_evidence=state.get("rag_evidence", []),
            tool_evidence=[self._tool_context_evidence(cast(ToolResult, state["retrieval_result"]))],
            generated_at=self._clock(),
        )
        result = plan_tasks(self.provider, context)
        self._append_model_trace(state, result, node=PromptNodeName.PLAN_TASKS.value)
        raw_plan = cast(PlanTasksOutput, self._node_output_or_fail(result, PEVRStage.PLAN, "plan_tasks"))
        plan, normalization_notes = canonicalize_normal_pevr_plan(
            raw_plan,
            contract=contract,
            expected_seed=request.seed,
        )
        usage_after = result.usage_after
        plan_call_count = 1
        first_validation = validate_normal_pevr_plan(
            contract,
            plan,
            tool_specs=self.registry.specs(),
            expected_seed=request.seed,
        )
        if not first_validation.valid:
            # Fast 实测会偶发把固定 seed/环境或链式参数写错。这里只允许一次
            # “模型语义修复”，并把确定性错误逐条反馈；修复结果仍必须进入下一
            # validate 节点，绝不把 Python 自动改值当作通过 Validator。
            repair_context = build_node_context(
                node_name=PromptNodeName.PLAN_TASKS,
                request_id=f"{request.run_id}:plan:semantic-repair:1",
                node_input={
                    **plan_input,
                    "semantic_repair": {
                        "attempt": 1,
                        "rejected_plan": plan.model_dump(mode="json"),
                        "deterministic_errors": [
                            item.model_dump(mode="json")
                            for item in first_validation.errors
                        ],
                        "instruction": "只修复列出的确定性错误并返回完整四任务计划，不得省略 Validator 或 dispatch。",
                    },
                },
                budget_limits=contract.budgets,
                budget_usage=usage_after,
                requested_output_tokens=self._requested_output_tokens(
                    request,
                    contract.budgets,
                    usage_after,
                ),
                run_state=run_state,
                rag_evidence=state.get("rag_evidence", []),
                tool_evidence=[self._tool_context_evidence(cast(ToolResult, state["retrieval_result"]))],
                generated_at=self._clock(),
            )
            repair_result = plan_tasks(self.provider, repair_context)
            self._append_model_trace(state, repair_result, node=PromptNodeName.PLAN_TASKS.value)
            repaired_raw = cast(
                PlanTasksOutput,
                self._node_output_or_fail(
                    repair_result,
                    PEVRStage.PLAN,
                    "plan_tasks semantic repair",
                ),
            )
            plan, repair_notes = canonicalize_normal_pevr_plan(
                repaired_raw,
                contract=contract,
                expected_seed=request.seed,
            )
            normalization_notes = [
                *normalization_notes,
                "semantic_repair:1",
                *repair_notes,
            ]
            usage_after = repair_result.usage_after
            plan_call_count = 2
        planned_state = self._replace_run_state(
            run_state,
            plan_version=plan.plan_version,
            plan_tasks=list(plan.tasks),
            status=RunStatus.VALIDATING,
            current_task_id=None,
        )
        return {
            "stage": PEVRStage.PLAN,
            "stage_trace": self._mark_stage(state, PEVRStage.PLAN),
            "plan": plan,
            "plan_normalization_notes": normalization_notes,
            "run_state": planned_state,
            "budget_usage": usage_after,
            "model_call_count": state.get("model_call_count", 0) + plan_call_count,
        }

    def _validate_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """在 Executor 前执行唯一的 Planner DAG 硬门禁。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        plan = cast(PlanTasksOutput, state["plan"])
        run_state = cast(RunState, state["run_state"])
        # 首轮计划固定 version=1；LocalReplanner 产出的 v2+ 必须走重规划门禁，
        # 否则会把合法替换子图误判为 plan_version_invalid / 预填证据。
        if plan.plan_version > 1 or run_state.completed_task_ids:
            validation = validate_replanned_pevr_plan(
                contract,
                plan,
                completed_task_ids=run_state.completed_task_ids,
                tool_specs=self.registry.specs(),
                expected_seed=request.seed,
                expected_plan_version=plan.plan_version,
            )
        else:
            validation = validate_normal_pevr_plan(
                contract,
                plan,
                tool_specs=self.registry.specs(),
                expected_seed=request.seed,
            )
        if not validation.valid:
            detail = "; ".join(f"{item.code}: {item.message}" for item in validation.errors)
            raise PEVRExecutionError(
                PEVRStage.VALIDATE,
                "plan_validation_failed",
                detail,
                fault=FaultClassifier.classify(
                    {
                        "code": "plan_validation_failed",
                        "message": detail,
                        "errors": [item.model_dump(mode="json") for item in validation.errors],
                    },
                    stage=PEVRStage.VALIDATE.value,
                ),
            )
        run_state = self._replace_run_state(run_state, status=RunStatus.VALIDATING)
        return {
            "stage": PEVRStage.VALIDATE,
            "stage_trace": self._mark_stage(state, PEVRStage.VALIDATE),
            "plan_validation": validation,
            "run_state": run_state,
        }

    def _registry_execute(
        self,
        tool_name: ToolName,
        arguments: Mapping[str, Any],
        *,
        role: UserRole,
        call_id: str,
        idempotency_key: str | None = None,
        principal: Principal | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        """调用真实或旧版 fake Registry，并在支持时传入业务幂等键。"""

        kwargs: dict[str, Any] = {"role": role, "call_id": call_id}
        try:
            parameters = inspect.signature(self.registry.execute).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_key = "idempotency_key" in parameters
        if accepts_key and idempotency_key is not None:
            kwargs["idempotency_key"] = idempotency_key
        if "principal" in parameters and principal is not None:
            kwargs["principal"] = principal
        if "approval_grant" in parameters and approval_grant is not None:
            kwargs["approval_grant"] = approval_grant
        result = self.registry.execute(tool_name, arguments, **kwargs)
        if idempotency_key is not None and not accepts_key and result.idempotency_key != idempotency_key:
            # P0-13 的 fake 仍返回 call_id；进入 P0-14 持久化边界后统一修正为
            # 三元组键，避免兼容测试造出第二套副作用身份。
            result = result.model_copy(update={"idempotency_key": idempotency_key})
        return result

    def _task_input_digest(
        self,
        task: PlanTask,
        arguments: Mapping[str, Any],
        *,
        role: UserRole,
    ) -> str:
        """计算与 ToolExecutor 相同语义的输入指纹，写入预留账本。"""

        del role  # ToolResult.input_digest 只覆盖规范化参数；角色另有 principal_role。

        parsed_arguments: Any = dict(arguments)
        definition = self.registry.get(task.tool_name)
        input_model = getattr(definition, "input_model", None)
        if input_model is not None:
            try:
                parsed_arguments = input_model.model_validate(arguments)
            except (TypeError, ValueError, ValidationError):
                # 真正的输入失败仍交给 Registry 返回 ToolResult；这里只需有一条
                # 稳定指纹，不能因为预检失败而绕过 Effect Ledger 约束。
                parsed_arguments = dict(arguments)
        return canonical_json_digest(parsed_arguments)

    def _prepare_side_effect(
        self,
        *,
        request: PEVRRequest,
        task: PlanTask,
        plan_version: int,
        arguments: Mapping[str, Any],
    ) -> tuple[str, ToolResult | None]:
        """预留/恢复一个副作用；返回业务 key 及可直接复用的结果。"""

        key = make_effect_idempotency_key(request.run_id, plan_version, task.task_id)
        if self.checkpoint_store is None:
            return key, None
        definition = self.registry.get(task.tool_name)
        if not definition.spec.has_side_effects:
            return key, None
        input_digest = self._task_input_digest(task, arguments, role=request.principal_role)
        reservation = self.checkpoint_store.reserve_effect(
            run_id=request.run_id,
            plan_version=plan_version,
            task_id=task.task_id,
            tool_name=task.tool_name,
            call_id=f"{request.run_id}:plan:{plan_version}:task:{task.task_id}",
            input_digest=input_digest,
            arguments=arguments,
            now=self._clock(),
        )
        if reservation.owner:
            return key, None
        assessment = self._recovery.assess(reservation.entry)
        if assessment.decision is RecoveryDecision.SKIP_COMPLETED:
            result = reservation.entry.result or assessment.external.result
            if result is None:
                raise PEVRExecutionError(
                    PEVRStage.EXECUTE,
                    "recovery_result_missing",
                    assessment.reason,
                )
            if reservation.entry.result is None:
                self.checkpoint_store.complete_effect(
                    key,
                    result,
                    external_effect_id=assessment.external.external_effect_id,
                    reconciled=True,
                    recovery_note=assessment.reason,
                )
            return key, result
        if assessment.decision is not RecoveryDecision.CONTINUE:
            if assessment.decision is RecoveryDecision.COMPENSATE:
                fail_effect = getattr(self.checkpoint_store, "fail_effect", None)
                if callable(fail_effect):
                    fail_effect(
                        key,
                        note=assessment.reason,
                        compensation_required=True,
                    )
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                f"recovery_{assessment.decision.value}",
                assessment.reason,
                fault=FaultClassifier.classify(
                    {
                        "code": "state_conflict",
                        "message": assessment.reason,
                        "details": {
                            "recovery_decision": assessment.decision.value,
                            "idempotency_key": key,
                        },
                    },
                    stage=PEVRStage.EXECUTE.value,
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    idempotent=definition.spec.idempotent,
                    has_side_effects=definition.spec.has_side_effects,
                ),
            )
        # 外部明确 not_found 时沿用原唯一键继续；新结果仍覆盖同一 reserved 行。
        return key, None

    def _hitl_digests(
        self,
        *,
        plan: PlanTasksOutput,
        validation: PlanValidationResult,
    ) -> tuple[str, str]:
        """只从当前内存中的完整计划和确定性 Validator 结果计算审批摘要。"""

        return canonical_json_digest(plan), canonical_json_digest(validation)

    def _request_hitl_interrupt(
        self,
        *,
        state: PEVRGraphState,
        task: PlanTask,
        plan: PlanTasksOutput,
        validation: PlanValidationResult,
        run_state: RunState,
    ) -> HITLInterrupt:
        """创建 pending 审批并先保存 waiting Checkpoint，再向上抛出 interrupt。"""

        request = state["request"]
        if request.principal is None:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "principal_required",
                "安全 HITL 必须绑定已验签 Principal",
            )
        if self.hitl_store is None:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "hitl_store_unavailable",
                "安全 HITL 未配置审批存储，拒绝继续执行副作用",
            )
        plan_digest, validator_digest = self._hitl_digests(plan=plan, validation=validation)
        checkpoint_id = f"cp_{uuid4().hex}"
        hitl_request = build_hitl_request(
            run_id=request.run_id,
            task_id=task.task_id,
            plan_version=plan.plan_version,
            requested_by=request.principal.subject,
            reason_code=HITLReason.HIGH_RISK_WRITE,
            reason=f"工具 {task.tool_name.value} 是高风险写操作，需要人工审批",
            checkpoint_id=checkpoint_id,
            plan_digest=plan_digest,
            validator_digest=validator_digest,
            now=self._clock(),
            ttl_seconds=self.hitl_ttl_seconds,
        )
        stored = self.hitl_store.request_approval(hitl_request)
        interrupt = HITLInterrupt(
            run_id=request.run_id,
            task_id=task.task_id,
            approval_id=stored.approval_id,
            checkpoint_id=stored.checkpoint_id,
            reason_code=stored.reason_code,
            created_at=stored.requested_at,
            expires_at=stored.expires_at,
        )
        waiting_state = self._replace_task_state(
            run_state,
            task_id=task.task_id,
            status=PlanTaskStatus.WAITING_APPROVAL,
            current_task_id=task.task_id,
            run_status=RunStatus.WAITING_APPROVAL,
        )
        self._persist_checkpoint(
            {
                **state,
                "stage": PEVRStage.EXECUTE,
                "run_state": waiting_state,
                "hitl_interrupt": interrupt,
                "approval_grant": None,
                "approval_id": interrupt.approval_id,
                "approval_checkpoint_id": interrupt.checkpoint_id,
            },
            stage=PEVRStage.EXECUTE,
        )
        return interrupt

    def _verify_hitl_grant(
        self,
        *,
        state: PEVRGraphState,
        task: PlanTask,
        plan: PlanTasksOutput,
        validation: PlanValidationResult,
        grant: ApprovalGrant,
    ) -> ApprovalGrant:
        """恢复前再次核对存储票据、当前主体、计划和 Validator 摘要。"""

        request = state["request"]
        interrupt = state.get("hitl_interrupt")
        if request.principal is None:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "principal_required",
                "恢复高风险工具必须携带已验签 Principal",
            )
        if not isinstance(interrupt, HITLInterrupt):
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "approval_checkpoint_missing",
                "审批票据没有对应的 waiting_approval Checkpoint",
            )
        if interrupt.approval_id != grant.approval_id or interrupt.task_id != task.task_id:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "approval_checkpoint_mismatch",
                "审批票据与当前等待任务不匹配",
            )
        if self.hitl_store is None:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "hitl_store_unavailable",
                "安全 HITL 未配置审批存储",
            )
        plan_digest, validator_digest = self._hitl_digests(plan=plan, validation=validation)
        try:
            verified = self.hitl_store.verify_grant(
                grant,
                principal=request.principal,
                run_id=request.run_id,
                task_id=task.task_id,
                plan_version=plan.plan_version,
                plan_digest=plan_digest,
                validator_digest=validator_digest,
                now=self._clock(),
            )
        except Exception as exc:
            raise PEVRExecutionError(
                PEVRStage.EXECUTE,
                "approval_invalid",
                "审批票据未通过存储、计划或 Validator 核对",
            ) from exc
        return verified

    def _execute_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """按拓扑顺序执行或恢复任务，并在每个任务后落 Checkpoint。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        plan = cast(PlanTasksOutput, state["plan"])
        validation = cast(PlanValidationResult, state["plan_validation"])
        run_state = cast(RunState, state["run_state"])
        results = list(state.get("tool_results", []))
        task_ids = list(state.get("tool_task_ids", []))
        observations = list(state.get("observations", []))
        resource_provenance = list(state.get("resource_provenance", []))
        usage = state["budget_usage"]
        results_by_task: dict[str, ToolResult] = {
            task_id: result
            for task_id, result in zip(task_ids, results)
            if task_id is not None
        }
        derived_plan = state.get("derived_plan")
        task_by_id = {task.task_id: task for task in plan.tasks}
        completed_ids = set(run_state.completed_task_ids)

        # 如果进程在“外部完成 → Checkpoint 尚未更新”窗口退出，账本结果可能比图
        # 快照更完整；先把它加入本次恢复上下文，仍由下方任务状态机决定是否复用。
        if self.checkpoint_store is not None:
            for task in plan.tasks:
                if task.task_id not in completed_ids or task.task_id in results_by_task:
                    continue
                key = make_effect_idempotency_key(request.run_id, plan.plan_version, task.task_id)
                entry = self.checkpoint_store.get_effect(key)
                if entry is not None and entry.result is not None:
                    results_by_task[task.task_id] = entry.result
                    if task.task_id not in task_ids:
                        task_ids.append(task.task_id)
                        results.append(entry.result)

        for task_id in validation.topological_order:
            task = task_by_id[task_id]
            if any(dependency not in completed_ids for dependency in task.dependencies):
                raise PEVRExecutionError(PEVRStage.EXECUTE, "dependency_not_completed", task.task_id)

            # 已完成节点是恢复锚点：结果必须来自 Checkpoint 或 Effect Ledger，缺失
            # 时宁可停止也不猜测，更不能为了补齐列表重新执行副作用。
            if task_id in completed_ids:
                existing = results_by_task.get(task_id)
                if existing is None:
                    raise PEVRExecutionError(
                        PEVRStage.EXECUTE,
                        "completed_task_result_missing",
                        f"已完成任务 {task_id} 缺少持久化 ToolResult",
                    )
                if task.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES and derived_plan is None:
                    route_arguments = self._materialize_arguments(
                        task,
                        results_by_task=results_by_task,
                        derived_plan=derived_plan,
                        contract=contract,
                    )
                    derived_plan = self._build_simulation_plan(
                        contract,
                        RoutePlanResponse.model_validate(existing.output),
                        route_arguments,
                    )
                continue

            spec = self.registry.get(task.tool_name).spec
            approved_grant: ApprovalGrant | None = None
            if spec.requires_approval:
                # 这是副作用工具的最后一道 guard；Planner、Prompt、request body
                # 和 legacy bool 都不能在安全模式下伪造批准事实。
                if self.security_required or request.principal is not None:
                    candidate = request.approval_grant or state.get("approval_grant")
                    if candidate is None:
                        existing_interrupt = state.get("hitl_interrupt")
                        if isinstance(existing_interrupt, HITLInterrupt):
                            if existing_interrupt.expires_at <= self._clock():
                                raise PEVRExecutionError(
                                    PEVRStage.EXECUTE,
                                    "approval_expired",
                                    "HITL 审批已过期，拒绝继续执行副作用",
                                )
                            get_request = getattr(self.hitl_store, "get_request", None)
                            stored_request = (
                                get_request(existing_interrupt.approval_id)
                                if callable(get_request)
                                else None
                            )
                            if stored_request is not None and stored_request.status is HITLStatus.REJECTED:
                                raise PEVRExecutionError(
                                    PEVRStage.EXECUTE,
                                    "approval_rejected",
                                    "HITL 审批已拒绝，拒绝继续执行副作用",
                                )
                            raise PEVRInterrupt(existing_interrupt)
                        pause_state = {
                            **state,
                            "run_state": run_state,
                            "tool_results": results,
                            "tool_task_ids": task_ids,
                            "derived_plan": derived_plan,
                            "observations": observations,
                            "resource_provenance": resource_provenance,
                            "budget_usage": usage,
                        }
                        interrupt = self._request_hitl_interrupt(
                            state=pause_state,
                            task=task,
                            plan=plan,
                            validation=validation,
                            run_state=run_state,
                        )
                        raise PEVRInterrupt(interrupt)
                    approved_grant = self._verify_hitl_grant(
                        state=state,
                        task=task,
                        plan=plan,
                        validation=validation,
                        grant=candidate,
                    )
                    self._active_approval_grant = approved_grant
                elif not request.approval_granted:
                    raise PEVRExecutionError(
                        PEVRStage.EXECUTE,
                        "approval_required",
                        f"工具 {task.tool_name.value} 需要可信审批上下文",
                    )
            run_state = self._replace_task_state(
                run_state,
                task_id=task_id,
                status=PlanTaskStatus.RUNNING,
                current_task_id=task_id,
                run_status=RunStatus.EXECUTING,
            )
            # 让后续失败 Trace 也能定位到尚未完成的当前任务；正常返回仍以
            # 下方的完整 Checkpoint 状态为准，不把这个临时指针当作外部事实。
            state["run_state"] = run_state
            arguments = self._materialize_arguments(
                task,
                results_by_task=results_by_task,
                derived_plan=derived_plan,
                contract=contract,
            )
            idempotency_key, recovered_result = self._prepare_side_effect(
                request=request,
                task=task,
                plan_version=plan.plan_version,
                arguments=arguments,
            )
            retry_suffix = (
                f":retry:{run_state.retry_count}"
                if run_state.retry_count > 0 and not spec.has_side_effects
                else ""
            )
            invocation_key = (
                idempotency_key + retry_suffix
                if not spec.has_side_effects
                else idempotency_key
            )
            result = recovered_result or self._registry_execute(
                task.tool_name,
                arguments,
                role=request.principal_role,
                call_id=(
                    f"{request.run_id}:plan:{plan.plan_version}:task:{task.task_id}"
                    f"{retry_suffix}"
                ),
                idempotency_key=invocation_key,
                principal=request.principal,
                approval_grant=approved_grant,
            )
            self._append_tool_trace(
                state,
                result,
                node=PEVRStage.EXECUTE,
                task_id=task.task_id,
                parameters=arguments,
                recovered=recovered_result is not None,
            )
            if result.status is not ToolResultStatus.SUCCESS:
                if self.checkpoint_store is not None and spec.has_side_effects:
                    self.checkpoint_store.fail_effect(
                        idempotency_key,
                        note=result.error.message if result.error is not None else "工具失败",
                        compensation_required=result.effect_id is not None,
                    )
                raise PEVRExecutionError(
                    PEVRStage.EXECUTE,
                    result.error.code if result.error is not None else "tool_failed",
                    result.error.message if result.error is not None else f"工具 {task.tool_name.value} 失败",
                    fault=FaultClassifier.classify(
                        result,
                        stage=PEVRStage.EXECUTE.value,
                        task_id=task.task_id,
                        tool_name=task.tool_name,
                        idempotent=spec.idempotent,
                        has_side_effects=spec.has_side_effects,
                    ),
                )
            if self.checkpoint_store is not None and spec.has_side_effects and recovered_result is None:
                self.checkpoint_store.complete_effect(
                    idempotency_key,
                    result,
                    external_effect_id=result.effect_id,
                )
            results.append(result)
            task_ids.append(task.task_id)
            results_by_task[task.task_id] = result
            if task.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
                derived_plan = self._build_simulation_plan(
                    contract,
                    RoutePlanResponse.model_validate(result.output),
                    arguments,
                )
            elif task.tool_name is ToolName.VALIDATE_FLEET_PLAN:
                validation_output = ValidationResponse.model_validate(result.output)
                if not validation_output.valid or validation_output.status != "valid" or validation_output.errors:
                    raise PEVRExecutionError(
                        PEVRStage.EXECUTE,
                        "validator_postcondition_failed",
                        "工具返回的 Validator 结果不是 valid=true",
                        fault=FaultClassifier.classify(
                            {
                                "code": "validator_postcondition_failed",
                                "message": "工具返回的 Validator 结果不是 valid=true",
                                "output": result.output,
                            },
                            stage=PEVRStage.EXECUTE.value,
                            task_id=task.task_id,
                            tool_name=task.tool_name,
                        ),
                    )
            elif task.tool_name is ToolName.DISPATCH_SIMULATION:
                simulation = SimulationResult.model_validate(result.output)
                if simulation.status.value != "completed" or any(item.status.value != "completed" for item in simulation.orders):
                    raise PEVRExecutionError(
                        PEVRStage.EXECUTE,
                        "simulation_not_completed",
                        "正常闭环要求仿真完成全部订单",
                        fault=FaultClassifier.classify(
                            {
                                "code": "simulation_not_completed",
                                "message": "正常闭环要求仿真完成全部订单",
                                "output": result.output,
                            },
                            stage=PEVRStage.EXECUTE.value,
                            task_id=task.task_id,
                            tool_name=task.tool_name,
                            idempotent=spec.idempotent,
                            has_side_effects=spec.has_side_effects,
                        ),
                    )
            observation = self._observation_from_tool(result, task_id=task.task_id)
            observations.append(observation)
            completed_ids.add(task_id)
            run_state = self._replace_task_state(
                run_state,
                task_id=task_id,
                status=PlanTaskStatus.COMPLETED,
                current_task_id=None,
                run_status=RunStatus.EXECUTING,
                evidence_refs=result.evidence_refs,
                effect_id=result.effect_id,
                completed_task_ids=sorted(completed_ids),
                observations=observations,
            )
            state["run_state"] = run_state
            usage = self._add_tool_usage(usage, result)
            resource_provenance = build_task_resource_provenance(
                plan,
                tool_results=results,
                tool_task_ids=task_ids,
                contract=contract,
                snapshot=self.snapshot_provider.get_snapshot(contract.environment_ref),
            )
            self._persist_checkpoint(
                {
                    **state,
                    "stage": PEVRStage.EXECUTE,
                    "run_state": run_state,
                    "derived_plan": derived_plan,
                    "tool_results": results,
                    "tool_task_ids": task_ids,
                    "resource_provenance": resource_provenance,
                    "observations": observations,
                    "budget_usage": usage,
                    "hitl_interrupt": None,
                    "approval_grant": None,
                },
                stage=PEVRStage.EXECUTE,
            )

        if derived_plan is None:
            raise PEVRExecutionError(PEVRStage.EXECUTE, "simulation_plan_missing", "路线任务未产生 SimulationPlan")
        return {
            "stage": PEVRStage.EXECUTE,
            "stage_trace": self._mark_stage(state, PEVRStage.EXECUTE),
            "run_state": run_state,
            "derived_plan": derived_plan,
            "tool_results": results,
            "tool_task_ids": task_ids,
            "resource_provenance": resource_provenance,
            "observations": observations,
            "budget_usage": usage,
            "hitl_interrupt": None,
            "approval_grant": None,
            "approval_id": state.get("approval_id"),
            "approval_checkpoint_id": state.get("approval_checkpoint_id"),
        }

    def _verify_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """让 P0-05 Verifier 对照真实仿真 Observation 判断订单完成。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        run_state = cast(RunState, state["run_state"])
        plan = cast(PlanTasksOutput, state["plan"])
        dispatch_task = next(task for task in plan.tasks if task.tool_name is ToolName.DISPATCH_SIMULATION)
        dispatch_index = next(
            index for index, task_id in enumerate(state.get("tool_task_ids", [])) if task_id == dispatch_task.task_id
        )
        dispatch_result = cast(ToolResult, state["tool_results"][dispatch_index])
        dispatch_observation = next(
            observation for observation in state["observations"] if observation.task_id == dispatch_task.task_id
        )
        simulation = SimulationResult.model_validate(dispatch_result.output)
        context = build_node_context(
            node_name=PromptNodeName.VERIFY_OBSERVATION,
            request_id=f"{request.run_id}:verify",
            node_input={
                "task_id": dispatch_task.task_id,
                "completion_criteria": dispatch_task.completion_criteria,
                "observation_id": dispatch_observation.observation_id,
                "observation": {
                    "status": dispatch_observation.status.value,
                    "summary": dispatch_observation.summary,
                    "evidence_refs": dispatch_observation.evidence_refs,
                    "simulation_status": simulation.status.value,
                    "end_time": simulation.end_time,
                    "orders": [item.model_dump(mode="json") for item in simulation.orders],
                    "event_count": len(simulation.events),
                },
                "all_plan_tasks_completed": set(run_state.completed_task_ids) == {task.task_id for task in plan.tasks},
                "expected_decision": "finish",
            },
            budget_limits=contract.budgets,
            budget_usage=state["budget_usage"],
            requested_output_tokens=self._requested_output_tokens(
                request,
                contract.budgets,
                state["budget_usage"],
            ),
            run_state=run_state,
            rag_evidence=state.get("rag_evidence", []),
            tool_evidence=[self._tool_context_evidence(dispatch_result, include_output=True)],
            generated_at=self._clock(),
        )
        result = verify_observation(self.provider, context)
        self._append_model_trace(state, result, node=PromptNodeName.VERIFY_OBSERVATION.value)
        verification = cast(ObservationVerification, self._node_output_or_fail(result, PEVRStage.VERIFY, "verify_observation"))
        expected_orders = {order.order_id for order in contract.orders}
        actual_completed = {
            item.order_id for item in simulation.orders if item.status.value == "completed"
        }
        if not verification.verified or actual_completed != expected_orders:
            raise PEVRExecutionError(PEVRStage.VERIFY, "observation_not_verified", verification.reason)
        if verification.decision not in {VerificationDecision.FINISH, VerificationDecision.CONTINUE}:
            raise PEVRExecutionError(PEVRStage.VERIFY, "verification_decision_not_finish", verification.reason)
        completed_state = self._replace_run_state(
            run_state,
            status=RunStatus.COMPLETED,
            current_task_id=None,
            observations=state["observations"],
        )
        return {
            "stage": PEVRStage.VERIFY,
            "stage_trace": self._mark_stage(state, PEVRStage.VERIFY),
            "run_state": completed_state,
            "verification": verification,
            "budget_usage": result.usage_after,
            "model_call_count": state.get("model_call_count", 0) + 1,
        }

    def _finish_node(self, state: PEVRGraphState) -> dict[str, Any]:
        """生成 LLM 报告，再用确定性事实补齐指标和工具证据索引。"""

        request = state["request"]
        contract = cast(TaskContract, state["contract"])
        run_state = cast(RunState, state["run_state"])
        plan = cast(PlanTasksOutput, state["plan"])
        validation = cast(PlanValidationResult, state["plan_validation"])
        simulation = SimulationResult.model_validate(
            next(
                result.output
                for result in reversed(state["tool_results"])
                if result.tool_name is ToolName.DISPATCH_SIMULATION
            )
        )
        retrieval_response = RetrievalResponse.model_validate(state["retrieval_result"].output)
        all_evidence_refs = self._all_evidence_refs(state["tool_results"])
        citations = list(dict.fromkeys(item.citation for item in retrieval_response.results))
        deterministic_risks = [
            "P0-04 TransportOrder 尚未包含重量字段，本次执行期 payload_kg 按 P0-13 正常链路固定为 1.0kg。"
        ]
        context = build_node_context(
            node_name=PromptNodeName.COMPOSE_REPORT,
            request_id=f"{request.run_id}:finish",
            node_input={
                "run_id": request.run_id,
                "run_status": run_state.status.value,
                "state_version": f"run:{request.run_id}/plan:{run_state.plan_version}",
                "plan_version": run_state.plan_version,
                "verified_completed_order_ids": sorted(item.order_id for item in simulation.orders if item.status.value == "completed"),
                "incomplete_order_ids": sorted(item.order_id for item in simulation.orders if item.status.value != "completed"),
                "evidence_refs": all_evidence_refs,
                "citations": citations,
                "metrics": {
                    "route_count": len(simulation.orders),
                    "simulation_status": simulation.status.value,
                    "simulation_end_time": simulation.end_time,
                    "validator_error_count": 0,
                },
                "unresolved_risks": deterministic_risks,
            },
            budget_limits=contract.budgets,
            budget_usage=state["budget_usage"],
            requested_output_tokens=self._requested_output_tokens(
                request,
                contract.budgets,
                state["budget_usage"],
            ),
            run_state=run_state,
            # 报告节点的 citations/evidence_refs 已在 node_input 中由真实
            # RetrievalResponse 冻结；不再重复注入五段 RAG 正文，避免报告
            # Prompt 超过 Fast 8192 上下文窗口。检索和 Planner 节点仍保留原文。
            rag_evidence=[],
            tool_evidence=[self._tool_context_evidence(result) for result in state["tool_results"]],
            generated_at=self._clock(),
        )
        result = compose_report(self.provider, context)
        self._append_model_trace(state, result, node=PromptNodeName.COMPOSE_REPORT.value)
        llm_report = cast(FinalReport, self._node_output_or_fail(result, PEVRStage.FINISH, "compose_report"))
        expected_orders = {order.order_id for order in contract.orders}
        if (
            llm_report.run_id != request.run_id
            or llm_report.plan_version != run_state.plan_version
            or llm_report.final_status is not FinalReportStatus.COMPLETED
            or set(llm_report.completed_order_ids) != expected_orders
            or llm_report.incomplete_order_ids
            or not set(llm_report.evidence_refs).intersection(all_evidence_refs)
        ):
            raise PEVRExecutionError(PEVRStage.FINISH, "report_fact_mismatch", "LLM 报告与真实闭环事实不一致")
        actual_usage = result.usage_after
        normalized_report = FinalReport.model_validate(
            {
                **llm_report.model_dump(mode="json"),
                "budget_usage": actual_usage.model_dump(mode="json"),
                "evidence_refs": list(dict.fromkeys([*llm_report.evidence_refs, *all_evidence_refs])),
                "unresolved_risks": list(dict.fromkeys([*llm_report.unresolved_risks, *deterministic_risks])),
            }
        )
        tool_evidence = [
            PEVRToolEvidence.from_result(result, task_id=task_id)
            for result, task_id in zip(state["tool_results"], state.get("tool_task_ids", []))
        ]
        metrics = self._build_metrics(state, validation, retrieval_response, simulation)
        report = PEVRRunReport(
            run_id=request.run_id,
            trace_id=state.get("trace_id") or request.trace_id,
            principal_subject=(request.principal.subject if request.principal is not None else None),
            approval_id=state.get("approval_id") or (
                request.approval_grant.approval_id if request.approval_grant is not None else None
            ),
            approval_checkpoint_id=state.get("approval_checkpoint_id"),
            final_status=normalized_report.final_status,
            state_version=normalized_report.state_version,
            plan_version=normalized_report.plan_version,
            generated_at=self._clock(),
            summary=normalized_report.summary,
            completed_order_ids=list(normalized_report.completed_order_ids),
            incomplete_order_ids=list(normalized_report.incomplete_order_ids),
            evidence_refs=list(normalized_report.evidence_refs),
            citations=citations,
            tool_evidence=tool_evidence,
            metrics=metrics,
            unresolved_risks=list(normalized_report.unresolved_risks),
            budget_usage=actual_usage,
            model=state.get("model_version"),
        )
        return {
            "stage": PEVRStage.FINISH,
            "stage_trace": self._mark_stage(state, PEVRStage.FINISH),
            "final_report": report,
            "budget_usage": actual_usage,
            "model_call_count": state.get("model_call_count", 0) + 1,
        }

    @staticmethod
    def _add_tool_usage(usage: BudgetUsage, result: ToolResult) -> BudgetUsage:
        """把真实工具步数和耗时加入 P0-05 预算快照。"""

        return BudgetUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tool_steps=usage.tool_steps + 1,
            elapsed_seconds=usage.elapsed_seconds + result.duration_ms / 1000.0,
            replans=usage.replans,
            retries=usage.retries,
        )

    @staticmethod
    def _all_evidence_refs(results: list[ToolResult]) -> list[str]:
        """按工具发生顺序合并 call/evidence 引用，保留首次出现顺序。"""

        refs: list[str] = []
        for result in results:
            refs.extend([f"tool://{result.call_id}", *result.evidence_refs])
        return list(dict.fromkeys(refs))

    @staticmethod
    def _observation_from_tool(result: ToolResult, *, task_id: str | None) -> Observation:
        """将 ToolResult 映射为 P0-04 Observation，而不把完整输出复制进 state_delta。"""

        status = ObservationStatus.OK if result.status is ToolResultStatus.SUCCESS else ObservationStatus.ERROR
        return Observation(
            observation_id=f"observation://{result.call_id}",
            run_id=result.call_id.split(":", 1)[0],
            task_id=task_id,
            source=ObservationSource.TOOL,
            observed_at=result.finished_at,
            status=status,
            summary=PEVRGraphRunner._tool_summary(result),
            state_delta={
                "tool_name": result.tool_name.value,
                "status": result.status.value,
                "output_digest": result.output_digest,
                "effect_id": result.effect_id,
            },
            evidence_refs=[f"tool://{result.call_id}", *result.evidence_refs],
            tool_result=result,
            violations=[],
            requires_replan=result.status is not ToolResultStatus.SUCCESS,
            requires_human=(
                result.error is not None
                and result.error.category.value in {"permission_denied", "unsafe_plan"}
            ),
        )

    @staticmethod
    def _tool_summary(result: ToolResult) -> str:
        """生成短摘要供 StateSummary 使用；正文和完整事件留在 ToolResult 证据中。"""

        if result.status is not ToolResultStatus.SUCCESS:
            return f"工具 {result.tool_name.value} 失败: {result.error.code if result.error else 'unknown'}"
        output = result.output if isinstance(result.output, dict) else {}
        if result.tool_name is ToolName.RETRIEVE_KNOWLEDGE:
            return f"RAG 检索成功，返回 {len(output.get('results', []))} 条引用"
        if result.tool_name is ToolName.ALLOCATE_TASKS:
            return f"Hungarian 分配成功，分配 {len(output.get('assignments', []))} 个订单"
        if result.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
            return f"A* 路线规划成功，生成 {len(output.get('routes', []))} 条路线"
        if result.tool_name is ToolName.VALIDATE_FLEET_PLAN:
            return f"P0-10 Validator 返回 {output.get('status', 'unknown')}"
        if result.tool_name is ToolName.DISPATCH_SIMULATION:
            completed = sum(item.get("status") == "completed" for item in output.get("orders", []))
            return f"仿真 {output.get('status', 'unknown')}，完成 {completed} 个订单"
        return f"工具 {result.tool_name.value} 成功"

    @staticmethod
    def _tool_context_evidence(result: ToolResult, *, include_output: bool = False) -> ContextEvidence:
        """把工具结果压缩成允许进入 Prompt 的 tool evidence。"""

        output: dict[str, Any] = {
            "tool_name": result.tool_name.value,
            "status": result.status.value,
            "output_digest": result.output_digest,
            "evidence_refs": result.evidence_refs,
        }
        if include_output and isinstance(result.output, dict):
            raw = result.output
            output["simulation_id"] = raw.get("simulation_id")
            output["simulation_status"] = raw.get("status")
            output["orders"] = raw.get("orders", [])
            output["end_time"] = raw.get("end_time")
            output["event_count"] = len(raw.get("events", []))
        timestamp = result.finished_at
        return ContextEvidence(
            source_type=EvidenceSourceType.TOOL,
            source_id=result.call_id,
            source_version=result.tool_version or "unknown",
            observed_at=timestamp,
            collected_at=timestamp,
            citation=f"tool://{result.call_id}",
            content=output,
        )

    def _materialize_arguments(
        self,
        task: PlanTask,
        *,
        results_by_task: Mapping[str, ToolResult],
        derived_plan: SimulationPlan | None,
        contract: TaskContract,
    ) -> dict[str, Any]:
        """解析两个固定 dataflow 引用，并组装跨工具所需的严格 envelope。"""

        arguments = json.loads(json.dumps(task.tool_arguments, ensure_ascii=False))
        if task.tool_name is ToolName.ALLOCATE_TASKS:
            return arguments
        if task.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
            reference = arguments.get("assignments", {})
            if not isinstance(reference, dict) or not str(reference.get("$ref", "")).startswith("task:"):
                raise PEVRExecutionError(PEVRStage.EXECUTE, "assignment_ref_invalid", task.task_id)
            source_task = str(reference["$ref"])[len("task:") :].split("/output/", 1)[0]
            source = results_by_task.get(source_task)
            if source is None or source.output is None:
                raise PEVRExecutionError(PEVRStage.EXECUTE, "assignment_source_missing", source_task)
            allocation = AllocationResponse.model_validate(source.output)
            arguments["assignments"] = [item.model_dump(mode="json") for item in allocation.assignments]
            arguments["blocked_cells"] = [item.model_dump(mode="json") for item in contract.constraints.blocked_cells]
            return arguments
        if task.tool_name in {ToolName.VALIDATE_FLEET_PLAN, ToolName.DISPATCH_SIMULATION}:
            if derived_plan is None:
                raise PEVRExecutionError(PEVRStage.EXECUTE, "simulation_plan_missing", task.task_id)
            arguments["plan"] = derived_plan.model_dump(mode="json", by_alias=True)
            if task.tool_name is ToolName.VALIDATE_FLEET_PLAN:
                arguments["environment_ref"] = contract.environment_ref
            if task.tool_name is ToolName.DISPATCH_SIMULATION:
                arguments.setdefault("seed", 7)
            return arguments
        return arguments

    def _build_simulation_plan(
        self,
        contract: TaskContract,
        route: RoutePlanResponse,
        route_arguments: Mapping[str, Any],
    ) -> SimulationPlan:
        """把 A* 输出包装成 P0-10/P0-11 共同的完整计划 envelope。"""

        snapshot = self.snapshot_provider.get_snapshot(contract.environment_ref)
        max_time = int(route_arguments.get("max_time", snapshot.max_time))
        routes = [
            FleetPlanRoute(
                **item.model_dump(mode="python"),
                payload_kg=self.DEFAULT_PAYLOAD_KG,
            )
            for item in route.routes
        ]
        return SimulationPlan(
            schema_version="1.0",
            environment_ref=snapshot.environment_ref,
            map_width=snapshot.map_width,
            map_height=snapshot.map_height,
            blocked_cells=[item.model_copy(deep=True) for item in snapshot.blocked_cells],
            blocked_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.blocked_edges],
            one_way_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.one_way_edges],
            amrs=[item.model_copy(deep=True) for item in snapshot.amrs],
            orders=[item.model_copy(deep=True) for item in contract.orders],
            location_positions={key: value.model_copy(deep=True) for key, value in snapshot.location_positions.items()},
            completed_order_ids=[],
            routes=routes,
            start_time=snapshot.start_time,
            max_time=max_time,
            config=ValidatorConfig(
                maximum_load_kg=contract.constraints.maximum_load_kg,
                energy_per_cell_percent=1.0,
                battery_safety_reserve_percent=15.0,
                new_task_battery_threshold_percent=20.0,
                critical_battery_threshold_percent=10.0,
                minimum_safety_distance_cells=1,
                default_workstation_capacity=1,
            ),
            workstation_capacities=dict(snapshot.workstation_capacities),
            ruleset_version="p0-10.v1",
        )

    def _replace_run_state(self, state: RunState, **updates: Any) -> RunState:
        """用完整 Pydantic 重建 RunState，确保每个节点后都重新走跨对象校验。"""

        payload = state.model_dump(mode="python")
        payload.update(updates)
        payload["updated_at"] = self._clock()
        return RunState.model_validate(payload)

    def _replace_task_state(
        self,
        state: RunState,
        *,
        task_id: str,
        status: PlanTaskStatus,
        current_task_id: str | None,
        run_status: RunStatus,
        evidence_refs: list[str] | None = None,
        effect_id: str | None = None,
        completed_task_ids: list[str] | None = None,
        observations: list[Observation] | None = None,
    ) -> RunState:
        """只修改一个计划任务，并同步 RunState 的冗余完成列表。"""

        tasks: list[PlanTask] = []
        for task in state.plan_tasks:
            if task.task_id != task_id:
                tasks.append(task)
                continue
            task_payload = task.model_dump(mode="python")
            task_payload["status"] = status
            if evidence_refs is not None:
                task_payload["evidence_refs"] = list(dict.fromkeys([*task.evidence_refs, *evidence_refs]))
            if effect_id is not None:
                task_payload["effect_id"] = effect_id
            tasks.append(PlanTask.model_validate(task_payload))
        payload: dict[str, Any] = {
            "plan_tasks": tasks,
            "current_task_id": current_task_id,
            "status": run_status,
        }
        if completed_task_ids is not None:
            payload["completed_task_ids"] = completed_task_ids
        if observations is not None:
            payload["observations"] = observations
        return self._replace_run_state(state, **payload)

    def _build_metrics(
        self,
        state: PEVRGraphState,
        validation: PlanValidationResult,
        retrieval: RetrievalResponse,
        simulation: SimulationResult,
    ) -> PEVRMetrics:
        """从真实工具和模型节点结果计算报告指标，不接受 LLM 自报数字。"""

        results = state["tool_results"]
        return PEVRMetrics(
            graph_stage_count=len(PEVR_STAGE_ORDER),
            # finish 的模型调用已经发生，但当前 state 是进入 finish 前的信封，
            # 因此在持久化返回值前把本次调用计入指标。
            model_call_count=state.get("model_call_count", 0) + 1,
            tool_call_count=len(results),
            successful_tool_call_count=sum(item.status is ToolResultStatus.SUCCESS for item in results),
            plan_task_count=len(cast(PlanTasksOutput, state["plan"]).tasks),
            validator_error_count=validation.error_count,
            retrieval_result_count=len(retrieval.results),
            completed_order_count=sum(item.status.value == "completed" for item in simulation.orders),
            route_count=len(simulation.orders),
            simulation_status=simulation.status.value,
            simulation_end_time=simulation.end_time,
            total_tool_duration_ms=sum(item.duration_ms for item in results),
        )


__all__ = ["PEVRExecutionError", "PEVRGraphRunner", "PEVRInterrupt"]
