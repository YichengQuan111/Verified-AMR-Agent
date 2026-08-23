"""P0-13 Planner DAG 的确定性正常闭环验证器。

LLM 只负责提出 ``PlanTasksOutput``，本模块负责在任何业务工具执行前确认：
工具集合、拓扑顺序、参数范围和受控数据流都符合 P0-13 的成功闭环。这里不
执行工具，也不接受任意模板表达式；跨任务数据只能使用本模块声明的两个
固定引用形状，避免把 Planner 输出变成代码执行或路径注入入口。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent.context.contracts import PlanTasksOutput
from agent.planning.contracts import PlanTask, PlanTaskStatus, RiskLevel, TaskContract
from agent.planning.dag import DAGValidationError, topological_sort
from agent.tools.contracts import ToolName, ToolSpec, UserRole, validate_tool_arguments


class PlanValidationContract(BaseModel):
    """验证结果基类；错误结果本身也必须是可序列化的严格对象。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class PlanValidationIssue(PlanValidationContract):
    """一条可定位的 Planner 计划错误。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    task_id: str | None = None


class PlanValidationResult(PlanValidationContract):
    """P0-13 计划门禁的确定性结果。"""

    schema_version: Literal["1.0"] = "1.0"
    valid: bool
    plan_version: int = Field(ge=1)
    topological_order: list[str]
    required_tool_names: list[ToolName]
    errors: list[PlanValidationIssue]

    @property
    def error_count(self) -> int:
        """返回错误数量；调用方不需要自己重复计算摘要字段。"""

        return len(self.errors)


NORMAL_PEVR_TOOL_CHAIN: tuple[ToolName, ...] = (
    ToolName.ALLOCATE_TASKS,
    ToolName.PLAN_MULTI_AMR_ROUTES,
    ToolName.VALIDATE_FLEET_PLAN,
    ToolName.DISPATCH_SIMULATION,
)


def make_data_ref(reference: str) -> dict[str, str]:
    """创建受控数据流引用；只允许单一 ``$ref`` 字段。"""

    if not reference or reference.startswith("$"):
        raise ValueError("数据流引用必须是非空的固定标识")
    return {"$ref": reference}


def is_data_ref(value: object, reference: str) -> bool:
    """严格匹配固定引用，不递归执行字符串或表达式。"""

    return value == {"$ref": reference}


def canonicalize_normal_pevr_plan(
    plan: PlanTasksOutput,
    *,
    contract: TaskContract | None = None,
    expected_seed: int | None = None,
) -> tuple[PlanTasksOutput, list[str]]:
    """还原 llama.cpp 的 JsonValue 包装，并把固定事实字段覆盖为合同真值。

    传入 contract 与 expected_seed 时，环境引用、订单全集、临时封路、seed 与
    规则版本一律以合同/请求真值覆盖（LLM 对这些字段没有合法选择权）；max_time
    仅在缺失或小于最晚 deadline 时拉回真值。所有覆盖都会记录 note 供审计，
    覆盖后的计划仍必须完整通过确定性 Validator。
    """

    # Pydantic 对 JsonValue 只生成空 Schema；部分本地模型会把字符串、数组和
    # 整数误写成 ``{"type": ..., "value": ...}``。这里只接受严格的两字段
    # 包装，并仅把 ref 包装映射成固定 ``$ref``；未知形状原样保留，后续 Validator
    # 仍会拒绝它，避免“兼容”逻辑变成绕过参数和数据流门禁的后门。
    payload = plan.model_dump(mode="python")
    notes: list[str] = []
    for task_payload in payload["tasks"]:
        arguments = task_payload["tool_arguments"]
        normalized: dict[str, Any] = {}
        for name, value in arguments.items():
            if (
                isinstance(value, dict)
                and set(value) == {"type", "value"}
                and isinstance(value.get("type"), str)
            ):
                kind = value["type"]
                wrapped_value = value["value"]
                if kind == "ref" and name in {
                    "assignments",
                    "plan",
                    "environment_ref",
                    "order_ids",
                    "blocked_cells",
                    "max_time",
                    "ruleset_version",
                    "seed",
                } and isinstance(wrapped_value, str):
                    normalized[name] = {"$ref": wrapped_value}
                    notes.append(f"{task_payload['task_id']}.{name}:ref_wrapper")
                    continue
                if kind in {"string", "array", "integer", "number", "boolean", "null", "object"}:
                    normalized[name] = wrapped_value
                    notes.append(f"{task_payload['task_id']}.{name}:{kind}_wrapper")
                    continue
            normalized[name] = value
        task_payload["tool_arguments"] = normalized
    if contract is not None and expected_seed is not None:
        # 下列字段的正确值由合同/请求唯一确定：环境引用、订单全集、临时封路、
        # 确定性 seed、Validator 规则版本。LLM 填写它们只有出错空间、没有决策
        # 价值——实测本地模型会把 $ref 语法照抄成 fixed:* 伪引用或自引用
        # task:<自身>/input/*（2026-08-22 演示实测 6 次运行 4 次因此失败），
        # 所以这里一律以真值覆盖并记录 note。覆盖只能让计划更贴近合同，不会
        # 放宽任何约束：Validator 对覆盖后的计划仍逐项生效，assignments/plan
        # 数据流引用不在豁免范围，未知工具/基数/拓扑错误照旧被拒绝。
        latest_deadline = max(order.deadline for order in contract.orders)
        fixed_values: dict[ToolName, dict[str, JsonValue]] = {
            ToolName.ALLOCATE_TASKS: {
                "environment_ref": contract.environment_ref,
                "order_ids": [order.order_id for order in contract.orders],
            },
            ToolName.PLAN_MULTI_AMR_ROUTES: {
                "environment_ref": contract.environment_ref,
                "blocked_cells": [
                    cell.model_dump(mode="python")
                    for cell in contract.constraints.blocked_cells
                ],
            },
            ToolName.VALIDATE_FLEET_PLAN: {
                "environment_ref": contract.environment_ref,
                "ruleset_version": "p0-10.v1",
            },
            ToolName.DISPATCH_SIMULATION: {"seed": expected_seed},
        }
        for task_payload in payload["tasks"]:
            tool_name = ToolName(task_payload["tool_name"])
            if tool_name not in fixed_values:
                continue
            arguments = task_payload["tool_arguments"]
            for name, expected in fixed_values[tool_name].items():
                if arguments.get(name) != expected:
                    arguments[name] = expected
                    notes.append(f"{task_payload['task_id']}.{name}:fixed_fact_override")
            if tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
                # max_time 是唯一允许 LLM 加大的字段（Validator 接受 >= 最晚
                # deadline）；只在缺失/非整数/不足时拉回真值，不抹掉合法 horizon。
                max_time = arguments.get("max_time")
                if (
                    not isinstance(max_time, int)
                    or isinstance(max_time, bool)
                    or max_time < latest_deadline
                ):
                    arguments["max_time"] = latest_deadline
                    notes.append(f"{task_payload['task_id']}.max_time:fixed_fact_override")
    return PlanTasksOutput.model_validate(payload), notes


def canonicalize_replanned_pevr_plan(
    plan: PlanTasksOutput,
    *,
    contract: TaskContract,
    expected_seed: int,
) -> PlanTasksOutput:
    """局部重规划落地前覆盖固定事实和四工具数据流，不放宽 Validator。

    首轮 ``plan_tasks`` 仍要求模型自己写出 ``$ref``，以便语义修复。替换子图
    里这些字段没有决策空间：Fast 会把硬地图障碍或内联 SimulationPlan 抄进
    ``ReplanOutput``，输出在上下文末尾被截断。覆盖后仍走完整
    ``validate_replanned_pevr_plan``；缺工具、乱改完成锚点、审批标志错误
    照旧拒绝。只改 pending 替换任务，不改写已完成 allocate/route 的证据。
    """

    normalized, _ = canonicalize_normal_pevr_plan(
        plan,
        contract=contract,
        expected_seed=expected_seed,
    )
    by_tool: dict[ToolName, list[PlanTask]] = {}
    for task in normalized.tasks:
        by_tool.setdefault(task.tool_name, []).append(task)
    if any(len(by_tool.get(name, [])) != 1 for name in NORMAL_PEVR_TOOL_CHAIN):
        return normalized
    allocate, route, validate, dispatch = (by_tool[name][0] for name in NORMAL_PEVR_TOOL_CHAIN)
    rewritten: dict[str, PlanTask] = {}
    if route.status is not PlanTaskStatus.COMPLETED:
        route_args = dict(route.tool_arguments)
        route_args["assignments"] = make_data_ref(f"task:{allocate.task_id}/output/assignments")
        route_args["environment_ref"] = contract.environment_ref
        rewritten[route.task_id] = route.model_copy(
            update={
                "dependencies": [allocate.task_id],
                "tool_arguments": route_args,
                "evidence_refs": [],
                "effect_id": None,
            }
        )
        route = rewritten[route.task_id]
    if validate.status is not PlanTaskStatus.COMPLETED:
        validate_args = dict(validate.tool_arguments)
        validate_args["plan"] = make_data_ref("derived:simulation_plan")
        validate_args["environment_ref"] = contract.environment_ref
        validate_args["ruleset_version"] = "p0-10.v1"
        rewritten[validate.task_id] = validate.model_copy(
            update={
                "dependencies": [route.task_id],
                "tool_arguments": validate_args,
                "evidence_refs": [],
                "effect_id": None,
            }
        )
        validate = rewritten[validate.task_id]
    if dispatch.status is not PlanTaskStatus.COMPLETED:
        dispatch_args = dict(dispatch.tool_arguments)
        dispatch_args["plan"] = make_data_ref("derived:simulation_plan")
        dispatch_args["seed"] = expected_seed
        rewritten[dispatch.task_id] = dispatch.model_copy(
            update={
                "dependencies": [validate.task_id],
                "tool_arguments": dispatch_args,
                "evidence_refs": [],
                "effect_id": None,
            }
        )
    if not rewritten:
        return normalized
    return normalized.model_copy(
        update={"tasks": [rewritten.get(task.task_id, task) for task in normalized.tasks]}
    )


def _issue(
    errors: list[PlanValidationIssue],
    code: str,
    message: str,
    task_id: str | None = None,
) -> None:
    """按统一格式追加错误，保持验证器输出可被报告和测试稳定消费。"""

    errors.append(PlanValidationIssue(code=code, message=message, task_id=task_id))


def _task_map(plan: PlanTasksOutput, errors: list[PlanValidationIssue]) -> dict[str, PlanTask]:
    """建立任务索引并检查重复；重复通常已被 Pydantic 拒绝，这里保留防御层。"""

    result: dict[str, PlanTask] = {}
    for task in plan.tasks:
        if task.task_id in result:
            _issue(errors, "duplicate_task_id", f"任务 ID 重复: {task.task_id}", task.task_id)
        else:
            result[task.task_id] = task
    return result


def _find_single_task(
    task_by_id: Mapping[str, PlanTask],
    tool_name: ToolName,
    errors: list[PlanValidationIssue],
) -> PlanTask | None:
    """从计划中定位一个工具任务，并拒绝重复工具步骤。"""

    matches = [task for task in task_by_id.values() if task.tool_name is tool_name]
    if len(matches) != 1:
        _issue(
            errors,
            "tool_cardinality",
            f"正常闭环必须恰好包含一个 {tool_name.value}，实际为 {len(matches)} 个",
        )
        return None
    return matches[0]


def _same_string_list(actual: object, expected: Iterable[str]) -> bool:
    """比较 ID 列表的集合和重复语义，避免 LLM 通过换序制造假差异。"""

    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return False
    expected_list = list(expected)
    return len(actual) == len(set(actual)) and set(actual) == set(expected_list)


def validate_normal_pevr_plan(
    contract: TaskContract,
    plan: PlanTasksOutput,
    *,
    tool_specs: Iterable[ToolSpec] = (),
    expected_seed: int | None = None,
) -> PlanValidationResult:
    """验证 P0-13 仅成功路径的四工具 DAG。

    正常闭环固定为 ``Hungarian → A* → P0-10 Validator → 仿真``。RAG 在状态图
    的 ``retrieve`` 节点先通过工具注册表完成，因此不允许 Planner 再偷偷新增
    一个检索分支。计划中的 ``validate_fleet_plan`` 仍是下一步即将执行的工具，
    但只有本函数无错误时，Executor 才能开始调用任何 Planner 任务。
    """

    return _validate_pevr_plan(
        contract,
        plan,
        tool_specs=tool_specs,
        expected_seed=expected_seed,
        expected_plan_version=1,
        completed_task_ids=(),
    )


def validate_replanned_pevr_plan(
    contract: TaskContract,
    plan: PlanTasksOutput,
    *,
    completed_task_ids: Iterable[str],
    tool_specs: Iterable[ToolSpec] = (),
    expected_seed: int | None = None,
    expected_plan_version: int | None = None,
) -> PlanValidationResult:
    """重新验证 P0-14 新版本的完整四工具闭环。

    已完成节点可以携带运行证据并保持 completed；其余节点仍必须是全新的 pending
    任务。四工具基数、数据流、审批和参数规则与首轮完全相同，因而局部替换不能
    只交回一条 route 就绕过 Validator/dispatch 链。
    """

    return _validate_pevr_plan(
        contract,
        plan,
        tool_specs=tool_specs,
        expected_seed=expected_seed,
        expected_plan_version=expected_plan_version or plan.plan_version,
        completed_task_ids=completed_task_ids,
    )


def validate_charging_pevr_plan(
    contract: TaskContract,
    plan: PlanTasksOutput,
    *,
    tool_specs: Iterable[ToolSpec] = (),
    expected_seed: int | None = None,
    expected_plan_version: int | None = None,
    completed_task_ids: Iterable[str] = (),
) -> PlanValidationResult:
    """充电合同只允许 dispatch_simulation，禁止再走运输四工具链。

    空订单 idle plan 由执行器按快照构造；Planner 不能塞回占位 TransportOrder。
    """

    errors: list[PlanValidationIssue] = []
    task_by_id = _task_map(plan, errors)
    completed = set(completed_task_ids)
    unknown_completed = completed - set(task_by_id)
    if unknown_completed:
        _issue(
            errors,
            "completed_task_unknown",
            f"已完成集合包含未知任务: {', '.join(sorted(unknown_completed))}",
        )
    dependencies = {task.task_id: task.dependencies for task in plan.tasks}
    try:
        ordered_ids = topological_sort(dependencies)
    except DAGValidationError as exc:
        ordered_ids = sorted(task_by_id)
        _issue(errors, "dag_invalid", str(exc))

    target_version = expected_plan_version if expected_plan_version is not None else 1
    if plan.plan_version != target_version:
        _issue(errors, "plan_version_invalid", f"计划版本必须为 {target_version}")
    if not contract.is_charging_contract():
        _issue(errors, "charging_contract_required", "充电计划只能用于充电合同")
    if len(plan.tasks) > contract.budgets.max_tool_steps:
        _issue(
            errors,
            "tool_budget_exceeded",
            f"计划任务数 {len(plan.tasks)} 超过工具步数预算 {contract.budgets.max_tool_steps}",
        )

    spec_by_name = {spec.tool_name: spec for spec in tool_specs}
    for task in plan.tasks:
        try:
            validate_tool_arguments(task.tool_name, task.tool_arguments)
        except (KeyError, TypeError, ValueError) as exc:
            _issue(errors, "tool_arguments_invalid", str(exc), task.task_id)
        if task.tool_name is not ToolName.DISPATCH_SIMULATION:
            _issue(
                errors,
                "charging_tool_not_allowed",
                "充电合同不能包含运输分配/路线/Validator 任务",
                task.task_id,
            )
        if task.task_id in completed:
            if task.status is not PlanTaskStatus.COMPLETED:
                _issue(errors, "completed_task_status_invalid", "保留完成任务必须是 completed", task.task_id)
        else:
            if task.status is not PlanTaskStatus.PENDING:
                _issue(errors, "task_status_invalid", "未完成任务必须是 pending", task.task_id)
            if task.evidence_refs or task.effect_id is not None:
                _issue(errors, "task_has_runtime_evidence", "新任务不能预填运行期证据或 effect_id", task.task_id)
        spec = spec_by_name.get(task.tool_name)
        if spec is not None and task.approval_required != spec.requires_approval:
            _issue(
                errors,
                "approval_flag_mismatch",
                f"任务 approval_required 必须与 ToolSpec.requires_approval={spec.requires_approval} 一致",
                task.task_id,
            )

    dispatch = _find_single_task(task_by_id, ToolName.DISPATCH_SIMULATION, errors)
    if dispatch is not None:
        if dispatch.dependencies:
            _issue(errors, "charging_dispatch_dependencies_invalid", "充电 dispatch 不能依赖运输任务", dispatch.task_id)
        if not is_data_ref(dispatch.tool_arguments.get("plan"), "derived:simulation_plan"):
            _issue(
                errors,
                "simulation_plan_ref_invalid",
                "dispatch_simulation 只能引用受控派生 SimulationPlan",
                dispatch.task_id,
            )
        seed = dispatch.tool_arguments.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            _issue(errors, "simulation_seed_invalid", "dispatch_simulation 必须使用确定性的整数 seed", dispatch.task_id)
        elif expected_seed is not None and seed != expected_seed:
            _issue(
                errors,
                "simulation_seed_mismatch",
                f"dispatch_simulation 的 seed 必须与本次请求一致: {expected_seed}",
                dispatch.task_id,
            )

    errors.sort(key=lambda item: (item.task_id or "", item.code, item.message))
    return PlanValidationResult(
        valid=not errors,
        plan_version=plan.plan_version,
        topological_order=ordered_ids,
        required_tool_names=[ToolName.DISPATCH_SIMULATION],
        errors=errors,
    )


def _validate_pevr_plan(
    contract: TaskContract,
    plan: PlanTasksOutput,
    *,
    tool_specs: Iterable[ToolSpec],
    expected_seed: int | None,
    expected_plan_version: int,
    completed_task_ids: Iterable[str],
) -> PlanValidationResult:
    """首轮与重规划共用的确定性四工具验证实现。"""

    errors: list[PlanValidationIssue] = []
    task_by_id = _task_map(plan, errors)
    completed = set(completed_task_ids)
    unknown_completed = completed - set(task_by_id)
    if unknown_completed:
        _issue(
            errors,
            "completed_task_unknown",
            f"已完成集合包含未知任务: {', '.join(sorted(unknown_completed))}",
        )
    dependencies = {task.task_id: task.dependencies for task in plan.tasks}
    try:
        ordered_ids = topological_sort(dependencies)
    except DAGValidationError as exc:
        ordered_ids = sorted(task_by_id)
        _issue(errors, "dag_invalid", str(exc))

    if plan.plan_version != expected_plan_version:
        _issue(
            errors,
            "plan_version_invalid",
            f"计划版本必须为 {expected_plan_version}",
        )
    if len(plan.tasks) > contract.budgets.max_tool_steps:
        _issue(
            errors,
            "tool_budget_exceeded",
            f"计划任务数 {len(plan.tasks)} 超过工具步数预算 {contract.budgets.max_tool_steps}",
        )
    if contract.risk_level is not RiskLevel.LOW or contract.approval.required:
        _issue(errors, "normal_risk_not_supported", "P0-13 正常闭环只接受低风险且无需合同审批的订单")

    spec_by_name = {spec.tool_name: spec for spec in tool_specs}
    for task in plan.tasks:
        try:
            validate_tool_arguments(task.tool_name, task.tool_arguments)
        except (KeyError, TypeError, ValueError) as exc:
            _issue(errors, "tool_arguments_invalid", str(exc), task.task_id)
        spec = spec_by_name.get(task.tool_name)
        if spec is not None and UserRole.OPERATOR not in spec.allowed_roles:
            _issue(errors, "executor_role_not_allowed", f"operator 不能调用 {task.tool_name.value}", task.task_id)
        if task.task_id in completed:
            if task.status is not PlanTaskStatus.COMPLETED:
                _issue(errors, "completed_task_status_invalid", "保留完成任务必须是 completed", task.task_id)
        else:
            if task.status is not PlanTaskStatus.PENDING:
                _issue(errors, "task_status_invalid", "未完成任务必须是 pending", task.task_id)
            if task.evidence_refs or task.effect_id is not None:
                _issue(errors, "task_has_runtime_evidence", "新任务不能预填运行期证据或 effect_id", task.task_id)
        if spec is not None and task.approval_required != spec.requires_approval:
            _issue(
                errors,
                "approval_flag_mismatch",
                f"任务 approval_required 必须与 ToolSpec.requires_approval={spec.requires_approval} 一致",
                task.task_id,
            )

    selected: dict[ToolName, PlanTask | None] = {
        tool_name: _find_single_task(task_by_id, tool_name, errors)
        for tool_name in NORMAL_PEVR_TOOL_CHAIN
    }
    allocate = selected[ToolName.ALLOCATE_TASKS]
    route = selected[ToolName.PLAN_MULTI_AMR_ROUTES]
    validate = selected[ToolName.VALIDATE_FLEET_PLAN]
    dispatch = selected[ToolName.DISPATCH_SIMULATION]

    if all(task is not None for task in (allocate, route, validate, dispatch)):
        assert allocate is not None and route is not None and validate is not None and dispatch is not None
        expected_ids = [task.task_id for task in (allocate, route, validate, dispatch)]
        positions = {task_id: index for index, task_id in enumerate(ordered_ids)}
        if any(task_id not in positions for task_id in expected_ids) or any(
            positions[left_id] >= positions[right_id]
            for left_id, right_id in zip(expected_ids, expected_ids[1:])
        ):
            _issue(errors, "dag_order_invalid", "正常闭环工具必须按 allocate→route→validate→dispatch 排序")
        if allocate.dependencies:
            _issue(errors, "allocate_dependencies_invalid", "allocate_tasks 不能依赖后续任务", allocate.task_id)
        for predecessor, successor in zip((allocate, route, validate), (route, validate, dispatch)):
            if successor.dependencies != [predecessor.task_id]:
                _issue(
                    errors,
                    "chain_dependency_invalid",
                    f"{successor.tool_name.value} 必须只依赖 {predecessor.task_id}",
                    successor.task_id,
                )

        environment_ref = contract.environment_ref
        allocate_args = allocate.tool_arguments
        if allocate_args.get("environment_ref") != environment_ref:
            _issue(errors, "environment_ref_mismatch", "allocate_tasks 的环境引用与合同不一致", allocate.task_id)
        if not _same_string_list(allocate_args.get("order_ids"), (order.order_id for order in contract.orders)):
            _issue(errors, "order_scope_mismatch", "allocate_tasks 必须覆盖合同中的全部订单", allocate.task_id)

        route_args = route.tool_arguments
        if route_args.get("environment_ref") != environment_ref:
            _issue(errors, "environment_ref_mismatch", "plan_multi_amr_routes 的环境引用与合同不一致", route.task_id)
        if not is_data_ref(route_args.get("assignments"), f"task:{allocate.task_id}/output/assignments"):
            _issue(
                errors,
                "assignment_ref_invalid",
                "路线任务只能引用 allocate_tasks 的 assignments 输出",
                route.task_id,
            )
        blocked_cells = route_args.get("blocked_cells")
        expected_cells = [cell.model_dump(mode="json") for cell in contract.constraints.blocked_cells]
        if blocked_cells is not None and blocked_cells != expected_cells:
            _issue(errors, "blocked_cells_mismatch", "路线临时封路必须与合同约束一致", route.task_id)
        max_time = route_args.get("max_time")
        latest_deadline = max(order.deadline for order in contract.orders)
        if not isinstance(max_time, int) or isinstance(max_time, bool) or max_time < latest_deadline:
            _issue(errors, "route_time_horizon_invalid", "路线 max_time 必须覆盖合同订单的最晚 deadline", route.task_id)

        for task, label in ((validate, "validate_fleet_plan"), (dispatch, "dispatch_simulation")):
            if task.tool_arguments.get("environment_ref") is not None:
                if label == "validate_fleet_plan" and task.tool_arguments.get("environment_ref") != environment_ref:
                    _issue(errors, "environment_ref_mismatch", f"{label} 的环境引用与合同不一致", task.task_id)
            if not is_data_ref(task.tool_arguments.get("plan"), "derived:simulation_plan"):
                _issue(errors, "simulation_plan_ref_invalid", f"{label} 只能引用受控派生 SimulationPlan", task.task_id)
        if validate.tool_arguments.get("ruleset_version") not in (None, "p0-10.v1"):
            _issue(errors, "ruleset_version_invalid", "Validator 规则版本必须为 p0-10.v1", validate.task_id)
        seed = dispatch.tool_arguments.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            _issue(errors, "simulation_seed_invalid", "dispatch_simulation 必须使用确定性的整数 seed", dispatch.task_id)
        elif expected_seed is not None and seed != expected_seed:
            _issue(
                errors,
                "simulation_seed_mismatch",
                f"dispatch_simulation 的 seed 必须与本次请求一致: {expected_seed}",
                dispatch.task_id,
            )

    # 错误排序固定，便于报告、测试和跨进程重放比较；不把 LLM 返回顺序当作证据。
    errors.sort(key=lambda item: (item.task_id or "", item.code, item.message))
    return PlanValidationResult(
        valid=not errors,
        plan_version=plan.plan_version,
        topological_order=ordered_ids,
        required_tool_names=list(NORMAL_PEVR_TOOL_CHAIN),
        errors=errors,
    )


__all__ = [
    "canonicalize_normal_pevr_plan",
    "canonicalize_replanned_pevr_plan",
    "NORMAL_PEVR_TOOL_CHAIN",
    "PlanValidationIssue",
    "PlanValidationResult",
    "is_data_ref",
    "make_data_ref",
    "validate_normal_pevr_plan",
    "validate_replanned_pevr_plan",
    "validate_charging_pevr_plan",
]
