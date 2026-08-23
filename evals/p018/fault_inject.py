"""在线异常例的评测侧故障注入；不进入生产 ToolRegistry 参数。

生产 ``dispatch_simulation`` 的 ``faults`` 必须保持空序列。本模块只包装评测
Registry 的 ``execute``：对指定工具前 N 次返回失败 ``ToolResult``，之后放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent.runtime.checkpoint import canonical_json_digest
from agent.tools import (
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
)
from evals.p018.contracts import EvalCase, EvalOutcome


@dataclass(frozen=True, slots=True)
class EvalFaultInjectSpec:
    """一条评测故障：打在哪个工具上、失败几次、用什么稳定码。"""

    tool_name: ToolName
    fail_times: int
    code: str
    message: str
    category: ToolErrorCategory
    retryable: bool
    status: ToolResultStatus = ToolResultStatus.FAILED


# 8 个期望完成的异常例才注入；007/008 继续 sidecar，不走本表。
_SCENARIO_SPECS: dict[str, EvalFaultInjectSpec] = {
    "low_battery": EvalFaultInjectSpec(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        fail_times=1,
        code="battery_below_new_task_threshold",
        message="评测注入：指定 AMR 电量低于新任务阈值",
        category=ToolErrorCategory.UNSAFE_PLAN,
        retryable=False,
    ),
    "amr_offline": EvalFaultInjectSpec(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        fail_times=1,
        code="amr_offline",
        message="评测注入：指定 AMR 离线",
        category=ToolErrorCategory.UNAVAILABLE,
        retryable=False,
    ),
    "channel_closed": EvalFaultInjectSpec(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        fail_times=1,
        code="channel_closed",
        message="评测注入：通道封闭，旧路线不可行",
        category=ToolErrorCategory.UNSAFE_PLAN,
        retryable=False,
    ),
    "workstation_occupied": EvalFaultInjectSpec(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        fail_times=3,
        code="workstation_occupied",
        message="评测注入：工位占用，先重试再重规划",
        category=ToolErrorCategory.UNAVAILABLE,
        retryable=True,
    ),
    "tool_timeout": EvalFaultInjectSpec(
        tool_name=ToolName.RETRIEVE_KNOWLEDGE,
        fail_times=1,
        code="tool_timeout",
        message="评测注入：无副作用检索超时",
        category=ToolErrorCategory.TIMEOUT,
        retryable=True,
        status=ToolResultStatus.TIMEOUT,
    ),
    "plan_infeasible": EvalFaultInjectSpec(
        tool_name=ToolName.VALIDATE_FLEET_PLAN,
        fail_times=1,
        code="plan_infeasible",
        message="评测注入：Validator 判定计划不可行",
        category=ToolErrorCategory.UNSAFE_PLAN,
        retryable=False,
    ),
    "replan_preserves_completed_effect": EvalFaultInjectSpec(
        tool_name=ToolName.PLAN_MULTI_AMR_ROUTES,
        fail_times=1,
        code="plan_infeasible",
        message="评测注入：路线不可行，保留已完成 allocate",
        category=ToolErrorCategory.UNSAFE_PLAN,
        retryable=False,
    ),
}


def inject_spec_for_case(case: EvalCase) -> EvalFaultInjectSpec | None:
    """仅 8 个期望完成的异常例返回注入规格；重复副作用例只写 fault_code。"""

    if case.expected_outcome is not EvalOutcome.COMPLETED:
        return None
    return _SCENARIO_SPECS.get(case.scenario)


class FaultInjectingRegistry:
    """委托真实 Registry；目标工具前 N 次失败，之后走原 handler。"""

    def __init__(self, inner: Any, spec: EvalFaultInjectSpec) -> None:
        self._inner = inner
        self._spec = spec
        self._remaining = spec.fail_times

    @property
    def approval_verifier(self) -> Any:
        """PEVR 把验证器写在 Registry 上；必须写到内层生产 Registry。"""

        return getattr(self._inner, "approval_verifier", None)

    @approval_verifier.setter
    def approval_verifier(self, value: Any) -> None:
        self._inner.approval_verifier = value

    @property
    def security_required(self) -> bool:
        return bool(getattr(self._inner, "security_required", False))

    @security_required.setter
    def security_required(self, value: bool) -> None:
        self._inner.security_required = value

    def get(self, tool_name: ToolName | str) -> Any:
        return self._inner.get(tool_name)

    def specs(self) -> tuple[Any, ...]:
        return self._inner.specs()

    def execute(
        self,
        tool_name: ToolName | str,
        arguments: Any,
        *,
        role: Any = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        principal: Any = None,
        approval_grant: Any = None,
        **kwargs: Any,
    ) -> ToolResult:
        # 关键字必须与生产 ToolRegistry.execute 对齐，否则 PEVR 用
        # inspect.signature 判断时不会把 HITL grant 传进来。
        forwarded = {
            **kwargs,
            "role": role,
            "call_id": call_id,
            "idempotency_key": idempotency_key,
            "principal": principal,
            "approval_grant": approval_grant,
        }
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        if name is self._spec.tool_name and self._remaining > 0:
            self._remaining -= 1
            now = datetime.now(timezone.utc)
            call_id = str(forwarded.get("call_id") or f"eval-fault-{name.value}")
            return ToolResult(
                tool_name=name,
                call_id=call_id,
                status=self._spec.status,
                output=None,
                error=ToolError(
                    category=self._spec.category,
                    code=self._spec.code,
                    message=self._spec.message,
                    retryable=self._spec.retryable,
                    details={"eval_fault_inject": True, "remaining_after": self._remaining},
                ),
                started_at=now,
                finished_at=now,
                duration_ms=1,
                evidence_refs=[f"eval-fault://{self._spec.code}"],
                effect_id=None,
                tool_version="eval-inject.v1",
                principal_role=forwarded.get("role"),
                input_digest=canonical_json_digest(arguments if isinstance(arguments, dict) else {}),
                output_digest=None,
                idempotency_key=forwarded.get("idempotency_key") or call_id,
                audit_metadata={"eval_fault_inject": self._spec.code},
            )
        return self._inner.execute(
            tool_name,
            arguments,
            **{key: value for key, value in forwarded.items() if value is not None},
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


__all__ = [
    "EvalFaultInjectSpec",
    "FaultInjectingRegistry",
    "inject_spec_for_case",
]
