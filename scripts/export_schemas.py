"""把 P0 已稳定的 Pydantic 公共契约导出为版本库内 JSON Schema。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel


# 直接执行 ``python scripts/export_schemas.py`` 时，Python 默认只把 scripts/ 放入
# 模块搜索路径。显式加入仓库根目录，确保不需要先安装项目包也能完成导出。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.planning.contracts import PlanTask, TaskContract  # noqa: E402
from agent.runtime.state import Observation, RunState  # noqa: E402
from agent.runtime.pevr import PEVRRunReport  # noqa: E402
from agent.tools.contracts import ToolResult, ToolSpec  # noqa: E402
from agent.tools.schemas import (  # noqa: E402
    AllocateTasksInput,
    AllocationResponse,
    ApprovalRequestOutput,
    DispatchSimulationInput,
    ExecutionStateOutput,
    FleetStateOutput,
    GetFleetStateInput,
    PlanMultiAMRRoutesInput,
    QueryExecutionStateInput,
    RequestApprovalInput,
    RetrieveKnowledgeInput,
    RoutePlanResponse,
    RunVerificationSuiteInput,
    ValidateFleetPlanInput,
    ValidationResponse,
    VerificationSuiteOutput,
)
from domains.amr_warehouse.contracts import AMRState, TransportOrder, WarehouseMap  # noqa: E402
from agent.context.contracts import (  # noqa: E402
    FinalReport,
    ObservationVerification,
    PlanTasksOutput,
    ReplanOutput,
)
from services.retrieval.contracts import (  # noqa: E402
    KnowledgeChunk,
    RetrievalResponse,
    RetrievalResult,
)
from services.amr_simulator.contracts import (  # noqa: E402
    SimulationEvent,
    SimulationPlan,
    SimulationResult,
)


SCHEMA_DIRECTORY = PROJECT_ROOT / "docs" / "schemas"
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "TaskContract.schema.json": TaskContract,
    "AMRState.schema.json": AMRState,
    "TransportOrder.schema.json": TransportOrder,
    "WarehouseMap.schema.json": WarehouseMap,
    "PlanTask.schema.json": PlanTask,
    "ToolSpec.schema.json": ToolSpec,
    "ToolResult.schema.json": ToolResult,
    "Observation.schema.json": Observation,
    "RunState.schema.json": RunState,
    # P0-13 最终报告把 P0-05 FinalReport 与工具审计/实际指标合并，作为主闭环
    # CLI 和后续 Checkpoint 的机器可读交付契约。
    "PEVRRunReport.schema.json": PEVRRunReport,
    # understand_goal 直接复用上面的 TaskContract；其余四个 P0-05 Prompt
    # 使用独立输出模型，因此各自提交一份可审查的 Schema。
    "PlanTasksOutput.schema.json": PlanTasksOutput,
    "ObservationVerification.schema.json": ObservationVerification,
    "ReplanOutput.schema.json": ReplanOutput,
    "FinalReport.schema.json": FinalReport,
    # P0-07 的 Qdrant payload、单条引用和拒答响应是后续 retrieve_knowledge
    # 工具的公共 JSON 边界，必须与运行时 Pydantic 契约同步导出。
    "KnowledgeChunk.schema.json": KnowledgeChunk,
    "RetrievalResult.schema.json": RetrievalResult,
    "RetrievalResponse.schema.json": RetrievalResponse,
    # P0-11 的计划、事件和最终快照是 P0-12/P0-13 的跨层 JSON 边界，继续由
    # model_json_schema() 生成，避免手写事件字段与运行态漂移。
    "SimulationPlan.schema.json": SimulationPlan,
    "SimulationEvent.schema.json": SimulationEvent,
    "SimulationResult.schema.json": SimulationResult,
    # P0-12 每个正常工具的输入/输出均从实时模型导出；ToolSpec 中仍嵌入同一
    # Schema，以下独立文件方便契约测试和跨语言消费者逐工具审查。
    "RetrieveKnowledgeInput.schema.json": RetrieveKnowledgeInput,
    "GetFleetStateInput.schema.json": GetFleetStateInput,
    "FleetStateOutput.schema.json": FleetStateOutput,
    "AllocateTasksInput.schema.json": AllocateTasksInput,
    "AllocationResponse.schema.json": AllocationResponse,
    "PlanMultiAMRRoutesInput.schema.json": PlanMultiAMRRoutesInput,
    "RoutePlanResponse.schema.json": RoutePlanResponse,
    "ValidateFleetPlanInput.schema.json": ValidateFleetPlanInput,
    "ValidationResponse.schema.json": ValidationResponse,
    "DispatchSimulationInput.schema.json": DispatchSimulationInput,
    "QueryExecutionStateInput.schema.json": QueryExecutionStateInput,
    "ExecutionStateOutput.schema.json": ExecutionStateOutput,
    "RunVerificationSuiteInput.schema.json": RunVerificationSuiteInput,
    "VerificationSuiteOutput.schema.json": VerificationSuiteOutput,
    "RequestApprovalInput.schema.json": RequestApprovalInput,
    "ApprovalRequestOutput.schema.json": ApprovalRequestOutput,
}


def export_schemas(output_directory: Path = SCHEMA_DIRECTORY) -> list[Path]:
    """生成全部 Schema，并返回按契约清单顺序排列的输出路径。"""

    output_directory.mkdir(parents=True, exist_ok=True)
    exported_paths: list[Path] = []

    for filename, model in SCHEMA_MODELS.items():
        # model_json_schema() 是唯一 Schema 来源，避免手写 JSON 与 Pydantic 校验漂移。
        schema = model.model_json_schema()
        output_path = output_directory / filename
        output_path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        exported_paths.append(output_path)

    return exported_paths


def main() -> int:
    """命令行入口：逐行打印相对路径，便于人工确认导出结果。"""

    for output_path in export_schemas():
        print(output_path.relative_to(PROJECT_ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
