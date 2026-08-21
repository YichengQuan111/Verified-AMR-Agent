"""五个不依赖 LangGraph 的独立 Prompt 节点执行入口。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast

from pydantic import BaseModel

from agent.context.budget import (
    estimate_message_tokens,
    estimate_text_tokens,
    evaluate_budget_before_call,
)
from agent.context.contracts import (
    BudgetUsage,
    FinalReport,
    NodeContext,
    NodeExecutionResult,
    NodeRoute,
    ObservationVerification,
    PlanTasksOutput,
    PromptNodeName,
    ReplanOutput,
)
from agent.context.prompt_registry import PromptDefinition, get_prompt_definition
from agent.planning.contracts import TaskContract
from services.model_gateway.exceptions import ModelGenerationTimeoutError
from services.model_gateway.protocols import ModelProviderProtocol


Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


def _utc_now() -> datetime:
    """集中提供带时区当前时间，方便单元测试注入固定时钟。"""

    return datetime.now(timezone.utc)


class StandalonePromptNode:
    """组合 Prompt、预算门禁与 ModelProvider 的最小同步节点。

    该类没有 LangGraph、Checkpoint 或路由器依赖；测试和未来状态图都调用同一个
    ``run`` 方法，因此“脱离 LangGraph 可测试”不是另写一套模拟逻辑。
    """

    def __init__(
        self,
        definition: PromptDefinition,
        provider: ModelProviderProtocol,
        *,
        clock: Clock = _utc_now,
        monotonic_clock: MonotonicClock = time.monotonic,
    ) -> None:
        self.definition = definition
        self.provider = provider
        self._clock = clock
        self._monotonic_clock = monotonic_clock

    def build_messages(self, context: NodeContext):
        """公开消息构造入口，便于无模型测试审查实际上下文。"""

        return self.definition.build_messages(context)

    def run(self, context: NodeContext) -> NodeExecutionResult[BaseModel]:
        """先做预算门禁，再执行一次受 Schema 约束的模型调用。"""

        messages = self.build_messages(context)
        estimated_input_tokens = estimate_message_tokens(messages)
        context_digest = hashlib.sha256(
            "\n".join(message.content for message in messages).encode("utf-8")
        ).hexdigest()
        started_at = self._clock()
        usage_before = context.budget.usage.model_copy(deep=True)

        decision = evaluate_budget_before_call(
            node_name=self.definition.node_name,
            budget=context.budget,
            estimated_input_tokens=estimated_input_tokens,
            requested_output_tokens=context.requested_output_tokens,
        )
        if not decision.allowed:
            return NodeExecutionResult(
                node_name=self.definition.node_name,
                prompt_id=self.definition.prompt_id,
                prompt_version=self.definition.version,
                route=cast(NodeRoute, decision.route),
                output=None,
                reason_code=decision.reason_code,
                reason=decision.reason,
                context_digest=context_digest,
                estimated_input_tokens=estimated_input_tokens,
                usage_before=usage_before,
                usage_after=usage_before.model_copy(deep=True),
                started_at=started_at,
                finished_at=self._clock(),
                model_alias=None,
            )

        monotonic_started = self._monotonic_clock()
        try:
            generation = self.provider.generate_structured(
                messages,
                self.definition.response_model,
                max_output_tokens=context.requested_output_tokens,
                timeout_seconds=context.budget.remaining_seconds,
            )
        except ModelGenerationTimeoutError:
            elapsed = max(0.0, self._monotonic_clock() - monotonic_started)
            usage_after = self._advance_usage(
                usage_before,
                input_tokens=0,
                output_tokens=0,
                elapsed_seconds=elapsed,
                count_replan=self.definition.node_name is PromptNodeName.REPLAN,
            )
            return NodeExecutionResult(
                node_name=self.definition.node_name,
                prompt_id=self.definition.prompt_id,
                prompt_version=self.definition.version,
                route=NodeRoute.FALLBACK,
                output=None,
                reason_code="TIME_BUDGET_EXCEEDED",
                reason="模型调用达到本节点剩余时间上限",
                context_digest=context_digest,
                estimated_input_tokens=estimated_input_tokens,
                usage_before=usage_before,
                usage_after=usage_after,
                started_at=started_at,
                finished_at=self._clock(),
                model_alias=None,
            )

        elapsed = max(0.0, self._monotonic_clock() - monotonic_started)
        # total_usage 包含首次生成和可能的一次 Schema 修复，避免预算漏记修复调用。
        input_tokens = generation.total_usage.input_tokens or estimated_input_tokens
        output_tokens = generation.total_usage.output_tokens or estimate_text_tokens(
            generation.call.content
        )
        usage_after = self._advance_usage(
            usage_before,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_seconds=elapsed,
            count_replan=self.definition.node_name is PromptNodeName.REPLAN,
        )

        post_route = self._post_call_budget_route(context, usage_after)
        if post_route is not None:
            route, reason_code, reason = post_route
            output = None
        else:
            route = NodeRoute.SUCCESS
            reason_code = None
            reason = None
            output = generation.value

        return NodeExecutionResult(
            node_name=self.definition.node_name,
            prompt_id=self.definition.prompt_id,
            prompt_version=self.definition.version,
            route=route,
            output=output,
            reason_code=reason_code,
            reason=reason,
            context_digest=context_digest,
            estimated_input_tokens=estimated_input_tokens,
            usage_before=usage_before,
            usage_after=usage_after,
            started_at=started_at,
            finished_at=self._clock(),
            model_alias=generation.call.version.served_alias,
        )

    @staticmethod
    def _advance_usage(
        usage: BudgetUsage,
        *,
        input_tokens: int,
        output_tokens: int,
        elapsed_seconds: float,
        count_replan: bool,
    ) -> BudgetUsage:
        """生成新的累计用量，不原地修改调用方提供的预算快照。"""

        return BudgetUsage(
            input_tokens=usage.input_tokens + input_tokens,
            output_tokens=usage.output_tokens + output_tokens,
            tool_steps=usage.tool_steps,
            elapsed_seconds=usage.elapsed_seconds + elapsed_seconds,
            replans=usage.replans + int(count_replan),
            retries=usage.retries,
        )

    @staticmethod
    def _post_call_budget_route(
        context: NodeContext,
        usage_after: BudgetUsage,
    ) -> tuple[NodeRoute, str, str] | None:
        """用模型返回的真实 usage 和实测耗时执行第二道预算门禁。"""

        limits = context.budget.limits
        if usage_after.input_tokens > limits.max_input_tokens:
            return (
                NodeRoute.FALLBACK,
                "ACTUAL_INPUT_TOKEN_BUDGET_EXCEEDED",
                "模型报告的实际输入 Token 超过任务合同预算",
            )
        if usage_after.output_tokens > limits.max_output_tokens:
            return (
                NodeRoute.FALLBACK,
                "ACTUAL_OUTPUT_TOKEN_BUDGET_EXCEEDED",
                "模型报告的实际输出 Token 超过任务合同预算",
            )
        if usage_after.elapsed_seconds > limits.max_total_seconds:
            return (
                NodeRoute.FALLBACK,
                "ACTUAL_TIME_BUDGET_EXCEEDED",
                "模型调用后的实际累计时长超过任务合同预算",
            )
        if usage_after.replans > limits.max_replans:
            return (
                NodeRoute.HUMAN,
                "REPLAN_BUDGET_EXCEEDED",
                "重规划调用后超过自动重规划预算",
            )
        return None


def _run_named_node(
    node_name: PromptNodeName,
    provider: ModelProviderProtocol,
    context: NodeContext,
) -> NodeExecutionResult[BaseModel]:
    """五个具名入口共享同一执行器，但各自绑定独立 Prompt 和输出模型。"""

    return StandalonePromptNode(get_prompt_definition(node_name), provider).run(context)


def understand_goal(
    provider: ModelProviderProtocol, context: NodeContext
) -> NodeExecutionResult[TaskContract]:
    return cast(
        NodeExecutionResult[TaskContract],
        _run_named_node(PromptNodeName.UNDERSTAND_GOAL, provider, context),
    )


def plan_tasks(
    provider: ModelProviderProtocol, context: NodeContext
) -> NodeExecutionResult[PlanTasksOutput]:
    return cast(
        NodeExecutionResult[PlanTasksOutput],
        _run_named_node(PromptNodeName.PLAN_TASKS, provider, context),
    )


def verify_observation(
    provider: ModelProviderProtocol, context: NodeContext
) -> NodeExecutionResult[ObservationVerification]:
    return cast(
        NodeExecutionResult[ObservationVerification],
        _run_named_node(PromptNodeName.VERIFY_OBSERVATION, provider, context),
    )


def replan(
    provider: ModelProviderProtocol, context: NodeContext
) -> NodeExecutionResult[ReplanOutput]:
    return cast(
        NodeExecutionResult[ReplanOutput],
        _run_named_node(PromptNodeName.REPLAN, provider, context),
    )


def compose_report(
    provider: ModelProviderProtocol, context: NodeContext
) -> NodeExecutionResult[FinalReport]:
    return cast(
        NodeExecutionResult[FinalReport],
        _run_named_node(PromptNodeName.COMPOSE_REPORT, provider, context),
    )


__all__ = [
    "StandalonePromptNode",
    "compose_report",
    "plan_tasks",
    "replan",
    "understand_goal",
    "verify_observation",
]
