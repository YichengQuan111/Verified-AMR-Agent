"""P0-19 在线 ReAct 评测适配器。

复用 P0-18 的数据集装配、JWT、加难地图和计分，但主路径调用独立
``ReActRunner``，禁止实例化或运行 ``PEVRGraphRunner``。
"""

from __future__ import annotations

from typing import Any

from agent.runtime.checkpoint import InMemoryRuntimeStore
from agent.runtime.hitl import InMemoryHITLStore
from agent.tools import UserRole, build_tool_registry
from agent.tools.snapshots import InMemoryExecutionStateStore
from evals.p018.contracts import EvalCase, EvalOutcome, ZeroToleranceMetrics
from evals.p018.fault_inject import FaultInjectingRegistry, inject_spec_for_case
from evals.p018.hard_map import HARD_ENVIRONMENT_REF
from evals.p018.online import OnlineControlStrategy, OnlineFastHarness, _as_str, _now
from evals.p018.oracle import POSITIVE_OUTCOMES
from agent.context.shared_prefix import SHARED_PREFIX_ID, SHARED_PREFIX_VERSION
from evals.p019.react_contracts import (
    REACT_PROMPT_ID,
    REACT_PROMPT_VERSION,
    REACT_RUNNER_VERSION,
    ReActInterrupt,
    ReActRequest,
    ReActRunResult,
    ReActTerminalStatus,
)
from evals.p019.react_runner import ReActRunner


