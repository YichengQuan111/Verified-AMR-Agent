"""Agent 运行期间的观测与聚合状态契约。

本模块只定义可持久化状态及其一致性规则，不包含 LangGraph 节点、Prompt、数据库仓储
或执行循环；这些能力分别属于 P0-05、P0-06 和 P0-13 之后的工作包。
"""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.planning.contracts import PlanTask, PlanTaskStatus, TaskContract
from agent.planning.dag import validate_dag
from agent.tools.contracts import ToolResult
from domains.amr_warehouse.contracts import AMRState, TransportOrder


class RuntimeContract(BaseModel):
    """运行态契约基类：拒绝未知字段并在赋值时重新校验。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class ObservationSource(str, Enum):
    """状态证据的来源。"""

    TOOL = "tool"
    SIMULATOR = "simulator"
    VALIDATOR = "validator"
    HUMAN = "human"
    SYSTEM = "system"


class ObservationStatus(str, Enum):
    """一次观测对当前执行的判断。"""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    BLOCKED = "blocked"


class FaultRecord(RuntimeContract):
    """已经处理过的故障审计记录，用于 Checkpoint 恢复时防止状态循环。"""

    fault_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=32)
    task_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=64)
    observed_at: AwareDatetime
    retry_count: int = Field(ge=0)
    replan_count: int = Field(ge=0)
    terminal: bool


class RunStatus(str, Enum):
    """一次 Agent 运行的生命周期状态。"""

    CREATED = "created"
    PLANNING = "planning"
    VALIDATING = "validating"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConstraintViolation(RuntimeContract):
    """验证器或仿真器发现的一条可定位约束违规。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    task_id: str | None
    amr_id: str | None
    evidence: dict[str, JsonValue]


class Observation(RuntimeContract):
    """一次工具、仿真、验证或人工操作产生的结构化状态证据。"""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(max_length=128)
    source: ObservationSource
    observed_at: AwareDatetime
    status: ObservationStatus
    summary: str = Field(min_length=1)
    state_delta: dict[str, JsonValue]
    evidence_refs: list[str]
    tool_result: ToolResult | None
    violations: list[ConstraintViolation]
    requires_replan: bool
    requires_human: bool

    @model_validator(mode="after")
    def validate_observation(self) -> "Observation":
        """让观测来源、错误状态、证据和处置标记保持一致。"""

        if self.source is ObservationSource.TOOL and self.tool_result is None:
            raise ValueError("tool 来源的 Observation 必须携带 tool_result")
        if self.source is not ObservationSource.TOOL and self.tool_result is not None:
            raise ValueError("非 tool 来源的 Observation 不能携带 tool_result")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs 不能重复")
        if self.violations and self.status is ObservationStatus.OK:
            raise ValueError("包含 violations 的 Observation 状态不能为 ok")
        if self.status is ObservationStatus.BLOCKED and not (
            self.requires_replan or self.requires_human
        ):
            raise ValueError("blocked Observation 必须触发重规划或人工处理")
        return self


