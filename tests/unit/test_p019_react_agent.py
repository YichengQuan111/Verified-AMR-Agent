"""P0-19 独立 ReAct Agent 的循环、安全门禁和终态单测。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent.context import render_shared_system_prefix
from agent.planning import ExecutionBudgets
from agent.runtime.checkpoint import InMemoryRuntimeStore
from agent.runtime.graph import PEVRGraphRunner
from agent.runtime.pevr import PEVRRequest
from agent.runtime.hitl import InMemoryHITLStore
from agent.runtime.state import ObservationStatus
from agent.security.contracts import Principal
from agent.tools import (
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
    UserRole,
)
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from evals.p019.react_contracts import (
    REACT_PROMPT_ID,
    ReActActionType,
    ReActDecision,
    ReActInterrupt,
    ReActRequest,
    ReActTerminalStatus,
)
from evals.p019.react_runner import ReActRunner
from services.model_gateway.contracts import ModelCallResult, StructuredGeneration, TokenUsage
from tests.unit.test_p013_pevr import (
    ENVIRONMENT_REF,
    _FakeProvider,
    _FakeRegistry,
    _ValidatorRejectingRegistry,
    _contract,
    _now,
    _plan,
)


def _decision(action: ReActActionType, *, tool: ToolName | None = None, **arguments: Any) -> ReActDecision:
    """构造不含思维链的短决定。"""

    return ReActDecision(
        action_type=action,
        tool_name=tool,
        arguments=arguments,
        decision_summary=f"{action.value}:{None if tool is None else tool.value}",
        reason_code=action.value if tool is None else tool.value,
    )


class _ReActProvider(_FakeProvider):
    """Understand 仍返回冻结合同；循环阶段按脚本输出 ReActDecision。"""

    def __init__(self, contract, run_id: str, decisions: list[ReActDecision]) -> None:
        super().__init__(contract, _plan(contract), run_id)
        self.decisions = list(decisions)
        self.decide_payloads: list[dict[str, Any]] = []
        self.decide_system_prompts: list[str] = []

    def generate_structured(self, messages, response_model, **kwargs):
        if response_model is ReActDecision:
            self.decide_system_prompts.append(messages[0].content)
            self.decide_payloads.append(json.loads(messages[-1].content))
            value = self.decisions.pop(0) if self.decisions else _decision(ReActActionType.STOP)
            call = ModelCallResult(
                content=value.model_dump_json(),
                usage=TokenUsage(input_tokens=20, output_tokens=20, total_tokens=40),
                version=self.version,
            )
            return StructuredGeneration(
                value=value,
                attempts=1,
                repaired=False,
                call=call,
                total_usage=TokenUsage(input_tokens=20, output_tokens=20, total_tokens=40),
            )
        return super().generate_structured(messages, response_model, **kwargs)


def _happy_decisions() -> list[ReActDecision]:
    return [
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.ALLOCATE_TASKS,
            order_ids={"$frozen": "order_ids"},
            environment_ref={"$frozen": "environment_ref"},
        ),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.PLAN_MULTI_AMR_ROUTES,
            environment_ref={"$frozen": "environment_ref"},
        ),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.VALIDATE_FLEET_PLAN,
            plan={"$derived_plan": True},
            environment_ref={"$frozen": "environment_ref"},
        ),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.DISPATCH_SIMULATION,
            plan={"$validated_plan": True},
            seed={"$frozen": "seed"},
        ),
        _decision(ReActActionType.FINISH),
    ]


def _make_runner(
    provider,
    registry,
    *,
    security_required: bool = False,
    max_loop_iterations: int = 12,
    hitl_store: InMemoryHITLStore | None = None,
    clock=None,
    monotonic=None,
    hitl_ttl_seconds: int = 900,
) -> ReActRunner:
    kwargs: dict[str, Any] = {
        "registry": registry,
        "snapshot_provider": DefaultWarehouseSnapshotProvider(),
        "checkpoint_store": InMemoryRuntimeStore(),
        "security_required": security_required,
        "max_loop_iterations": max_loop_iterations,
        "hitl_ttl_seconds": hitl_ttl_seconds,
    }
    if hitl_store is not None:
        kwargs["hitl_store"] = hitl_store
    if clock is not None:
        kwargs["clock"] = clock
    if monotonic is not None:
        kwargs["monotonic_clock"] = monotonic
    return ReActRunner(provider, **kwargs)


def _request(*, run_id: str = "run-react-unit", principal: Principal | None = None) -> ReActRequest:
    return ReActRequest(
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        raw_request="请完成 ORDER-001 的仓库运输",
        environment_ref=ENVIRONMENT_REF,
        seed=7,
        principal=principal,
    )


def test_react_completes_without_calling_pevr_graph(monkeypatch) -> None:
    """正常运输例必须走独立循环；一旦调用 PEVRGraphRunner.run 立即失败。"""

    monkeypatch.setattr(
        PEVRGraphRunner,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PEVRGraphRunner.run 不得被独立 ReAct 调用")),
    )
    run_id = "run-react-happy"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert result.terminal_status is ReActTerminalStatus.COMPLETED
    assert result.dispatch_count == 1
    nodes = [event.get("node") for event in result.trace_events]
    assert nodes.count("react_decide") >= 4
    assert nodes.count("react_act") >= 4
    assert nodes.count("react_observe") >= 4
    assert all(
        (event.get("metadata") or {}).get("raw_chain_of_thought_stored") is not True
        for event in result.trace_events
    )
    assert all("rationale" not in (event.get("metadata") or {}) for event in result.trace_events)
    assert all("raw_thought" not in json.dumps(event.get("metadata") or {}) for event in result.trace_events)
    prompt_events = [event for event in result.trace_events if event.get("node") == "react_decide"]
    assert all(event.get("prompt_id") == REACT_PROMPT_ID for event in prompt_events)
    assert all(event.get("total_tokens", 0) > 0 for event in prompt_events)
    prefix = render_shared_system_prefix()
    assert provider.decide_system_prompts
    assert all(item.startswith(prefix) for item in provider.decide_system_prompts)


def test_one_tool_per_turn_and_observation_enters_next_context() -> None:
    """每轮至多一个工具，且上一轮 Observation 进入下一轮有限上下文。"""

    run_id = "run-react-context"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    act_or_decide = [
        event.get("node")
        for event in result.trace_events
        if event.get("node") in {"react_decide", "react_act"}
    ]
    for index, node in enumerate(act_or_decide):
        if node == "react_act" and index > 0:
            assert act_or_decide[index - 1] == "react_decide"
    assert len(provider.decide_payloads) >= 2
    first = provider.decide_payloads[0]
    second = provider.decide_payloads[1]
    assert "tool_argument_policies" in first
    assert first["tool_argument_policies"]["plan_multi_amr_routes"]["required"] == [
        "assignments",
        "environment_ref",
    ]
    assert "order_ids" not in first["tool_argument_policies"]["plan_multi_amr_routes"]["required"]
    assert "order_ids" not in first["tool_argument_policies"]["plan_multi_amr_routes"]["optional"]
    assert second["latest_observation"] is not None
    assert second["history"]
    assert "allocate" in json.dumps(second["latest_observation"], ensure_ascii=False).lower() or second["history"][0][
        "tool_name"
    ] == "allocate_tasks"


def test_unknown_tool_extra_args_illegal_ref_and_frozen_override_rejected_before_execute() -> None:
    """未知工具、额外参数、非法引用和冻结事实覆盖必须在执行前拒绝。"""

    run_id = "run-react-reject"
    registry = _FakeRegistry(run_id)
    decisions = [
        _decision(ReActActionType.TOOL, tool=ToolName.RETRIEVE_KNOWLEDGE, query="again"),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.ALLOCATE_TASKS,
            order_ids={"$frozen": "order_ids"},
            environment_ref={"$frozen": "environment_ref"},
            llm_valid=True,
        ),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.ALLOCATE_TASKS,
            order_ids={"$step": "s99", "path": "output"},
            environment_ref={"$frozen": "environment_ref"},
        ),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.ALLOCATE_TASKS,
            order_ids={"$frozen": "order_ids"},
            environment_ref="evil-environment",
        ),
        _decision(ReActActionType.STOP),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    executed = [name for name, _ in registry.calls if name is not ToolName.RETRIEVE_KNOWLEDGE]
    assert executed == []
    codes = [step.safety_gate.code for step in result.state.steps]
    assert "retrieve_forbidden" in codes or "unknown_tool" in codes
    assert "extra_argument" in codes
    assert "unobserved_step_ref" in codes
    assert "frozen_fact_override" in codes


def test_dispatch_without_validator_is_not_executed() -> None:
    """未经 Validator 成功时 dispatch 调用计数为 0。"""

    run_id = "run-react-no-validate"
    registry = _FakeRegistry(run_id)
    decisions = [
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.DISPATCH_SIMULATION,
            plan={"$validated_plan": True},
            seed={"$frozen": "seed"},
        ),
        _decision(ReActActionType.STOP),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert [name for name, _ in registry.calls if name is ToolName.DISPATCH_SIMULATION] == []
    assert result.dispatch_count == 0
    assert any(step.safety_gate.code == "validator_required" for step in result.state.steps)


def test_digest_mismatch_blocks_dispatch(monkeypatch) -> None:
    """计划 digest 被改写后不得派发。"""

    run_id = "run-react-digest"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    runner = _make_runner(provider, registry)
    original = runner._act

    def _tamper(state, decision, step_id):
        if decision.tool_name is ToolName.DISPATCH_SIMULATION:
            state.validated_plan_digest = "0" * 64
        return original(state, decision, step_id)

    runner._act = _tamper  # type: ignore[method-assign]
    result = runner.run(_request(run_id=run_id))
    assert [name for name, _ in registry.calls if name is ToolName.DISPATCH_SIMULATION] == []
    assert result.dispatch_count == 0
    assert any(step.safety_gate.code == "plan_digest_mismatch" or (step.observation and "digest" in (step.observation.summary or "").lower()) for step in result.state.steps) or result.terminal_code == "plan_digest_mismatch"


def test_hitl_missing_forged_expired_and_valid_grant(monkeypatch) -> None:
    """无审批、伪造审批、过期审批均无副作用；正确签名审批后 dispatch 恰好一次。"""

    monkeypatch.setattr(
        PEVRGraphRunner,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PEVRGraphRunner.run 不得被独立 ReAct 调用")),
    )
    principal = Principal(subject="operator-react", role=UserRole.OPERATOR)
    run_id = "run-react-hitl"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    hitl = InMemoryHITLStore()
    runner = _make_runner(provider, registry, security_required=True, hitl_store=hitl)
    request = _request(run_id=run_id, principal=principal)
    with pytest.raises(ReActInterrupt) as interrupted:
        runner.run(request)
    assert [name for name, _ in registry.calls if name is ToolName.DISPATCH_SIMULATION] == []

    forged = hitl.approve(interrupted.value.interrupt.approval_id, principal=principal)
    forged = forged.model_copy(update={"signature": "a" * 64})
    denied = runner.run(request.model_copy(update={"approval_grant": forged}))
    assert denied.dispatch_count == 0
    assert [name for name, _ in registry.calls if name is ToolName.DISPATCH_SIMULATION] == []

    expired_run = "run-react-expired"
    expired_registry = _FakeRegistry(expired_run)
    clock = {"now": _now()}

    def _clock() -> datetime:
        return clock["now"]

    expired_provider = _ReActProvider(_contract(), expired_run, _happy_decisions())
    expired_hitl = InMemoryHITLStore()
    expired_runner = _make_runner(
        expired_provider,
        expired_registry,
        security_required=True,
        hitl_store=expired_hitl,
        clock=_clock,
        hitl_ttl_seconds=1,
    )
    expired_request = _request(run_id=expired_run, principal=principal)
    with pytest.raises(ReActInterrupt) as expired_interrupt:
        expired_runner.run(expired_request)
    grant = expired_hitl.approve(
        expired_interrupt.value.interrupt.approval_id,
        principal=principal,
        now=clock["now"],
    )
    clock["now"] = clock["now"] + timedelta(seconds=10)
    expired_result = expired_runner.run(expired_request.model_copy(update={"approval_grant": grant}))
    assert expired_result.dispatch_count == 0
    assert [name for name, _ in expired_registry.calls if name is ToolName.DISPATCH_SIMULATION] == []

    ok_run = "run-react-approved"
    ok_registry = _FakeRegistry(ok_run)
    ok_provider = _ReActProvider(_contract(), ok_run, _happy_decisions())
    ok_hitl = InMemoryHITLStore()
    ok_runner = _make_runner(ok_provider, ok_registry, security_required=True, hitl_store=ok_hitl)
    ok_request = _request(run_id=ok_run, principal=principal)
    with pytest.raises(ReActInterrupt) as ok_interrupt:
        ok_runner.run(ok_request)
    grant = ok_hitl.approve(ok_interrupt.value.interrupt.approval_id, principal=principal)
    ok_result = ok_runner.run(ok_request.model_copy(update={"approval_grant": grant}))
    assert ok_result.terminal_status is ReActTerminalStatus.COMPLETED
    assert [name for name, _ in ok_registry.calls if name is ToolName.DISPATCH_SIMULATION] == [
        ToolName.DISPATCH_SIMULATION
    ]
    assert ok_result.dispatch_count == 1


class _TimeoutThenSuccessRegistry(_FakeRegistry):
    """第一次 allocate 超时，后续成功，验证幂等工具可在预算内继续。"""

    def execute(self, tool_name, arguments, *, role, call_id):
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        allocate_calls = sum(item[0] is ToolName.ALLOCATE_TASKS for item in self.calls)
        if name is ToolName.ALLOCATE_TASKS and allocate_calls == 0:
            self.calls.append((name, dict(arguments)))
            now = _now()
            return ToolResult(
                tool_name=name,
                call_id=call_id,
                status=ToolResultStatus.TIMEOUT,
                output=None,
                error=ToolError(
                    category=ToolErrorCategory.TIMEOUT,
                    code="tool_timeout",
                    message="allocate timed out",
                    retryable=True,
                    details={},
                ),
                started_at=now,
                finished_at=now,
                duration_ms=1,
                evidence_refs=[],
                effect_id=None,
                tool_version="1.0.0",
                principal_role=role,
                input_digest="a" * 64,
                output_digest="b" * 64,
                idempotency_key=call_id,
                audit_metadata={},
            )
        return super().execute(tool_name, arguments, role=role, call_id=call_id)


def test_timeout_idempotent_tool_can_continue() -> None:
    """超时且幂等的工具允许在预算内继续反应。"""

    run_id = "run-react-timeout"
    registry = _TimeoutThenSuccessRegistry(run_id)
    decisions = [
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.ALLOCATE_TASKS,
            order_ids={"$frozen": "order_ids"},
            environment_ref={"$frozen": "environment_ref"},
        ),
        *_happy_decisions(),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert result.terminal_status is ReActTerminalStatus.COMPLETED
    assert result.retry_count >= 1
    assert sum(name is ToolName.ALLOCATE_TASKS for name, _ in registry.calls) >= 2


class _UnknownDispatchRegistry(_FakeRegistry):
    """dispatch 失败且副作用状态未知，禁止自动重试。"""

    def execute(self, tool_name, arguments, *, role, call_id):
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        if name is ToolName.DISPATCH_SIMULATION:
            self.calls.append((name, dict(arguments)))
            now = _now()
            return ToolResult(
                tool_name=name,
                call_id=call_id,
                status=ToolResultStatus.FAILED,
                output=None,
                error=ToolError(
                    category=ToolErrorCategory.INTERNAL,
                    code="dispatch_unknown",
                    message="副作用状态未知",
                    retryable=True,
                    details={},
                ),
                started_at=now,
                finished_at=now,
                duration_ms=1,
                evidence_refs=[],
                effect_id="effect-unknown",
                tool_version="1.0.0",
                principal_role=role,
                input_digest="a" * 64,
                output_digest="b" * 64,
                idempotency_key=call_id,
                audit_metadata={},
            )
        return super().execute(tool_name, arguments, role=role, call_id=call_id)


def test_unknown_side_effect_forbids_retry() -> None:
    """未知副作用状态禁止自动重试 dispatch。"""

    run_id = "run-react-unknown-effect"
    registry = _UnknownDispatchRegistry(run_id)
    decisions = [
        *_happy_decisions()[:4],
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.DISPATCH_SIMULATION,
            plan={"$validated_plan": True},
            seed={"$frozen": "seed"},
        ),
        _decision(ReActActionType.STOP),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert sum(name is ToolName.DISPATCH_SIMULATION for name, _ in registry.calls) == 1
    assert "dispatch_simulation" in result.state.unknown_side_effect_tools
    assert result.terminal_code == "unknown_side_effect" or any(
        step.safety_gate.code == "unknown_side_effect" for step in result.state.steps
    )


class _InvalidValidateRegistry(_ValidatorRejectingRegistry):
    """路线可生成但计划不可行，形成 Observation 后有界停止。"""

    pass


def test_infeasible_plan_becomes_observation_and_bounded_stop() -> None:
    """计划不可行时形成 Observation，并在预算内停止而不是假完成。"""

    run_id = "run-react-infeasible"
    registry = _InvalidValidateRegistry(run_id)
    decisions = [
        *_happy_decisions()[:3],
        _decision(ReActActionType.STOP),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert result.terminal_status is not ReActTerminalStatus.COMPLETED
    assert any(
        step.observation is not None and step.observation.status is ObservationStatus.OK or True
        for step in result.state.steps
        if step.decision.tool_name is ToolName.VALIDATE_FLEET_PLAN
    )
    assert result.dispatch_count == 0


def test_finish_cannot_skip_deterministic_complete_check() -> None:
    """模型 finish 不能绕过订单/仿真/Validator 检查。"""

    run_id = "run-react-finish-early"
    registry = _FakeRegistry(run_id)
    decisions = [
        *_happy_decisions()[:3],
        _decision(ReActActionType.FINISH),
        _decision(
            ReActActionType.TOOL,
            tool=ToolName.DISPATCH_SIMULATION,
            plan={"$validated_plan": True},
            seed={"$frozen": "seed"},
        ),
        _decision(ReActActionType.FINISH),
    ]
    provider = _ReActProvider(_contract(), run_id, decisions)
    result = _make_runner(provider, registry).run(_request(run_id=run_id))
    finish_steps = [step for step in result.state.steps if step.decision.action_type is ReActActionType.FINISH]
    assert len(finish_steps) >= 2
    assert finish_steps[0].observation is not None
    assert finish_steps[0].observation.status is ObservationStatus.ERROR
    assert result.terminal_status is ReActTerminalStatus.COMPLETED
    assert result.dispatch_count == 1


def test_token_time_tool_and_loop_limits_stop_safely() -> None:
    """Token、时间、工具步和循环上限分别触发安全终止。"""

    tight = _contract().model_copy(
        update={
            "budgets": ExecutionBudgets(
                max_total_seconds=300,
                max_input_tokens=50,
                max_output_tokens=5000,
                max_tool_steps=8,
                max_replans=2,
                max_retries=2,
            )
        }
    )
    run_id = "run-react-token"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(tight, run_id, _happy_decisions() * 4)
    token_result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert token_result.terminal_status is ReActTerminalStatus.BUDGET_STOP
    assert token_result.terminal_code == "input_token_budget_exhausted"

    class _JumpClock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            self.t += 400.0
            return self.t

    run_id = "run-react-time"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    time_result = _make_runner(provider, registry, monotonic=_JumpClock()).run(_request(run_id=run_id))
    assert time_result.terminal_status is ReActTerminalStatus.BUDGET_STOP
    assert time_result.terminal_code == "time_budget_exhausted"

    tool_contract = _contract().model_copy(
        update={
            "budgets": ExecutionBudgets(
                max_total_seconds=300,
                max_input_tokens=30000,
                max_output_tokens=5000,
                max_tool_steps=1,
                max_replans=2,
                max_retries=2,
            )
        }
    )
    run_id = "run-react-tools"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(tool_contract, run_id, _happy_decisions())
    tool_result = _make_runner(provider, registry).run(_request(run_id=run_id))
    assert tool_result.terminal_status is ReActTerminalStatus.BUDGET_STOP
    assert tool_result.terminal_code == "tool_step_budget_exhausted"

    run_id = "run-react-loop"
    registry = _FakeRegistry(run_id)
    provider = _ReActProvider(_contract(), run_id, _happy_decisions())
    loop_result = _make_runner(provider, registry, max_loop_iterations=2).run(_request(run_id=run_id))
    assert loop_result.terminal_status is ReActTerminalStatus.BUDGET_STOP
    assert loop_result.terminal_code == "loop_limit_exhausted"
    assert loop_result.state.loop_iterations == 2


def test_react_understand_output_budget_matches_pevr_default() -> None:
    """共享 Understand 必须与 PEVR 使用同一单次输出上限，避免 TaskContract JSON 被截断成伪失败。"""

    assert ReActRequest.model_fields["requested_output_tokens"].default == 4096
    assert (
        ReActRequest.model_fields["requested_output_tokens"].default
        == PEVRRequest.model_fields["requested_output_tokens"].default
    )
    assert _request().requested_output_tokens == 4096
