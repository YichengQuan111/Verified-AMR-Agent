"""P0-12 白名单工具注册表、统一执行器和九个确定性 handler。

注册表是唯一允许 Agent 进入 RAG、车队快照、C++ 规划器、P0-10 Validator、
仿真、状态查询、验证套件和审批存储的入口。调用流程固定为：

``ToolName → ToolSpec/角色门禁 → 输入 Pydantic 校验 → handler 超时边界 →
输出 Schema 校验 → ToolResult 审计/幂等缓存``。

工具 handler 不接受命令字符串、路径或故障注入；C++ 适配器只使用固定 exe +
JSON stdin/stdout。P0-11 的 FaultInjection 仍留在 Eval/仿真 API，绝不会出现在
正常工具输入或注册表中。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ValidationError

from agent.runtime.hitl import ApprovalGrant
from agent.security.contracts import Principal
from agent.security.rbac import AuthorizationError, assert_retrieval_scope, authorize_tool
from agent.tools.approval import ApprovalStoreProtocol, InMemoryApprovalStore
from agent.tools.contracts import (
    TOOL_ARGUMENT_POLICIES,
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    UserRole,
    validate_tool_arguments,
)
from agent.tools.cpp_client import CppAdapterError, FixedCppJsonClient
from agent.tools.schemas import (
    AllocateTasksInput,
    AllocationResponse,
    ApprovalRequestOutput,
    DispatchSimulationInput,
    DispatchSimulationOutput,
    ExecutionStateOutput,
    FleetStateOutput,
    GetFleetStateInput,
    PlanMultiAMRRoutesInput,
    QueryExecutionStateInput,
    RequestApprovalInput,
    RetrieveKnowledgeInput,
    RetrieveKnowledgeOutput,
    RoutePlanResponse,
    RunVerificationSuiteInput,
    ToolSchema,
    ValidateFleetPlanInput,
    ValidationResponse,
    VerificationSuiteOutput,
)
from agent.tools.snapshots import (
    DefaultWarehouseSnapshotProvider,
    EnvironmentSnapshot,
    ExecutionStateStoreProtocol,
    InMemoryExecutionStateStore,
    SnapshotNotFoundError,
    SnapshotProviderProtocol,
)
from agent.tools.verification import (
    FixedVerificationRunner,
    VerificationRunnerError,
    VerificationRunnerTimeout,
    VerificationRunnerUnavailable,
)
from services.amr_simulator import (
    AMRSimulator,
    PlanValidationError,
    SimulationConfigurationError,
    SimulationInvariantError,
    ValidatorExecutionError,
)
from services.config import AppSettings, load_settings
from services.retrieval import build_hybrid_retriever
from services.retrieval.contracts import RetrievalResponse


class UnknownToolError(KeyError):
    """调用方请求了不在注册表中的工具。"""


class ToolInvocationFailure(RuntimeError):
    """handler 主动报告的可审计失败。"""

    def __init__(
        self,
        message: str,
        *,
        category: ToolErrorCategory,
        code: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
        output: Any | None = None,
        evidence_refs: list[str] | None = None,
        effect_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})
        self.output = output
        self.evidence_refs = list(evidence_refs or [])
        self.effect_id = effect_id


@dataclass(frozen=True)
class ToolInvocationContext:
    """handler 只拿到已通过门禁的调用元数据和只读取消信号。"""

    tool_name: ToolName
    call_id: str
    principal_role: UserRole
    # secure registry 会把完整 Principal 传给 handler；旧模式保持 None，便于
    # P0-12 的本地 fake 继续验证工具契约而不伪造身份。
    principal: Principal | None
    input_digest: str
    # 副作用调用由 P0-14 注入稳定业务键；只读工具或旧 P0-12 调用可为 None。
    # handler 只能用它关联外部事实，不能自行生成或改写。
    idempotency_key: str | None
    # Python 线程无法被安全强杀；超时后设置该事件，带副作用的 handler 必须在
    # 最终提交前复核，避免已经返回 timeout 后又异步写入状态存储。
    cancelled: threading.Event


@dataclass(frozen=True)
class ToolHandlerResponse:
    """handler 返回的内部载荷；最终仍由 ToolExecutor 生成 ToolResult。"""

    output: BaseModel | Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    effect_id: str | None = None
    audit_metadata: Mapping[str, Any] | None = None


ToolHandler = Callable[[BaseModel, ToolInvocationContext], ToolHandlerResponse]


@dataclass(frozen=True)
class ToolDefinition:
    """把静态 ToolSpec 与运行时输入/输出模型和 handler 绑定。"""

    spec: ToolSpec
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler


@dataclass
class ToolDependencies:
    """默认注册表的可替换依赖；替换只发生在进程组装期，不进入工具参数。"""

    settings: AppSettings | None = None
    snapshot_provider: SnapshotProviderProtocol | None = None
    knowledge_retriever: Any | None = None
    cpp_client: FixedCppJsonClient | None = None
    simulator: AMRSimulator | None = None
    execution_store: ExecutionStateStoreProtocol | None = None
    verification_runner: FixedVerificationRunner | Any | None = None
    approval_store: ApprovalStoreProtocol | None = None
    knowledge_root: Path | None = None


@dataclass(frozen=True)
class _InFlightInvocation:
    """同一 call_id 正在执行的请求，用于合并并发重复调用。"""

    tool_name: ToolName
    principal_role: UserRole
    principal_subject: str | None
    fingerprints: frozenset[str]
    completed: threading.Event


# ToolSpec 和 handler 共用同一份时限；子进程预留 1 秒给 JSON 解析、输出校验和
# ToolResult 落账，避免子进程 timeout 与外层线程 timeout 在同一时刻竞态。
_TOOL_TIMEOUT_SECONDS: dict[ToolName, float] = {
    ToolName.RETRIEVE_KNOWLEDGE: 15.0,
    ToolName.GET_FLEET_STATE: 5.0,
    ToolName.ALLOCATE_TASKS: 10.0,
    ToolName.PLAN_MULTI_AMR_ROUTES: 20.0,
    ToolName.VALIDATE_FLEET_PLAN: 10.0,
    ToolName.DISPATCH_SIMULATION: 30.0,
    ToolName.QUERY_EXECUTION_STATE: 5.0,
    ToolName.RUN_VERIFICATION_SUITE: 120.0,
    ToolName.REQUEST_APPROVAL: 5.0,
}


def _child_timeout(tool_name: ToolName) -> float:
    """返回严格短于 ToolSpec 的子进程时限。"""

    return max(0.1, _TOOL_TIMEOUT_SECONDS[tool_name] - 1.0)


def _jsonable(value: Any) -> Any:
    """把 Pydantic/Enum/日期转换为 canonical JSON 可接受的值。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _jsonable(value.value)
    return value


def _canonical_digest(value: Any) -> str:
    """以排序键、无空格和拒绝 NaN 的 JSON 计算审计 digest。"""

    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _unique_refs(refs: list[str] | tuple[str, ...]) -> list[str]:
    """按首次出现顺序去重证据引用，避免同一证据污染审计结果。"""

    return list(dict.fromkeys(refs))


_FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "command",
        "commands",
        "shell",
        "sql",
        "query_sql",
        "executable",
        "cwd",
        "script",
        "code",
        "eval",
        "exec",
        "http_url",
        "url",
        "headers",
        "endpoint",
    }
)