class RunState(RuntimeContract):
    """一次 Agent 运行可检查点化的完整聚合状态。"""

    run_id: str = Field(min_length=1, max_length=64)
    status: RunStatus
    plan_version: int = Field(ge=1)
    task_contract: TaskContract
    plan_tasks: list[PlanTask]
    amr_states: list[AMRState] = Field(min_length=1)
    # 充电合同允许空订单；运输合同仍由 TaskContract 保证至少 1 条。
    orders: list[TransportOrder] = Field(default_factory=list)
    observations: list[Observation]
    current_task_id: str | None = Field(max_length=128)
    completed_task_ids: list[str]
    failed_task_ids: list[str]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    replan_count: int = Field(ge=0)
    retry_count: int = Field(default=0, ge=0)
    fault_history: list[FaultRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_run_state(self) -> "RunState":
        """跨对象校验 ID、任务 DAG、状态集合、观测归属和运行预算。"""

        task_by_id = self._unique_by_id(
            self.plan_tasks,
            id_attribute="task_id",
            collection_name="plan_tasks",
        )
        amr_by_id = self._unique_by_id(
            self.amr_states,
            id_attribute="amr_id",
            collection_name="amr_states",
        )
        order_by_id = self._unique_by_id(
            self.orders,
            id_attribute="order_id",
            collection_name="orders",
        )
        observation_by_id = self._unique_by_id(
            self.observations,
            id_attribute="observation_id",
            collection_name="observations",
        )
        del observation_by_id  # 只需要触发重复 ID 校验，不需要再次读取字典。

        validate_dag(
            {task.task_id: task.dependencies for task in self.plan_tasks}
        )

        contract_orders = {
            order.order_id: order for order in self.task_contract.orders
        }
        if set(order_by_id) != set(contract_orders):
            raise ValueError("RunState.orders 必须与 task_contract.orders 的 ID 集合一致")
        for order_id, order in order_by_id.items():
            if order != contract_orders[order_id]:
                raise ValueError(f"订单 {order_id} 与 task_contract 中的定义不一致")

        completed = self._validate_task_id_list(
            self.completed_task_ids,
            field_name="completed_task_ids",
            known_task_ids=set(task_by_id),
        )
        failed = self._validate_task_id_list(
            self.failed_task_ids,
            field_name="failed_task_ids",
            known_task_ids=set(task_by_id),
        )
        if completed & failed:
            raise ValueError("同一任务不能同时出现在 completed_task_ids 和 failed_task_ids")
        if self.current_task_id is not None:
            if self.current_task_id not in task_by_id:
                raise ValueError("current_task_id 引用了未知任务")
            if self.current_task_id in completed | failed:
                raise ValueError("current_task_id 不能指向已完成或失败的任务")

        # 冗余状态列表必须与每个 PlanTask 自身状态相符，避免持久化后出现两套真相。
        task_completed = {
            task.task_id
            for task in self.plan_tasks
            if task.status is PlanTaskStatus.COMPLETED
        }
        task_failed = {
            task.task_id
            for task in self.plan_tasks
            if task.status is PlanTaskStatus.FAILED
        }
        if completed != task_completed:
            raise ValueError("completed_task_ids 与 PlanTask.status 不一致")
        if failed != task_failed:
            raise ValueError("failed_task_ids 与 PlanTask.status 不一致")

        # Checkpoint 会把 completed 节点当作不可重做的恢复锚点，因此必须先证明
        # 其依赖闭包真实完成。逐任务检查直接依赖即可递归覆盖传递依赖；若 A 未
        # 完成，任何依赖链上的首个 completed 后继都会在这里被拒绝。
        for task_id in completed:
            missing = sorted(set(task_by_id[task_id].dependencies) - completed)
            if missing:
                raise ValueError(
                    f"已完成任务 {task_id} 的依赖尚未完成: {', '.join(missing)}"
                )
        active_task_ids = {
            task.task_id
            for task in self.plan_tasks
            if task.status is PlanTaskStatus.RUNNING
        }
        if self.current_task_id is not None:
            active_task_ids.add(self.current_task_id)
        for task_id in active_task_ids:
            missing = sorted(set(task_by_id[task_id].dependencies) - completed)
            if missing:
                raise ValueError(
                    f"运行中任务 {task_id} 的依赖尚未完成: {', '.join(missing)}"
                )

        for task in self.plan_tasks:
            if task.target_amr is not None and task.target_amr not in amr_by_id:
                raise ValueError(f"任务 {task.task_id} 引用了未知 AMR: {task.target_amr}")
        for observation in self.observations:
            if observation.run_id != self.run_id:
                raise ValueError(
                    f"Observation {observation.observation_id} 的 run_id 不匹配"
                )
            if observation.task_id is not None and observation.task_id not in task_by_id:
                raise ValueError(
                    f"Observation {observation.observation_id} 引用了未知任务"
                )

        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if self.replan_count > self.task_contract.budgets.max_replans:
            raise ValueError("replan_count 超过 TaskContract 允许的重规划次数")
        if self.retry_count > self.task_contract.budgets.max_retries:
            raise ValueError("retry_count 超过 TaskContract 允许的重试次数")
        fault_ids = [item.fault_id for item in self.fault_history]
        if len(fault_ids) != len(set(fault_ids)):
            raise ValueError("fault_history 不能包含重复 fault_id")
        for record in self.fault_history:
            if record.retry_count > self.retry_count:
                raise ValueError("FaultRecord.retry_count 不能超过 RunState.retry_count")
            if record.replan_count > self.replan_count:
                raise ValueError("FaultRecord.replan_count 不能超过 RunState.replan_count")

        terminal_statuses = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
        if self.status in terminal_statuses and self.current_task_id is not None:
            raise ValueError("终态 RunState 的 current_task_id 必须为 null")
        if self.status is RunStatus.COMPLETED:
            if failed:
                raise ValueError("completed RunState 不能包含失败任务")
            if completed != set(task_by_id):
                raise ValueError("completed RunState 必须完成全部计划任务")
        return self

    @staticmethod
    def _unique_by_id(items: list[object], *, id_attribute: str, collection_name: str) -> dict[str, object]:
        """把对象列表按 ID 建索引，同时拒绝重复 ID。"""

        indexed: dict[str, object] = {}
        for item in items:
            item_id = getattr(item, id_attribute)
            if item_id in indexed:
                raise ValueError(f"{collection_name} 包含重复 ID: {item_id}")
            indexed[item_id] = item
        return indexed

    @staticmethod
    def _validate_task_id_list(
        task_ids: list[str], *, field_name: str, known_task_ids: set[str]
    ) -> set[str]:
        """拒绝状态列表中的重复任务和未知任务。"""

        values = set(task_ids)
        if len(values) != len(task_ids):
            raise ValueError(f"{field_name} 不能包含重复任务 ID")
        unknown = sorted(values - known_task_ids)
        if unknown:
            raise ValueError(f"{field_name} 引用了未知任务: {', '.join(unknown)}")
        return values


__all__ = [
    "ConstraintViolation",
    "FaultRecord",
    "Observation",
    "ObservationSource",
    "ObservationStatus",
    "RunState",
    "RunStatus",
]
