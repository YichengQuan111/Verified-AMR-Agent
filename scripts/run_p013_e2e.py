"""运行 P0-13 真实本地模型端到端验收。

该入口不启动模型进程，也不自动批准副作用；模型必须由固定 Fast 脚本先启动，
调用者必须显式传入 ``--approve-dispatch`` 才能通过主图的可信审批 guard。真实运行
同时把 Checkpoint、Effect Ledger 和仿真外部状态写入 PostgreSQL，使自然语言、RAG、
LLM DAG、C++、仿真、跨进程恢复和报告都走生产适配器，测试替身只留在单测。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime.graph import PEVRGraphRunner  # noqa: E402
from agent.runtime.pevr import PEVRRequest  # noqa: E402
from agent.tools import build_tool_registry  # noqa: E402
from agent.tools.snapshots import DefaultWarehouseSnapshotProvider  # noqa: E402
from services.config import load_settings  # noqa: E402
from services.model_gateway.provider import ModelProvider  # noqa: E402
from services.application import PostgresRuntimeStore  # noqa: E402
from services.persistence import create_database_runtime  # noqa: E402


def _default_run_id(raw_request: str, environment_ref: str, seed: int) -> str:
    """由输入摘要生成稳定运行 ID，便于重复验收时比较证据而不是比较随机 UUID。"""

    digest = hashlib.sha256(f"{environment_ref}\n{seed}\n{raw_request}".encode("utf-8")).hexdigest()
    return f"p013-e2e-{digest[:20]}"


def parse_args() -> argparse.Namespace:
    """解析有限 CLI 参数；不提供 command/path/faults 等旁路字段。"""

    parser = argparse.ArgumentParser(description="P0-13 PEVR local Fast LLM end-to-end run")
    parser.add_argument(
        "--request",
        default="请把 MAT-001 从 P1 运到 S3，并在截止时间前完成。",
        help="自然语言运输订单；应指向固定 orders_seed_v1.json 中的订单",
    )
    parser.add_argument("--environment-ref", default="warehouse_v1@seed-v1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--approve-dispatch",
        action="store_true",
        help="显式提供可信 operator 审批上下文；不传时主图会在 dispatch 前阻断",
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "p013_e2e_result.json")
    return parser.parse_args()


def main() -> int:
    """加载 Fast Profile，并用 PostgreSQL 组装可跨进程恢复的真实九工具主图。"""

    args = parse_args()
    run_id = args.run_id or _default_run_id(args.request, args.environment_ref, args.seed)
    settings = load_settings()
    provider = ModelProvider(settings.model_gateway)
    snapshot_provider = DefaultWarehouseSnapshotProvider()
    database_runtime = create_database_runtime(settings.database)
    checkpoint_store = PostgresRuntimeStore(database_runtime.session_factory)
    try:
        # Registry 的 dispatch/query 与主图 Checkpoint 共用同一 PostgreSQL
        # 适配器；进程退出后外部仿真事实和 Effect Ledger 因而不会各自丢失。
        registry = build_tool_registry(
            settings=settings,
            snapshot_provider=snapshot_provider,
            execution_store=checkpoint_store,
        )
        runner = PEVRGraphRunner(
            provider,
            registry=registry,
            snapshot_provider=snapshot_provider,
            checkpoint_store=checkpoint_store,
        )
        result = runner.run(
            PEVRRequest(
                run_id=run_id,
                raw_request=args.request,
                environment_ref=args.environment_ref,
                seed=args.seed,
                approval_granted=args.approve_dispatch,
            )
        )
    finally:
        database_runtime.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = result.report
    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "status": report.final_status.value,
                "plan_version": report.plan_version,
                "completed_order_ids": report.completed_order_ids,
                "stage_order": [event.stage.value for event in result.stage_trace],
                "tool_names": [item.tool_name.value for item in report.tool_evidence],
                "metrics": report.metrics.model_dump(mode="json"),
                "model_alias": report.model.served_alias if report.model is not None else None,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