def _find_forbidden_execution_key(value: Any, *, path: str = "") -> str | None:
    """递归拒绝命令/SQL/Shell/外部 HTTP 选择器，但不扫描用户 query 文本。"""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in _FORBIDDEN_EXECUTION_KEYS:
                return child_path
            found = _find_forbidden_execution_key(child, path=child_path)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_forbidden_execution_key(child, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _failure(
    message: str,
    *,
    category: ToolErrorCategory,
    code: str,
    retryable: bool,
    details: Mapping[str, Any] | None = None,
    output: Any | None = None,
    evidence_refs: list[str] | None = None,
    effect_id: str | None = None,
) -> ToolInvocationFailure:
    """构造统一 handler 失败，确保每个错误都有机器码和失败语义。"""

    return ToolInvocationFailure(
        message,
        category=category,
        code=code,
        retryable=retryable,
        details=details,
        output=output,
        evidence_refs=evidence_refs,
        effect_id=effect_id,
    )


def _cpp_failure(exc: CppAdapterError) -> ToolInvocationFailure:
    """把固定 CLI 适配器错误映射到统一 ToolError。"""

    return _failure(
        str(exc),
        category=exc.category,
        code=exc.code,
        retryable=exc.retryable,
        details=exc.details,
    )


def _snapshot(dependencies: ToolDependencies, environment_ref: str) -> EnvironmentSnapshot:
    """取得受控环境快照；没有 provider 时使用固定仓库 seed。"""

    provider = dependencies.snapshot_provider or DefaultWarehouseSnapshotProvider()
    try:
        return provider.get_snapshot(environment_ref)
    except SnapshotNotFoundError as exc:
        raise _failure(
            str(exc),
            category=ToolErrorCategory.NOT_FOUND,
            code="environment_not_found",
            retryable=False,
        ) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise _failure(
            str(exc),
            category=ToolErrorCategory.UNAVAILABLE,
            code="environment_snapshot_unavailable",
            retryable=True,
        ) from exc


def _select_by_ids(
    values: list[BaseModel],
    requested: list[str] | None,
    *,
    id_field: str,
    label: str,
) -> list[BaseModel]:
    """按 ID 做执行前存在性检查，并以 ID 排序保证跨次调用稳定。"""

    indexed = {str(getattr(item, id_field)): item for item in values}
    selected_ids = sorted(indexed) if requested is None else sorted(requested)
    missing = sorted(set(selected_ids) - set(indexed))
    if missing:
        raise _failure(
            f"未知 {label}: {', '.join(missing)}",
            category=ToolErrorCategory.NOT_FOUND,
            code=f"{label}_not_found",
            retryable=False,
            details={"missing_ids": missing},
        )
    return [indexed[item_id] for item_id in selected_ids]


def _edge_payload(edge: Mapping[str, Any]) -> dict[str, Any]:
    """把内部地图边转换成 C++ codec 需要的 from/to JSON。"""

    return {
        "from": _jsonable(edge["from"]),
        "to": _jsonable(edge["to"]),
    }


def _base_map_payload(snapshot: EnvironmentSnapshot, *, blocked_cells: list[Any] | None = None) -> dict[str, Any]:
    """生成三类 C++ 工具共同使用的地图快照，不从 environment_ref 读文件。"""

    cells = list(snapshot.blocked_cells)
    if blocked_cells:
        cells.extend(blocked_cells)
    unique_cells = {(cell.x, cell.y): cell for cell in cells}
    return {
        "map_width": snapshot.map_width,
        "map_height": snapshot.map_height,
        "blocked_cells": [_jsonable(unique_cells[key]) for key in sorted(unique_cells)],
        "blocked_edges": [_edge_payload(edge) for edge in snapshot.blocked_edges],
        "one_way_edges": [_edge_payload(edge) for edge in snapshot.one_way_edges],
        "location_positions": {
            key: _jsonable(snapshot.location_positions[key])
            for key in sorted(snapshot.location_positions)
        },
    }


def _common_cpp_payload(snapshot: EnvironmentSnapshot) -> dict[str, Any]:
    """生成不会被用户覆盖的 AMR/订单/完成依赖快照。"""

    return {
        "amrs": [item.model_dump(mode="json") for item in sorted(snapshot.amrs, key=lambda value: value.amr_id)],
        "orders": [item.model_dump(mode="json") for item in sorted(snapshot.orders, key=lambda value: value.order_id)],
        "completed_order_ids": sorted(snapshot.completed_order_ids),
    }


def _retrieve_handler(dependencies: ToolDependencies) -> ToolHandler:
    """创建 RAG handler；默认 retriever 延迟构造，导入工具层不要求 Qdrant 在线。"""

    retriever_holder: dict[str, Any] = {"value": dependencies.knowledge_retriever}
    lock = threading.Lock()

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = RetrieveKnowledgeInput.model_validate(model)
        try:
            role_scope = (
                assert_retrieval_scope(context.principal, request.role_scope)
                if context.principal is not None
                else request.role_scope or context.principal_role
            )
        except AuthorizationError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.PERMISSION_DENIED,
                code=exc.code,
                retryable=False,
            ) from exc
        try:
            # 默认检索器也在异常映射内延迟构造；Embedding/Qdrant 初始化失败应
            # 明确归类 unavailable，不能泄漏成执行器 generic internal。
            with lock:
                if retriever_holder["value"] is None:
                    settings = dependencies.settings or load_settings()
                    root = dependencies.knowledge_root or (
                        Path(__file__).resolve().parents[2]
                        / "domains"
                        / "amr_warehouse"
                        / "knowledge"
                    )
                    retriever_holder["value"] = build_hybrid_retriever(
                        settings.retrieval,
                        root,
                    )
                retriever = retriever_holder["value"]
            response = retriever.retrieve(
                request.query,
                role_scope=role_scope,
                top_k=request.top_k,
                document_ids=request.document_ids,
            )
            output = RetrieveKnowledgeOutput.model_validate(response)
            # P0-07 已在 Qdrant/BM25 候选阶段执行 ACL；工具边界仍复核返回
            # 身份、逐条 ACL 和文档过滤，防止后端回归把越权正文送进 Agent。
            requested_document_ids = set(request.document_ids or [])
            scope_violation = (
                output.query != request.query
                or output.role_scope is not role_scope
                or output.top_k != request.top_k
                or len(output.results) > request.top_k
                or any(role_scope not in item.role_scope for item in output.results)
                or (
                    request.document_ids is not None
                    and any(item.doc_id not in requested_document_ids for item in output.results)
                )
            )
            if scope_violation:
                raise _failure(
                    "RAG 返回结果违反请求范围或 ACL",
                    category=ToolErrorCategory.INTERNAL,
                    code="retrieval_output_scope_violation",
                    retryable=False,
                )
        except ToolInvocationFailure:
            raise
        except ValueError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="retrieval_query_invalid",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise _failure(
                f"RAG 后端不可用: {type(exc).__name__}",
                category=ToolErrorCategory.UNAVAILABLE,
                code="retrieval_backend_unavailable",
                retryable=True,
            ) from exc
        refs = [f"rag://{item.chunk_id}" for item in output.results]
        return ToolHandlerResponse(
            output=output,
            evidence_refs=tuple(refs),
            audit_metadata={"role_scope": role_scope.value, "result_status": output.status.value},
        )

    return handler


