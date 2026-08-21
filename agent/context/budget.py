"""独立 Prompt 节点的确定性预算估算和路由策略。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from agent.context.contracts import (
    BudgetSnapshot,
    NodeRoute,
    PromptNodeName,
)
from services.model_gateway.contracts import ChatMessage


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """预算门禁结果；route=None 表示允许调用模型。"""

    route: NodeRoute | None
    reason_code: str | None
    reason: str | None

    @property
    def allowed(self) -> bool:
        return self.route is None


def estimate_text_tokens(text: str) -> int:
    """用固定规则估算 Token，不依赖某个模型私有 tokenizer。

    ASCII 文本按约 4 字符/Token，非 ASCII（尤其中文）按 1 字符/Token 估算。
    这是前置门禁的保守近似；真实用量仍以模型返回 usage 为准并做后置检查。
    """

    ascii_characters = sum(ord(character) < 128 for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, math.ceil(ascii_characters / 4) + non_ascii_characters)


def estimate_message_tokens(messages: list[ChatMessage]) -> int:
    """估算 system/user 消息内容及每条消息的少量格式开销。"""

    return sum(estimate_text_tokens(message.content) + 4 for message in messages)


def evaluate_budget_before_call(
    *,
    node_name: PromptNodeName,
    budget: BudgetSnapshot,
    estimated_input_tokens: int,
    requested_output_tokens: int,
) -> BudgetDecision:
    """在任何模型调用前返回 allow、fallback 或 human。

    重规划次数耗尽会转人工；其余硬预算不足转 fallback。compose_report 不需要
    新工具步骤，因此即使工具额度为零仍允许在剩余 Token/时间内生成报告。
    """

    if node_name is PromptNodeName.REPLAN and budget.remaining_replans <= 0:
        return BudgetDecision(
            NodeRoute.HUMAN,
            "REPLAN_BUDGET_EXHAUSTED",
            "自动重规划次数已耗尽，需要人工处理",
        )
    if budget.usage.retries > budget.limits.max_retries:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "RETRY_BUDGET_EXCEEDED",
            "普通工具重试次数已经超过任务合同预算",
        )
    if budget.already_exceeded:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "BUDGET_ALREADY_EXCEEDED",
            "调用前累计用量已经超过任务合同预算",
        )
    if budget.remaining_seconds <= 0:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "TIME_BUDGET_EXHAUSTED",
            "任务总时间预算已耗尽",
        )
    if estimated_input_tokens > budget.remaining_input_tokens:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "INPUT_TOKEN_BUDGET_EXCEEDED",
            "当前 Prompt 估算输入 Token 超过剩余额度",
        )
    if requested_output_tokens > budget.remaining_output_tokens:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "OUTPUT_TOKEN_BUDGET_EXCEEDED",
            "请求的输出 Token 超过剩余额度",
        )
    tool_dependent_nodes = {
        PromptNodeName.PLAN_TASKS,
        PromptNodeName.VERIFY_OBSERVATION,
        PromptNodeName.REPLAN,
    }
    if node_name in tool_dependent_nodes and budget.remaining_tool_steps <= 0:
        return BudgetDecision(
            NodeRoute.FALLBACK,
            "TOOL_BUDGET_EXHAUSTED",
            "后续执行所需的工具步数预算已耗尽",
        )
    return BudgetDecision(None, None, None)


__all__ = [
    "BudgetDecision",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "evaluate_budget_before_call",
]