class ReActOnlineHarness(OnlineFastHarness):
    """评测层独立 ReAct；sidecar/RAG/注入反例仍复用父类分流。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("control_strategy", OnlineControlStrategy.REACT)
        super().__init__(**kwargs)
        if self.control_strategy is not OnlineControlStrategy.REACT:
            raise ValueError("ReActOnlineHarness 必须使用 react 控制策略身份")
        self.reproducibility = {
            **self.reproducibility,
            "prompt_versions": {
                **dict(self.reproducibility.get("prompt_versions") or {}),
                REACT_PROMPT_ID: REACT_PROMPT_VERSION,
                SHARED_PREFIX_ID: SHARED_PREFIX_VERSION,
            },
            "react_runner_version": REACT_RUNNER_VERSION,
            "react_uses_pevr_runner": False,
        }

    def _run_pevr_case(
        self,
        case: EvalCase,
        *,
        auto_approve: bool = True,
    ) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int, list[dict[str, Any]]]:
        """Retrieve 之后走独立 ReAct 循环，不调用生产 PEVR 图。"""

        principal = self._principal(case, UserRole.OPERATOR)
        snapshot_provider = self._snapshot_provider(case)
        extra_count = self._extra_obstacle_count(case)
        checkpoints = InMemoryRuntimeStore()
        execution_store = InMemoryExecutionStateStore()
        hitl = InMemoryHITLStore()
        registry_kwargs: dict[str, Any] = {
            "settings": self.settings,
            "snapshot_provider": snapshot_provider,
            "execution_store": execution_store,
            "principal": principal,
            "security_required": True,
        }
        if case.scenario == "charging":
            registry_kwargs["simulator"] = self._charging_simulator(case)
        registry: Any = build_tool_registry(**registry_kwargs)
        inject = inject_spec_for_case(case)
        if inject is not None:
            registry = FaultInjectingRegistry(registry, inject)
        runner = ReActRunner(
            self.provider,
            registry=registry,
            snapshot_provider=snapshot_provider,
            checkpoint_store=checkpoints,
            hitl_store=hitl,
            security_required=True,
        )
        run_id = f"ol-react-{case.case_id}"[:64]
        request = ReActRequest(
            run_id=run_id,
            trace_id=f"trace-p019-react-{case.case_id}"[:128],
            raw_request=self._natural_language(case),
            environment_ref=HARD_ENVIRONMENT_REF,
            seed=case.seed,
            principal=principal,
            requested_output_tokens=4096,
        )
        resumes = 0
        current = request
        while True:
            try:
                result = runner.run(current)
                break
            except ReActInterrupt as interrupted:
                if not auto_approve:
                    try:
                        hitl.reject(interrupted.interrupt.approval_id, principal=principal)
                    except Exception:
                        pass
                    events = self._merge_trace_events(
                        self._checkpoint_events(checkpoints, run_id),
                        self._interrupt_events(case, interrupted, extra_count),
                    )
                    return (
                        EvalOutcome.BLOCKED,
                        "approval_rejected",
                        "评测按用例拒绝审批，dispatch 未恢复",
                        {
                            "model_call_count": self._model_calls_from_events(events),
                            "online_mode": "react_rejected",
                            "extra_obstacle_count": extra_count,
                            "agent_completed": 0,
                            "react_runner_version": REACT_RUNNER_VERSION,
                            "react_uses_pevr_runner": False,
                        },
                        ZeroToleranceMetrics(),
                        [],
                        0,
                        0,
                        0,
                        events,
                    )
                if resumes >= 3:
                    return self._react_failure(
                        case,
                        extra_count,
                        checkpoints,
                        run_id,
                        interrupted,
                        resumes=resumes,
                    )
                grant = hitl.approve(interrupted.interrupt.approval_id, principal=principal)
                resumes += 1
                current = request.model_copy(update={"approval_grant": grant})
            except Exception as exc:
                return self._react_failure(
                    case,
                    extra_count,
                    checkpoints,
                    run_id,
                    exc,
                    resumes=resumes,
                )
        return self._react_success(case, extra_count, result, resumes)

    def _react_success(
        self,
        case: EvalCase,
        extra_count: int,
        result: ReActRunResult,
        resumes: int,
    ) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int, list[dict[str, Any]]]:
        """把独立 ReAct 终态映射到 P0-18 在线观察口径。"""

        zero = self._zero_from_react(result)
        unique_effects = list(dict.fromkeys(result.side_effect_ids))
        if len(result.side_effect_ids) != len(unique_effects):
            zero = zero.model_copy(
                update={"duplicate_side_effect_count": len(result.side_effect_ids) - len(unique_effects)}
            )
        if case.scenario == "charging":
            if result.charged and zero.total() == 0:
                observed = EvalOutcome.CHARGED
            elif result.terminal_status is ReActTerminalStatus.BLOCKED:
                observed = EvalOutcome.BLOCKED
            else:
                observed = EvalOutcome.FAILED
        elif (
            result.terminal_status is ReActTerminalStatus.COMPLETED
            and result.simulation_status == "completed"
            and zero.total() == 0
        ):
            observed = EvalOutcome.COMPLETED
        elif result.terminal_status is ReActTerminalStatus.BLOCKED:
            observed = EvalOutcome.BLOCKED
        else:
            observed = EvalOutcome.FAILED
        code = None if observed in POSITIVE_OUTCOMES else (result.terminal_code or result.terminal_status.value)
        reason = None if code is None else (result.terminal_reason or "独立 ReAct 未达到预期终态")
        events = list(result.trace_events) or [
            {
                "sequence": 1,
                "event_type": "node",
                "node": "react_finish",
                "status": result.terminal_status.value,
            }
        ]
        recovery_ok = int(observed is case.expected_outcome)
        metrics = {
            "model_call_count": self._model_calls_from_events(events),
            "online_mode": "react",
            "extra_obstacle_count": extra_count,
            "agent_completed": int(observed is EvalOutcome.COMPLETED),
            "normal_order_completed": int(observed is EvalOutcome.COMPLETED and case.scenario == "normal_order"),
            "charging_completed": int(observed is EvalOutcome.CHARGED),
            "validator_error_count": int(result.validator_error_count),
            "simulation_status": result.simulation_status,
            "completed_order_count": int(result.completed_order_count),
            "plan_version": 1,
            "recovery_terminal_correct": recovery_ok,
            "recovery_replan_success": 0,
            "react_recovery_action_count": int(result.recovery_action_count),
            "react_runner_version": REACT_RUNNER_VERSION,
            "react_uses_pevr_runner": False,
            "react_production_path_touched": False,
            "trace_complete": 1,
        }
        return (
            observed,
            code,
            reason,
            metrics,
            zero,
            unique_effects,
            0,
            int(result.retry_count),
            resumes,
            events,
        )

    def _react_failure(
        self,
        case: EvalCase,
        extra_count: int,
        checkpoints: InMemoryRuntimeStore,
        run_id: str,
        exc: Exception,
        *,
        resumes: int,
    ) -> tuple[EvalOutcome, str | None, str | None, dict[str, Any], ZeroToleranceMetrics, list[str], int, int, int, list[dict[str, Any]]]:
        """ReAct 异常也保留已提交 Trace，零容忍只统计已派发冲突。"""

        code = getattr(exc, "code", None) or type(exc).__name__
        reason = str(exc)[:500] or "独立 ReAct 失败"
        terminal_time = _now()
        terminal_event = {
            "sequence": 0,
            "event_type": "node",
            "node": "react_terminal",
            "status": "failed",
            "latency_ms": 0,
            "started_at": terminal_time.isoformat(),
            "finished_at": terminal_time.isoformat(),
            "error": {"category": "runtime", "code": str(code)[:128], "message": reason},
            "metadata": {"case_id": case.case_id, "react_uses_pevr_runner": False},
        }
        events = self._merge_trace_events(self._checkpoint_events(checkpoints, run_id), [terminal_event])
        observed = EvalOutcome.FAILED
        return (
            observed,
            str(code)[:128],
            reason,
            {
                "model_call_count": self._model_calls_from_events(events),
                "online_mode": "react_failed",
                "extra_obstacle_count": extra_count,
                "agent_completed": 0,
                "recovery_terminal_correct": int(observed is case.expected_outcome),
                "react_uses_pevr_runner": False,
            },
            ZeroToleranceMetrics(),
            [],
            0,
            0,
            resumes,
            events,
        )

    @staticmethod
    def _zero_from_react(result: ReActRunResult) -> ZeroToleranceMetrics:
        """已完成派发才计碰撞；Validator 拒绝不算现场撞车。"""

        codes: list[str] = []
        for item in result.tool_results:
            if item.error is not None:
                codes.append(item.error.code)
        blob = " ".join(codes).lower()
        vertex = sum("vertex" in code and "conflict" in code for code in codes) + blob.count("vertex_collision")
        edge = sum("edge" in code and "conflict" in code for code in codes)
        forbidden = sum("forbidden" in code for code in codes)
        battery = sum("battery" in code or "low_battery" in code for code in codes)
        if result.validator_error_count == 0 and result.simulation_status == "completed":
            return ZeroToleranceMetrics()
        return ZeroToleranceMetrics(
            vertex_collision_count=max(0, vertex),
            edge_collision_count=max(0, edge),
            forbidden_zone_entry_count=max(0, forbidden),
            low_battery_violation_count=max(0, battery),
        )

    def _interrupt_events(self, case: EvalCase, interrupted: ReActInterrupt, extra_count: int) -> list[dict[str, Any]]:
        """审批拒绝时补一条可定位终态事件。"""

        now = _now()
        return [
            {
                "sequence": 0,
                "event_type": "node",
                "node": "react_hitl",
                "status": "denied",
                "latency_ms": 0,
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "error": {
                    "category": "safety",
                    "code": "approval_rejected",
                    "message": f"HITL {interrupted.interrupt.approval_id} 被评测拒绝",
                },
                "metadata": {
                    "case_id": case.case_id,
                    "extra_obstacle_count": extra_count,
                    "react_uses_pevr_runner": False,
                },
            }
        ]


__all__ = ["ReActOnlineHarness"]