def _fleet_state_handler(dependencies: ToolDependencies) -> ToolHandler:
    """读取固定快照，不把状态查询解释成控制命令。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        del context
        request = GetFleetStateInput.model_validate(model)
        snapshot = _snapshot(dependencies, request.environment_ref)
        amrs = _select_by_ids(
            snapshot.amrs,
            request.amr_ids,
            id_field="amr_id",
            label="amr",
        )
        output = FleetStateOutput(
            environment_ref=snapshot.environment_ref,
            state_version=snapshot.state_version,
            source="warehouse_seed",
            amrs=amrs,
        )
        return ToolHandlerResponse(
            output=output,
            evidence_refs=(f"environment://{snapshot.environment_ref}",),
        )

    return handler


def _allocation_handler(dependencies: ToolDependencies) -> ToolHandler:
    """组装 P0-08 请求并只调用生产 Hungarian。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = AllocateTasksInput.model_validate(model)
        snapshot = _snapshot(dependencies, request.environment_ref)
        amrs = _select_by_ids(snapshot.amrs, request.amr_ids, id_field="amr_id", label="amr")
        orders = _select_by_ids(snapshot.orders, request.order_ids, id_field="order_id", label="order")
        payload = {
            "schema_version": "1.0",
            **_common_cpp_payload(snapshot),
            # P0-08 只需要工位位置，不接受 P0-09 的地图字段；把两套 envelope
            # 分开构造，避免多余字段在 C++ 严格 codec 中才暴露错误。
            "location_positions": {
                key: _jsonable(snapshot.location_positions[key])
                for key in sorted(snapshot.location_positions)
            },
            "amrs": [item.model_dump(mode="json") for item in amrs],
            "orders": [item.model_dump(mode="json") for item in orders],
            "weights": {
                "distance": 1.0,
                "lateness_risk": 10.0,
                "battery_risk": 5.0,
                "load_penalty": 2.0,
                "priority_bonus": 1.0,
            },
            "config": {
                "current_time": snapshot.start_time,
                "maximum_load_kg": 100.0,
                "travel_speed_cells_per_second": 1.0,
                "energy_per_cell_percent": 1.0,
                "battery_warning_threshold_percent": 30.0,
                "new_task_battery_threshold_percent": 20.0,
                "critical_battery_threshold_percent": 10.0,
                "battery_safety_reserve_percent": 15.0,
            },
        }
        client = dependencies.cpp_client or FixedCppJsonClient()
        try:
            raw = client.allocate(
                payload,
                timeout_seconds=_child_timeout(ToolName.ALLOCATE_TASKS),
            )
            output = AllocationResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise _cpp_failure(exc) from exc
        except ValidationError as exc:
            raise _failure(
                f"Hungarian 输出不符合工具 Schema: {exc}",
                category=ToolErrorCategory.INTERNAL,
                code="allocation_output_schema_violation",
                retryable=False,
            ) from exc
        return ToolHandlerResponse(
            output=output,
            evidence_refs=(f"allocation://{context.input_digest}",),
            audit_metadata={"algorithm": output.algorithm},
        )

    return handler


def _route_handler(dependencies: ToolDependencies) -> ToolHandler:
    """组装 P0-09 请求并只调用生产 A*；不以 Dijkstra 隐式回退。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = PlanMultiAMRRoutesInput.model_validate(model)
        snapshot = _snapshot(dependencies, request.environment_ref)
        assignment_amr_ids = [item.amr_id for item in request.assignments]
        assignment_order_ids = [item.order_id for item in request.assignments]
        _select_by_ids(snapshot.amrs, assignment_amr_ids, id_field="amr_id", label="amr")
        _select_by_ids(snapshot.orders, assignment_order_ids, id_field="order_id", label="order")
        payload = {
            "schema_version": "1.0",
            "environment_ref": snapshot.environment_ref,
            **_base_map_payload(snapshot, blocked_cells=request.blocked_cells),
            **_common_cpp_payload(snapshot),
            "assignments": [item.model_dump(mode="json", exclude_none=True) for item in request.assignments],
            "start_time": snapshot.start_time,
            "max_time": request.max_time,
            "costs": {"move_cost": 1.0, "turn_cost": 0.25, "wait_cost": 1.0},
        }
        client = dependencies.cpp_client or FixedCppJsonClient()
        try:
            raw = client.plan_routes(
                payload,
                timeout_seconds=_child_timeout(ToolName.PLAN_MULTI_AMR_ROUTES),
            )
            output = RoutePlanResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise _cpp_failure(exc) from exc
        except ValidationError as exc:
            raise _failure(
                f"A* 输出不符合工具 Schema: {exc}",
                category=ToolErrorCategory.INTERNAL,
                code="route_output_schema_violation",
                retryable=False,
            ) from exc
        refs = tuple(
            f"route://{item.amr_id}/{item.order_id}"
            for item in output.routes
        )
        if output.status == "infeasible":
            raise _failure(
                "A* 无法为全部分配生成安全路线",
                category=ToolErrorCategory.UNSAFE_PLAN,
                code="route_infeasible",
                retryable=False,
                output=output,
                evidence_refs=list(refs),
            )
        return ToolHandlerResponse(output=output, evidence_refs=refs)

    return handler


def _validate_handler(dependencies: ToolDependencies) -> ToolHandler:
    """调用 P0-10，并把 invalid 计划作为 unsafe_plan 失败返回完整证据。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        del context
        request = ValidateFleetPlanInput.model_validate(model)
        payload = request.plan.model_dump(mode="json", by_alias=True, exclude_none=True)
        client = dependencies.cpp_client if dependencies.cpp_client is not None else FixedCppJsonClient()
        try:
            raw = client.validate_plan(
                payload,
                timeout_seconds=_child_timeout(ToolName.VALIDATE_FLEET_PLAN),
            )
            output = ValidationResponse.model_validate(raw)
        except CppAdapterError as exc:
            raise _cpp_failure(exc) from exc
        except ValidationError as exc:
            raise _failure(
                f"Validator 输出不符合工具 Schema: {exc}",
                category=ToolErrorCategory.INTERNAL,
                code="validator_output_schema_violation",
                retryable=False,
            ) from exc
        refs = tuple(f"validator://{item.code}" for item in output.errors)
        if not output.valid or output.status != "valid" or output.errors:
            raise _failure(
                "P0-10 Validator 拒绝计划",
                category=ToolErrorCategory.UNSAFE_PLAN,
                code="fleet_plan_invalid",
                retryable=False,
                output=output,
                evidence_refs=list(refs),
                details={"error_count": output.error_count, "ruleset_version": output.ruleset_version},
            )
        return ToolHandlerResponse(
            output=output,
            evidence_refs=(f"validator://{output.ruleset_version}/valid",),
        )

    return handler


