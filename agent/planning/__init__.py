"""任务合同、确定性规划校验和局部重规划的稳定导出入口。"""

from agent.planning.contracts import (
    ApprovalRequirement,
    ChargingGoal,
    ExecutionBudgets,
    FallbackStrategy,
    PlanTask,
    PlanTaskStatus,
    RiskLevel,
    TaskConstraints,
    TaskContract,
)
from agent.planning.dag import DAGValidationError, topological_sort, validate_dag

__all__ = [
    "ApprovalRequirement",
    "ChargingGoal",
    "DAGValidationError",
    "ExecutionBudgets",
    "FallbackStrategy",
    "PlanTask",
    "PlanTaskStatus",
    "RiskLevel",
    "TaskConstraints",
    "TaskContract",
    "topological_sort",
    "validate_dag",
    "NORMAL_PEVR_TOOL_CHAIN",
    "canonicalize_normal_pevr_plan",
    "canonicalize_replanned_pevr_plan",
    "PlanValidationIssue",
    "PlanValidationResult",
    "is_data_ref",
    "make_data_ref",
    "validate_normal_pevr_plan",
    "validate_replanned_pevr_plan",
    "validate_charging_pevr_plan",
    "AffectedEntitySet",
    "LocalReplanAnalysis",
    "LocalReplanResult",
    "LocalReplanner",
    "TaskResourceProvenance",
    "build_task_resource_provenance",
]


def __getattr__(name: str):
    """延迟导出 P0-13 验证器，避免 contracts 包初始化时循环导入 Context。"""

    lazy_names = {
        "NORMAL_PEVR_TOOL_CHAIN",
        "canonicalize_normal_pevr_plan",
        "canonicalize_replanned_pevr_plan",
        "PlanValidationIssue",
        "PlanValidationResult",
        "is_data_ref",
        "make_data_ref",
        "validate_normal_pevr_plan",
        "validate_replanned_pevr_plan",
        "validate_charging_pevr_plan",
        "AffectedEntitySet",
        "LocalReplanAnalysis",
        "LocalReplanResult",
        "LocalReplanner",
        "TaskResourceProvenance",
        "build_task_resource_provenance",
    }
    if name in lazy_names:
        from agent.planning import replanner, validator

        source = validator if name in {
            "NORMAL_PEVR_TOOL_CHAIN",
            "canonicalize_normal_pevr_plan",
            "canonicalize_replanned_pevr_plan",
            "PlanValidationIssue",
            "PlanValidationResult",
            "is_data_ref",
            "make_data_ref",
            "validate_normal_pevr_plan",
            "validate_replanned_pevr_plan",
            "validate_charging_pevr_plan",
        } else replanner
        value = getattr(source, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
