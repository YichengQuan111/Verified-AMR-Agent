"""P0-13 PEVR 主图的 mock 单测和 Planner 反例。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from agent.context.contracts import (
    FinalReport,
    FinalReportStatus,
    NodeRoute,
    ObservationVerification,
    PlanTasksOutput,
    VerificationDecision,
)
from agent.planning import (
    ApprovalRequirement,
    ExecutionBudgets,
    FallbackStrategy,
    PlanTask,
    PlanTaskStatus,
    RiskLevel,
    TaskConstraints,
    TaskContract,
)
from agent.planning.validator import (
    canonicalize_normal_pevr_plan,
    validate_normal_pevr_plan,
)
from agent.runtime.graph import PEVRExecutionError, PEVRGraphRunner
from agent.runtime.checkpoint import canonical_json_digest
from agent.runtime.pevr import PEVRRequest, PEVRStage
from agent.runtime.state import RunState
from agent.tools import (
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
    UserRole,
    build_tool_registry,
)
from agent.tools.schemas import (
    AllocationResponse,
    RoutePlanResponse,
)
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider
from domains.amr_warehouse import Heading
from services.amr_simulator.contracts import (
    SimulationOrderState,
    SimulationOrderStatus,
    SimulationResult,
    SimulationStatus,
    RouteStep,
)
from services.model_gateway.contracts import (
    ModelCallResult,
    ModelVersionRecord,
    StructuredGeneration,
    TokenUsage,
)


ENVIRONMENT_REF = "warehouse_v1@seed-v1"


def _now() -> datetime:
    """单测固定使用带时区时间，避免 Observation/ToolResult 产生朦胧时间。"""

    return datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _contract() -> TaskContract:
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(ENVIRONMENT_REF)
    return TaskContract(
        contract_id="CONTRACT-P013-TEST",
        goal="在截止时间前完成 ORDER-001 的仓库运输",
        orders=[snapshot.orders[0]],
        environment_ref=ENVIRONMENT_REF,
        constraints=TaskConstraints(
            map_width=30,
            map_height=20,
            blocked_cells=list(snapshot.blocked_cells),
            minimum_battery_percent=20,
            maximum_load_kg=100,
            enforce_time_windows=True,
        ),
        completion_criteria=["ORDER-001 在仿真中到达交付点"],
        risk_level=RiskLevel.LOW,
        approval=ApprovalRequirement(required=False, reason=None, required_role=None),
        budgets=ExecutionBudgets(
            max_total_seconds=300,
            max_input_tokens=8000,
            max_output_tokens=3000,
            max_tool_steps=8,
            max_replans=0,
        ),
        missing_information=[],
    )


def _task(
    task_id: str,
    tool_name: ToolName,
    dependencies: list[str],
    arguments: dict[str, Any],
    *,
    approval_required: bool = False,
) -> PlanTask:
    """构造只含公共字段的正常任务，测试关注的是数据流和门禁而非文案。"""

    return PlanTask(
        task_id=task_id,
        dependencies=dependencies,
        tool_name=tool_name,
        tool_arguments=arguments,
        target_amr=None,
        pickup=None,
        dropoff=None,
        workstation=None,
        preconditions=[],
        completion_criteria=[f"{tool_name.value} 返回成功证据"],
        time_budget=30,
        energy_budget=0,
        risk_level=RiskLevel.LOW,
        approval_required=approval_required,
        fallback_strategy=FallbackStrategy.FATAL,
        status=PlanTaskStatus.PENDING,
        evidence_refs=[],
        effect_id=None,
    )


def _plan(contract: TaskContract) -> PlanTasksOutput:
    """返回 P0-13 正常四步 DAG，引用由 Executor 在运行期解析。"""

    allocate_id = "TASK-ALLOCATE"
    route_id = "TASK-ROUTE"
    validate_id = "TASK-VALIDATE"
    dispatch_id = "TASK-DISPATCH"
    return PlanTasksOutput(
        plan_version=1,
        tasks=[
            _task(
                allocate_id,
                ToolName.ALLOCATE_TASKS,
                [],
                {"order_ids": ["ORDER-001"], "environment_ref": contract.environment_ref},
            ),
            _task(
                route_id,
                ToolName.PLAN_MULTI_AMR_ROUTES,
                [allocate_id],
                {
                    "assignments": {"$ref": f"task:{allocate_id}/output/assignments"},
                    "environment_ref": contract.environment_ref,
                    "blocked_cells": [
                        item.model_dump(mode="json")
                        for item in contract.constraints.blocked_cells
                    ],
                    "max_time": 120,
                },
            ),
            _task(
                validate_id,
                ToolName.VALIDATE_FLEET_PLAN,
                [route_id],
                {
                    "plan": {"$ref": "derived:simulation_plan"},
                    "environment_ref": contract.environment_ref,
                    "ruleset_version": "p0-10.v1",
                },
            ),
            _task(
                dispatch_id,
                ToolName.DISPATCH_SIMULATION,
                [validate_id],
                {"plan": {"$ref": "derived:simulation_plan"}, "seed": 7},
                approval_required=True,
            ),
        ],
        planning_assumptions=["P0-04 未声明订单重量，执行期使用测试固定值"],
        unresolved_risks=[],
    )


class _FakeProvider:
    """只模拟结构化模型返回，验证主图不会复制 P0-05 Prompt 逻辑。"""

    def __init__(self, contract: TaskContract, plan: PlanTasksOutput, run_id: str) -> None:
        self.contract = contract
        self.plan = plan
        self.run_id = run_id
        self.version = ModelVersionRecord(
            profile="fast",
            configured_alias="qwen3.6-fast",
            served_alias="qwen3.6-fast",
            base_url="http://127.0.0.1:8080/v1",
            openai_sdk_version="3.3.0",
            observed_at=_now(),
        )

    @property
    def version_record(self) -> ModelVersionRecord:
        return self.version

    def startup(self) -> ModelVersionRecord:
        return self.version

    def health_check(self):  # pragma: no cover - P0-13 不调用主动 health check
        raise AssertionError("PEVR runner 不应在图内重复 health check")

    def generate_text(self, messages):  # pragma: no cover - P0-13 只用结构化节点
        raise AssertionError("PEVR runner 不应调用普通文本生成")

    def generate_structured(self, messages, response_model, **kwargs):
        del messages, kwargs
        if response_model is TaskContract:
            value = self.contract
        elif response_model is PlanTasksOutput:
            value = self.plan
        elif response_model is ObservationVerification:
            value = ObservationVerification(
                observation_id="observation://dispatch",
                verified=True,
                decision=VerificationDecision.FINISH,
                reason="仿真完成且全部订单有完成状态证据",
                evidence_refs=[f"tool://{self.run_id}:plan:1:task:TASK-DISPATCH"],
                affected_entities=[],
                next_task_id=None,
            )
        elif response_model is FinalReport:
            value = FinalReport(
                run_id=self.run_id,
                final_status=FinalReportStatus.COMPLETED,
                state_version=f"run:{self.run_id}/plan:1",
                plan_version=1,
                generated_at=_now(),
                summary="正常闭环完成 ORDER-001",
                completed_order_ids=["ORDER-001"],
                incomplete_order_ids=[],
                evidence_refs=[f"tool://{self.run_id}:retrieve"],
                unresolved_risks=[],
                budget_usage=TokenUsage().model_copy(update={}) if False else BudgetUsageForFake(),
            )
        else:  # pragma: no cover - response models are the four P0-05 nodes
            raise AssertionError(response_model)
        content = value.model_dump_json()
        call = ModelCallResult(
            content=content,
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


def BudgetUsageForFake():
    """避免测试 fake 为报告预算造第二套字段；真实图会用实际 node usage 覆盖。"""

    from agent.context.contracts import BudgetUsage

    return BudgetUsage()


class _FakeRegistry:
    """模拟 ToolRegistry 的公共方法，同时保留真实 ToolSpec 门禁。"""

    def __init__(self, run_id: str) -> None:
        self.specs_value = build_tool_registry().specs()
        self.run_id = run_id
        self.calls: list[tuple[ToolName, dict[str, Any]]] = []

    def specs(self):
        return self.specs_value

    def get(self, tool_name):
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        return SimpleNamespace(spec=next(item for item in self.specs_value if item.tool_name is name))

    def execute(self, tool_name, arguments, *, role, call_id):
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        self.calls.append((name, dict(arguments)))
        now = _now()
        if name is ToolName.RETRIEVE_KNOWLEDGE:
            output = {
                "query": arguments["query"],
                "role_scope": "operator",
                "status": "answerable",
                "reason": "测试证据",
                "top_k": 5,
                "minimum_hybrid_score": 0.809,
                "minimum_vector_score": 0.499,
                "top_candidate_score": 0.9,
                "top_candidate_vector_score": 0.9,
                "results": [
                    {
                        "chunk_id": "chunk-p013",
                        "doc_id": "warehouse_transport_sop",
                        "title": "仓储运输 SOP",
                        "section": "执行顺序",
                        "version": "1.0",
                        "role_scope": ["viewer", "operator"],
                        "source": "warehouse_transport_sop.md",
                        "checksum": "a" * 64,
                        "text": "先分配、再规划、验证后仿真，并以交付事件确认完成。",
                        "frozen_at": date(2026, 8, 20).isoformat(),
                        "citation": "warehouse_transport_sop.md#执行顺序",
                        "hybrid_score": 0.9,
                        "vector_score": 0.9,
                        "bm25_score": 0.9,
                        "normalized_vector_score": 0.9,
                        "normalized_bm25_score": 0.9,
                    }
                ],
            }
        elif name is ToolName.ALLOCATE_TASKS:
            output = {
                "algorithm": "hungarian",
                "amr_ids": ["AMR-01"],
                "assignments": [{"amr_id": "AMR-01", "order_id": "ORDER-001", "components": _cost()}],
                "cost_matrix": [[1.0]],
                "order_ids": ["ORDER-001"],
                "pair_evaluations": [],
                "schema_version": "1.0",
                "status": "complete",
                "total_cost": 1.0,
                "unassigned_amrs": [],
                "unassigned_orders": [],
            }
        elif name is ToolName.PLAN_MULTI_AMR_ROUTES:
            output = _route_output()
        elif name is ToolName.VALIDATE_FLEET_PLAN:
            output = {
                "schema_version": "1.0",
                "ruleset_version": "p0-10.v1",
                "status": "valid",
                "valid": True,
                "error_count": 0,
                "errors": [],
            }
        elif name is ToolName.DISPATCH_SIMULATION:
            snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(ENVIRONMENT_REF)
            output = SimulationResult(
                simulation_id="simulation-fake",
                seed=7,
                status=SimulationStatus.COMPLETED,
                start_time=0,
                end_time=10,
                validation_result={"status": "valid", "valid": True, "errors": []},
                amrs=snapshot.amrs,
                orders=[
                    SimulationOrderState(
                        order_id="ORDER-001",
                        status=SimulationOrderStatus.COMPLETED,
                        assigned_amr_id="AMR-01",
                        payload_kg=1.0,
                        pickup_time=2,
                        dropoff_time=10,
                        blocked_reason=None,
                    )
                ],
                workstations=[],
                charging_stations=[],
                observations=[],
                events=[],
            ).model_dump(mode="json")
        else:  # pragma: no cover - normal PEVR validator forbids other tools
            raise AssertionError(name)
        return ToolResult(
            tool_name=name,
            call_id=call_id,
            status=ToolResultStatus.SUCCESS,
            output=output,
            error=None,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            evidence_refs=[f"{name.value}://{call_id}"],
            effect_id=("effect-fake" if name is ToolName.DISPATCH_SIMULATION else None),
            tool_version="1.0.0",
            principal_role=role,
            input_digest=canonical_json_digest(arguments),
            output_digest="c" * 64,
            idempotency_key=call_id,
            audit_metadata={"request_fingerprint": canonical_json_digest(arguments)},
        )


class _SequencePlanProvider(_FakeProvider):
    """按次序返回 Planner 候选，用于验证唯一一次语义修复。"""

    def __init__(self, contract, plans, run_id):
        super().__init__(contract, plans[-1], run_id)
        self._plans = list(plans)
        self.plan_calls = 0

    def generate_structured(self, messages, response_model, **kwargs):
        if response_model is PlanTasksOutput:
            index = min(self.plan_calls, len(self._plans) - 1)
            self.plan_calls += 1
            original = self.plan
            self.plan = self._plans[index]
            try:
                return super().generate_structured(messages, response_model, **kwargs)
            finally:
                self.plan = original
        return super().generate_structured(messages, response_model, **kwargs)


class _ValidatorRejectingRegistry(_FakeRegistry):
    """返回 HTTP/Schema 成功但业务 invalid 的 Validator 反例。"""

    def execute(self, tool_name, arguments, *, role, call_id):
        result = super().execute(tool_name, arguments, role=role, call_id=call_id)
        if result.tool_name is not ToolName.VALIDATE_FLEET_PLAN:
            return result
        output = {
            "schema_version": "1.0",
            "ruleset_version": "p0-10.v1",
            "status": "invalid",
            "valid": False,
            "error_count": 1,
            "errors": [
                {
                    "code": "vertex_conflict",
                    "constraint": "vertex_conflict",
                    "message": "测试冲突",
                    "task_id": "TASK-ROUTE",
                    "related_task_id": "",
                    "order_id": "ORDER-001",
                    "related_order_id": "",
                    "amr_id": "AMR-01",
                    "related_amr_id": "",
                    "coordinate": {"x": 2, "y": 3},
                    "related_coordinate": None,
                    "time": 2,
                    "related_time": None,
                    "observed": None,
                    "limit": None,
                    "path_index": 2,
                    "related_path_index": -1,
                }
            ],
        }
        return result.model_copy(
            update={"output": output, "output_digest": canonical_json_digest(output)}
        )


class _RouteTimeoutRegistry(_FakeRegistry):
    """把路线工具变成稳定 timeout，验证下游 Validator/dispatch 不会运行。"""

    def execute(self, tool_name, arguments, *, role, call_id):
        result = super().execute(tool_name, arguments, role=role, call_id=call_id)
        if result.tool_name is not ToolName.PLAN_MULTI_AMR_ROUTES:
            return result
        return result.model_copy(
            update={
                "status": ToolResultStatus.TIMEOUT,
                "output": None,
                "output_digest": None,
                "error": ToolError(
                    category=ToolErrorCategory.TIMEOUT,
                    code="tool_timeout",
                    message="路线工具测试超时",
                    retryable=True,
                    details={"timeout_seconds": 20},
                ),
            }
        )


class _SimulationMismatchRegistry(_FakeRegistry):
    """返回成功 ToolResult 但订单未完成的仿真反例。"""

    def execute(self, tool_name, arguments, *, role, call_id):
        result = super().execute(tool_name, arguments, role=role, call_id=call_id)
        if result.tool_name is not ToolName.DISPATCH_SIMULATION:
            return result
        output = dict(result.output)
        output["status"] = "blocked"
        output["orders"] = [
            {
                **dict(output["orders"][0]),
                "status": "blocked",
                "dropoff_time": None,
                "blocked_reason": "simulator state mismatch",
            }
        ]
        SimulationResult.model_validate(output)
        return result.model_copy(
            update={"output": output, "output_digest": canonical_json_digest(output)}
        )


def _cost() -> dict[str, float]:
    return {
        "distance_to_pickup": 1.0,
        "route_distance": 1.0,
        "estimated_completion_time": 10.0,
        "lateness_risk": 0.0,
        "priority_bonus": 0.0,
        "battery_risk": 0.0,
        "estimated_battery_after": 90.0,
        "load_penalty": 0.0,
        "total_cost": 1.0,
    }


def _route_output() -> dict[str, Any]:
    path = [
        RouteStep(position={"x": 1, "y": 2}, heading=Heading.NORTH, time=0, action="start", g_cost=0.0),
        RouteStep(position={"x": 2, "y": 2}, heading=Heading.EAST, time=1, action="move", g_cost=1.0),
        RouteStep(position={"x": 2, "y": 3}, heading=Heading.NORTH, time=2, action="move", g_cost=2.0),
        RouteStep(position={"x": 27, "y": 9}, heading=Heading.EAST, time=10, action="move", g_cost=10.0),
    ]
    return RoutePlanResponse(
        algorithm="astar",
        cell_reservation_count=11,
        edge_reservation_count=3,
        planned_count=1,
        routes=[
            {
                "amr_id": "AMR-01",
                "order_id": "ORDER-001",
                "pickup_time": 2,
                "dropoff_time": 10,
                "expanded_states": 10,
                "path": path,
                "priority": 3,
                "reason": None,
                "reason_code": None,
                "status": "planned",
                "total_cost": 10.0,
            }
        ],
        schema_version="1.0",
        status="complete",
        total_cost=10.0,
        total_expanded_states=10,
    ).model_dump(mode="json")


def test_normal_pevr_graph_runs_all_eight_states_with_mocks() -> None:
    """成功路径必须按固定顺序运行，且 dispatch 前的 dataflow 已被解析。"""

    run_id = "run-p013-test"
    contract = _contract()
    registry = _FakeRegistry(run_id)
    runner = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        snapshot_provider=DefaultWarehouseSnapshotProvider(),
        clock=_now,
    )
    result = runner.run(
        PEVRRequest(
            run_id=run_id,
            raw_request="把 MAT-001 从 P1 运到 S3",
            environment_ref=ENVIRONMENT_REF,
            seed=7,
            approval_granted=True,
        )
    )

    assert [event.stage for event in result.stage_trace] == list(PEVRStage)
    assert result.report.final_status is FinalReportStatus.COMPLETED
    assert result.report.plan_version == 1
    assert result.report.completed_order_ids == ["ORDER-001"]
    assert result.report.metrics.graph_stage_count == 8
    assert result.report.metrics.tool_call_count == 5  # RAG + 四个 Planner 任务
    assert [call[0] for call in registry.calls] == [
        ToolName.RETRIEVE_KNOWLEDGE,
        ToolName.ALLOCATE_TASKS,
        ToolName.PLAN_MULTI_AMR_ROUTES,
        ToolName.VALIDATE_FLEET_PLAN,
        ToolName.DISPATCH_SIMULATION,
    ]
    assert "environment_ref" not in registry.calls[-1][1]
    assert isinstance(result.run_state, RunState)
    assert result.run_state.status.value == "completed"
    assert result.trace_id.startswith("trace-")
    assert result.report.trace_id == result.trace_id
    assert [event.sequence for event in result.trace_events] == list(
        range(1, len(result.trace_events) + 1)
    )
    assert sum(event.event_type == "node" for event in result.trace_events) == 8
    assert sum(event.event_type == "model" for event in result.trace_events) == 4
    assert sum(event.event_type == "tool" for event in result.trace_events) == 5
    assert all(event.run_id == run_id for event in result.trace_events)
    assert any(
        event.event_type == "tool"
        and event.tool_name == ToolName.DISPATCH_SIMULATION.value
        and event.task_id == "TASK-DISPATCH"
        and event.evidence_refs
        for event in result.trace_events
    )


def test_plan_jsonvalue_wrappers_are_canonicalized_before_validation() -> None:
    """本地模型的 JsonValue 包装只能被还原为固定 JSON，不能直接执行。"""

    contract = _contract()
    plan = _plan(contract)
    wrapped_tasks = []
    for task in plan.tasks:
        arguments: dict[str, Any] = {}
        for name, value in task.tool_arguments.items():
            if name in {"assignments", "plan"}:
                reference = value["$ref"] if isinstance(value, dict) else value
                arguments[name] = {"type": "ref", "value": reference}
            elif isinstance(value, list):
                arguments[name] = {"type": "array", "value": value}
            elif isinstance(value, int) and not isinstance(value, bool):
                arguments[name] = {"type": "integer", "value": value}
            else:
                arguments[name] = {"type": "string", "value": value}
        wrapped_tasks.append(task.model_copy(update={"tool_arguments": arguments}))
    wrapped = PlanTasksOutput.model_validate(
        {**plan.model_dump(mode="python"), "tasks": wrapped_tasks}
    )

    normalized, notes = canonicalize_normal_pevr_plan(wrapped)

    assert normalized.tasks[0].tool_arguments["environment_ref"] == ENVIRONMENT_REF
    assert normalized.tasks[1].tool_arguments["assignments"] == {
        "$ref": f"task:{normalized.tasks[0].task_id}/output/assignments"
    }
    assert normalized.tasks[3].tool_arguments["seed"] == 7
    assert notes
    validation = validate_normal_pevr_plan(
        contract,
        normalized,
        tool_specs=build_tool_registry().specs(),
        expected_seed=7,
    )
    assert validation.valid is True


def test_fixed_fact_alias_refs_are_resolved_only_from_whitelist() -> None:
    """只解析 P0-13 固定事实别名，任意表达式仍应留给 Validator 拒绝。"""

    contract = _contract()
    plan = _plan(contract)
    allocate_id = plan.tasks[0].task_id
    replacements = {
        ToolName.ALLOCATE_TASKS: {
            "environment_ref": {"$ref": "fixed_execution_facts/environment_ref"},
            "order_ids": {"$ref": "fixed_execution_facts/order_ids"},
        },
        ToolName.PLAN_MULTI_AMR_ROUTES: {
            "environment_ref": {"$ref": "fixed_execution_facts/environment_ref"},
            "blocked_cells": {"$ref": "fixed_execution_facts/blocked_cells"},
            "max_time": {"$ref": "fixed_execution_facts/latest_deadline"},
        },
        ToolName.VALIDATE_FLEET_PLAN: {
            "environment_ref": {"$ref": "fixed_execution_facts/environment_ref"},
            "ruleset_version": {"$ref": "fixed_execution_facts/ruleset_version"},
        },
        ToolName.DISPATCH_SIMULATION: {
            "seed": {"$ref": "fixed_execution_facts/simulation_seed"},
        },
    }
    tasks = [
        task.model_copy(
            update={
                "tool_arguments": {
                    **task.tool_arguments,
                    **replacements.get(task.tool_name, {}),
                }
            }
        )
        for task in plan.tasks
    ]
    wrapped = PlanTasksOutput.model_validate(
        {**plan.model_dump(mode="python"), "tasks": tasks}
    )

    normalized, notes = canonicalize_normal_pevr_plan(
        wrapped,
        contract=contract,
        expected_seed=7,
    )

    assert normalized.tasks[0].tool_arguments["environment_ref"] == ENVIRONMENT_REF
    assert normalized.tasks[1].tool_arguments["max_time"] == 120
    assert normalized.tasks[3].tool_arguments["seed"] == 7
    assert len(notes) == 8
    validation = validate_normal_pevr_plan(
        contract,
        normalized,
        tool_specs=build_tool_registry().specs(),
        expected_seed=7,
    )
    assert validation.valid is True
    assert allocate_id == normalized.tasks[0].task_id


def test_plan_validator_blocks_untrusted_dataflow_before_executor() -> None:
    """非法 assignments 引用必须在任何工具调用前失败。"""

    contract = _contract()
    plan = _plan(contract)
    route = plan.tasks[1]
    invalid_route = route.model_copy(
        update={"tool_arguments": {**route.tool_arguments, "assignments": {"$ref": "python:eval"}}}
    )
    invalid_plan = PlanTasksOutput.model_validate(
        {**plan.model_dump(mode="python"), "tasks": [plan.tasks[0], invalid_route, *plan.tasks[2:]]}
    )
    validation = validate_normal_pevr_plan(contract, invalid_plan, tool_specs=build_tool_registry().specs())

    assert validation.valid is False
    assert any(item.code == "assignment_ref_invalid" for item in validation.errors)


def test_dispatch_requires_trusted_approval_context_before_handler() -> None:
    """P0-12 声明 requires_approval 时，主图不得让 Planner 自己批准。"""

    run_id = "run-p013-no-approval"
    contract = _contract()
    registry = _FakeRegistry(run_id)
    runner = PEVRGraphRunner(
        _FakeProvider(contract, _plan(contract), run_id),
        registry=registry,
        snapshot_provider=DefaultWarehouseSnapshotProvider(),
        clock=_now,
    )

    with pytest.raises(PEVRExecutionError, match="可信审批") as exc_info:
        runner.run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                seed=7,
                approval_granted=False,
            )
        )
    assert exc_info.value.stage is PEVRStage.EXECUTE
    assert exc_info.value.code == "approval_required"
    assert all(name is not ToolName.DISPATCH_SIMULATION for name, _ in registry.calls)


def test_plan_semantic_repair_runs_once_then_still_uses_hard_validator() -> None:
    """Fast 首次固定事实写错时只重问一次，修复计划仍走正式 validate 节点。"""

    run_id = "run-p013-plan-repair"
    contract = _contract()
    valid = _plan(contract)
    invalid_dispatch = valid.tasks[3].model_copy(
        update={
            "tool_arguments": {
                **valid.tasks[3].tool_arguments,
                "seed": 99,
            }
        }
    )
    invalid = valid.model_copy(
        update={"tasks": [*valid.tasks[:3], invalid_dispatch]}
    )
    provider = _SequencePlanProvider(contract, [invalid, valid], run_id)
    registry = _FakeRegistry(run_id)

    result = PEVRGraphRunner(
        provider,
        registry=registry,
        snapshot_provider=DefaultWarehouseSnapshotProvider(),
        clock=_now,
    ).run(
        PEVRRequest(
            run_id=run_id,
            raw_request="把 MAT-001 从 P1 运到 S3",
            environment_ref=ENVIRONMENT_REF,
            seed=7,
            approval_granted=True,
        )
    )

    assert provider.plan_calls == 2
    assert result.report.final_status is FinalReportStatus.COMPLETED
    assert result.report.metrics.model_call_count == 5


def test_plan_semantic_repair_stops_after_one_invalid_retry() -> None:
    """第二份候选仍非法时必须停在 validate，不能继续请求或执行工具 DAG。"""

    run_id = "run-p013-plan-repair-fails"
    contract = _contract()
    plan = _plan(contract)
    invalid_dispatch = plan.tasks[3].model_copy(
        update={"tool_arguments": {**plan.tasks[3].tool_arguments, "seed": 99}}
    )
    invalid = plan.model_copy(update={"tasks": [*plan.tasks[:3], invalid_dispatch]})
    provider = _SequencePlanProvider(contract, [invalid, invalid, plan], run_id)
    registry = _FakeRegistry(run_id)

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            provider,
            registry=registry,
            snapshot_provider=DefaultWarehouseSnapshotProvider(),
            clock=_now,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                seed=7,
                approval_granted=True,
            )
        )

    assert error.value.code == "plan_validation_failed"
    assert provider.plan_calls == 2
    assert [name for name, _ in registry.calls] == [ToolName.RETRIEVE_KNOWLEDGE]


def test_invalid_cpp_validator_result_blocks_dispatch() -> None:
    """Validator 工具即使返回 success 信封，业务 invalid 也必须阻断仿真。"""

    run_id = "run-p013-validator-invalid"
    contract = _contract()
    registry = _ValidatorRejectingRegistry(run_id)

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=registry,
            snapshot_provider=DefaultWarehouseSnapshotProvider(),
            clock=_now,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                approval_granted=True,
            )
        )

    assert error.value.code == "validator_postcondition_failed"
    assert all(name is not ToolName.DISPATCH_SIMULATION for name, _ in registry.calls)


def test_route_timeout_stops_validator_and_dispatch() -> None:
    """路线工具超时后不得沿用旧计划或继续确定性验证/派发。"""

    run_id = "run-p013-route-timeout"
    contract = _contract()
    registry = _RouteTimeoutRegistry(run_id)

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=registry,
            clock=_now,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                approval_granted=True,
            )
        )

    assert error.value.code == "tool_timeout"
    assert [name for name, _ in registry.calls][-1] is ToolName.PLAN_MULTI_AMR_ROUTES


def test_simulator_success_envelope_cannot_hide_incomplete_order() -> None:
    """ToolResult=success 但仿真订单 blocked 时，Verifier/报告不得伪造完成。"""

    run_id = "run-p013-simulation-mismatch"
    contract = _contract()
    registry = _SimulationMismatchRegistry(run_id)

    with pytest.raises(PEVRExecutionError) as error:
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=registry,
            clock=_now,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                approval_granted=True,
            )
        )

    assert error.value.code == "simulation_not_completed"