def _dispatch_handler(dependencies: ToolDependencies) -> ToolHandler:
    """通过 P0-11 执行仿真，并把成功结果写入共享状态存储。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = DispatchSimulationInput.model_validate(model)
        plan = request.plan
        simulation_key = _canonical_digest(
            {"plan": plan, "seed": request.seed, "until_time": request.until_time}
        )
        simulation_id = f"simulation-{simulation_key[:24]}"
        simulator = dependencies.simulator or AMRSimulator()
        try:
            result = simulator.run(
                plan,
                simulation_id=simulation_id,
                seed=request.seed,
                until_time=request.until_time,
                # 故障注入不在正常工具参数中；这里只能传空序列。
                faults=(),
            )
        except PlanValidationError as exc:
            codes = [
                str(item.get("code"))
                for item in exc.result.get("errors", [])
                if isinstance(item, dict) and item.get("code")
            ]
            raise _failure(
                str(exc),
                category=ToolErrorCategory.UNSAFE_PLAN,
                code="simulation_plan_invalid",
                retryable=False,
                details={"validator_codes": codes},
                output=exc.result,
                evidence_refs=[f"validator://{code}" for code in codes],
            ) from exc
        except ValidatorExecutionError as exc:
            message = str(exc)
            category = (
                ToolErrorCategory.TIMEOUT
                if "超时" in message
                else ToolErrorCategory.UNAVAILABLE
            )
            raise _failure(
                message,
                category=category,
                code="simulation_validator_unavailable",
                retryable=True,
            ) from exc
        except SimulationConfigurationError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="simulation_configuration_invalid",
                retryable=False,
            ) from exc
        except SimulationInvariantError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.INTERNAL,
                code="simulation_invariant_failed",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise _failure(
                f"仿真执行失败: {type(exc).__name__}",
                category=ToolErrorCategory.INTERNAL,
                code="simulation_internal_error",
                retryable=False,
            ) from exc
        output = DispatchSimulationOutput.model_validate(result)
        if context.cancelled.is_set():
            # 外层已经给调用方返回 timeout 时，不允许迟到的仿真线程再写状态。
            raise _failure(
                "仿真在工具超时后完成，结果未提交",
                category=ToolErrorCategory.TIMEOUT,
                code="simulation_completed_after_timeout",
                retryable=True,
            )
        store = dependencies.execution_store
        if store is not None:
            store.put(
                simulation_id,
                output,
                idempotency_key=context.idempotency_key,
            )
        return ToolHandlerResponse(
            output=output,
            evidence_refs=(f"simulation://{simulation_id}", f"simulation://{simulation_id}/events"),
            effect_id=simulation_id,
            audit_metadata={"simulation_status": output.status.value},
        )

    return handler


def _query_handler(dependencies: ToolDependencies) -> ToolHandler:
    """读取共享状态存储，并在返回前应用 ID 筛选。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        del context
        request = QueryExecutionStateInput.model_validate(model)
        store = dependencies.execution_store
        if store is None:
            raise _failure(
                "执行状态存储未配置",
                category=ToolErrorCategory.UNAVAILABLE,
                code="execution_state_store_unavailable",
                retryable=True,
            )
        snapshot = store.get(request.run_id)
        if snapshot is None:
            raise _failure(
                f"未知运行状态: {request.run_id}",
                category=ToolErrorCategory.NOT_FOUND,
                code="execution_state_not_found",
                retryable=False,
            )
        values = snapshot.get("amrs")
        known_amr_ids = {
            str(item.get("amr_id")) for item in values if isinstance(item, dict) and item.get("amr_id")
        } if isinstance(values, list) else set()
        if request.amr_ids is not None:
            missing = sorted(set(request.amr_ids) - known_amr_ids)
            if missing:
                raise _failure(
                    f"状态中不存在 AMR: {', '.join(missing)}",
                    category=ToolErrorCategory.NOT_FOUND,
                    code="execution_state_amr_not_found",
                    retryable=False,
                )
            snapshot["amrs"] = [
                item for item in values
                if isinstance(item, dict) and item.get("amr_id") in set(request.amr_ids)
            ]

        task_collection = "plan_tasks"
        task_id_field = "task_id"
        task_values = snapshot.get(task_collection)
        if not isinstance(task_values, list) and isinstance(snapshot.get("orders"), list):
            # SimulationResult 没有 PlanTask，执行期可查询对象是 order_id；仍沿用
            # P0-04 已固定的 task_ids 顶层参数，输出明确返回实际选中的业务 ID。
            task_collection = "orders"
            task_id_field = "order_id"
            task_values = snapshot.get(task_collection)
        iterable_task_values = task_values if isinstance(task_values, list) else []
        known_task_ids = {
            str(item.get(task_id_field))
            for item in iterable_task_values
            if isinstance(item, dict) and item.get(task_id_field)
        }
        if request.task_ids is not None:
            missing = sorted(set(request.task_ids) - known_task_ids)
            if missing:
                raise _failure(
                    f"状态中不存在任务: {', '.join(missing)}",
                    category=ToolErrorCategory.NOT_FOUND,
                    code="execution_state_task_not_found",
                    retryable=False,
                )
            snapshot[task_collection] = [
                item for item in iterable_task_values
                if isinstance(item, dict) and item.get(task_id_field) in set(request.task_ids)
            ]

        selected_amr_ids = (
            request.amr_ids
            if request.amr_ids is not None
            else sorted(known_amr_ids)
        )
        selected_task_ids = (
            request.task_ids
            if request.task_ids is not None
            else sorted(known_task_ids)
        )
        source = "simulation" if "simulation_id" in snapshot else "run_state"
        output = ExecutionStateOutput(
            run_id=request.run_id,
            source=source,
            status=str(snapshot.get("status", "unknown")),
            selected_task_ids=selected_task_ids,
            selected_amr_ids=selected_amr_ids,
            snapshot=snapshot,
            evidence_refs=[f"run://{request.run_id}"],
        )
        return ToolHandlerResponse(output=output, evidence_refs=tuple(output.evidence_refs))

    return handler


