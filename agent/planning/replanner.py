"""P0-14 局部重规划：影响集合、未完成子图失效与版本化替换。

Replanner 不重新规划整张业务图，也不删除历史计划或 Effect Ledger。它先把
Verifier 给出的实体标识规范化，再沿原 DAG 的反向邻接关系传播到受影响后继；
已完成节点始终作为只读锚点保留，只有未完成节点被移出当前版本。替换任务必须
使用新 ID、只能依赖保留节点或替换子图，并在返回前再次通过 Pydantic/DAG 校验。
这样 LLM 只能提出局部候选，不能借重规划绕过既有完成事实或 Validator。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.context.contracts import PlanTasksOutput, ReplanOutput
from agent.planning.contracts import PlanTask, PlanTaskStatus, TaskContract
from agent.planning.dag import validate_dag
from agent.planning.validator import PlanValidationResult, validate_replanned_pevr_plan
from agent.tools.contracts import ToolName, ToolResult, ToolResultStatus, ToolSpec
from agent.tools.schemas import AllocationResponse, RoutePlanResponse
from agent.tools.snapshots import EnvironmentSnapshot
from domains.amr_warehouse.contracts import GridPosition


class ReplannerContract(BaseModel):
    """局部重规划对象拒绝未知字段，避免影响集合被静默扩展。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class AffectedEntitySet(ReplannerContract):
    """Verifier 观测转成的结构化影响实体集合。"""

    amr_ids: list[str] = Field(default_factory=list)
    blocked_cells: list[GridPosition] = Field(default_factory=list)
    blocked_edges: list[dict[str, GridPosition]] = Field(default_factory=list)
    workstation_ids: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    tool_names: list[ToolName] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_entities(self) -> "AffectedEntitySet":
        """拒绝重复实体，避免同一故障被重复传播或重复计数。"""

        for name in (
            "amr_ids",
            "workstation_ids",
            "channel_ids",
            "task_ids",
        ):
            values = getattr(self, name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{name} 不能包含空字符串")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} 不能包含重复值")
        coordinates = [(item.x, item.y) for item in self.blocked_cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("blocked_cells 不能包含重复坐标")
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("tool_names 不能包含重复工具")
        edge_values = [
            ((item["from"].x, item["from"].y), (item["to"].x, item["to"].y))
            for item in self.blocked_edges
        ]
        if len(edge_values) != len(set(edge_values)):
            raise ValueError("blocked_edges 不能包含重复边")
        return self

    @classmethod
    def from_strings(cls, values: Iterable[str]) -> "AffectedEntitySet":
        """解析受控的 ``kind:value`` 故障标签，不执行任意表达式。"""

        amrs: list[str] = []
        cells: list[GridPosition] = []
        edges: list[dict[str, GridPosition]] = []
        workstations: list[str] = []
        channels: list[str] = []
        tools: list[ToolName] = []
        tasks: list[str] = []
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("affected_entities 只能包含非空字符串")
            value = raw.strip()
            prefix, separator, body = value.partition(":")
            if not separator:
                # 没有类型前缀时只按任务 ID 处理；不会把自然语言猜测成 AMR。
                tasks.append(value)
                continue
            if prefix in {"amr", "amr_id"}:
                amrs.append(body)
            elif prefix in {"workstation", "station"}:
                workstations.append(body)
            elif prefix in {"channel", "channel_id"}:
                # 物理通道既可能由稳定名称标识，也可能直接以地图 cell/edge
                # 表示；坐标形式转入同一几何匹配集合，避免维护两套路径语义。
                if "->" in body:
                    edges.append(cls._parse_edge(body))
                elif re.fullmatch(r"\(?\s*-?\d+\s*,\s*-?\d+\s*\)?", body):
                    cells.append(cls._parse_position(body, label="channel"))
                else:
                    channels.append(body)
            elif prefix in {"task", "task_id"}:
                tasks.append(body)
            elif prefix in {"tool", "tool_name"}:
                try:
                    tools.append(ToolName(body))
                except ValueError as exc:
                    raise ValueError(f"未知受影响工具: {body}") from exc
            elif prefix in {"cell", "blocked_cell"}:
                cells.append(cls._parse_position(body, label="cell"))
            elif prefix in {"edge", "blocked_edge"}:
                edges.append(cls._parse_edge(body))
            else:
                raise ValueError(f"不支持的 affected_entities 类型: {prefix}")
        return cls(
            amr_ids=amrs,
            blocked_cells=cells,
            blocked_edges=edges,
            workstation_ids=workstations,
            channel_ids=channels,
            tool_names=tools,
            task_ids=tasks,
        )

    @staticmethod
    def _parse_position(value: str, *, label: str) -> GridPosition:
        """只接受 ``x,y`` 或 ``(x,y)``，不解析代码或路径。"""

        match = re.fullmatch(r"\(?\s*(-?\d+)\s*,\s*(-?\d+)\s*\)?", value)
        if match is None:
            raise ValueError(f"{label} 必须使用 x,y 坐标")
        return GridPosition(x=int(match.group(1)), y=int(match.group(2)))

    @classmethod
    def _parse_edge(cls, value: str) -> dict[str, GridPosition]:
        """解析 ``x1,y1->x2,y2`` 边并保持与地图契约相同的 from/to 字段。"""

        left, separator, right = value.partition("->")
        if not separator:
            raise ValueError("blocked edge 必须使用 x1,y1->x2,y2")
        return {
            "from": cls._parse_position(left.strip(), label="edge.from"),
            "to": cls._parse_position(right.strip(), label="edge.to"),
        }


class LocalReplanAnalysis(ReplannerContract):
    """影响传播后的保留/失效集合。"""

    direct_task_ids: list[str]
    invalidated_task_ids: list[str]
    retained_task_ids: list[str]
    completed_task_ids: list[str]


class TaskResourceProvenance(ReplannerContract):
    """某个已执行任务实际使用的 AMR、路径和工位证据。"""

    task_id: str = Field(min_length=1, max_length=128)
    amr_ids: list[str] = Field(default_factory=list)
    positions: list[GridPosition] = Field(default_factory=list)
    edges: list[dict[str, GridPosition]] = Field(default_factory=list)
    workstation_ids: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_resources(self) -> "TaskResourceProvenance":
        """资源索引必须稳定去重，避免同一路径反复放大故障集合。"""

        for name in ("amr_ids", "workstation_ids", "channel_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)) or any(not item.strip() for item in values):
                raise ValueError(f"{name} 必须是非空且不重复的 ID")
        coordinates = [(item.x, item.y) for item in self.positions]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("positions 不能重复")
        edge_values = [
            ((item["from"].x, item["from"].y), (item["to"].x, item["to"].y))
            for item in self.edges
        ]
        if len(edge_values) != len(set(edge_values)):
            raise ValueError("edges 不能重复")
        return self


class LocalReplanResult(ReplannerContract):
    """一个通过 DAG 校验的新计划版本及其审计集合。"""

    previous_plan_version: int = Field(ge=1)
    new_plan_version: int = Field(ge=2)
    plan: PlanTasksOutput
    invalidated_task_ids: list[str]
    retained_task_ids: list[str]
    completed_task_ids: list[str]
    reason: str = Field(min_length=1)
    plan_validation: PlanValidationResult

    @model_validator(mode="after")
    def validate_replanned_gate(self) -> "LocalReplanResult":
        """结果对象本身不能表示一个未通过确定性门禁的新版本。"""

        if not self.plan_validation.valid:
            raise ValueError("局部重规划结果必须通过 PEVR 计划验证")
        if self.plan_validation.plan_version != self.new_plan_version:
            raise ValueError("重规划验证结果与新计划版本不一致")
        return self


@dataclass(frozen=True)
class _TaskReference:
    """任务中被用于资源匹配的扁平引用集合。"""

    strings: frozenset[str]
    positions: frozenset[tuple[int, int]]
    edges: frozenset[tuple[tuple[int, int], tuple[int, int]]]


class LocalReplanner:
    """只替换故障影响的未完成 DAG 子图。"""

    def analyze(
        self,
        plan: PlanTasksOutput,
        *,
        completed_task_ids: Iterable[str],
        affected_entities: AffectedEntitySet | Iterable[str],
        failed_task_id: str | None = None,
        failed_tool_name: ToolName | str | None = None,
        runtime_resources: Iterable[TaskResourceProvenance] = (),
    ) -> LocalReplanAnalysis:
        """计算直接影响和其未完成下游，不修改传入计划。"""

        entity_set = (
            affected_entities
            if isinstance(affected_entities, AffectedEntitySet)
            else AffectedEntitySet.from_strings(affected_entities)
        )
        known_ids = {task.task_id for task in plan.tasks}
        completed = set(completed_task_ids)
        unknown_completed = completed - known_ids
        if unknown_completed:
            raise ValueError(f"completed_task_ids 包含未知任务: {', '.join(sorted(unknown_completed))}")
        direct: set[str] = set(entity_set.task_ids)
        if failed_task_id is not None:
            direct.add(failed_task_id)
        if direct - known_ids:
            raise ValueError(f"affected task 引用了未知任务: {', '.join(sorted(direct - known_ids))}")
        if failed_tool_name is not None:
            failed_tool = failed_tool_name if isinstance(failed_tool_name, ToolName) else ToolName(failed_tool_name)
            entity_set = entity_set.model_copy(
                update={"tool_names": list(dict.fromkeys([*entity_set.tool_names, failed_tool]))}
            )

        runtime_by_task: dict[str, TaskResourceProvenance] = {}
        for resource in runtime_resources:
            if resource.task_id not in known_ids:
                raise ValueError(f"runtime_resources 引用了未知任务: {resource.task_id}")
            if resource.task_id in runtime_by_task:
                raise ValueError(f"runtime_resources 包含重复任务: {resource.task_id}")
            runtime_by_task[resource.task_id] = resource

        for task in plan.tasks:
            refs = self._references(task)
            runtime = runtime_by_task.get(task.task_id)
            if task.target_amr in entity_set.amr_ids:
                direct.add(task.task_id)
            if task.workstation in entity_set.workstation_ids:
                direct.add(task.task_id)
            resource_ids = (
                set(entity_set.amr_ids)
                | set(entity_set.workstation_ids)
                | set(entity_set.channel_ids)
            )
            if refs.strings.intersection(resource_ids):
                direct.add(task.task_id)
            if refs.positions.intersection({(item.x, item.y) for item in entity_set.blocked_cells}):
                direct.add(task.task_id)
            if refs.edges.intersection(self._edge_tuples(entity_set.blocked_edges)):
                direct.add(task.task_id)
            if runtime is not None:
                runtime_strings = (
                    set(runtime.amr_ids)
                    | set(runtime.workstation_ids)
                    | set(runtime.channel_ids)
                )
                if runtime_strings.intersection(resource_ids):
                    direct.add(task.task_id)
                runtime_positions = {(item.x, item.y) for item in runtime.positions}
                if runtime_positions.intersection(
                    {(item.x, item.y) for item in entity_set.blocked_cells}
                ):
                    direct.add(task.task_id)
                if self._edge_tuples(runtime.edges).intersection(
                    self._edge_tuples(entity_set.blocked_edges)
                ):
                    direct.add(task.task_id)
            if task.tool_name in entity_set.tool_names:
                direct.add(task.task_id)

        # 影响向后传播：任务依赖任何已影响节点就必须重新验证/替换，不能只重跑
        # 当前节点而把旧的下游路线、Validator 或仿真计划继续使用。
        changed = True
        while changed:
            changed = False
            for task in plan.tasks:
                if task.task_id not in direct and set(task.dependencies).intersection(direct):
                    direct.add(task.task_id)
                    changed = True

        invalidated = direct - completed
        retained = known_ids - invalidated
        return LocalReplanAnalysis(
            direct_task_ids=sorted(direct),
            invalidated_task_ids=sorted(invalidated),
            retained_task_ids=sorted(retained),
            completed_task_ids=sorted(completed),
        )

    def apply(
        self,
        plan: PlanTasksOutput,
        analysis: LocalReplanAnalysis,
        replacement_tasks: Iterable[PlanTask],
        *,
        reason: str,
        contract: TaskContract,
        tool_specs: Iterable[ToolSpec],
        expected_seed: int,
        new_plan_version: int | None = None,
    ) -> LocalReplanResult:
        """组合保留节点和新子图，并在版本加一后重新验证 DAG。"""

        target_version = (
            plan.plan_version + 1
            if new_plan_version is None
            else new_plan_version
        )
        if target_version != plan.plan_version + 1:
            raise ValueError("局部重规划版本必须恰好递增 1")
        invalidated = set(analysis.invalidated_task_ids)
        known_ids = {task.task_id for task in plan.tasks}
        if invalidated - known_ids:
            raise ValueError("analysis 包含不在当前计划中的失效任务")
        completed = set(analysis.completed_task_ids)
        if completed - known_ids:
            raise ValueError("analysis 包含不在当前计划中的已完成任务")
        if completed & invalidated:
            raise ValueError("已完成任务不能同时出现在失效集合")
        expected_retained = known_ids - invalidated
        if set(analysis.retained_task_ids) != expected_retained:
            raise ValueError("analysis 的 retained_task_ids 与失效集合不一致")
        replacements = list(replacement_tasks)
        replacement_ids = [task.task_id for task in replacements]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise ValueError("replacement_tasks 不能包含重复 task_id")
        if set(replacement_ids) & known_ids:
            raise ValueError("局部替换任务必须使用新 task_id")
        allowed_dependencies = (known_ids - invalidated) | set(replacement_ids)
        for task in replacements:
            unknown = set(task.dependencies) - allowed_dependencies
            if unknown:
                raise ValueError(
                    f"替换任务 {task.task_id} 依赖已失效或未知任务: {', '.join(sorted(unknown))}"
                )
            if task.status in {PlanTaskStatus.COMPLETED, PlanTaskStatus.RUNNING}:
                raise ValueError("替换任务不能预先标记为 completed 或 running")
            if task.effect_id is not None:
                raise ValueError("新替换任务不能携带旧 effect_id")

        retained_tasks: list[PlanTask] = []
        for task in plan.tasks:
            if task.task_id in invalidated:
                continue
            if task.task_id in completed:
                # 完成节点和 effect_id 原样保留；新版本执行器会把它当作只读事实，
                # 不为它生成新的副作用键，也不会再次调用外部工具。
                retained_tasks.append(task)
            else:
                retained_tasks.append(
                    task.model_copy(
                        update={
                            "status": PlanTaskStatus.PENDING,
                            "evidence_refs": list(task.evidence_refs),
                        }
                    )
                )
        all_tasks = [*retained_tasks, *replacements]
        rebuilt = PlanTasksOutput(
            plan_version=target_version,
            tasks=all_tasks,
            planning_assumptions=[
                *plan.planning_assumptions,
                "局部重规划仅替换受影响未完成子图",
            ],
            unresolved_risks=list(dict.fromkeys([*plan.unresolved_risks, reason])),
        )
        validate_dag({task.task_id: task.dependencies for task in rebuilt.tasks})
        plan_validation = validate_replanned_pevr_plan(
            contract,
            rebuilt,
            completed_task_ids=completed,
            tool_specs=tool_specs,
            expected_seed=expected_seed,
            expected_plan_version=target_version,
        )
        if not plan_validation.valid:
            detail = "; ".join(
                f"{item.code}: {item.message}" for item in plan_validation.errors
            )
            raise ValueError(f"局部重规划未通过确定性 PEVR 门禁: {detail}")
        return LocalReplanResult(
            previous_plan_version=plan.plan_version,
            new_plan_version=target_version,
            plan=rebuilt,
            invalidated_task_ids=sorted(invalidated),
            retained_task_ids=sorted(task.task_id for task in retained_tasks),
            completed_task_ids=sorted(completed),
            reason=reason,
            plan_validation=plan_validation,
        )

    def apply_to_run_state(
        self,
        state: "RunState",
        result: LocalReplanResult,
        *,
        updated_at: datetime | None = None,
    ) -> "RunState":
        """把新计划写回 RunState，同时保留已完成任务的副作用事实。

        该方法只重建业务状态，不写数据库；调用方应随后保存新的 Checkpoint，并
        让 PostgreSQL Effect Ledger 继续以由 ``run_id + plan_version + task_id``
        规范三元组摘要得到的业务键区分新旧版本。失败任务若已被替换，不会把旧失败
        ID 带入新计划；真实补偿证据仍
        由旧版本的 Effect Ledger/Observation 保留，不能通过清空 RunState 伪造成功。
        """

        # 延迟导入避免 planning.contracts → runtime.state → planning.contracts 的循环。
        from agent.runtime.state import RunState, RunStatus

        if not isinstance(state, RunState):
            raise TypeError("state 必须是 RunState")
        if state.plan_version != result.previous_plan_version:
            raise ValueError("RunState.plan_version 与重规划结果的旧版本不一致")
        completed_ids = sorted(
            task.task_id
            for task in result.plan.tasks
            if task.status is PlanTaskStatus.COMPLETED
        )
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "plan_version": result.new_plan_version,
                "plan_tasks": list(result.plan.tasks),
                "status": RunStatus.REPLANNING,
                "current_task_id": None,
                "completed_task_ids": completed_ids,
                "failed_task_ids": [],
                "replan_count": state.replan_count + 1,
                "updated_at": updated_at or datetime.now(timezone.utc),
            }
        )
        return RunState.model_validate(payload)

    def apply_model_output(
        self,
        plan: PlanTasksOutput,
        output: ReplanOutput,
        *,
        completed_task_ids: Iterable[str],
        affected_entities: AffectedEntitySet | Iterable[str],
        failed_task_id: str | None = None,
        failed_tool_name: ToolName | str | None = None,
        runtime_resources: Iterable[TaskResourceProvenance] = (),
        contract: TaskContract,
        tool_specs: Iterable[ToolSpec],
        expected_seed: int,
    ) -> LocalReplanResult:
        """验证 LLM ReplanOutput 与确定性影响集合一致后再应用。"""

        if output.previous_plan_version != plan.plan_version:
            raise ValueError("ReplanOutput.previous_plan_version 与当前计划不一致")
        analysis = self.analyze(
            plan,
            completed_task_ids=completed_task_ids,
            affected_entities=affected_entities,
            failed_task_id=failed_task_id,
            failed_tool_name=failed_tool_name,
            runtime_resources=runtime_resources,
        )
        if set(output.invalidated_task_ids) != set(analysis.invalidated_task_ids):
            raise ValueError("LLM invalidated_task_ids 与确定性影响集合不一致")
        if set(output.retained_task_ids) != set(analysis.retained_task_ids):
            raise ValueError("LLM retained_task_ids 与确定性保留集合不一致")
        if output.requires_human:
            if output.replacement_tasks:
                raise ValueError("requires_human=true 不能携带替换子图")
            raise RuntimeError(output.reason)
        return self.apply(
            plan,
            analysis,
            output.replacement_tasks,
            reason=output.reason,
            contract=contract,
            tool_specs=tool_specs,
            expected_seed=expected_seed,
            new_plan_version=output.new_plan_version,
        )

    @staticmethod
    def _references(task: PlanTask) -> _TaskReference:
        """递归收集任务参数中的资源 ID、坐标和边，绝不执行字符串。"""

        strings: set[str] = set()
        positions: set[tuple[int, int]] = set()
        edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

        def visit(value: object) -> None:
            if isinstance(value, str):
                strings.add(value)
                return
            if isinstance(value, Mapping):
                if set(value) >= {"x", "y"} and isinstance(value.get("x"), int) and isinstance(value.get("y"), int):
                    positions.add((int(value["x"]), int(value["y"])))
                if "from" in value and "to" in value:
                    left = value["from"]
                    right = value["to"]
                    if isinstance(left, Mapping) and isinstance(right, Mapping):
                        if {"x", "y"} <= set(left) and {"x", "y"} <= set(right):
                            edges.add(
                                (
                                    (int(left["x"]), int(left["y"])),
                                    (int(right["x"]), int(right["y"])),
                                )
                            )
                for nested in value.values():
                    visit(nested)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for nested in value:
                    visit(nested)

        visit(task.tool_arguments)
        for value in (task.target_amr, task.pickup, task.dropoff, task.workstation):
            if value is not None:
                strings.add(value)
        return _TaskReference(
            strings=frozenset(strings),
            positions=frozenset(positions),
            edges=frozenset(edges),
        )

    @staticmethod
    def _edge_tuples(edges: Iterable[Mapping[str, GridPosition]]) -> set[tuple[tuple[int, int], tuple[int, int]]]:
        """把 Pydantic 边对象转换为可比较的坐标二元组。"""

        result: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for edge in edges:
            left = edge["from"]
            right = edge["to"]
            result.add(((left.x, left.y), (right.x, right.y)))
        return result


