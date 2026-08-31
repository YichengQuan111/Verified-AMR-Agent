"""策略无关的 Guard → Understand → 初次 Retrieve 前置组件。

P0-13 生产 PEVR 与 P0-19 独立 ReAct 共用同一套入口身份、合同冻结和首次 RAG
证据，避免把前置阶段复制进某一种控制策略。本模块不编排 ``plan → validate →
execute → verify``，也不实例化 LangGraph。失败以 ``SharedPrefixError`` 抛出，
调用方再映射到各自的 Runner 异常与 Trace。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict

from agent.context import (
    BudgetUsage,
    ContextEvidence,
    PromptNodeName,
    build_node_context,
    understand_goal,
)
from agent.context.contracts import NodeRoute
from agent.planning import ChargingGoal, TaskContract
from agent.runtime.state import Observation, ObservationSource, ObservationStatus, RunState, RunStatus
from agent.security.contracts import Principal
from agent.tools import ToolName, ToolResult, ToolResultStatus, UserRole
from agent.tools.snapshots import EnvironmentSnapshot, SnapshotProviderProtocol
from services.amr_simulator.contracts import FleetPlanRoute, SimulationPlan, ValidatorConfig
from services.retrieval.contracts import RetrievalResponse, RetrievalStatus


Clock = Callable[[], datetime]


class PrefixRequest(Protocol):
    """前置阶段只依赖入口身份字段，不绑定 PEVR 图状态。"""

    run_id: str
    raw_request: str
    environment_ref: str
    principal_role: UserRole
    principal: Principal | None
    requested_output_tokens: int


class SharedPrefixError(RuntimeError):
    """Guard/Understand/Retrieve 的确定性失败；调用方不得改写 code。"""

    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


# 与历史 PEVR 入口预算保持同一数字，避免 Understand 合同默认值漂移。
SHARED_ENTRY_BUDGETS: dict[str, int] = {
    "max_total_seconds": 300,
    "max_input_tokens": 30000,
    "max_output_tokens": 5000,
    "max_tool_steps": 8,
    "max_replans": 2,
    "max_retries": 2,
}

DEFAULT_PAYLOAD_KG = 1.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SharedUnderstandResult(BaseModel):
    """Understand 成功后冻结的合同、初始 RunState 和模型节点结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    contract: TaskContract
    run_state: RunState
    snapshot: EnvironmentSnapshot
    node_result: Any
    budget_usage: BudgetUsage


