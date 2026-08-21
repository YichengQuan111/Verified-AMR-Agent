"""P0-12 九工具注册、预检、跨语言和幂等/超时契约测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agent.tools import (
    ToolDefinition,
    ToolHandlerResponse,
    ToolName,
    ToolRegistry,
    ToolResultStatus,
    UserRole,
    build_tool_registry,
)
from agent.tools.cpp_client import FixedCppJsonClient
from agent.tools.registry import ToolInvocationContext
from agent.tools.schemas import (
    ApprovalRequestOutput,
    FleetStateOutput,
    RetrieveKnowledgeOutput,
    ValidationResponse,
    VerificationSuiteOutput,
)
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider, InMemoryExecutionStateStore
from agent.tools.verification import FixedVerificationRunner, VerificationRunnerError
from domains.amr_warehouse import GridPosition
from services.retrieval.contracts import RetrievalResponse, RetrievalResult, RetrievalStatus


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT_REF = "warehouse_v1@state-001"


class _FakeRetriever:
    """只返回证据不足响应，测试不依赖本地 Embedding/Qdrant 服务。"""

    def retrieve(self, query: str, *, role_scope: UserRole, top_k: int, document_ids: Any) -> RetrievalResponse:
        return RetrievalResponse(
            query=query,
            role_scope=role_scope,
            status=RetrievalStatus.INSUFFICIENT_EVIDENCE,
            reason="测试响应",
            top_k=top_k,
            minimum_hybrid_score=0.809,
            minimum_vector_score=0.499,
            top_candidate_score=None,
            top_candidate_vector_score=None,
            results=[],
        )


def test_registry_contains_exactly_nine_tools_and_explicit_metadata() -> None:
    """注册表必须封闭为路线要求的九个名字，并声明审计/副作用语义。"""

    registry = build_tool_registry(knowledge_retriever=_FakeRetriever())
    assert registry.names == tuple(ToolName)
    assert len(registry.specs()) == 9
    for spec in registry.specs():
        assert spec.input_schema["additionalProperties"] is False
        assert spec.output_schema["additionalProperties"] is False
        assert spec.audit_fields
        assert "input_digest" in spec.audit_fields
        assert "effect_id" in spec.audit_fields
        assert "audit_metadata" in spec.audit_fields
        assert spec.timeout_seconds > 0
        assert {
            "invalid_argument",
            "permission_denied",
            "timeout",
            "conflict",
            "internal",
        } <= {item.value for item in spec.error_categories}
    assert "fault_injection" not in {field for spec in registry.specs() for field in spec.input_schema["properties"]}
    assert registry.get(ToolName.ALLOCATE_TASKS).spec.output_schema["properties"]["algorithm"]["const"] == "hungarian"
    assert registry.get(ToolName.PLAN_MULTI_AMR_ROUTES).spec.output_schema["properties"]["algorithm"]["const"] == "astar"


def test_invalid_arguments_are_rejected_before_handler() -> None:
    """未知顶层参数在输入模型和外部程序之前被拒绝。"""

    registry = build_tool_registry(knowledge_retriever=_FakeRetriever())
    called = 0
    definition = registry.get(ToolName.GET_FLEET_STATE)

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        nonlocal called
        del model, context
        called += 1
        return ToolHandlerResponse(
            output=FleetStateOutput(
                environment_ref=ENVIRONMENT_REF,
                state_version="state-001",
                source="warehouse_seed",
                amrs=[],
            )
        )

    test_registry = ToolRegistry(
        [
            ToolDefinition(
                spec=definition.spec,
                input_model=definition.input_model,
                output_model=definition.output_model,
                handler=handler,
            )
        ]
    )
    result = test_registry.execute(
        ToolName.GET_FLEET_STATE,
        {"environment_ref": ENVIRONMENT_REF, "shell_command": "whoami"},
        role=UserRole.VIEWER,
        call_id="invalid-arguments",
    )
    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.category.value == "invalid_argument"
    assert called == 0


def test_viewer_cannot_execute_operator_only_tools() -> None:
    """角色门禁发生在快照/C++ handler 之前。"""

    registry = build_tool_registry()
    result = registry.execute(
        ToolName.ALLOCATE_TASKS,
        {"environment_ref": ENVIRONMENT_REF, "order_ids": ["ORDER-001"]},
        role=UserRole.VIEWER,
        call_id="viewer-allocation",
    )
    assert result.status is ToolResultStatus.DENIED
    assert result.error is not None
    assert result.error.category.value == "permission_denied"


def test_rag_and_fleet_state_use_unified_tool_result() -> None:
    """纯 Python 工具也必须返回相同的审计字段，而不是私有结果类型。"""

    registry = build_tool_registry(knowledge_retriever=_FakeRetriever())
    rag = registry.execute(
        ToolName.RETRIEVE_KNOWLEDGE,
        {"query": "无答案问题"},
        role=UserRole.VIEWER,
        call_id="rag-1",
    )
    state = registry.execute(
        ToolName.GET_FLEET_STATE,
        {"environment_ref": ENVIRONMENT_REF, "amr_ids": ["AMR-01"]},
        role=UserRole.VIEWER,
        call_id="state-1",
    )
    assert rag.status is ToolResultStatus.SUCCESS
    assert rag.output["status"] == "insufficient_evidence"
    assert state.status is ToolResultStatus.SUCCESS
    assert len(state.output["amrs"]) == 1
    for result in (rag, state):
        assert result.tool_version == "1.0.0"
        assert result.input_digest is not None
        assert result.output_digest is not None
        assert result.audit_metadata["preflight_validated"] is True


def test_duplicate_call_id_returns_cached_effect_without_repeating_request() -> None:
    """审批是副作用工具，但精确重复 call_id 只返回首次 pending 记录。"""

    registry = build_tool_registry()
    arguments = {"run_id": "RUN-1", "task_id": "TASK-1", "reason": "高风险搬运"}
    first = registry.execute(
        ToolName.REQUEST_APPROVAL,
        arguments,
        role=UserRole.OPERATOR,
        call_id="approval-call-1",
    )
    second = registry.execute(
        ToolName.REQUEST_APPROVAL,
        arguments,
        role=UserRole.OPERATOR,
        call_id="approval-call-1",
    )
    assert first.status is ToolResultStatus.SUCCESS
    assert second.status is ToolResultStatus.SUCCESS
    assert second.output == first.output
    assert second.effect_id == first.effect_id


def test_concurrent_duplicate_call_id_executes_handler_once() -> None:
    """并发到达的相同请求必须合并，不能在首次落账前重复副作用。"""

    registry = build_tool_registry()
    original = registry.get(ToolName.GET_FLEET_STATE)
    lock = threading.Lock()
    call_count = 0

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        nonlocal call_count
        del model, context
        with lock:
            call_count += 1
        time.sleep(0.05)
        return ToolHandlerResponse(
            output=FleetStateOutput(
                environment_ref=ENVIRONMENT_REF,
                state_version="state-001",
                source="warehouse_seed",
                amrs=[],
            )
        )

    concurrent_registry = ToolRegistry(
        [
            ToolDefinition(
                spec=original.spec,
                input_model=original.input_model,
                output_model=original.output_model,
                handler=handler,
            )
        ]
    )

    def invoke(_: int):
        return concurrent_registry.execute(
            ToolName.GET_FLEET_STATE,
            {"environment_ref": ENVIRONMENT_REF},
            role=UserRole.VIEWER,
            call_id="concurrent-duplicate",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(invoke, range(2)))
    assert call_count == 1
    assert first.model_dump() == second.model_dump()


def test_call_id_reuse_with_different_payload_is_conflict() -> None:
    """不同或不可序列化载荷不能覆盖原审批或重放未知副作用。"""

    registry = build_tool_registry()
    first = registry.execute(
        ToolName.REQUEST_APPROVAL,
        {"run_id": "RUN-1", "task_id": "TASK-1", "reason": "原因 A"},
        role=UserRole.OPERATOR,
        call_id="approval-call-2",
    )
    malformed = registry.execute(
        ToolName.REQUEST_APPROVAL,
        {"run_id": "RUN-1", "task_id": "TASK-1", "reason": {"not-json"}},
        role=UserRole.OPERATOR,
        call_id="approval-call-2",
    )
    replay = registry.execute(
        ToolName.REQUEST_APPROVAL,
        {"run_id": "RUN-1", "task_id": "TASK-1", "reason": "原因 A"},
        role=UserRole.OPERATOR,
        call_id="approval-call-2",
    )
    second = registry.execute(
        ToolName.REQUEST_APPROVAL,
        {"run_id": "RUN-1", "task_id": "TASK-1", "reason": "原因 B"},
        role=UserRole.OPERATOR,
        call_id="approval-call-2",
    )
    assert first.status is ToolResultStatus.SUCCESS
    assert malformed.status is ToolResultStatus.FAILED
    assert malformed.error is not None
    assert malformed.error.code == "arguments_not_json"
    assert replay.model_dump() == first.model_dump()
    assert second.status is ToolResultStatus.FAILED
    assert second.error is not None
    assert second.error.category.value == "conflict"


def test_dispatch_rejects_fault_injection_before_simulator() -> None:
    """faults 字段不属于正常 Agent 工具表，不能借 dispatch 旁路进入仿真器。"""

    registry = build_tool_registry()
    result = registry.execute(
        ToolName.DISPATCH_SIMULATION,
        {"plan": {}, "seed": 7, "faults": []},
        role=UserRole.OPERATOR,
        call_id="fault-bypass",
    )
    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.category.value == "invalid_argument"
    assert result.error.code == "tool_arguments_not_allowed"


def test_invalid_types_and_ranges_fail_before_backends() -> None:
    """JSON Schema 声明的整数类型和 C++ 时间域上限必须在 handler 前执行。"""

    registry = build_tool_registry(knowledge_retriever=_FakeRetriever())
    bad_top_k = registry.execute(
        ToolName.RETRIEVE_KNOWLEDGE,
        {"query": "test", "top_k": "5"},
        role=UserRole.VIEWER,
        call_id="bad-top-k",
    )
    bad_horizon = registry.execute(
        ToolName.PLAN_MULTI_AMR_ROUTES,
        {
            "environment_ref": ENVIRONMENT_REF,
            "assignments": [{"amr_id": "AMR-01", "order_id": "ORDER-001"}],
            "max_time": 2001,
        },
        role=UserRole.OPERATOR,
        call_id="bad-horizon",
    )
    for result in (bad_top_k, bad_horizon):
        assert result.status is ToolResultStatus.FAILED
        assert result.error is not None
        assert result.error.code == "tool_input_schema_invalid"
        assert result.audit_metadata["preflight_validated"] is False


def test_invalid_role_is_not_audited_as_operator() -> None:
    """无法解析的角色应记录为空，不能在审计中伪装成 operator。"""

    result = build_tool_registry().execute(
        ToolName.GET_FLEET_STATE,
        {"environment_ref": ENVIRONMENT_REF},
        role="administrator",  # type: ignore[arg-type]
        call_id="invalid-role",
    )
    assert result.status is ToolResultStatus.FAILED
    assert result.principal_role is None
    assert result.audit_metadata["preflight_validated"] is False


def test_tool_timeout_is_deterministic_for_stuck_handler() -> None:
    """通用执行器在声明时限返回 timeout，不继续等待不可控的 handler。"""

    registry = build_tool_registry()
    original = registry.get(ToolName.GET_FLEET_STATE)

    def slow_handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        del model, context
        time.sleep(0.2)
        return ToolHandlerResponse(
            output=FleetStateOutput(
                environment_ref=ENVIRONMENT_REF,
                state_version="state-001",
                source="warehouse_seed",
                amrs=[],
            )
        )

    short_spec = original.spec.model_copy(update={"timeout_seconds": 0.01})
    timeout_registry = ToolRegistry(
        [
            ToolDefinition(
                spec=short_spec,
                input_model=original.input_model,
                output_model=original.output_model,
                handler=slow_handler,
            )
        ]
    )
    result = timeout_registry.execute(
        ToolName.GET_FLEET_STATE,
        {"environment_ref": ENVIRONMENT_REF},
        role=UserRole.VIEWER,
        call_id="slow-call",
    )
    assert result.status is ToolResultStatus.TIMEOUT
    assert result.error is not None
    assert result.error.category.value == "timeout"


def test_cpp_client_uses_fixed_argv_and_shell_false() -> None:
    """C++ 适配器不接受命令字符串，并显式关闭 shell。"""

    observed: dict[str, Any] = {}

    def fake_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, '{"status":"ok"}', "")

    client = FixedCppJsonClient(process_runner=fake_runner)
    response = client.validate_plan({"safe": True}, timeout_seconds=1.0)
    assert response == {"status": "ok"}
    assert observed["kwargs"]["shell"] is False
    assert observed["args"][-1] == "--validate"
    assert observed["args"][0].endswith("fleet_plan_validator_cli.exe")
    assert "safe" in json.loads(observed["kwargs"]["input"])


def test_verification_runner_resolves_fixed_argv_and_rejects_unknown_case() -> None:
    """受控验证只解析固定绝对程序和 case，未知选择不能启动子进程。"""

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "ok", "")

    runner = FixedVerificationRunner(process_runner=fake_runner)
    with pytest.raises(VerificationRunnerError):
        runner.run(
            "p0_12",
            run_id=None,
            case_ids=["arbitrary-command"],
            timeout_seconds=1.0,
        )
    assert calls == []
    security_case = runner._cases_for_suite("p0_12", ["security"])[0]
    assert "-k" not in security_case.argv
    assert any(
        item.endswith("::test_rag_output_acl_violation_is_fused_before_return")
        for item in security_case.argv
    )

    result = runner.run(
        "p0_cpp",
        run_id="RUN-VERIFY",
        case_ids=["all"],
        timeout_seconds=1.0,
    )
    assert result.status == "passed"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).name.lower() in {"ctest", "ctest.exe"}
    assert argv[1:] == ["--test-dir", "build/cpp", "--output-on-failure"]
    assert kwargs["shell"] is False


def test_default_snapshot_provider_never_uses_environment_ref_as_path() -> None:
    """环境引用只匹配固定 seed 前缀，路径穿越不会触发文件访问。"""

    provider = DefaultWarehouseSnapshotProvider()
    with pytest.raises(LookupError):
        provider.get_snapshot("..\\domains\\amr_warehouse\\data\\amrs_v1.json")


def test_snapshot_combines_static_and_temporary_blocked_cells() -> None:
    """固定 Provider 不能在地图加入 obstacles 后静默漏传给 A*/Validator。"""

    cells = DefaultWarehouseSnapshotProvider._blocked_cells(
        [GridPosition(x=1, y=2)],
        [GridPosition(x=3, y=4)],
    )
    assert [(item.x, item.y) for item in cells] == [(1, 2), (3, 4)]


def test_query_execution_state_reads_dispatch_store_without_extra_tool() -> None:
    """查询工具只读共享状态存储，且不会暴露任意数据库/命令参数。"""

    store = InMemoryExecutionStateStore()
    store.put(
        "RUN-QUERY",
        {
            "status": "completed",
            "amrs": [{"amr_id": "AMR-01"}],
            "plan_tasks": [{"task_id": "TASK-01", "status": "completed"}],
        },
    )
    registry = build_tool_registry(execution_store=store)
    result = registry.execute(
        ToolName.QUERY_EXECUTION_STATE,
        {"run_id": "RUN-QUERY", "task_ids": ["TASK-01"], "amr_ids": ["AMR-01"]},
        role=UserRole.VIEWER,
        call_id="query-1",
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["source"] == "run_state"
    assert result.output["selected_task_ids"] == ["TASK-01"]


def test_query_simulation_state_filters_order_ids_and_rejects_unknown_ids() -> None:
    """仿真快照使用 order_id 承载 task_ids 筛选，未知 ID 不得误报成功。"""

    store = InMemoryExecutionStateStore()
    store.put(
        "SIM-QUERY",
        {
            "simulation_id": "SIM-QUERY",
            "status": "completed",
            "amrs": [],
            "orders": [
                {"order_id": "ORDER-01", "status": "completed"},
                {"order_id": "ORDER-02", "status": "pending"},
            ],
        },
    )
    registry = build_tool_registry(execution_store=store)
    selected = registry.execute(
        ToolName.QUERY_EXECUTION_STATE,
        {"run_id": "SIM-QUERY", "task_ids": ["ORDER-01"]},
        role=UserRole.VIEWER,
        call_id="query-simulation-order",
    )
    missing = registry.execute(
        ToolName.QUERY_EXECUTION_STATE,
        {"run_id": "SIM-QUERY", "task_ids": ["ORDER-99"]},
        role=UserRole.VIEWER,
        call_id="query-simulation-missing",
    )
    assert [item["order_id"] for item in selected.output["snapshot"]["orders"]] == ["ORDER-01"]
    assert missing.status is ToolResultStatus.FAILED
    assert missing.error is not None
    assert missing.error.code == "execution_state_task_not_found"


def test_rag_output_acl_violation_is_fused_before_return() -> None:
    """即使检索后端回归，operator-only 正文也不能越过 viewer 工具边界。"""

    class LeakyRetriever:
        def retrieve(self, query: str, *, role_scope: UserRole, top_k: int, document_ids: Any):
            result = RetrievalResult(
                chunk_id="LEAK",
                doc_id="operator-only",
                title="secret",
                section="restricted",
                version="1",
                role_scope=[UserRole.OPERATOR],
                source="test",
                checksum="0" * 64,
                text="secret",
                citation="restricted",
                hybrid_score=1.0,
                vector_score=1.0,
                bm25_score=1.0,
                normalized_vector_score=1.0,
                normalized_bm25_score=1.0,
            )
            return RetrievalResponse(
                query=query,
                role_scope=role_scope,
                status=RetrievalStatus.ANSWERABLE,
                reason="leaky fake",
                top_k=top_k,
                minimum_hybrid_score=0.0,
                minimum_vector_score=0.0,
                top_candidate_score=1.0,
                top_candidate_vector_score=1.0,
                results=[result],
            )

    result = build_tool_registry(knowledge_retriever=LeakyRetriever()).execute(
        ToolName.RETRIEVE_KNOWLEDGE,
        {"query": "restricted"},
        role=UserRole.VIEWER,
        call_id="rag-acl-fuse",
    )
    assert result.status is ToolResultStatus.FAILED
    assert result.output is None
    assert result.error is not None
    assert result.error.code == "retrieval_output_scope_violation"


def test_cross_language_summary_models_reject_contradictions() -> None:
    """C++/验证 runner 的汇总字段不能与逐条证据互相矛盾。"""

    with pytest.raises(ValidationError):
        ValidationResponse.model_validate(
            {
                "schema_version": "1.0",
                "ruleset_version": "p0-10.v1",
                "status": "valid",
                "valid": True,
                "error_count": 1,
                "errors": [],
            }
        )
    with pytest.raises(ValidationError):
        VerificationSuiteOutput.model_validate(
            {
                "suite_id": "p0_12",
                "run_id": None,
                "status": "passed",
                "case_count": 1,
                "passed_count": 0,
                "failed_count": 0,
                "cases": [],
            }
        )


def test_fixed_cpp_and_simulation_tool_chain_is_integrated() -> None:
    """实际串联 P0-08 → P0-09 → P0-10 → P0-11 → query，验证契约未被复制。"""

    registry = build_tool_registry()
    snapshot = DefaultWarehouseSnapshotProvider().get_snapshot(ENVIRONMENT_REF)
    allocation = registry.execute(
        ToolName.ALLOCATE_TASKS,
        {"environment_ref": ENVIRONMENT_REF, "order_ids": ["ORDER-001"]},
        role=UserRole.OPERATOR,
        call_id="chain-allocation",
    )
    assert allocation.status is ToolResultStatus.SUCCESS
    assignment = allocation.output["assignments"][0]
    route = registry.execute(
        ToolName.PLAN_MULTI_AMR_ROUTES,
        {
            "environment_ref": ENVIRONMENT_REF,
            "assignments": [
                {"amr_id": assignment["amr_id"], "order_id": assignment["order_id"]}
            ],
            "max_time": 120,
        },
        role=UserRole.OPERATOR,
        call_id="chain-route",
    )
    assert route.status is ToolResultStatus.SUCCESS
    route_item = route.output["routes"][0]
    plan = {
        "schema_version": "1.0",
        "environment_ref": ENVIRONMENT_REF,
        "map_width": snapshot.map_width,
        "map_height": snapshot.map_height,
        "blocked_cells": [item.model_dump(mode="json") for item in snapshot.blocked_cells],
        "blocked_edges": [
            {"from": item["from"].model_dump(mode="json"), "to": item["to"].model_dump(mode="json")}
            for item in snapshot.blocked_edges
        ],
        "one_way_edges": [
            {"from": item["from"].model_dump(mode="json"), "to": item["to"].model_dump(mode="json")}
            for item in snapshot.one_way_edges
        ],
        "amrs": [item.model_dump(mode="json") for item in snapshot.amrs],
        "orders": [item.model_dump(mode="json") for item in snapshot.orders if item.order_id == "ORDER-001"],
        "location_positions": {
            key: value.model_dump(mode="json") for key, value in snapshot.location_positions.items()
        },
        "completed_order_ids": [],
        "routes": [{**route_item, "payload_kg": 5.0}],
        "start_time": 0,
        "max_time": 120,
        "config": {
            "maximum_load_kg": 100.0,
            "energy_per_cell_percent": 1.0,
            "battery_safety_reserve_percent": 15.0,
            "new_task_battery_threshold_percent": 20.0,
            "critical_battery_threshold_percent": 10.0,
            "minimum_safety_distance_cells": 1,
            "default_workstation_capacity": 1,
        },
        "workstation_capacities": {},
        "ruleset_version": "p0-10.v1",
    }
    validation = registry.execute(
        ToolName.VALIDATE_FLEET_PLAN,
        {"environment_ref": ENVIRONMENT_REF, "plan": plan},
        role=UserRole.OPERATOR,
        call_id="chain-validation",
    )
    assert validation.status is ToolResultStatus.SUCCESS
    assert validation.output["valid"] is True
    dispatch = registry.execute(
        ToolName.DISPATCH_SIMULATION,
        {"plan": plan, "seed": 7, "until_time": route_item["dropoff_time"]},
        role=UserRole.OPERATOR,
        call_id="chain-dispatch",
    )
    assert dispatch.status is ToolResultStatus.SUCCESS
    query = registry.execute(
        ToolName.QUERY_EXECUTION_STATE,
        {"run_id": dispatch.output["simulation_id"]},
        role=UserRole.VIEWER,
        call_id="chain-query",
    )
    assert query.status is ToolResultStatus.SUCCESS
    assert query.output["source"] == "simulation"
