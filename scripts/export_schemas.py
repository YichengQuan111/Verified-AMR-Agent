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
from agent.tools.contracts import ToolResult, ToolSpec  # noqa: E402
from domains.amr_warehouse.contracts import AMRState, TransportOrder  # noqa: E402
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


SCHEMA_DIRECTORY = PROJECT_ROOT / "docs" / "schemas"
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "TaskContract.schema.json": TaskContract,
    "AMRState.schema.json": AMRState,
    "TransportOrder.schema.json": TransportOrder,
    "PlanTask.schema.json": PlanTask,
    "ToolSpec.schema.json": ToolSpec,
    "ToolResult.schema.json": ToolResult,
    "Observation.schema.json": Observation,
    "RunState.schema.json": RunState,
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