def _verification_handler(dependencies: ToolDependencies) -> ToolHandler:
    """运行固定验证 runner；case 白名单在 runner 启动子进程前检查。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = RunVerificationSuiteInput.model_validate(model)
        runner = dependencies.verification_runner or FixedVerificationRunner()
        try:
            output = runner.run(
                request.suite_id,
                run_id=request.run_id,
                trace_id=request.trace_id,
                case_ids=request.case_ids,
                timeout_seconds=_child_timeout(ToolName.RUN_VERIFICATION_SUITE),
            )
            output = VerificationSuiteOutput.model_validate(output)
        except VerificationRunnerTimeout as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.TIMEOUT,
                code="verification_suite_timeout",
                retryable=True,
                output=exc.output,
                evidence_refs=(exc.output.evidence_refs if exc.output is not None else None),
            ) from exc
        except VerificationRunnerUnavailable as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.UNAVAILABLE,
                code="verification_runner_unavailable",
                retryable=True,
            ) from exc
        except VerificationRunnerError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="verification_suite_invalid",
                retryable=False,
            ) from exc
        effect_id = f"verification-{context.input_digest[:24]}"
        return ToolHandlerResponse(
            output=output,
            evidence_refs=tuple(
                dict.fromkeys(
                    [
                        f"verification://{request.suite_id}",
                        *output.evidence_refs,
                    ]
                )
            ),
            effect_id=effect_id,
            audit_metadata={
                "case_count": output.case_count,
                "suite_status": output.status,
                "report_id": output.report_id,
                "report_digest": output.report_digest,
                "trace_id": output.trace_id,
            },
        )

    return handler


def _approval_handler(dependencies: ToolDependencies) -> ToolHandler:
    """创建 pending 审批；任何批准决定必须走独立 HITL/API 入口。"""

    def handler(model: BaseModel, context: ToolInvocationContext) -> ToolHandlerResponse:
        request = RequestApprovalInput.model_validate(model)
        if context.cancelled.is_set():
            raise _failure(
                "审批请求已在提交前超时",
                category=ToolErrorCategory.TIMEOUT,
                code="approval_cancelled_before_commit",
                retryable=True,
            )
        store = dependencies.approval_store or InMemoryApprovalStore()
        try:
            output = store.request(request)
        except ValueError as exc:
            raise _failure(
                str(exc),
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="approval_request_invalid",
                retryable=False,
            ) from exc
        except OSError as exc:
            raise _failure(
                f"审批存储不可用: {type(exc).__name__}",
                category=ToolErrorCategory.UNAVAILABLE,
                code="approval_store_unavailable",
                retryable=True,
            ) from exc
        output = ApprovalRequestOutput.model_validate(output)
        return ToolHandlerResponse(
            output=output,
            evidence_refs=(f"approval://{output.approval_id}",),
            effect_id=output.effect_id,
        )

    return handler


def _spec(
    *,
    name: ToolName,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    description: str,
    roles: list[UserRole],
    timeout_seconds: float,
    idempotent: bool,
    has_side_effects: bool,
    requires_approval: bool,
    errors: list[ToolErrorCategory],
    handler: ToolHandler,
) -> ToolDefinition:
    """从实时 Pydantic Schema 生成静态 ToolSpec，杜绝手写字段漂移。"""

    # call_id 冲突、外层超时和未预期 handler/输出错误由统一执行器产生，所有
    # 九个 ToolSpec 都必须声明；handler 只补充自身会产生的业务分类。
    executor_categories = [
        ToolErrorCategory.TIMEOUT,
        ToolErrorCategory.CONFLICT,
        ToolErrorCategory.INTERNAL,
    ]
    declared_errors = list(dict.fromkeys([*errors, *executor_categories]))

    return ToolDefinition(
        spec=ToolSpec(
            tool_name=name,
            version="1.0.0",
            description=description,
            input_schema=input_model.model_json_schema(),
            output_schema=output_model.model_json_schema(),
            allowed_roles=roles,
            timeout_seconds=timeout_seconds,
            idempotent=idempotent,
            has_side_effects=has_side_effects,
            requires_approval=requires_approval,
            error_categories=declared_errors,
        ),
        input_model=input_model,
        output_model=output_model,
        handler=handler,
    )


def _definitions(dependencies: ToolDependencies) -> list[ToolDefinition]:
    """按固定顺序构造恰好九个工具；故障注入不在此列表。"""

    common = [ToolErrorCategory.INVALID_ARGUMENT, ToolErrorCategory.PERMISSION_DENIED]
    return [
        _spec(
            name=ToolName.RETRIEVE_KNOWLEDGE,
            input_model=RetrieveKnowledgeInput,
            output_model=RetrieveKnowledgeOutput,
            description="按调用者 ACL 检索冻结仓储知识并返回可引用证据",
            roles=[UserRole.VIEWER, UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.RETRIEVE_KNOWLEDGE],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_retrieve_handler(dependencies),
        ),
        _spec(
            name=ToolName.GET_FLEET_STATE,
            input_model=GetFleetStateInput,
            output_model=FleetStateOutput,
            description="读取固定环境快照中的 AMR 车队状态",
            roles=[UserRole.VIEWER, UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.GET_FLEET_STATE],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.NOT_FOUND, ToolErrorCategory.UNAVAILABLE],
            handler=_fleet_state_handler(dependencies),
        ),
        _spec(
            name=ToolName.ALLOCATE_TASKS,
            input_model=AllocateTasksInput,
            output_model=AllocationResponse,
            description="调用固定 Hungarian 程序为订单分配可行 AMR",
            roles=[UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.ALLOCATE_TASKS],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.NOT_FOUND, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_allocation_handler(dependencies),
        ),
        _spec(
            name=ToolName.PLAN_MULTI_AMR_ROUTES,
            input_model=PlanMultiAMRRoutesInput,
            output_model=RoutePlanResponse,
            description="调用固定 A* 和时空预约表规划多车路径",
            roles=[UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.PLAN_MULTI_AMR_ROUTES],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.NOT_FOUND, ToolErrorCategory.UNSAFE_PLAN, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_route_handler(dependencies),
        ),
        _spec(
            name=ToolName.VALIDATE_FLEET_PLAN,
            input_model=ValidateFleetPlanInput,
            output_model=ValidationResponse,
            description="调用固定 P0-10 Validator 独立复核完整车队计划",
            roles=[UserRole.VIEWER, UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.VALIDATE_FLEET_PLAN],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.UNSAFE_PLAN, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_validate_handler(dependencies),
        ),
        _spec(
            name=ToolName.DISPATCH_SIMULATION,
            input_model=DispatchSimulationInput,
            output_model=DispatchSimulationOutput,
            description="经 P0-10 前置门禁后派发到固定时间步仿真器",
            roles=[UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.DISPATCH_SIMULATION],
            idempotent=True,
            has_side_effects=True,
            requires_approval=True,
            errors=common + [ToolErrorCategory.UNSAFE_PLAN, ToolErrorCategory.TIMEOUT, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_dispatch_handler(dependencies),
        ),
        _spec(
            name=ToolName.QUERY_EXECUTION_STATE,
            input_model=QueryExecutionStateInput,
            output_model=ExecutionStateOutput,
            description="查询已登记运行或仿真状态并按 ID 筛选",
            roles=[UserRole.VIEWER, UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.QUERY_EXECUTION_STATE],
            idempotent=True,
            has_side_effects=False,
            requires_approval=False,
            errors=common + [ToolErrorCategory.NOT_FOUND, ToolErrorCategory.UNAVAILABLE],
            handler=_query_handler(dependencies),
        ),
        _spec(
            name=ToolName.RUN_VERIFICATION_SUITE,
            input_model=RunVerificationSuiteInput,
            output_model=VerificationSuiteOutput,
            description="只运行预登记的 Python/CTest/Smoke 验证套件",
            roles=[UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.RUN_VERIFICATION_SUITE],
            idempotent=True,
            has_side_effects=True,
            requires_approval=False,
            errors=common + [ToolErrorCategory.TIMEOUT, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_verification_handler(dependencies),
        ),
        _spec(
            name=ToolName.REQUEST_APPROVAL,
            input_model=RequestApprovalInput,
            output_model=ApprovalRequestOutput,
            description="创建高风险计划步骤的 pending 人工审批请求",
            roles=[UserRole.OPERATOR],
            timeout_seconds=_TOOL_TIMEOUT_SECONDS[ToolName.REQUEST_APPROVAL],
            idempotent=True,
            has_side_effects=True,
            requires_approval=False,
            errors=common + [ToolErrorCategory.CONFLICT, ToolErrorCategory.UNAVAILABLE, ToolErrorCategory.INTERNAL],
            handler=_approval_handler(dependencies),
        ),
    ]


class ToolRegistry:
    """封闭的九工具注册表；可注入 fake handler 做契约/失败路径测试。

    ``security_required=True`` 是 API/PEVR 的生产边界：没有验签 Principal 就
    不能执行任何工具。默认 legacy 模式只为 P0-12 的纯契约测试保留，不应由
    外部请求直接使用。
    """

    def __init__(
        self,
        definitions: list[ToolDefinition],
        *,
        bound_principal: Principal | None = None,
        security_required: bool = False,
        approval_verifier: Callable[[ApprovalGrant, ToolSpec, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if len({item.spec.tool_name for item in definitions}) != len(definitions):
            raise ValueError("工具注册表不能包含重复 tool_name")
        self._definitions = {item.spec.tool_name: item for item in definitions}
        self.bound_principal = bound_principal
        self.security_required = security_required
        self.approval_verifier = approval_verifier
        self._executor = ToolExecutor(self)

    @property
    def names(self) -> tuple[ToolName, ...]:
        """按 ToolName 枚举顺序返回注册工具，便于白名单审计。"""

        return tuple(name for name in ToolName if name in self._definitions)

    def get(self, tool_name: ToolName | str) -> ToolDefinition:
        """只解析已登记工具；未知名称不会进入任何 handler。"""

        try:
            name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        except (TypeError, ValueError) as exc:
            raise UnknownToolError(str(tool_name)) from exc
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise UnknownToolError(name.value) from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        """返回静态注册说明，不暴露 handler。"""

        return tuple(self._definitions[name].spec for name in self.names)

    def execute(
        self,
        tool_name: ToolName | str,
        arguments: Mapping[str, Any],
        *,
        role: UserRole | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        principal: Principal | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        """调用统一执行器；参数、权限和输出错误均变成 ToolResult。

        安全模式下 ``role`` 只是兼容性校验值，真实角色始终取自 Principal；
        没有 Principal、主体不匹配或高风险工具缺少可验证 grant 都会在 handler
        前返回 denied。
        """

        return self._executor.execute(
            tool_name,
            arguments,
            role=role,
            call_id=call_id,
            idempotency_key=idempotency_key,
            principal=principal,
            approval_grant=approval_grant,
        )


class ToolExecutor:
    """实现预检、超时、幂等缓存和 ToolResult 审计闭环。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._ledger: dict[str, ToolResult] = {}
        self._inflight: dict[str, _InFlightInvocation] = {}
        self._lock = threading.RLock()

    def _result(
        self,
        definition: ToolDefinition,
        *,
        call_id: str,
        role: UserRole | None,
        principal_subject: str | None,
        input_digest: str,
        started_at: datetime,
        status: ToolResultStatus,
        output: Any | None,
        error: ToolError | None,
        evidence_refs: list[str],
        effect_id: str | None,
        idempotency_key: str | None = None,
        audit_metadata: Mapping[str, Any] | None = None,
        preflight_validated: bool = True,
    ) -> ToolResult:
        """集中生成结果，确保失败/超时也有完整时间和 digest 字段。"""

        finished_at = datetime.now(timezone.utc)
        duration_ms = max(0, int(round((finished_at - started_at).total_seconds() * 1000)))
        output_payload = None if output is None else _jsonable(output)
        output_digest = None if output_payload is None else _canonical_digest(output_payload)
        metadata: dict[str, Any] = {
            "request_fingerprint": input_digest,
            "preflight_validated": preflight_validated,
            "idempotent": definition.spec.idempotent,
            "has_side_effects": definition.spec.has_side_effects,
        }
        if audit_metadata:
            metadata.update(_jsonable(audit_metadata))
        if error is not None:
            metadata["error_category"] = error.category.value
            metadata["error_code"] = error.code
        return ToolResult(
            tool_name=definition.spec.tool_name,
            call_id=call_id,
            status=status,
            output=output_payload,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            evidence_refs=_unique_refs(evidence_refs),
            effect_id=effect_id,
            tool_version=definition.spec.version,
            principal_role=role,
            principal_subject=principal_subject,
            input_digest=input_digest,
            output_digest=output_digest,
            # 副作用任务由上层传入 run:plan_version:task；旧调用未传时仍以
            # call_id 兼容 P0-12 语义。这个字段只做审计，不允许 handler 自行改写。
            idempotency_key=idempotency_key or call_id,
            audit_metadata=metadata,
        )

    def _error_result(
        self,
        definition: ToolDefinition,
        *,
        call_id: str,
        role: UserRole | None,
        principal_subject: str | None = None,
        input_digest: str,
        started_at: datetime,
        category: ToolErrorCategory,
        code: str,
        message: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
        output: Any | None = None,
        evidence_refs: list[str] | None = None,
        effect_id: str | None = None,
        idempotency_key: str | None = None,
        preflight_validated: bool = True,
    ) -> ToolResult:
        """把预检或 handler 失败统一成 ToolResult。"""

        error = ToolError(
            category=category,
            code=code,
            message=message,
            retryable=retryable,
            details=dict(_jsonable(details or {})),
        )
        status = (
            ToolResultStatus.TIMEOUT
            if category is ToolErrorCategory.TIMEOUT
            else ToolResultStatus.DENIED
            if category is ToolErrorCategory.PERMISSION_DENIED
            else ToolResultStatus.FAILED
        )
        return self._result(
            definition,
            call_id=call_id,
            role=role,
            principal_subject=principal_subject,
            input_digest=input_digest,
            started_at=started_at,
            status=status,
            output=output,
            error=error,
            evidence_refs=evidence_refs or [],
            effect_id=effect_id,
            idempotency_key=idempotency_key,
            preflight_validated=preflight_validated,
        )

    def _finalize_result(self, ledger_key: str, result: ToolResult) -> ToolResult:
        """原子发布首次结果并唤醒同一幂等键的并发重试。"""

        with self._lock:
            self._ledger[ledger_key] = result
            inflight = self._inflight.pop(ledger_key, None)
            if inflight is not None:
                inflight.completed.set()
        return result

    def execute(
        self,
        tool_name: ToolName | str,
        arguments: Mapping[str, Any],
        *,
        role: UserRole | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        principal: Principal | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        """严格按固定顺序执行一次工具调用。"""

        definition = self._registry.get(tool_name)
        started_at = datetime.now(timezone.utc)
        raw_arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        bound_principal = self._registry.bound_principal
        if bound_principal is not None:
            if principal is not None and principal != bound_principal:
                # 不把两个主体合并；同一注册表绑定的身份必须保持稳定。
                principal = bound_principal
                role = None
                mismatch = True
            else:
                principal = bound_principal
                mismatch = False
        else:
            mismatch = False
        if principal is not None:
            effective_role = principal.role
            if role is not None:
                try:
                    supplied_role = role if isinstance(role, UserRole) else UserRole(role)
                except (TypeError, ValueError):
                    supplied_role = None
                if supplied_role is not principal.role:
                    mismatch = True
        else:
            try:
                effective_role = UserRole.OPERATOR if role is None else (
                    role if isinstance(role, UserRole) else UserRole(role)
                )
            except (TypeError, ValueError) as exc:
                invalid_digest = sha256(repr(role).encode("utf-8")).hexdigest()
                return self._error_result(
                    definition,
                    call_id=call_id or f"call-{invalid_digest[:24]}",
                    role=None,
                    input_digest=invalid_digest,
                    started_at=started_at,
                    category=ToolErrorCategory.INVALID_ARGUMENT,
                    code="role_invalid",
                    message="role 必须是 viewer 或 operator",
                    retryable=False,
                    details={"error_type": type(exc).__name__},
                    idempotency_key=idempotency_key,
                    preflight_validated=False,
                )

        # secure registry 的主体要求必须在幂等查重前生效，避免无身份调用复用
        # 另一主体的缓存结果；显式 role 也不能覆盖绑定 Principal。
        if self._registry.security_required and principal is None:
            digest = sha256(repr(raw_arguments).encode("utf-8")).hexdigest()
            return self._error_result(
                definition,
                call_id=call_id or f"call-{digest[:24]}",
                role=None,
                input_digest=digest,
                started_at=started_at,
                category=ToolErrorCategory.PERMISSION_DENIED,
                code="principal_required",
                message="安全工具调用必须携带已验签 Principal",
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
        if mismatch:
            digest = sha256(repr(raw_arguments).encode("utf-8")).hexdigest()
            return self._error_result(
                definition,
                call_id=call_id or f"call-{digest[:24]}",
                role=principal.role if principal is not None else None,
                principal_subject=principal.subject if principal is not None else None,
                input_digest=digest,
                started_at=started_at,
                category=ToolErrorCategory.PERMISSION_DENIED,
                code="principal_role_mismatch",
                message="调用方声明的 role 与已验证 Principal 不一致",
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
        role = effective_role
        principal_subject = principal.subject if principal is not None else None
        try:
            raw_digest = _canonical_digest(
                {
                    "tool": definition.spec.tool_name.value,
                    "role": role.value,
                    "principal_subject": principal_subject,
                    "arguments": raw_arguments,
                }
            )
        except (TypeError, ValueError) as exc:
            raw_digest = sha256(repr(raw_arguments).encode("utf-8")).hexdigest()
            result = self._error_result(
                definition,
                call_id=call_id or f"call-{raw_digest[:24]}",
                role=role,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="arguments_not_json",
                message=f"工具参数不是有限 JSON: {exc}",
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            # 无法形成规范 JSON 的请求没有可比较的稳定指纹，因此不能写入
            # 幂等账本；否则它可借复用 call_id 覆盖此前合法调用的结果。
            return result

        active_call_id = call_id or f"call-{raw_digest[:24]}"
        if not isinstance(active_call_id, str) or not active_call_id.strip():
            result = self._error_result(
                definition,
                call_id="call-invalid",
                role=role,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="call_id_invalid",
                message="call_id 必须是非空字符串",
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            return result

        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="idempotency_key_invalid",
                message="idempotency_key 必须是非空字符串",
                retryable=False,
                preflight_validated=False,
            )
            return result

        # 传入业务幂等键时，缓存/并发协调按业务键而不是易变 call_id；未传入时
        # 保持 P0-12 原有的 call_id 语义，避免旧调用方的冲突检查退化。
        ledger_key = idempotency_key or active_call_id

        # 在查重前做一次只读的输入规范化，便于把“省略默认值”和“显式默认值”
        # 视作同一请求；这一步不启动 handler，也不会触发外部副作用。
        normalised_digest: str | None = None
        try:
            normalised_candidate = definition.input_model.model_validate(raw_arguments)
            normalised_digest = _canonical_digest(normalised_candidate)
        except (ValidationError, TypeError, ValueError):
            pass

        request_fingerprints = {raw_digest}
        if normalised_digest is not None:
            request_fingerprints.add(normalised_digest)
        wait_for: threading.Event | None = None
        cached_result: ToolResult | None = None
        conflict_tool: ToolName | None = None
        with self._lock:
            previous = self._ledger.get(ledger_key)
            if previous is not None:
                previous_fingerprint = previous.audit_metadata.get("request_fingerprint")
                if (
                    previous_fingerprint in request_fingerprints
                    and previous.tool_name is definition.spec.tool_name
                    and previous.principal_role is role
                    and previous.principal_subject == principal_subject
                ):
                    cached_result = previous
                else:
                    conflict_tool = previous.tool_name
            else:
                inflight = self._inflight.get(ledger_key)
                if inflight is None:
                    self._inflight[ledger_key] = _InFlightInvocation(
                        tool_name=definition.spec.tool_name,
                        principal_role=role,
                        principal_subject=principal_subject,
                        fingerprints=frozenset(request_fingerprints),
                        completed=threading.Event(),
                    )
                elif (
                    inflight.tool_name is definition.spec.tool_name
                    and inflight.principal_role is role
                    and inflight.principal_subject == principal_subject
                    and not inflight.fingerprints.isdisjoint(request_fingerprints)
                ):
                    wait_for = inflight.completed
                else:
                    conflict_tool = inflight.tool_name

        if cached_result is not None:
            # 任何状态（含失败/超时）的精确重试都返回原结果。
            return cached_result.model_copy(deep=True)
        if conflict_tool is not None:
            conflict_code = (
                "idempotency_key_reused_with_different_request"
                if idempotency_key is not None
                else "call_id_reused_with_different_request"
            )
            conflict_message = (
                "idempotency_key 已用于另一工具请求"
                if idempotency_key is not None
                else "call_id 已用于另一工具请求"
            )
            return self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=normalised_digest or raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.CONFLICT,
                code=conflict_code,
                message=conflict_message,
                retryable=False,
                details={"existing_tool": conflict_tool.value},
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
        if wait_for is not None:
            # 首个调用自身会在 ToolSpec 时限内返回或超时，再预留 1 秒给结果落账。
            if wait_for.wait(definition.spec.timeout_seconds + 1.0):
                with self._lock:
                    cached_result = self._ledger.get(ledger_key)
                if cached_result is not None:
                    return cached_result.model_copy(deep=True)
            return self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=normalised_digest or raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INTERNAL,
                code="inflight_result_missing",
                message="并发重复调用未取得首次结果",
                retryable=True,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )

        # 顶层参数白名单先执行；因此未知字段不会触发输入模型副作用或 handler。
        forbidden_key = _find_forbidden_execution_key(raw_arguments)
        if forbidden_key is not None:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                principal_subject=principal_subject,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.PERMISSION_DENIED,
                code="forbidden_execution_surface",
                message=f"工具参数包含禁止的执行选择器: {forbidden_key}",
                retryable=False,
                details={"path": forbidden_key},
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            return self._finalize_result(ledger_key, result)
        try:
            validate_tool_arguments(definition.spec.tool_name, raw_arguments)
        except (TypeError, ValueError) as exc:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="tool_arguments_not_allowed",
                message=str(exc),
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            return self._finalize_result(ledger_key, result)

        if role not in definition.spec.allowed_roles:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                principal_subject=principal_subject,
                input_digest=normalised_digest or raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.PERMISSION_DENIED,
                code="tool_role_not_allowed",
                message=f"角色 {role.value} 无权调用 {definition.spec.tool_name.value}",
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            return self._finalize_result(ledger_key, result)
        if self._registry.security_required and principal is not None:
            try:
                authorize_tool(principal, definition.spec)
            except AuthorizationError as exc:
                result = self._error_result(
                    definition,
                    call_id=active_call_id,
                    role=role,
                    principal_subject=principal_subject,
                    input_digest=normalised_digest or raw_digest,
                    started_at=started_at,
                    category=ToolErrorCategory.PERMISSION_DENIED,
                    code=exc.code,
                    message=str(exc),
                    retryable=False,
                    preflight_validated=False,
                )
                return self._finalize_result(ledger_key, result)

        try:
            parsed = definition.input_model.model_validate(raw_arguments)
        except (ValidationError, TypeError, ValueError) as exc:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                principal_subject=principal_subject,
                input_digest=raw_digest,
                started_at=started_at,
                category=ToolErrorCategory.INVALID_ARGUMENT,
                code="tool_input_schema_invalid",
                message=str(exc),
                retryable=False,
                idempotency_key=idempotency_key,
                preflight_validated=False,
            )
            return self._finalize_result(ledger_key, result)

        # 输入模型经过规范化后重新计算 digest；审批验证使用的正是 handler 将收到
        # 的规范化输入，而不是调用方可能重复排列的原始 JSON。
        input_digest = normalised_digest or _canonical_digest(parsed)
        if self._registry.security_required and definition.spec.requires_approval:
            if approval_grant is None:
                result = self._error_result(
                    definition,
                    call_id=active_call_id,
                    role=role,
                    principal_subject=principal_subject,
                    input_digest=input_digest,
                    started_at=started_at,
                    category=ToolErrorCategory.PERMISSION_DENIED,
                    code="approval_required",
                    message=f"工具 {definition.spec.tool_name.value} 需要有效 HITL 审批",
                    retryable=False,
                    preflight_validated=False,
                )
                return self._finalize_result(ledger_key, result)
            verifier = self._registry.approval_verifier
            if verifier is None:
                result = self._error_result(
                    definition,
                    call_id=active_call_id,
                    role=role,
                    principal_subject=principal_subject,
                    input_digest=input_digest,
                    started_at=started_at,
                    category=ToolErrorCategory.PERMISSION_DENIED,
                    code="approval_verifier_unconfigured",
                    message="安全注册表未配置审批票据验证器",
                    retryable=False,
                    preflight_validated=False,
                )
                return self._finalize_result(ledger_key, result)
            try:
                verifier(approval_grant, definition.spec, raw_arguments)
            except Exception as exc:
                result = self._error_result(
                    definition,
                    call_id=active_call_id,
                    role=role,
                    principal_subject=principal_subject,
                    input_digest=input_digest,
                    started_at=started_at,
                    category=ToolErrorCategory.PERMISSION_DENIED,
                    code="approval_invalid",
                    message="HITL 审批票据未通过存储、计划或 Validator 核对",
                    retryable=False,
                    details={"error_type": type(exc).__name__},
                    preflight_validated=False,
                )
                return self._finalize_result(ledger_key, result)

        cancellation_event = threading.Event()
        context = ToolInvocationContext(
            tool_name=definition.spec.tool_name,
            call_id=active_call_id,
            principal_role=role,
            principal=principal,
            input_digest=input_digest,
            idempotency_key=idempotency_key,
            cancelled=cancellation_event,
        )
        holder: dict[str, Any] = {}
        completed = threading.Event()

        def run_handler() -> None:
            """在线程中执行 handler；等待超时后主线程不会继续等待未知副作用。"""

            try:
                holder["response"] = definition.handler(parsed, context)
            except BaseException as exc:  # 在线程边界保存异常，主线程统一映射。
                holder["exception"] = exc
            finally:
                completed.set()

        worker = threading.Thread(
            target=run_handler,
            name=f"tool-{definition.spec.tool_name.value}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=input_digest,
                started_at=started_at,
                category=ToolErrorCategory.INTERNAL,
                code="tool_worker_start_failed",
                message=f"无法启动工具 worker: {type(exc).__name__}",
                retryable=True,
                idempotency_key=idempotency_key,
            )
            return self._finalize_result(ledger_key, result)
        if not completed.wait(definition.spec.timeout_seconds):
            cancellation_event.set()
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=input_digest,
                started_at=started_at,
                category=ToolErrorCategory.TIMEOUT,
                code="tool_timeout",
                message=f"工具执行超过 {definition.spec.timeout_seconds:g}s",
                retryable=True,
                details={"timeout_seconds": definition.spec.timeout_seconds},
                idempotency_key=idempotency_key,
            )
            return self._finalize_result(ledger_key, result)

        exception = holder.get("exception")
        if exception is not None:
            if isinstance(exception, ToolInvocationFailure):
                failure = exception
            else:
                failure = _failure(
                    f"工具 handler 未预期失败: {type(exception).__name__}",
                    category=ToolErrorCategory.INTERNAL,
                    code="tool_handler_internal_error",
                    retryable=False,
                    details={"exception_type": type(exception).__name__},
                )
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=input_digest,
                started_at=started_at,
                category=failure.category,
                code=failure.code,
                message=str(failure),
                retryable=failure.retryable,
                details=failure.details,
                output=failure.output,
                evidence_refs=failure.evidence_refs,
                effect_id=failure.effect_id,
                idempotency_key=idempotency_key,
            )
            return self._finalize_result(ledger_key, result)

        response = holder.get("response")
        if not isinstance(response, ToolHandlerResponse):
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=input_digest,
                started_at=started_at,
                category=ToolErrorCategory.INTERNAL,
                code="tool_handler_response_invalid",
                message="handler 没有返回 ToolHandlerResponse",
                retryable=False,
                idempotency_key=idempotency_key,
            )
            return self._finalize_result(ledger_key, result)

        try:
            output = definition.output_model.model_validate(response.output)
        except (ValidationError, TypeError, ValueError) as exc:
            result = self._error_result(
                definition,
                call_id=active_call_id,
                role=role,
                input_digest=input_digest,
                started_at=started_at,
                category=ToolErrorCategory.INTERNAL,
                code="tool_output_schema_invalid",
                message=str(exc),
                retryable=False,
                idempotency_key=idempotency_key,
            )
            return self._finalize_result(ledger_key, result)

        result = self._result(
            definition,
            call_id=active_call_id,
            role=role,
            principal_subject=principal_subject,
            input_digest=input_digest,
            started_at=started_at,
            status=ToolResultStatus.SUCCESS,
            output=output,
            error=None,
            evidence_refs=list(response.evidence_refs),
            effect_id=response.effect_id,
            idempotency_key=idempotency_key,
            audit_metadata=response.audit_metadata,
        )
        return self._finalize_result(ledger_key, result)


