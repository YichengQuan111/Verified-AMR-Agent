"""P0-19 评测层独立 ReAct Runner。

Retrieve 之后只运行 ``decide → guard → act → observe → terminal_check`` 循环，
不实例化 ``PEVRGraphRunner``，也不调用 ``plan_tasks`` 生成四任务 DAG。
工具参数中的动态数据必须经受控引用物化；dispatch 的 Validator/HITL/Effect
门禁由确定性代码执行。
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from pydantic import ValidationError

from agent.context import BudgetUsage
from agent.planning.contracts import TaskContract
from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    InMemoryExternalStateReconciler,
    RecoveryCoordinator,
    RecoveryDecision,
    RuntimePersistenceProtocol,
    canonical_json_digest,
    make_effect_idempotency_key,
    to_jsonable,
)
from agent.runtime.faults import FaultClassifier
from agent.runtime.hitl import (
    ApprovalGrant,
    HITLInterrupt,
    HITLReason,
    HITLStatus,
    HITLStoreProtocol,
    InMemoryHITLStore,
    build_hitl_request,
)
from agent.runtime.prefix import (
    SHARED_ENTRY_BUDGETS,
    SharedPrefixService,
    add_tool_usage,
    build_simulation_plan_from_routes,
    idle_charging_simulation_plan,
    observation_from_tool,
)
from agent.runtime.state import Observation, ObservationSource, ObservationStatus, RunStatus
from agent.runtime.trace import TraceError, TraceEvent
from agent.tools import ToolName, ToolResult, ToolResultStatus, UserRole, validate_tool_arguments
from agent.tools.contracts import TOOL_ARGUMENT_POLICIES
from agent.tools.schemas import AllocationResponse, RoutePlanResponse, ValidationResponse
from agent.context.prompt_registry import P005_PROMPT_VERSION
from agent.context.shared_prefix import prepend_shared_system_prefix
from evals.p019.react_contracts import (
    REACT_DISPATCH_TASK_ID,
    REACT_PROMPT_ID,
    REACT_PROMPT_VERSION,
    REACT_RUNNER_VERSION,
    ReActActionType,
    ReActDecision,
    ReActInterrupt,
    ReActRequest,
    ReActRunResult,
    ReActRunState,
    ReActSafetyGateResult,
    ReActStep,
    ReActTerminalStatus,
)
from services.amr_simulator.contracts import SimulationPlan, SimulationResult
from services.model_gateway.contracts import ChatMessage


Clock = Callable[[], datetime]
LOOP_TOOLS = frozenset(
    {
        ToolName.GET_FLEET_STATE,
        ToolName.ALLOCATE_TASKS,
        ToolName.PLAN_MULTI_AMR_ROUTES,
        ToolName.VALIDATE_FLEET_PLAN,
        ToolName.DISPATCH_SIMULATION,
        ToolName.QUERY_EXECUTION_STATE,
        ToolName.RUN_VERIFICATION_SUITE,
        ToolName.REQUEST_APPROVAL,
    }
)
FROZEN_KEYS = frozenset(
    {
        "environment_ref",
        "seed",
        "run_id",
        "principal_role",
        "order_ids",
        "amr_ids",
        "blocked_cells",
        "idle_charging_plan",
    }
)
HISTORY_STEP_LIMIT = 6
DEFAULT_MAX_LOOP_ITERATIONS = 12

REACT_NODE_PROMPT = """你是 AMR 仓储评测层的独立 ReAct Agent。
你必须按 decide → act → observe 循环工作：每轮至多选择一个白名单工具，或请求 finish/stop。
禁止输出思维链、隐藏推理或任意代码。只返回符合 JSON Schema 的短 decision_summary。

每轮 user JSON 含 tool_argument_policies：只能使用该工具的 required/optional 顶层键。
多出来的键会被确定性门禁拒绝，本轮不执行工具。
本轮实验禁止再次调用 retrieve_knowledge。

各工具允许的顶层键：
- get_fleet_state: environment_ref；可选 amr_ids
- allocate_tasks: order_ids, environment_ref；可选 amr_ids
- plan_multi_amr_routes: assignments, environment_ref；可选 blocked_cells, max_time。禁止传 order_ids 或 seed
- validate_fleet_plan: plan, environment_ref；可选 ruleset_version
- dispatch_simulation: plan, seed；可选 until_time
- query_execution_state: run_id；可选 task_ids, amr_ids
- request_approval: run_id, task_id, reason；可选 expires_at

缺省的 environment_ref/order_ids/seed/plan 可由运行时从冻结事实或已观察结果注入。
动态载荷不要手写完整计划 JSON，可使用受控引用：
- {"$frozen":"environment_ref"} / {"$frozen":"seed"} / {"$frozen":"order_ids"} / {"$frozen":"run_id"}
- {"$step":"s1","path":"output.assignments"}
- {"$derived_plan":true} 仅用于 validate_fleet_plan
- {"$validated_plan":true} 仅用于 dispatch_simulation