def build_task_resource_provenance(
    plan: PlanTasksOutput,
    *,
    tool_results: Iterable[ToolResult],
    tool_task_ids: Iterable[str | None],
    contract: TaskContract,
    snapshot: EnvironmentSnapshot,
) -> list[TaskResourceProvenance]:
    """从真实分配/路线结果构建局部重规划资源索引。

    该索引只读取已经通过 ToolResult Schema 的输出，不从自然语言猜测 AMR 或通道。
    路径中的每个 cell/edge、订单工位和窄通道标签都会绑定回产生它的 route 任务，
    因而新封路或 AMR 故障即使未写在 Planner 静态参数中也能命中正确子图。
    """

    results = list(tool_results)
    task_ids = list(tool_task_ids)
    if len(results) != len(task_ids):
        raise ValueError("tool_results 与 tool_task_ids 数量必须一致")
    task_by_id = {task.task_id: task for task in plan.tasks}
    order_by_id = {order.order_id: order for order in contract.orders}
    aisle_cells = {
        aisle.aisle_id: {(cell.x, cell.y) for cell in aisle.cells}
        for aisle in snapshot.narrow_aisles
    }
    mutable: dict[str, dict[str, set[object]]] = {}

    def bucket(task_id: str) -> dict[str, set[object]]:
        return mutable.setdefault(
            task_id,
            {
                "amr_ids": set(),
                "positions": set(),
                "edges": set(),
                "workstation_ids": set(),
                "channel_ids": set(),
            },
        )

    for task_id, result in zip(task_ids, results):
        if task_id is None or result.status is not ToolResultStatus.SUCCESS:
            continue
        task = task_by_id.get(task_id)
        if task is None:
            raise ValueError(f"ToolResult 引用了当前计划外任务: {task_id}")
        resources = bucket(task_id)
        if task.tool_name is ToolName.ALLOCATE_TASKS:
            allocation = AllocationResponse.model_validate(result.output)
            resources["amr_ids"].update(item.amr_id for item in allocation.assignments)
        elif task.tool_name is ToolName.PLAN_MULTI_AMR_ROUTES:
            route_result = RoutePlanResponse.model_validate(result.output)
            for route in route_result.routes:
                resources["amr_ids"].add(route.amr_id)
                order = order_by_id.get(route.order_id)
                if order is not None:
                    resources["workstation_ids"].update((order.pickup, order.dropoff))
                coordinates = [(step.position.x, step.position.y) for step in route.path]
                resources["positions"].update(coordinates)
                for left, right in zip(coordinates, coordinates[1:]):
                    if left != right:
                        resources["edges"].add((left, right))
                used_cells = set(coordinates)
                resources["channel_ids"].update(
                    aisle_id
                    for aisle_id, cells in aisle_cells.items()
                    if used_cells.intersection(cells)
                )

    output: list[TaskResourceProvenance] = []
    for task_id in sorted(mutable):
        values = mutable[task_id]
        output.append(
            TaskResourceProvenance(
                task_id=task_id,
                amr_ids=sorted(str(item) for item in values["amr_ids"]),
                positions=[
                    GridPosition(x=x, y=y)
                    for x, y in sorted(values["positions"])
                ],
                edges=[
                    {
                        "from": GridPosition(x=left[0], y=left[1]),
                        "to": GridPosition(x=right[0], y=right[1]),
                    }
                    for left, right in sorted(values["edges"])
                ],
                workstation_ids=sorted(
                    str(item) for item in values["workstation_ids"]
                ),
                channel_ids=sorted(str(item) for item in values["channel_ids"]),
            )
        )
    return output


__all__ = [
    "AffectedEntitySet",
    "LocalReplanAnalysis",
    "LocalReplanResult",
    "LocalReplanner",
    "TaskResourceProvenance",
    "build_task_resource_provenance",
]
