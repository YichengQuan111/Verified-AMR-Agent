"""使用真实本地 Qwen 验证五个 P0-05 2-shot Prompt 节点。

该脚本只构造一次节点所需的有限上下文，不读取聊天历史、数据库或外部工具。
它既检查 Pydantic 结构化输出，也检查输入中的关键业务事实没有被示例污染。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel


# 允许直接执行 ``python scripts/smoke_p005_prompts.py``，无需 editable 安装。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent.context import (
    BudgetUsage,
    ContextEvidence,
    DynamicStateSnapshot,
    FinalReport,
    NodeContext,
    NodeExecutionResult,
    NodeRoute,
    ObservationVerification,
    P005_PROMPT_VERSION,
    PlanTasksOutput,
    PromptNodeName,
    ReplanOutput,
    build_node_context,
    compose_report,
    plan_tasks,
    replan,
    understand_goal,
    verify_observation,
)
from agent.planning import (
    ApprovalRequirement,
    ExecutionBudgets,
    TaskConstraints,
    TaskContract,
)
from domains.amr_warehouse import TransportOrder
from services.config import load_settings
from services.model_gateway import ModelProvider, StructuredGeneration


NodeRunner = Callable[[Any, NodeContext], NodeExecutionResult[Any]]
SemanticCheck = Callable[[BaseModel], None]


class RecordingProvider:
    """透明转发真实 Provider，并保留最近一次生成的修复与 Token 证据。"""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.last_generation: StructuredGeneration[Any] | None = None

    def generate_structured(
        self,
        messages,
        response_model,
        *,
        max_output_tokens=None,
        timeout_seconds=None,
    ):
        """保持节点依赖的最小协议，不开放额外模型参数。"""

        generation = self.provider.generate_structured(
            messages,
            response_model,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        self.last_generation = generation
        return generation


def _budgets() -> ExecutionBudgets:
    """给在线冒烟留出足够额度，避免把 Prompt 质量与预算门禁混为一谈。"""

    return ExecutionBudgets(
        max_total_seconds=600,
        max_input_tokens=30_000,
        max_output_tokens=5_000,
        max_tool_steps=20,
        max_replans=2,
    )


def _order() -> TransportOrder:
    """返回与五个节点共享的非示例订单，便于检测 2-shot 内容复制。"""

    return TransportOrder(
        order_id="ORDER-LIVE-701",
        material_id="MAT-LIVE-701",
        pickup="P3",
        dropoff="S4",
        priority=4,
        release_time=5,
        deadline=180,
        dependencies=[],
    )


def _contract() -> TaskContract:
    """构造 plan_tasks 节点需要的已验证低风险合同。"""

    return TaskContract(
        contract_id="CONTRACT-LIVE-701",
        goal="在截止时间前把 MAT-LIVE-701 从 P3 运到 S4",
        orders=[_order()],
        environment_ref="warehouse_v1@state-live-701",
        constraints=TaskConstraints(
            map_width=30,
            map_height=20,
            blocked_cells=[],
            minimum_battery_percent=25,
            maximum_load_kg=100,
            enforce_time_windows=True,
        ),
        completion_criteria=["ORDER-LIVE-701 在仿真时间 180 秒前到达 S4"],
        risk_level="low",
        approval=ApprovalRequirement(
            required=False,
            reason=None,
            required_role=None,
        ),
        budgets=_budgets(),
        missing_information=[],
    )


def _dynamic_state(now: datetime) -> DynamicStateSnapshot:
    """提供带版本和时间的最小实时车队摘要。"""

    return DynamicStateSnapshot(
        snapshot_id="fleet-live-701",
        snapshot_version="701",
        environment_ref="warehouse_v1@state-live-701",
        observed_at=now - timedelta(seconds=30),
        payload={
            "online_amrs": ["AMR-LIVE-02"],
            "available_amrs": ["AMR-LIVE-02"],
        },
    )


def _tool_evidence(now: datetime) -> ContextEvidence:
    """把工具事实与来源、版本、观测时间和引用一起传入 Prompt。"""

    return ContextEvidence(
        source_type="tool",
        source_id="CALL-LIVE-701",
        source_version="get_fleet_state@1.0.0/state-701",
        observed_at=now - timedelta(seconds=30),
        collected_at=now - timedelta(seconds=20),
        citation="tool://CALL-LIVE-701",
        content={
            "status": "success",
            "online_amrs": ["AMR-LIVE-02"],
            "available_amrs": ["AMR-LIVE-02"],
        },
    )


def _context(
    node_name: PromptNodeName,
    node_input: dict[str, Any],
    *,
    now: datetime,
    include_tool_evidence: bool = False,
    usage: BudgetUsage | None = None,
) -> NodeContext:
    """为单个节点构造当前上下文，不把前一个模型输出自动串成历史。"""

    return build_node_context(
        node_name=node_name,
        request_id=f"REQ-LIVE-{node_name.value}",
        node_input=node_input,
        budget_limits=_budgets(),
        budget_usage=usage,
        requested_output_tokens=1024,
        dynamic_state=_dynamic_state(now),
        tool_evidence=[_tool_evidence(now)] if include_tool_evidence else [],
        generated_at=now,
    )


def _build_cases(now: datetime) -> list[tuple[PromptNodeName, NodeRunner, NodeContext, SemanticCheck]]:
    """定义五个相互独立的在线样例及其关键语义断言。"""

    order_payload = _order().model_dump(mode="json")
    contract_payload = _contract().model_dump(mode="json")

    def check_understand(output: BaseModel) -> None:
        assert isinstance(output, TaskContract)
        assert output.contract_id == "CONTRACT-LIVE-701"
        assert output.environment_ref == "warehouse_v1@state-live-701"
        assert [(item.order_id, item.pickup, item.dropoff) for item in output.orders] == [
            ("ORDER-LIVE-701", "P3", "S4")
        ]

    def check_plan(output: BaseModel) -> None:
        assert isinstance(output, PlanTasksOutput)
        allowed_tools = {"get_fleet_state", "allocate_tasks"}
        assert {task.tool_name.value for task in output.tasks} <= allowed_tools
        assert output.plan_version == 1

    def check_verify(output: BaseModel) -> None:
        assert isinstance(output, ObservationVerification)
        assert output.observation_id == "OBS-LIVE-701"
        assert output.verified is True
        assert output.decision.value in {"continue", "finish"}

    def check_replan(output: BaseModel) -> None:
        assert isinstance(output, ReplanOutput)
        assert output.previous_plan_version == 1
        assert output.new_plan_version == 2
        assert output.trigger_observation_id == "OBS-LIVE-702"
        assert output.invalidated_task_ids == ["TASK-LIVE-BLOCKED"]
        assert output.requires_human is False
        assert output.replacement_tasks

    def check_report(output: BaseModel) -> None:
        assert isinstance(output, FinalReport)
        assert output.run_id == "RUN-LIVE-701"
        assert output.final_status.value == "completed"
        assert output.completed_order_ids == ["ORDER-LIVE-701"]
        assert output.incomplete_order_ids == []

    return [
        (
            PromptNodeName.UNDERSTAND_GOAL,
            understand_goal,
            _context(
                PromptNodeName.UNDERSTAND_GOAL,
                {
                    "raw_request": "把 MAT-LIVE-701 从 P3 运到 S4",
                    "contract_id": "CONTRACT-LIVE-701",
                    "orders": [order_payload],
                    "environment_ref": "warehouse_v1@state-live-701",
                    "constraints": {
                        "map_width": 30,
                        "map_height": 20,
                        "blocked_cells": [],
                        "minimum_battery_percent": 25,
                        "maximum_load_kg": 100,
                        "enforce_time_windows": True,
                    },
                    "risk_hint": "low",
                    "approval_required": False,
                    "completion_criteria": [
                        "ORDER-LIVE-701 在仿真时间 180 秒前到达 S4"
                    ],
                },
                now=now,
            ),
            check_understand,
        ),
        (
            PromptNodeName.PLAN_TASKS,
            plan_tasks,
            _context(
                PromptNodeName.PLAN_TASKS,
                {
                    "task_contract": contract_payload,
                    "available_tool_names": ["get_fleet_state", "allocate_tasks"],
                    "planning_instruction": "先读取最新车队状态，再分配订单；不要执行工具",
                },
                now=now,
                include_tool_evidence=True,
            ),
            check_plan,
        ),
        (
            PromptNodeName.VERIFY_OBSERVATION,
            verify_observation,
            _context(
                PromptNodeName.VERIFY_OBSERVATION,
                {
                    "current_task": {
                        "task_id": "TASK-LIVE-GET",
                        "completion_criteria": ["获得版本 701 的在线车队状态"],
                    },
                    "observation": {
                        "observation_id": "OBS-LIVE-701",
                        "status": "ok",
                        "summary": "已获得版本 701 的在线车队状态，AMR-LIVE-02 在线",
                        "evidence_refs": ["tool://CALL-LIVE-701"],
                    },
                },
                now=now,
                include_tool_evidence=True,
            ),
            check_verify,
        ),
        (
            PromptNodeName.REPLAN,
            replan,
            _context(
                PromptNodeName.REPLAN,
                {
                    "current_plan_version": 1,
                    "trigger_observation": {
                        "observation_id": "OBS-LIVE-702",
                        "status": "blocked",
                        "summary": "AMR-LIVE-01 离线，TASK-LIVE-BLOCKED 无法继续",
                        "affected_entities": ["AMR-LIVE-01", "TASK-LIVE-BLOCKED"],
                        "evidence_refs": ["event://OBS-LIVE-702"],
                    },
                    "retained_task_ids": ["TASK-LIVE-DONE"],
                    "invalidated_task_ids": ["TASK-LIVE-BLOCKED"],
                    "available_amr_ids": ["AMR-LIVE-02"],
                    "allowed_tool_names": ["plan_multi_amr_routes"],
                    "environment_ref": "warehouse_v1@state-live-701",
                },
                now=now,
                include_tool_evidence=True,
            ),
            check_replan,
        ),
        (
            PromptNodeName.COMPOSE_REPORT,
            compose_report,
            _context(
                PromptNodeName.COMPOSE_REPORT,
                {
                    "run_id": "RUN-LIVE-701",
                    "run_status": "completed",
                    "state_version": "run:RUN-LIVE-701/plan:1",
                    "plan_version": 1,
                    "verified_completed_order_ids": ["ORDER-LIVE-701"],
                    "incomplete_order_ids": [],
                    "evidence_refs": ["event://OBS-LIVE-FINISH-701"],
                    "unresolved_risks": [],
                    "requested_language": "zh-CN",
                },
                now=now,
                usage=BudgetUsage(
                    input_tokens=900,
                    output_tokens=200,
                    tool_steps=3,
                    elapsed_seconds=40,
                    replans=0,
                ),
            ),
            check_report,
        ),
    ]


def main() -> int:
    """执行启动门禁和五节点在线测试，全部通过才返回零。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("fast", "smart"), default="fast")
    args = parser.parse_args()

    environment = dict(os.environ)
    environment["LLM_PROFILE"] = args.profile
    environment.pop("LLM_MODEL", None)
    settings = load_settings(environ=environment)
    provider = ModelProvider(settings.model_gateway)
    version = provider.startup()
    recording_provider = RecordingProvider(provider)
    print(
        f"Validated profile={version.profile} alias={version.served_alias} "
        f"prompt_version={P005_PROMPT_VERSION}"
    )

    success = 0
    cases = _build_cases(datetime.now(timezone.utc))
    for index, (node_name, runner, context, semantic_check) in enumerate(cases, start=1):
        recording_provider.last_generation = None
        try:
            result = runner(recording_provider, context)
            assert result.route is NodeRoute.SUCCESS, (
                f"route={result.route.value} reason_code={result.reason_code} "
                f"reason={result.reason}"
            )
            assert result.prompt_version == P005_PROMPT_VERSION
            assert result.model_alias == version.served_alias
            assert result.output is not None
            semantic_check(result.output)
            generation = recording_provider.last_generation
            assert generation is not None
            success += 1
            print(
                f"[{index}/5] PASS node={node_name.value} "
                f"output={type(result.output).__name__} "
                f"attempts={generation.attempts} repaired={generation.repaired} "
                f"tokens={generation.total_usage.total_tokens}"
            )
        # 单节点失败后继续，最终报告可以同时展示其余 Prompt 的真实状态。
        except Exception as exc:
            print(f"[{index}/5] FAIL node={node_name.value}: {type(exc).__name__}: {exc}")

    print(f"\nResult: {success}/{len(cases)}")
    return 0 if success == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