运输主链建议顺序：allocate_tasks → plan_multi_amr_routes → validate_fleet_plan → dispatch_simulation。
充电合同可对冻结 idle_charging_plan 做 validate_fleet_plan，再 dispatch。
dispatch 前必须已有最近一次成功的 validate_fleet_plan，且计划 digest 一致。
finish 只表示你请求完成；是否真完成由确定性程序检查订单/仿真/Validator/副作用。
检查失败时你会收到新的 Observation，预算允许应继续行动，否则安全停止。
"""
REACT_SYSTEM_PROMPT = prepend_shared_system_prefix(REACT_NODE_PROMPT)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ReActRunner:
    """独立 ReAct 循环。生产 PEVR 图不得被本类导入后调用。"""

    def __init__(
        self,
        provider: Any,
        *,
        registry: Any,
        snapshot_provider: Any,
        checkpoint_store: RuntimePersistenceProtocol | None = None,
        hitl_store: HITLStoreProtocol | None = None,
        security_required: bool = True,
        hitl_ttl_seconds: int = 900,
        clock: Clock = _utc_now,
        max_loop_iterations: int = DEFAULT_MAX_LOOP_ITERATIONS,
        allow_repeat_retrieve: bool = False,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if allow_repeat_retrieve:
            raise ValueError("本轮对照禁止 ReAct 重复检索")
        self.provider = provider
        self.registry = registry
        self.snapshot_provider = snapshot_provider
        self.checkpoint_store = checkpoint_store
        self.hitl_store = hitl_store or (InMemoryHITLStore() if security_required else None)
        self.security_required = security_required
        self.hitl_ttl_seconds = hitl_ttl_seconds
        self._clock = clock
        self._monotonic = monotonic_clock
        self.max_loop_iterations = max(1, int(max_loop_iterations))
        self._active_approval_grant: ApprovalGrant | None = None
        self._resume_state: ReActRunState | None = None
        self._prefix = SharedPrefixService(
            provider,
            registry,
            snapshot_provider,
            clock=clock,
            entry_budgets=SHARED_ENTRY_BUDGETS,
            security_required=security_required,
        )
        self._recovery = RecoveryCoordinator(InMemoryExternalStateReconciler())
        if security_required:
            targets: list[Any] = [self.registry]
            inner = getattr(self.registry, "_inner", None)
            if inner is not None:
                targets.append(inner)
            for candidate in targets:
                if hasattr(candidate, "approval_verifier"):
                    candidate.security_required = True
                    candidate.approval_verifier = self._registry_approval_verifier
                    break

    def _registry_approval_verifier(
        self,
        grant: ApprovalGrant,
        spec: Any,
        arguments: Mapping[str, Any],
    ) -> None:
        """Registry 最后一道审批必须绑定当前已核对票据。"""

        del spec, arguments
        if self._active_approval_grant is None or grant != self._active_approval_grant:
            raise PermissionError("工具调用未绑定当前 ReAct 已核对的审批票据")

    def run(self, request: ReActRequest) -> ReActRunResult:
        """执行或从 HITL 等待点恢复一次独立 ReAct。"""

        if self._resume_state is not None and self._resume_state.request.run_id == request.run_id:
            state = self._resume_state
            state.approval_grant = request.approval_grant
            state.request = request
            self._resume_state = None
            return self._loop(state)

        state = ReActRunState(request=request, started_monotonic=self._monotonic())
        self._prefix.guard(request)
        self._append_node_trace(state, node="react_guard", status="completed")
        understood = self._prefix.understand(request, budget_usage=state.budget_usage)
        state.task_contract = understood.contract
        state.run_state = understood.run_state
        state.budget_usage = understood.budget_usage
        self._append_model_node_trace(state, understood.node_result, node="understand")
        retrieved = self._prefix.retrieve(
            request,
            understood.contract,
            run_state=understood.run_state,
            budget_usage=state.budget_usage,
        )
        state.budget_usage = retrieved.budget_usage
        state.observations.append(retrieved.observation)
        state.tool_results.append(retrieved.tool_result)
        self._append_tool_trace(
            state,
            retrieved.tool_result,
            node="retrieve",
            parameters=retrieved.retrieve_arguments,
        )
        if (
            retrieved.tool_result.status is ToolResultStatus.SUCCESS
            and retrieved.response is not None
            and retrieved.rag_evidence
        ):
            state.rag_evidence = list(retrieved.rag_evidence)
            state.retrieve_query = str(retrieved.retrieve_arguments.get("query") or "")
            state.evidence_digest = canonical_json_digest(
                [item.model_dump(mode="json") for item in state.rag_evidence]
            )
        else:
            # 初次 Retrieve 失败也进入循环，但本轮不得再检索。
            state.retrieve_query = str(retrieved.retrieve_arguments.get("query") or "")
            state.evidence_digest = canonical_json_digest({"retrieve_failed": True})
            if retrieved.tool_result.error is not None and retrieved.tool_result.error.retryable:
                state.recovery_action_count += 1
        snapshot = self.snapshot_provider.get_snapshot(request.environment_ref)
        idle_plan = (
            idle_charging_simulation_plan(understood.contract, snapshot)
            if understood.contract.is_charging_contract()
            else None
        )
        if idle_plan is not None:
            state.derived_plan = idle_plan
        state.frozen_facts = {
            "environment_ref": understood.contract.environment_ref,
            "seed": request.seed,
            "run_id": request.run_id,
            "principal_role": request.principal_role.value,
            "order_ids": [item.order_id for item in understood.contract.orders],
            "amr_ids": [item.amr_id for item in snapshot.amrs],
            "blocked_cells": [item.model_dump(mode="json") for item in understood.contract.constraints.blocked_cells],
            "idle_charging_plan": None if idle_plan is None else idle_plan.model_dump(mode="json", by_alias=True),
        }
        return self._loop(state)

    def _loop(self, state: ReActRunState) -> ReActRunResult:
        """持续循环直到确定性终态或硬预算耗尽。"""

        while state.terminal_status is None:
            exhausted = self._budget_failure(state)
            if exhausted is not None:
                self._stop(state, ReActTerminalStatus.BUDGET_STOP, exhausted[0], exhausted[1])
                break
            if state.pending_decision is not None:
                decision = state.pending_decision
                state.pending_decision = None
                decide_tokens = (0, 0, 0, 0, None)
            else:
                decision, decide_tokens = self._decide(state)
            state.loop_iterations += 1
            gate = self._guard_decision(state, decision)
            step_id = f"s{len(state.steps) + 1}"
            step = ReActStep(
                step_id=step_id,
                sequence=len(state.steps) + 1,
                decision=decision,
                safety_gate=gate,
                input_tokens=decide_tokens[0],
                output_tokens=decide_tokens[1],
                total_tokens=decide_tokens[2],
                latency_ms=decide_tokens[3],
                model_version=decide_tokens[4],
            )
            if not gate.allowed:
                observation = self._system_observation(
                    state,
                    summary=gate.message,
                    code=gate.code,
                    ok=False,
                )
                step.observation = observation
                state.steps.append(step)
                state.observations.append(observation)
                self._append_node_trace(
                    state,
                    node="react_guard",
                    status="denied",
                    error_code=gate.code,
                    message=gate.message,
                )
                self._append_observe_trace(state, observation, step_id=step_id)
                if gate.code in {"unknown_side_effect"}:
                    self._stop(state, ReActTerminalStatus.FAILED, gate.code, gate.message)
                continue
            if decision.action_type is ReActActionType.FINISH:
                ok, code, reason = self._terminal_check(state)
                observation = self._system_observation(
                    state,
                    summary=reason,
                    code=code,
                    ok=ok,
                )
                step.observation = observation
                state.steps.append(step)
                state.observations.append(observation)
                self._append_node_trace(state, node="react_terminal", status="completed" if ok else "failed")
                self._append_observe_trace(state, observation, step_id=step_id)
                if ok:
                    self._stop(state, ReActTerminalStatus.COMPLETED, "finished", reason)
                continue
            if decision.action_type is ReActActionType.STOP:
                ok, code, reason = self._terminal_check(state)
                observation = self._system_observation(state, summary=reason, code=code, ok=ok)
                step.observation = observation
                state.steps.append(step)
                state.observations.append(observation)
                self._append_observe_trace(state, observation, step_id=step_id)
                if ok:
                    self._stop(state, ReActTerminalStatus.COMPLETED, "stopped_but_complete", reason)
                else:
                    self._stop(state, ReActTerminalStatus.FAILED, code, reason)
                break
            try:
                result = self._act(state, decision, step_id)
            except ReActInterrupt as interrupt:
                state.pending_decision = decision
                self._resume_state = state
                self._persist(state, stage="react_waiting_approval", status="waiting_approval")
                raise interrupt
            except ReActSafetyError as exc:
                observation = self._system_observation(
                    state,
                    summary=str(exc),
                    code=exc.code,
                    ok=False,
                )
                step.observation = observation
                state.steps.append(step)
                state.observations.append(observation)
                self._append_node_trace(
                    state,
                    node="react_act",
                    status="denied",
                    error_code=exc.code,
                    message=str(exc),
                )
                self._append_observe_trace(state, observation, step_id=step_id)
                if exc.code.startswith("recovery_") or exc.code in {"unknown_side_effect", "approval_invalid"}:
                    self._stop(state, ReActTerminalStatus.FAILED, exc.code, str(exc))
                continue
            step.tool_result = result
            step.tool_version = result.tool_version
            observation = observation_from_tool(result, task_id=step_id)
            step.observation = observation
            state.steps.append(step)
            state.tool_results.append(result)
            state.observations.append(observation)
            state.tool_call_count += 1
            state.budget_usage = add_tool_usage(state.budget_usage, result)
            self._append_tool_trace(state, result, node="react_act", parameters={"step_id": step_id})
            self._append_observe_trace(state, observation, step_id=step_id)
            self._post_act(state, result)
            if result.status is not ToolResultStatus.SUCCESS:
                state.recovery_action_count += 1
                if result.status is ToolResultStatus.TIMEOUT:
                    state.retry_count += 1
        return self._build_result(state)

    def _decide(self, state: ReActRunState) -> tuple[ReActDecision, tuple[int, int, int, int, str | None]]:
        """调用 Fast 产生一轮结构化决定，并把有限历史摘要写入 user 消息。"""

        started = self._clock()
        user_payload = self._decide_context(state)
        try:
            generated = self.provider.generate_structured(
                [
                    ChatMessage(role="system", content=REACT_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=_json_dump(user_payload)),
                ],
                ReActDecision,
                max_output_tokens=min(512, state.request.requested_output_tokens),
                timeout_seconds=min(60.0, self._remaining_seconds(state)),
            )
        except Exception as exc:
            finished = self._clock()
            decision = ReActDecision(
                action_type=ReActActionType.STOP,
                decision_summary="模型决定失败，安全停止。",
                reason_code="model_decide_failed",
            )
            self._append_model_trace(
                state,
                node="react_decide",
                status="failed",
                prompt_id=REACT_PROMPT_ID,
                prompt_version=REACT_PROMPT_VERSION,
                usage=(0, 0, 0),
                latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
                started_at=started,
                finished_at=finished,
                error_code=getattr(exc, "code", None) or type(exc).__name__,
                message=str(exc)[:500],
            )
            return decision, (0, 0, 0, max(0, int((finished - started).total_seconds() * 1000)), None)
        finished = self._clock()
        usage = generated.total_usage
        latency_ms = max(0, int((finished - started).total_seconds() * 1000))
        state.budget_usage = BudgetUsage(
            input_tokens=state.budget_usage.input_tokens + int(usage.input_tokens or 0),
            output_tokens=state.budget_usage.output_tokens + int(usage.output_tokens or 0),
            tool_steps=state.budget_usage.tool_steps,
            elapsed_seconds=state.budget_usage.elapsed_seconds + latency_ms / 1000.0,
            replans=state.budget_usage.replans,
            retries=state.budget_usage.retries,
        )
        alias = getattr(getattr(generated.call, "version", None), "served_alias", None)
        self._append_model_trace(
            state,
            node="react_decide",
            status="completed",
            prompt_id=REACT_PROMPT_ID,
            prompt_version=REACT_PROMPT_VERSION,
            usage=(usage.input_tokens, usage.output_tokens, usage.total_tokens),
            latency_ms=latency_ms,
            started_at=started,
            finished_at=finished,
            model_version=alias,
            metadata={
                "decision_summary": generated.value.decision_summary,
                "action_type": generated.value.action_type.value,
                "reason_code": generated.value.reason_code,
                "raw_chain_of_thought_stored": False,
                "attempts": generated.attempts,
                "schema_repaired": generated.repaired,
            },
        )
        return generated.value, (
            int(usage.input_tokens or 0),
            int(usage.output_tokens or 0),
            int(usage.total_tokens or 0),
            latency_ms,
            alias,
        )

    def _decide_context(self, state: ReActRunState) -> dict[str, Any]:
        """只提供任务合同、冻结事实、白名单工具、有限历史和剩余预算。"""

        contract = state.task_contract
        assert contract is not None
        history = []
        for step in state.steps[-HISTORY_STEP_LIMIT:]:
            history.append(
                {
                    "step_id": step.step_id,
                    "action_type": step.decision.action_type.value,
                    "tool_name": None if step.decision.tool_name is None else step.decision.tool_name.value,
                    "reason_code": step.decision.reason_code,
                    "decision_summary": step.decision.decision_summary,
                    "gate": {"allowed": step.safety_gate.allowed, "code": step.safety_gate.code},
                    "observation_summary": None if step.observation is None else step.observation.summary,
                    "tool_status": None if step.tool_result is None else step.tool_result.status.value,
                    "output_digest": None if step.tool_result is None else step.tool_result.output_digest,
                }
            )
        last_observation = None
        if state.observations:
            item = state.observations[-1]
            last_observation = {
                "summary": item.summary,
                "status": item.status.value,
                "tool_name": item.state_delta.get("tool_name"),
                "output_digest": item.state_delta.get("output_digest"),
            }
        remaining = self._remaining_budget(state)
        return {
            "task_contract": {
                "goal": contract.goal,
                "environment_ref": contract.environment_ref,
                "order_ids": [item.order_id for item in contract.orders],
                "charging": None if contract.charging is None else contract.charging.model_dump(mode="json"),
                "completion_criteria": list(contract.completion_criteria),
            },
            "frozen_facts": {
                key: value
                for key, value in state.frozen_facts.items()
                if key != "idle_charging_plan" or contract.is_charging_contract()
            },
            "initial_rag_digest": state.evidence_digest,
            "initial_retrieve_query": state.retrieve_query,
            "allowed_tools": sorted(item.value for item in LOOP_TOOLS),
            "tool_argument_policies": {
                name.value: {
                    "required": sorted(policy.required),
                    "optional": sorted(policy.optional),
                }
                for name, policy in TOOL_ARGUMENT_POLICIES.items()
                if name in LOOP_TOOLS
            },
            "argument_contract": (
                "arguments 只能包含该工具 required/optional 键；"
                "plan_multi_amr_routes 不得传 order_ids 或 seed；"
                "缺省的 environment_ref/order_ids/seed/plan 由运行时注入。"
            ),
            "validated_plan_ready": state.validated_plan is not None and state.validator_valid,
            "derived_plan_ready": state.derived_plan is not None,
            "history": history,
            "latest_observation": last_observation,
            "remaining_budget": remaining,
            "runner_version": REACT_RUNNER_VERSION,
        }

    def _guard_decision(self, state: ReActRunState, decision: ReActDecision) -> ReActSafetyGateResult:
        """在任何工具执行前拒绝非法动作、引用和冻结事实覆盖。"""

        if decision.action_type is not ReActActionType.TOOL:
            return ReActSafetyGateResult(allowed=True, code="control_action", message="控制动作无需工具门禁")
        tool_name = decision.tool_name
        if tool_name is None or tool_name not in LOOP_TOOLS:
            code = "retrieve_forbidden" if tool_name is ToolName.RETRIEVE_KNOWLEDGE else "unknown_tool"
            return ReActSafetyGateResult(
                allowed=False,
                code=code,
                message="工具不在本轮 ReAct 白名单内或被禁止重复检索",
            )
        extra = set(decision.arguments) - TOOL_ARGUMENT_POLICIES[tool_name].allowed
        if extra:
            return ReActSafetyGateResult(
                allowed=False,
                code="extra_argument",
                message=f"工具包含未授权参数: {', '.join(sorted(extra))}",
            )
        if tool_name.value in state.unknown_side_effect_tools:
            return ReActSafetyGateResult(
                allowed=False,
                code="unknown_side_effect",
                message="未知副作用状态禁止自动重试该工具",
            )
        if tool_name is ToolName.PLAN_MULTI_AMR_ROUTES and not self._last_success(state, ToolName.ALLOCATE_TASKS):
            return ReActSafetyGateResult(
                allowed=False,
                code="dependency_missing",
                message="plan_multi_amr_routes 需要先观察到成功的 allocate_tasks",
            )
        if tool_name is ToolName.VALIDATE_FLEET_PLAN and state.derived_plan is None:
            return ReActSafetyGateResult(
                allowed=False,
                code="derived_plan_missing",
                message="validate_fleet_plan 需要已物化的 derived_plan",
            )
        if tool_name is ToolName.DISPATCH_SIMULATION:
            if not state.validator_valid or state.validated_plan is None or state.validated_plan_digest is None:
                return ReActSafetyGateResult(
                    allowed=False,
                    code="validator_required",
                    message="dispatch 前必须有最近一次成功的 validate_fleet_plan",
                )
        try:
            self.materialize_arguments(state, tool_name, decision.arguments)
        except ReActSafetyError as exc:
            return ReActSafetyGateResult(allowed=False, code=exc.code, message=str(exc), details=exc.details)
        except (TypeError, ValueError, ValidationError) as exc:
            return ReActSafetyGateResult(
                allowed=False,
                code="argument_invalid",
                message=f"工具参数未通过确定性校验: {exc}"[:500],
            )
        return ReActSafetyGateResult(allowed=True, code="allowed", message="动作通过确定性安全门禁")

    def materialize_arguments(
        self,
        state: ReActRunState,
        tool_name: ToolName,
        raw_arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """解析受控引用，拒绝跨 run/未知步骤/冻结事实覆盖。"""

        materialized: dict[str, Any] = {}
        for key, value in dict(raw_arguments).items():
            materialized[key] = self._resolve_value(state, value, field_name=key)
        self._apply_frozen_defaults(state, tool_name, materialized)
        self._overwrite_observed_payloads(state, tool_name, materialized)
        self._reject_frozen_overrides(state, materialized)
        try:
            validate_tool_arguments(tool_name, materialized)
        except ValueError as exc:
            raise ReActSafetyError("argument_invalid", str(exc)) from exc
        definition = self.registry.get(tool_name)
        input_model = getattr(definition, "input_model", None)
        if input_model is not None:
            try:
                parsed = input_model.model_validate(materialized)
                materialized = parsed.model_dump(mode="json")
            except (TypeError, ValueError, ValidationError) as exc:
                raise ReActSafetyError("argument_schema_invalid", f"工具参数未通过 Schema: {exc}") from exc
        return materialized

    def _resolve_value(self, state: ReActRunState, value: Any, *, field_name: str) -> Any:
        """递归解析 $frozen/$step/$derived_plan/$validated_plan。"""

        if isinstance(value, list):
            return [self._resolve_value(state, item, field_name=field_name) for item in value]
        if not isinstance(value, Mapping):
            return value
        keys = set(value)
        if keys == {"$frozen"}:
            frozen_key = str(value["$frozen"])
            if frozen_key not in FROZEN_KEYS or frozen_key not in state.frozen_facts:
                raise ReActSafetyError("unknown_frozen_ref", f"未知冻结引用: {frozen_key}")
            return state.frozen_facts[frozen_key]
        if keys <= {"$step", "path"} and "$step" in keys:
            step_id = str(value["$step"])
            path = str(value.get("path") or "output")
            source = next((item for item in state.steps if item.step_id == step_id), None)
            if source is None or source.tool_result is None:
                raise ReActSafetyError("unobserved_step_ref", f"引用了未观察到的步骤 {step_id}")
            if source.tool_result.call_id.split(":", 1)[0] != state.request.run_id:
                raise ReActSafetyError("cross_run_ref", "禁止跨 run 引用工具结果")
            payload = {"output": source.tool_result.output}
            return self._lookup_path(payload, path)
        if keys == {"$derived_plan"} and value.get("$derived_plan") is True:
            if state.derived_plan is None:
                raise ReActSafetyError("derived_plan_missing", "当前没有 derived_plan")
            return state.derived_plan.model_dump(mode="json", by_alias=True)
        if keys == {"$validated_plan"} and value.get("$validated_plan") is True:
            if state.validated_plan is None or not state.validator_valid:
                raise ReActSafetyError("validated_plan_missing", "当前没有已验证计划")
            return state.validated_plan.model_dump(mode="json", by_alias=True)
        if any(str(key).startswith("$") for key in keys):
            raise ReActSafetyError("unknown_ref", f"字段 {field_name} 含有未知引用")
        return {str(key): self._resolve_value(state, item, field_name=str(key)) for key, item in value.items()}

    @staticmethod
    def _lookup_path(payload: Any, path: str) -> Any:
        """按点路径读取已观察输出，路径不存在即拒绝。"""

        current = payload
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
                continue
            if isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
                continue
            raise ReActSafetyError("ref_path_missing", f"引用路径不存在: {path}")
        return current

    def _apply_frozen_defaults(self, state: ReActRunState, tool_name: ToolName, arguments: dict[str, Any]) -> None:
        """缺省时注入冻结身份，模型不能靠省略字段改写环境。"""

        if "environment_ref" in TOOL_ARGUMENT_POLICIES[tool_name].allowed and "environment_ref" not in arguments:
            arguments["environment_ref"] = state.frozen_facts["environment_ref"]
        if tool_name is ToolName.ALLOCATE_TASKS and "order_ids" not in arguments:
            arguments["order_ids"] = list(state.frozen_facts["order_ids"])
        if tool_name is ToolName.DISPATCH_SIMULATION:
            arguments.setdefault("seed", state.frozen_facts["seed"])
            if "plan" not in arguments and state.validated_plan is not None:
                arguments["plan"] = state.validated_plan.model_dump(mode="json", by_alias=True)
        if tool_name is ToolName.VALIDATE_FLEET_PLAN and "plan" not in arguments and state.derived_plan is not None:
            arguments["plan"] = state.derived_plan.model_dump(mode="json", by_alias=True)
        if tool_name is ToolName.QUERY_EXECUTION_STATE and "run_id" not in arguments:
            arguments["run_id"] = state.frozen_facts["run_id"]

    def _overwrite_observed_payloads(
        self,
        state: ReActRunState,
        tool_name: ToolName,
        arguments: dict[str, Any],
    ) -> None:
        """路线/计划等动态载荷只允许来自本 run 已观察结果，不能靠模型正文改写。"""

        if tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
            allocate = next(
                (
                    item
                    for item in reversed(state.tool_results)
                    if item.tool_name is ToolName.ALLOCATE_TASKS and item.status is ToolResultStatus.SUCCESS
                ),
                None,
            )
            if allocate is None or allocate.output is None:
                raise ReActSafetyError("assignment_source_missing", "没有可物化的 allocate 结果")
            allocation = AllocationResponse.model_validate(allocate.output)
            arguments["assignments"] = [item.model_dump(mode="json") for item in allocation.assignments]
            arguments["blocked_cells"] = list(state.frozen_facts["blocked_cells"])
        elif tool_name is ToolName.VALIDATE_FLEET_PLAN:
            if state.derived_plan is None:
                raise ReActSafetyError("derived_plan_missing", "没有可验证的 derived_plan")
            arguments["plan"] = state.derived_plan.model_dump(mode="json", by_alias=True)
            arguments["environment_ref"] = state.frozen_facts["environment_ref"]
        elif tool_name is ToolName.DISPATCH_SIMULATION:
            if state.validated_plan is None:
                raise ReActSafetyError("validated_plan_missing", "没有已验证计划可派发")
            arguments["plan"] = state.validated_plan.model_dump(mode="json", by_alias=True)
            arguments["seed"] = state.frozen_facts["seed"]

    def _reject_frozen_overrides(self, state: ReActRunState, arguments: Mapping[str, Any]) -> None:
        """模型给出的冻结字段必须与初始事实逐值相等。"""

        mapping = {
            "environment_ref": state.frozen_facts["environment_ref"],
            "seed": state.frozen_facts["seed"],
            "run_id": state.frozen_facts["run_id"],
            "role_scope": state.frozen_facts["principal_role"],
        }
        for key, expected in mapping.items():
            if key in arguments and arguments[key] != expected:
                raise ReActSafetyError("frozen_fact_override", f"禁止覆盖冻结字段 {key}")
        if "order_ids" in arguments:
            expected_orders = list(state.frozen_facts["order_ids"])
            if list(arguments["order_ids"]) != expected_orders:
                raise ReActSafetyError("frozen_fact_override", "禁止覆盖冻结订单集合")

    def _act(self, state: ReActRunState, decision: ReActDecision, step_id: str) -> ToolResult:
        """执行至多一个工具，并在 dispatch 上叠加 HITL 与 Effect 幂等。"""

        assert decision.tool_name is not None
        arguments = self.materialize_arguments(state, decision.tool_name, decision.arguments)
        if decision.tool_name is ToolName.DISPATCH_SIMULATION:
            plan = SimulationPlan.model_validate(arguments["plan"])
            plan_digest = canonical_json_digest(plan)
            if (
                not state.validator_valid
                or state.validated_plan_digest is None
                or plan_digest != state.validated_plan_digest
            ):
                raise ReActSafetyError(
                    "plan_digest_mismatch",
                    "dispatch 计划 digest 与 Validator 验证的 digest 不一致",
                )
        spec = self.registry.get(decision.tool_name).spec
        approved_grant: ApprovalGrant | None = None
        if spec.requires_approval and (self.security_required or state.request.principal is not None):
            approved_grant = self._require_dispatch_grant(state, arguments)
            self._active_approval_grant = approved_grant
        recovered: ToolResult | None = None
        idempotency_key = None
        if spec.has_side_effects and self.checkpoint_store is not None:
            idempotency_key, recovered = self._prepare_side_effect(state, decision.tool_name, arguments)
        result = recovered or self._registry_execute(
            decision.tool_name,
            arguments,
            call_id=f"{state.request.run_id}:react:{step_id}",
            idempotency_key=idempotency_key or f"{state.request.run_id}:react:{step_id}",
            approval_grant=approved_grant,
            principal=state.request.principal,
        )
        if idempotency_key is not None:
            updates: dict[str, Any] = {}
            if result.idempotency_key not in {None, idempotency_key}:
                # Fake Registry 常用 call_id 充当幂等键；账本预留键必须回写到结果上。
                updates["idempotency_key"] = idempotency_key
            if self.checkpoint_store is not None:
                entry = self.checkpoint_store.get_effect(idempotency_key)
                if entry is not None and result.input_digest != entry.input_digest:
                    # 真实 Registry 应已通过 input_model digest 对齐；评测 Fake
                    # 若自行填写 digest，回写账本指纹以免 complete_effect 抛成无轨迹失败。
                    updates["input_digest"] = entry.input_digest
            if updates:
                result = result.model_copy(update=updates)
        if (
            spec.has_side_effects
            and self.checkpoint_store is not None
            and recovered is None
            and idempotency_key is not None
        ):
            if result.status is ToolResultStatus.SUCCESS:
                self.checkpoint_store.complete_effect(
                    idempotency_key,
                    result,
                    external_effect_id=result.effect_id,
                )
            else:
                self.checkpoint_store.fail_effect(
                    idempotency_key,
                    note=result.error.message if result.error is not None else "工具失败",
                    compensation_required=result.effect_id is not None,
                )
        return result

    def _require_dispatch_grant(self, state: ReActRunState, arguments: Mapping[str, Any]) -> ApprovalGrant:
        """dispatch 必须同时满足 Validator digest、审批票据和当前 run 绑定。"""

        plan = SimulationPlan.model_validate(arguments["plan"])
        plan_digest = canonical_json_digest(plan)
        if plan_digest != state.validated_plan_digest:
            raise ReActSafetyError("plan_digest_mismatch", "dispatch 计划 digest 与 Validator 验证的 digest 不一致")
        if state.request.principal is None:
            raise ReActSafetyError("principal_required", "安全 dispatch 必须携带已验签 Principal")
        if self.hitl_store is None:
            raise ReActSafetyError("hitl_store_unavailable", "安全 HITL 未配置审批存储")
        candidate = state.approval_grant or state.request.approval_grant
        if candidate is None:
            interrupt = self._request_hitl(state, plan_digest)
            raise ReActInterrupt(interrupt)
        try:
            verified = self.hitl_store.verify_grant(
                candidate,
                principal=state.request.principal,
                run_id=state.request.run_id,
                task_id=REACT_DISPATCH_TASK_ID,
                plan_version=1,
                plan_digest=plan_digest,
                validator_digest=state.validator_digest,
                now=self._clock(),
            )
        except Exception as exc:
            raise ReActSafetyError("approval_invalid", "审批票据未通过存储、计划或 Validator 核对") from exc
        return verified

    def _request_hitl(self, state: ReActRunState, plan_digest: str) -> HITLInterrupt:
        """创建 pending 审批并保存 waiting Checkpoint。"""

        assert state.request.principal is not None
        assert self.hitl_store is not None
        checkpoint_id = f"cp_{uuid4().hex}"
        hitl_request = build_hitl_request(
            run_id=state.request.run_id,
            task_id=REACT_DISPATCH_TASK_ID,
            plan_version=1,
            requested_by=state.request.principal.subject,
            reason_code=HITLReason.HIGH_RISK_WRITE,
            reason="ReAct dispatch_simulation 是高风险写操作，需要人工审批",
            checkpoint_id=checkpoint_id,
            plan_digest=plan_digest,
            validator_digest=state.validator_digest or ("0" * 64),
            now=self._clock(),
            ttl_seconds=self.hitl_ttl_seconds,
        )
        stored = self.hitl_store.request_approval(hitl_request)
        interrupt = HITLInterrupt(
            run_id=state.request.run_id,
            task_id=REACT_DISPATCH_TASK_ID,
            approval_id=stored.approval_id,
            checkpoint_id=stored.checkpoint_id,
            reason_code=stored.reason_code,
            created_at=stored.requested_at,
            expires_at=stored.expires_at,
        )
        state.hitl_interrupt = interrupt
        if state.run_state is not None:
            state.run_state = state.run_state.model_copy(update={"status": RunStatus.WAITING_APPROVAL})
        return interrupt

    def _prepare_side_effect(
        self,
        state: ReActRunState,
        tool_name: ToolName,
        arguments: Mapping[str, Any],
    ) -> tuple[str, ToolResult | None]:
        """预留 dispatch Effect；同一副作用最多执行一次。"""

        assert self.checkpoint_store is not None
        key = make_effect_idempotency_key(state.request.run_id, 1, REACT_DISPATCH_TASK_ID)
        input_digest = self._effect_input_digest(tool_name, arguments)
        reservation = self.checkpoint_store.reserve_effect(
            run_id=state.request.run_id,
            plan_version=1,
            task_id=REACT_DISPATCH_TASK_ID,
            tool_name=tool_name,
            call_id=f"{state.request.run_id}:react:dispatch",
            input_digest=input_digest,
            arguments=dict(arguments),
            now=self._clock(),
        )
        if reservation.owner:
            return key, None
        assessment = self._recovery.assess(reservation.entry)
        if assessment.decision is RecoveryDecision.SKIP_COMPLETED:
            result = reservation.entry.result or assessment.external.result
            if result is None:
                raise ReActSafetyError("recovery_result_missing", assessment.reason)
            return key, result
        raise ReActSafetyError(f"recovery_{assessment.decision.value}", assessment.reason)

    def _effect_input_digest(self, tool_name: ToolName, arguments: Mapping[str, Any]) -> str:
        """按与 ToolRegistry 相同的 input_model canonical dump 计算账本指纹。

        直接对原始 dict 做 SHA 会与真实 dispatch 的 ``ToolResult.input_digest``
        不一致，complete_effect 会把已成功的仿真打成无轨迹 harness 异常。
        """

        parsed: Any = dict(arguments)
        definition = self.registry.get(tool_name)
        input_model = getattr(definition, "input_model", None)
        if input_model is not None:
            try:
                parsed = input_model.model_validate(arguments)
            except (TypeError, ValueError, ValidationError):
                parsed = dict(arguments)
        return canonical_json_digest(parsed)

    def _post_act(self, state: ReActRunState, result: ToolResult) -> None:
        """根据工具结果更新 derived/validated plan，并记录未知副作用。"""

        if result.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES and result.status is ToolResultStatus.SUCCESS:
            route = RoutePlanResponse.model_validate(result.output)
            contract = state.task_contract
            assert contract is not None
            snapshot = self.snapshot_provider.get_snapshot(contract.environment_ref)
            source_step = next(item for item in reversed(state.steps) if item.tool_result is result)
            route_arguments = self.materialize_arguments(
                state,
                ToolName.PLAN_MULTI_AMR_ROUTES,
                source_step.decision.arguments,
            )
            state.derived_plan = build_simulation_plan_from_routes(contract, snapshot, route, route_arguments)
            state.validator_valid = False
            state.validated_plan = None
            state.validated_plan_digest = None
            state.validator_digest = None
        elif result.tool_name is ToolName.VALIDATE_FLEET_PLAN and result.status is ToolResultStatus.SUCCESS:
            payload = ValidationResponse.model_validate(result.output)
            if payload.valid and payload.status == "valid" and not payload.errors:
                source_step = next(item for item in reversed(state.steps) if item.tool_result is result)
                arguments = self.materialize_arguments(
                    state,
                    ToolName.VALIDATE_FLEET_PLAN,
                    source_step.decision.arguments,
                )
                plan = SimulationPlan.model_validate(arguments["plan"])
                state.validated_plan = plan
                state.validated_plan_digest = canonical_json_digest(plan)
                state.validator_digest = canonical_json_digest(payload)
                state.validator_valid = True
            else:
                state.validator_valid = False
        elif result.tool_name is ToolName.DISPATCH_SIMULATION and result.status is ToolResultStatus.SUCCESS:
            state.dispatch_count += 1
        spec = self.registry.get(result.tool_name).spec
        if result.status is not ToolResultStatus.SUCCESS and spec.has_side_effects:
            fault = FaultClassifier.classify(
                result,
                tool_name=result.tool_name,
                idempotent=spec.idempotent,
                has_side_effects=True,
            )
            if not (fault.retryable and fault.idempotent and fault.side_effect_not_found):
                if result.tool_name.value not in state.unknown_side_effect_tools:
                    state.unknown_side_effect_tools.append(result.tool_name.value)

    def _terminal_check(self, state: ReActRunState) -> tuple[bool, str, str]:
        """确定性完成检查；失败只形成 Observation，不让 finish 绕过事实。"""

        contract = state.task_contract
        if contract is None:
            return False, "contract_missing", "缺少冻结 TaskContract"
        dispatch = next(
            (
                item
                for item in reversed(state.tool_results)
                if item.tool_name is ToolName.DISPATCH_SIMULATION and item.status is ToolResultStatus.SUCCESS
            ),
            None,
        )
        if dispatch is None or dispatch.output is None:
            return False, "dispatch_missing", "尚未观察到成功的 dispatch_simulation"
        if not state.validator_valid or state.validated_plan_digest is None:
            return False, "validator_missing", "完成前缺少成功的 Validator 证据"
        simulation = SimulationResult.model_validate(dispatch.output)
        if contract.is_charging_contract():
            if not self._charging_completed(simulation, contract):
                return False, "charging_incomplete", "缺少 charging.completed 或电量未达标"
            return True, "charged", "充电完成证据已核对"
        completed = {
            item.order_id
            for item in simulation.orders
            if str(getattr(item.status, "value", item.status)) == "completed"
        }
        missing = [item.order_id for item in contract.orders if item.order_id not in completed]
        if missing or str(getattr(simulation.status, "value", simulation.status)) != "completed":
            return False, "orders_incomplete", f"订单未全部完成: {', '.join(missing) or 'simulation not completed'}"
        return True, "completed", "订单、仿真与 Validator 事实已核对"

    @staticmethod
    def _charging_completed(simulation: SimulationResult, contract: TaskContract) -> bool:
        """只认 charging.completed，不能把运输 completed 记成 charged。"""

        goal = contract.charging
        if goal is None:
            return False
        events = [
            event
            for event in simulation.events
            if event.event_type == "charging.completed" and (event.amr_id is None or event.amr_id == goal.amr_id)
        ]
        if not events:
            return False
        amr = next((item for item in simulation.amrs if item.amr_id == goal.amr_id), None)
        return amr is not None and float(amr.battery) + 1e-9 >= float(goal.target_percent)

    def _budget_failure(self, state: ReActRunState) -> tuple[str, str] | None:
        """Token、时间、工具步和循环上限分别可触发安全终止。"""

        contract = state.task_contract
        limits = contract.budgets if contract is not None else None
        max_seconds = SHARED_ENTRY_BUDGETS["max_total_seconds"] if limits is None else limits.max_total_seconds
        max_input = SHARED_ENTRY_BUDGETS["max_input_tokens"] if limits is None else limits.max_input_tokens
        max_output = SHARED_ENTRY_BUDGETS["max_output_tokens"] if limits is None else limits.max_output_tokens
        max_tools = SHARED_ENTRY_BUDGETS["max_tool_steps"] if limits is None else limits.max_tool_steps
        elapsed = self._monotonic() - state.started_monotonic
        if elapsed >= max_seconds:
            return "time_budget_exhausted", "已达到总时间预算"
        if state.budget_usage.input_tokens >= max_input:
            return "input_token_budget_exhausted", "已达到输入 Token 预算"
        if state.budget_usage.output_tokens >= max_output:
            return "output_token_budget_exhausted", "已达到输出 Token 预算"
        if state.budget_usage.tool_steps >= max_tools:
            return "tool_step_budget_exhausted", "已达到工具步预算"
        if state.loop_iterations >= self.max_loop_iterations:
            return "loop_limit_exhausted", "已达到 ReAct 循环上限"
        return None

    def _remaining_seconds(self, state: ReActRunState) -> float:
        limits = SHARED_ENTRY_BUDGETS["max_total_seconds"]
        if state.task_contract is not None:
            limits = state.task_contract.budgets.max_total_seconds
        return max(1.0, float(limits) - (self._monotonic() - state.started_monotonic))

    def _remaining_budget(self, state: ReActRunState) -> dict[str, Any]:
        contract = state.task_contract
        limits = SHARED_ENTRY_BUDGETS if contract is None else contract.budgets.model_dump(mode="json")
        return {
            "max_total_seconds": limits["max_total_seconds"] if isinstance(limits, dict) else limits.max_total_seconds,
            "elapsed_seconds": round(self._monotonic() - state.started_monotonic, 3),
            "input_tokens": state.budget_usage.input_tokens,
            "output_tokens": state.budget_usage.output_tokens,
            "tool_steps": state.budget_usage.tool_steps,
            "loop_iterations": state.loop_iterations,
            "max_loop_iterations": self.max_loop_iterations,
        }

    def _stop(self, state: ReActRunState, status: ReActTerminalStatus, code: str, reason: str) -> None:
        state.terminal_status = status
        state.terminal_code = code
        state.terminal_reason = reason
        self._persist(state, stage="react_finish", status=status.value)

    def _last_success(self, state: ReActRunState, tool_name: ToolName) -> bool:
        return any(
            item.tool_name is tool_name and item.status is ToolResultStatus.SUCCESS for item in state.tool_results
        )

    def _system_observation(self, state: ReActRunState, *, summary: str, code: str, ok: bool) -> Observation:
        now = self._clock()
        return Observation(
            observation_id=f"observation://react:{state.request.run_id}:{len(state.observations) + 1}",
            run_id=state.request.run_id,
            task_id=None,
            source=ObservationSource.SYSTEM,
            observed_at=now,
            status=ObservationStatus.OK if ok else ObservationStatus.ERROR,
            summary=summary[:300],
            state_delta={"code": code, "ok": ok},
            evidence_refs=[],
            tool_result=None,
            violations=[],
            requires_replan=not ok,
            requires_human=code in {"approval_invalid", "unknown_side_effect"},
        )

    def _registry_execute(
        self,
        tool_name: ToolName,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        idempotency_key: str,
        principal: Any,
        approval_grant: ApprovalGrant | None,
    ) -> ToolResult:
        """兼容真实 Registry 与评测包装器的关键字签名。"""

        kwargs: dict[str, Any] = {
            "role": self._resume_role(principal),
            "call_id": call_id,
        }
        try:
            parameters = inspect.signature(self.registry.execute).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_var_kw = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        if "idempotency_key" in parameters or accepts_var_kw:
            kwargs["idempotency_key"] = idempotency_key
        if ("principal" in parameters or accepts_var_kw) and principal is not None:
            kwargs["principal"] = principal
        if ("approval_grant" in parameters or accepts_var_kw) and approval_grant is not None:
            kwargs["approval_grant"] = approval_grant
        return self.registry.execute(tool_name, arguments, **kwargs)

    @staticmethod
    def _resume_role(principal: Any) -> UserRole:
        if principal is None:
            return UserRole.OPERATOR
        return principal.role

    def _trace_events(self, state: ReActRunState) -> list[TraceEvent]:
        if self.checkpoint_store is None:
            return []
        return self.checkpoint_store.list_trace_events(state.request.run_id)

    def _next_sequence(self, state: ReActRunState) -> int:
        return len(self._trace_events(state)) + 1

    def _append_trace(self, state: ReActRunState, event: TraceEvent) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.append_trace_event(event)

    def _append_node_trace(
        self,
        state: ReActRunState,
        *,
        node: str,
        status: str,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        now = self._clock()
        error = None
        if error_code or status in {"failed", "timeout", "denied"}:
            error = TraceError(
                category="safety",
                code=error_code or status,
                message=(message or error_code or status)[:500],
            )
        event = TraceEvent(
            trace_id=state.request.trace_id,
            run_id=state.request.run_id,
            sequence=self._next_sequence(state),
            event_type="node",
            status=status,  # type: ignore[arg-type]
            node=node,
            latency_ms=0,
            started_at=now,
            finished_at=now,
            error=error,
            metadata={"raw_chain_of_thought_stored": False, "runner_version": REACT_RUNNER_VERSION},
        )
        self._append_trace(state, event)

    def _append_observe_trace(self, state: ReActRunState, observation: Observation, *, step_id: str) -> None:
        """把结构化 Observation 记为 react_observe；不写入原始思维链。"""

        now = self._clock()
        status = "completed" if observation.status is ObservationStatus.OK else "failed"
        error = None
        if status != "completed":
            error = TraceError(
                category="runtime",
                code=str(observation.state_delta.get("code") or "observation_error"),
                message=observation.summary[:500],
            )
        event = TraceEvent(
            trace_id=state.request.trace_id,
            run_id=state.request.run_id,
            sequence=self._next_sequence(state),
            event_type="node",
            status=status,  # type: ignore[arg-type]
            node="react_observe",
            latency_ms=0,
            started_at=now,
            finished_at=now,
            error=error,
            metadata={
                "step_id": step_id,
                "observation_summary": observation.summary,
                "observation_status": observation.status.value,
                "raw_chain_of_thought_stored": False,
                "runner_version": REACT_RUNNER_VERSION,
            },
        )
        self._append_trace(state, event)

    def _append_model_trace(
        self,
        state: ReActRunState,
        *,
        node: str,
        status: str,
        prompt_id: str,
        prompt_version: str,
        usage: tuple[int, int, int],
        latency_ms: int,
        started_at: datetime,
        finished_at: datetime,
        model_version: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        error = None
        if error_code:
            error = TraceError(category="model", code=error_code, message=(message or error_code)[:500])
        event = TraceEvent(
            trace_id=state.request.trace_id,
            run_id=state.request.run_id,
            sequence=self._next_sequence(state),
            event_type="model",
            status=status,  # type: ignore[arg-type]
            node=node,
            model_version=model_version,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=usage[2],
            latency_ms=latency_ms,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
            metadata={
                "raw_chain_of_thought_stored": False,
                "runner_version": REACT_RUNNER_VERSION,
                **(metadata or {}),
            },
        )
        self._append_trace(state, event)

    def _append_model_node_trace(self, state: ReActRunState, result: Any, *, node: str) -> None:
        before = getattr(result, "usage_before", None)
        after = getattr(result, "usage_after", None)
        input_tokens = max(0, int(getattr(after, "input_tokens", 0) or 0) - int(getattr(before, "input_tokens", 0) or 0))
        output_tokens = max(
            0, int(getattr(after, "output_tokens", 0) or 0) - int(getattr(before, "output_tokens", 0) or 0)
        )
        started = getattr(result, "started_at", self._clock())
        finished = getattr(result, "finished_at", started)
        self._append_model_trace(
            state,
            node=node,
            status="completed",
            prompt_id=getattr(result, "prompt_id", None) or "amr.p005.understand_goal",
            prompt_version=getattr(result, "prompt_version", None) or P005_PROMPT_VERSION,
            usage=(input_tokens, output_tokens, input_tokens + output_tokens),
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
            started_at=started,
            finished_at=finished,
            model_version=getattr(result, "model_alias", None),
        )

    def _append_tool_trace(
        self,
        state: ReActRunState,
        result: ToolResult,
        *,
        node: str,
        parameters: Mapping[str, Any],
    ) -> None:
        error = None
        if result.error is not None:
            error = TraceError(
                category=result.error.category.value,
                code=result.error.code,
                message=result.error.message[:500],
                retryable=result.error.retryable,
            )
        status = (
            "completed"
            if result.status is ToolResultStatus.SUCCESS
            else "timeout"
            if result.status is ToolResultStatus.TIMEOUT
            else "denied"
            if result.status is ToolResultStatus.DENIED
            else "failed"
        )
        event = TraceEvent(
            trace_id=state.request.trace_id,
            run_id=state.request.run_id,
            sequence=self._next_sequence(state),
            event_type="tool",
            status=status,  # type: ignore[arg-type]
            node=node,
            tool_name=result.tool_name.value,
            tool_version=result.tool_version,
            latency_ms=max(0, int((result.finished_at - result.started_at).total_seconds() * 1000)),
            started_at=result.started_at,
            finished_at=result.finished_at,
            parameters_digest=canonical_json_digest(to_jsonable(parameters)),
            output_digest=result.output_digest,
            error=error,
            metadata={"raw_chain_of_thought_stored": False, "runner_version": REACT_RUNNER_VERSION},
        )
        self._append_trace(state, event)

    def _persist(self, state: ReActRunState, *, stage: str, status: str) -> None:
        if self.checkpoint_store is None:
            return
        snapshot = CheckpointSnapshot(
            checkpoint_id=f"cp_{uuid4().hex}",
            run_id=state.request.run_id,
            stage=stage,
            status=status,
            plan_version=1,
            current_task_id=REACT_DISPATCH_TASK_ID if state.hitl_interrupt is not None else None,
            graph_state={
                "runner_version": REACT_RUNNER_VERSION,
                "terminal_status": None if state.terminal_status is None else state.terminal_status.value,
                "evidence_digest": state.evidence_digest,
                "loop_iterations": state.loop_iterations,
            },
            saved_at=self._clock(),
        )
        self.checkpoint_store.save_checkpoint(snapshot)

    def _build_result(self, state: ReActRunState) -> ReActRunResult:
        status = state.terminal_status or ReActTerminalStatus.FAILED
        events = [item.model_dump(mode="json") for item in self._trace_events(state)]
        if not events:
            now = self._clock().isoformat()
            events = [
                {
                    "sequence": 1,
                    "event_type": "node",
                    "node": "react_finish",
                    "status": status.value,
                    "started_at": now,
                    "finished_at": now,
                    "metadata": {"raw_chain_of_thought_stored": False},
                }
            ]
        dispatch = next(
            (
                item
                for item in reversed(state.tool_results)
                if item.tool_name is ToolName.DISPATCH_SIMULATION and item.output is not None
            ),
            None,
        )
        simulation_status = None
        completed_orders = 0
        charged = False
        if dispatch is not None:
            simulation = SimulationResult.model_validate(dispatch.output)
            simulation_status = str(getattr(simulation.status, "value", simulation.status))
            completed_orders = sum(
                str(getattr(item.status, "value", item.status)) == "completed" for item in simulation.orders
            )
            if state.task_contract is not None:
                charged = self._charging_completed(simulation, state.task_contract)
        validator_errors = 0 if state.validator_valid else int(not state.validator_valid)
        model_calls = sum(1 for event in events if event.get("event_type") == "model")
        effects = [item.effect_id for item in state.tool_results if item.effect_id]
        return ReActRunResult(
            run_id=state.request.run_id,
            trace_id=state.request.trace_id,
            terminal_status=status,
            terminal_code=state.terminal_code,
            terminal_reason=state.terminal_reason,
            state=state,
            trace_events=events,
            tool_results=list(state.tool_results),
            model_call_count=model_calls,
            retry_count=state.retry_count,
            dispatch_count=state.dispatch_count,
            recovery_action_count=state.recovery_action_count,
            simulation_status=simulation_status,
            completed_order_count=completed_orders,
            validator_error_count=validator_errors,
            charged=charged,
            side_effect_ids=list(dict.fromkeys(effects)),
        )


class ReActSafetyError(RuntimeError):
    """工具执行前的确定性拒绝。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


__all__ = ["REACT_NODE_PROMPT", "REACT_SYSTEM_PROMPT", "ReActRunner", "ReActSafetyError"]