class SharedRetrieveResult(BaseModel):
    """初次 Retrieve 的工具结果；失败也返回，由调用方决定是否终止。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tool_result: ToolResult
    retrieve_arguments: dict[str, Any]
    rag_evidence: list[ContextEvidence]
    observation: Observation
    response: RetrievalResponse | None = None
    budget_usage: BudgetUsage


@dataclass(frozen=True, slots=True)
class FrozenInitialFacts:
    """Retrieve 之后对三策略锁定的共同初始事实，禁止模型覆盖。"""

    run_id: str
    environment_ref: str
    seed: int
    principal_role: UserRole
    order_ids: tuple[str, ...]
    amr_ids: tuple[str, ...]
    evidence_digest: str
    retrieve_query: str


class SharedPrefixService:
    """执行 Guard、Understand 和恰好一次初始 Retrieve。

    PEVR 图节点只负责 Trace/Checkpoint 包装；ReAct Runner 直接调用本服务后
    进入自己的 ``decide → act → observe`` 循环，不再进入固定四任务 DAG。
    """

    def __init__(
        self,
        provider: Any,
        registry: Any,
        snapshot_provider: SnapshotProviderProtocol,
        *,
        clock: Clock = _utc_now,
        entry_budgets: dict[str, int] | None = None,
        security_required: bool = False,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.snapshot_provider = snapshot_provider
        self._clock = clock
        self.entry_budgets = dict(entry_budgets or SHARED_ENTRY_BUDGETS)
        self.security_required = security_required

    def guard(self, request: PrefixRequest) -> None:
        """检查 operator 角色、验签 Principal 和 dispatch 审批声明。"""

        if request.principal_role is not UserRole.OPERATOR:
            raise SharedPrefixError("guard", "role_not_allowed", "正常执行必须使用 operator")
        if self.security_required and request.principal is None:
            raise SharedPrefixError("guard", "principal_required", "安全执行必须使用已验签 Principal")
        dispatch_spec = self.registry.get(ToolName.DISPATCH_SIMULATION).spec
        if not dispatch_spec.requires_approval:
            raise SharedPrefixError(
                "guard",
                "dispatch_approval_contract_missing",
                "dispatch_simulation 的 requires_approval 声明缺失",
            )

    def understand(
        self,
        request: PrefixRequest,
        *,
        budget_usage: BudgetUsage | None = None,
        requested_output_tokens: int | None = None,
    ) -> SharedUnderstandResult:
        """把自然语言冻结为 TaskContract，并用快照真值覆盖环境/订单。"""

        from agent.planning import ExecutionBudgets

        usage = budget_usage or BudgetUsage()
        snapshot = self.snapshot_provider.get_snapshot(request.environment_ref)
        limits = ExecutionBudgets(**self.entry_budgets)
        remaining = max(1, limits.max_output_tokens - usage.output_tokens)
        output_tokens = min(
            requested_output_tokens or request.requested_output_tokens,
            limits.max_output_tokens,
            remaining,
        )
        context = build_node_context(
            node_name=PromptNodeName.UNDERSTAND_GOAL,
            request_id=f"{request.run_id}:understand",
            node_input={
                "raw_request": request.raw_request,
                "environment_ref": snapshot.environment_ref,
                "environment_snapshot": {
                    "state_version": snapshot.state_version,
                    "map_width": snapshot.map_width,
                    "map_height": snapshot.map_height,
                    "location_ids": sorted(snapshot.location_positions),
                    "blocked_cells": [item.model_dump(mode="json") for item in snapshot.blocked_cells],
                },
                "available_orders": [item.model_dump(mode="json") for item in snapshot.orders],
                "fixed_execution_defaults": {
                    "max_total_seconds": self.entry_budgets["max_total_seconds"],
                    "max_input_tokens": self.entry_budgets["max_input_tokens"],
                    "max_output_tokens": self.entry_budgets["max_output_tokens"],
                    "minimum_battery_percent": 20,
                    "maximum_load_kg": 100,
                    "enforce_time_windows": True,
                    "max_tool_steps": 8,
                    "max_replans": 2,
                    "max_retries": 2,
                },
            },
            budget_limits=limits,
            budget_usage=usage,
            requested_output_tokens=output_tokens,
            generated_at=self._clock(),
        )
        result = understand_goal(self.provider, context)
        if result.route is not NodeRoute.SUCCESS or result.output is None:
            raise SharedPrefixError(
                "understand",
                result.reason_code or "understand_goal_failed",
                result.reason or "understand 节点没有成功输出",
            )
        contract = result.output
        if getattr(self.snapshot_provider, "injected_charging", None):
            contract = canonicalize_charging_contract(
                contract,
                snapshot,
                injected=getattr(self.snapshot_provider, "injected_charging", None),
            )
        elif getattr(self.snapshot_provider, "injected_orders", None):
            contract = canonicalize_contract_against_snapshot(contract, snapshot)
        validate_contract_against_snapshot(contract, snapshot)
        now = self._clock()
        run_state = RunState(
            run_id=request.run_id,
            status=RunStatus.PLANNING,
            plan_version=1,
            task_contract=contract,
            plan_tasks=[],
            amr_states=[item.model_copy(deep=True) for item in snapshot.amrs],
            orders=[item.model_copy(deep=True) for item in contract.orders],
            observations=[],
            current_task_id=None,
            completed_task_ids=[],
            failed_task_ids=[],
            created_at=now,
            updated_at=now,
            replan_count=0,
        )
        return SharedUnderstandResult(
            contract=contract,
            run_state=run_state,
            snapshot=snapshot,
            node_result=result,
            budget_usage=result.usage_after,
        )

    def retrieve(
        self,
        request: PrefixRequest,
        contract: TaskContract,
        *,
        run_state: RunState,
        budget_usage: BudgetUsage,
    ) -> SharedRetrieveResult:
        """执行恰好一次 ``retrieve_knowledge``；调用方不得在本轮对照中再检索。"""

        query = (
            f"{contract.goal}；请参考仓储电量安全余量、充电 SOP 和充电完成事件。"
            if contract.is_charging_contract()
            else (
                f"{contract.goal}；请参考仓储运输 SOP、交通冲突、电量安全余量、"
                "Validator 和运输完成条件。"
            )
        )
        retrieve_arguments = {
            "query": query,
            "top_k": 5,
            "role_scope": request.principal_role,
        }
        result = self._registry_execute(
            ToolName.RETRIEVE_KNOWLEDGE,
            retrieve_arguments,
            role=request.principal_role,
            call_id=f"{request.run_id}:retrieve",
            principal=request.principal,
        )
        observation = observation_from_tool(result, task_id=None)
        usage = add_tool_usage(budget_usage, result)
        if result.status is not ToolResultStatus.SUCCESS:
            return SharedRetrieveResult(
                tool_result=result,
                retrieve_arguments=retrieve_arguments,
                rag_evidence=[],
                observation=observation,
                response=None,
                budget_usage=usage,
            )
        response = RetrievalResponse.model_validate(result.output)
        rag_evidence: list[ContextEvidence] = []
        if response.status is RetrievalStatus.ANSWERABLE:
            rag_evidence = response.to_context_evidence(collected_at=result.finished_at)
        return SharedRetrieveResult(
            tool_result=result,
            retrieve_arguments=retrieve_arguments,
            rag_evidence=rag_evidence,
            observation=observation,
            response=response,
            budget_usage=usage,
        )

    def _registry_execute(
        self,
        tool_name: ToolName,
        arguments: Mapping[str, Any],
        *,
        role: Any,
        call_id: str,
        principal: Any = None,
    ) -> Any:
        """兼容真实 Registry 与 P0-13 fake：只在签名接受时传入 principal。"""

        kwargs: dict[str, Any] = {"role": role, "call_id": call_id}
        try:
            parameters = inspect.signature(self.registry.execute).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_var_kw = any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
        )
        if ("principal" in parameters or accepts_var_kw) and principal is not None:
            kwargs["principal"] = principal
        return self.registry.execute(tool_name, arguments, **kwargs)


def canonicalize_contract_against_snapshot(
    contract: TaskContract,
    snapshot: EnvironmentSnapshot,
) -> TaskContract:
    """把 LLM 合同订单/环境约束强制覆盖为快照真值，并清零 missing_information。"""

    if not snapshot.orders:
        raise SharedPrefixError("understand", "dynamic_order_missing", "动态订单快照没有可对齐的订单真值")
    constraints = contract.constraints.model_copy(
        update={
            "map_width": snapshot.map_width,
            "map_height": snapshot.map_height,
            "blocked_cells": list(snapshot.blocked_cells),
        }
    )
    return contract.model_copy(
        update={
            "orders": [item.model_copy(deep=True) for item in snapshot.orders],
            "environment_ref": snapshot.environment_ref,
            "constraints": constraints,
            "missing_information": [],
        }
    )


def canonicalize_charging_contract(
    contract: TaskContract,
    snapshot: EnvironmentSnapshot,
    *,
    injected: ChargingGoal | None,
) -> TaskContract:
    """把充电合同冻结为快照真值：空订单、指定 AMR/充电站。"""

    goal = injected if isinstance(injected, ChargingGoal) else contract.charging
    if goal is None:
        raise SharedPrefixError("understand", "charging_goal_missing", "充电快照没有可对齐的充电目标")
    constraints = contract.constraints.model_copy(
        update={
            "map_width": snapshot.map_width,
            "map_height": snapshot.map_height,
            "blocked_cells": list(snapshot.blocked_cells),
        }
    )
    return contract.model_copy(
        update={
            "orders": [],
            "charging": goal.model_copy(deep=True),
            "environment_ref": snapshot.environment_ref,
            "constraints": constraints,
            "missing_information": [],
            "completion_criteria": ["目标 AMR 在指定充电站达到目标电量并产生 charging.completed"],
        }
    )


def validate_contract_against_snapshot(contract: TaskContract, snapshot: EnvironmentSnapshot) -> None:
    """确认合同与当前快照一致，且没有未解决的执行必需信息。"""

    if contract.environment_ref != snapshot.environment_ref:
        raise SharedPrefixError("understand", "environment_ref_mismatch", "合同环境与固定快照不一致")
    if contract.constraints.map_width != snapshot.map_width or contract.constraints.map_height != snapshot.map_height:
        raise SharedPrefixError("understand", "map_size_mismatch", "合同地图尺寸与固定快照不一致")
    if contract.constraints.blocked_cells != snapshot.blocked_cells:
        raise SharedPrefixError("understand", "blocked_cells_mismatch", "合同封路与固定环境快照不一致")
    if contract.missing_information:
        raise SharedPrefixError("understand", "missing_information", "正常闭环不能带未解决的执行必需信息")
    if contract.is_charging_contract():
        goal = contract.charging
        assert goal is not None
        amr_ids = {item.amr_id for item in snapshot.amrs}
        if goal.amr_id not in amr_ids:
            raise SharedPrefixError("understand", "charging_amr_not_found", f"充电合同引用了未知 AMR: {goal.amr_id}")
        if goal.charge_station not in snapshot.location_positions:
            raise SharedPrefixError(
                "understand",
                "charging_station_not_found",
                f"充电合同引用了未知充电站: {goal.charge_station}",
            )
        if contract.orders:
            raise SharedPrefixError("understand", "charging_order_not_allowed", "充电合同不能携带运输订单")
        return
    snapshot_orders = {item.order_id: item for item in snapshot.orders}
    for order in contract.orders:
        if order.order_id not in snapshot_orders or order != snapshot_orders[order.order_id]:
            raise SharedPrefixError(
                "understand",
                "order_snapshot_mismatch",
                f"合同订单不是固定快照中的原始订单: {order.order_id}",
            )
        for location_id in (order.pickup, order.dropoff):
            if location_id not in snapshot.location_positions:
                raise SharedPrefixError(
                    "understand",
                    "location_not_found",
                    f"订单 {order.order_id} 引用了未知工位: {location_id}",
                )


def add_tool_usage(usage: BudgetUsage, result: ToolResult) -> BudgetUsage:
    """把真实工具步数和耗时加入预算快照。"""

    return BudgetUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        tool_steps=usage.tool_steps + 1,
        elapsed_seconds=usage.elapsed_seconds + result.duration_ms / 1000.0,
        replans=usage.replans,
        retries=usage.retries,
    )


def observation_from_tool(result: ToolResult, *, task_id: str | None) -> Observation:
    """将 ToolResult 映射为 Observation，不把完整输出复制进 state_delta。"""

    status = ObservationStatus.OK if result.status is ToolResultStatus.SUCCESS else ObservationStatus.ERROR
    return Observation(
        observation_id=f"observation://{result.call_id}",
        run_id=result.call_id.split(":", 1)[0],
        task_id=task_id,
        source=ObservationSource.TOOL,
        observed_at=result.finished_at,
        status=status,
        summary=_tool_summary(result),
        state_delta={
            "tool_name": result.tool_name.value,
            "status": result.status.value,
            "output_digest": result.output_digest,
            "effect_id": result.effect_id,
        },
        evidence_refs=[f"tool://{result.call_id}", *result.evidence_refs],
        tool_result=result,
        violations=[],
        requires_replan=result.status is not ToolResultStatus.SUCCESS,
        requires_human=(
            result.error is not None and result.error.category.value in {"permission_denied", "unsafe_plan"}
        ),
    )


def _tool_summary(result: ToolResult) -> str:
    """生成短摘要；正文留在 ToolResult 证据中。"""

    if result.status is not ToolResultStatus.SUCCESS:
        return f"工具 {result.tool_name.value} 失败: {result.error.code if result.error else 'unknown'}"
    output = result.output if isinstance(result.output, dict) else {}
    if result.tool_name is ToolName.RETRIEVE_KNOWLEDGE:
        return f"RAG 检索成功，返回 {len(output.get('results', []))} 条引用"
    if result.tool_name is ToolName.ALLOCATE_TASKS:
        return f"Hungarian 分配成功，分配 {len(output.get('assignments', []))} 个订单"
    if result.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
        return f"A* 路线规划成功，生成 {len(output.get('routes', []))} 条路线"
    if result.tool_name is ToolName.VALIDATE_FLEET_PLAN:
        return f"P0-10 Validator 返回 {output.get('status', 'unknown')}"
    if result.tool_name is ToolName.DISPATCH_SIMULATION:
        completed = sum(item.get("status") == "completed" for item in output.get("orders", []))
        return f"仿真 {output.get('status', 'unknown')}，完成 {completed} 个订单"
    return f"工具 {result.tool_name.value} 成功"


def idle_charging_simulation_plan(contract: TaskContract, snapshot: EnvironmentSnapshot) -> SimulationPlan:
    """空订单、空路线的合法仿真 envelope；供充电合同在 ReAct 中验证后派发。"""

    return SimulationPlan(
        schema_version="1.0",
        environment_ref=snapshot.environment_ref,
        map_width=snapshot.map_width,
        map_height=snapshot.map_height,
        blocked_cells=[item.model_copy(deep=True) for item in snapshot.blocked_cells],
        blocked_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.blocked_edges],
        one_way_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.one_way_edges],
        amrs=[item.model_copy(deep=True) for item in snapshot.amrs],
        orders=[],
        location_positions={key: value.model_copy(deep=True) for key, value in snapshot.location_positions.items()},
        completed_order_ids=[],
        routes=[],
        start_time=snapshot.start_time,
        max_time=snapshot.max_time,
        config=ValidatorConfig(
            maximum_load_kg=contract.constraints.maximum_load_kg,
            energy_per_cell_percent=1.0,
            battery_safety_reserve_percent=15.0,
            new_task_battery_threshold_percent=20.0,
            critical_battery_threshold_percent=10.0,
            minimum_safety_distance_cells=1,
            default_workstation_capacity=1,
        ),
        workstation_capacities=dict(snapshot.workstation_capacities),
        ruleset_version="p0-10.v1",
    )


def build_simulation_plan_from_routes(
    contract: TaskContract,
    snapshot: EnvironmentSnapshot,
    route: Any,
    route_arguments: dict[str, Any],
) -> SimulationPlan:
    """把 A* 输出包装成 P0-10/P0-11 共同的完整计划 envelope。"""

    max_time = int(route_arguments.get("max_time", snapshot.max_time))
    routes = [
        FleetPlanRoute(
            **item.model_dump(mode="python"),
            payload_kg=DEFAULT_PAYLOAD_KG,
        )
        for item in route.routes
    ]
    return SimulationPlan(
        schema_version="1.0",
        environment_ref=snapshot.environment_ref,
        map_width=snapshot.map_width,
        map_height=snapshot.map_height,
        blocked_cells=[item.model_copy(deep=True) for item in snapshot.blocked_cells],
        blocked_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.blocked_edges],
        one_way_edges=[{"from": edge["from"], "to": edge["to"]} for edge in snapshot.one_way_edges],
        amrs=[item.model_copy(deep=True) for item in snapshot.amrs],
        orders=[item.model_copy(deep=True) for item in contract.orders],
        location_positions={key: value.model_copy(deep=True) for key, value in snapshot.location_positions.items()},
        completed_order_ids=list(snapshot.completed_order_ids),
        routes=routes,
        start_time=snapshot.start_time,
        max_time=max_time,
        config=ValidatorConfig(
            maximum_load_kg=contract.constraints.maximum_load_kg,
            energy_per_cell_percent=1.0,
            battery_safety_reserve_percent=15.0,
            new_task_battery_threshold_percent=20.0,
            critical_battery_threshold_percent=10.0,
            minimum_safety_distance_cells=1,
            default_workstation_capacity=1,
        ),
        workstation_capacities=dict(snapshot.workstation_capacities),
        ruleset_version="p0-10.v1",
    )


__all__ = [
    "DEFAULT_PAYLOAD_KG",
    "FrozenInitialFacts",
    "SHARED_ENTRY_BUDGETS",
    "SharedPrefixError",
    "SharedPrefixService",
    "SharedRetrieveResult",
    "SharedUnderstandResult",
    "add_tool_usage",
    "build_simulation_plan_from_routes",
    "canonicalize_charging_contract",
    "canonicalize_contract_against_snapshot",
    "idle_charging_simulation_plan",
    "observation_from_tool",
    "validate_contract_against_snapshot",
]