def build_tool_registry(
    *,
    settings: AppSettings | None = None,
    snapshot_provider: SnapshotProviderProtocol | None = None,
    knowledge_retriever: Any | None = None,
    cpp_client: FixedCppJsonClient | None = None,
    simulator: AMRSimulator | None = None,
    execution_store: ExecutionStateStoreProtocol | None = None,
    verification_runner: FixedVerificationRunner | Any | None = None,
    approval_store: ApprovalStoreProtocol | None = None,
    knowledge_root: str | Path | None = None,
    principal: Principal | None = None,
    security_required: bool = False,
    approval_verifier: Callable[[ApprovalGrant, ToolSpec, Mapping[str, Any]], None] | None = None,
) -> ToolRegistry:
    """构造正式九工具注册表；所有可变依赖都在组装时显式注入。

    security_required 必须由 API/PEVR 显式打开；旧的 P0-12 契约测试因此可以
    继续使用不含身份的本地 registry，而真实入口不会意外落回 legacy 角色参数。
    """

    dependencies = ToolDependencies(
        settings=settings,
        # 默认 Provider 在注册表生命周期内复用同一已校验快照，保证状态、分配
        # 和路线不会在三次调用之间重新读取出不同的 seed 版本。
        snapshot_provider=snapshot_provider or DefaultWarehouseSnapshotProvider(),
        knowledge_retriever=knowledge_retriever,
        cpp_client=cpp_client,
        simulator=simulator,
        execution_store=execution_store or InMemoryExecutionStateStore(),
        verification_runner=verification_runner,
        approval_store=approval_store or InMemoryApprovalStore(),
        knowledge_root=Path(knowledge_root) if knowledge_root is not None else None,
    )
    return ToolRegistry(
        _definitions(dependencies),
        bound_principal=principal,
        security_required=security_required,
        approval_verifier=approval_verifier,
    )


def get_tool_specs() -> tuple[ToolSpec, ...]:
    """返回不初始化外部服务的完整九工具 Schema 清单。"""

    return build_tool_registry().specs()


__all__ = [
    "ToolDefinition",
    "ToolDependencies",
    "ToolExecutor",
    "ToolHandlerResponse",
    "ToolInvocationContext",
    "ToolInvocationFailure",
    "ToolRegistry",
    "UnknownToolError",
    "build_tool_registry",
    "get_tool_specs",
]
