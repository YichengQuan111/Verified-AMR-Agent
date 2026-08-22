"""运行带 JWT Principal、PostgreSQL HITL 与 Effect Ledger 的 P0-13 验收。

首次调用必定在 dispatch 前写入 ``waiting_approval`` Checkpoint 并返回退出码 3；
operator 可通过受保护 API 批准后用 ``--resume-approved`` 恢复，或在第二次受验签
CLI 调用中用 ``--approve-and-resume`` 完成决定与恢复。入口不再提供布尔审批参数，
因此自然语言、命令行开关和 LLM 都不能直接放行副作用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime.graph import PEVRGraphRunner, PEVRInterrupt  # noqa: E402
from agent.runtime.pevr import PEVRRequest  # noqa: E402
from agent.security import JWTAuthenticator, authorize_operator  # noqa: E402
from agent.tools import build_tool_registry  # noqa: E402
from agent.tools.snapshots import (  # noqa: E402
    DefaultWarehouseSnapshotProvider,
    DynamicOrderSnapshotProvider,
)
from domains.amr_warehouse import TransportOrder  # noqa: E402
from services.config import load_settings  # noqa: E402
from services.demo.order_json import (  # noqa: E402
    load_demo_nl_order_json,
    resolve_demo_nl_order_json_path,
)
from services.model_gateway.provider import ModelProvider  # noqa: E402
from services.application import PostgresHITLStore, PostgresRuntimeStore  # noqa: E402
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
        help="自然语言运输订单；动态订单由 --order-json 注入快照，不再要求命中种子",
    )
    parser.add_argument(
        "--order-json",
        type=Path,
        default=None,
        help="服务端生成的动态订单 JSON（仅 tmp/demo_nl_order_*.json）",
    )
    parser.add_argument("--environment-ref", default="warehouse_v1@seed-v1")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--jwt-token-file",
        type=Path,
        default=None,
        help="读取 operator JWT 的 UTF-8 文件；缺省读取 AMR_OPERATOR_JWT 环境变量",
    )
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument(
        "--approve-and-resume",
        metavar="APPROVAL_ID",
        help="由当前已验签 operator 决定 pending 审批并从同一 checkpoint 恢复",
    )
    approval.add_argument(
        "--resume-approved",
        metavar="APPROVAL_ID",
        help="读取已由受保护 API/CLI 批准的签名 grant 并恢复",
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "p013_e2e_result.json")
    return parser.parse_args()


def _load_jwt_token(args: argparse.Namespace) -> str:
    """从文件或环境读取 JWT；令牌绝不进入报告、日志或异常正文。"""

    if args.jwt_token_file is not None:
        token = args.jwt_token_file.read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("AMR_OPERATOR_JWT", "").strip()
    if not token:
        raise ValueError("缺少 operator JWT：设置 AMR_OPERATOR_JWT 或使用 --jwt-token-file")
    return token


def _write_output(path: Path, payload: object) -> None:
    """统一以 UTF-8 写入完成或 waiting artifact，避免两条路径格式漂移。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """加载 Fast Profile，并用 PostgreSQL 组装可跨进程恢复的真实九工具主图。"""

    args = parse_args()
    run_id = args.run_id or _default_run_id(args.request, args.environment_ref, args.seed)
    settings = load_settings()
    authenticator = JWTAuthenticator(
        settings.security.jwt_secret.get_secret_value(),
        issuer=settings.security.issuer,
        audience=settings.security.audience,
        leeway_seconds=settings.security.leeway_seconds,
    )
    principal = authenticator.authenticate_token(_load_jwt_token(args))
    authorize_operator(principal)
    provider = ModelProvider(settings.model_gateway)
    snapshot_provider = DefaultWarehouseSnapshotProvider()
    if args.order_json is not None:
        order_path = resolve_demo_nl_order_json_path(args.order_json, repository_root=PROJECT_ROOT)
        dynamic_order: TransportOrder = load_demo_nl_order_json(order_path)
        snapshot_provider = DynamicOrderSnapshotProvider(
            [dynamic_order],
            base=snapshot_provider,
        )
    database_runtime = create_database_runtime(settings.database)
    checkpoint_store = PostgresRuntimeStore(database_runtime.session_factory)
    hitl_store = PostgresHITLStore(
        database_runtime.session_factory,
        signing_secret=settings.security.hitl_signing_secret.get_secret_value(),
    )
    try:
        # 迁移只为旧外部快照补唯一 lookup_id，不改写历史仿真/Trace/报告证据。
        migration = checkpoint_store.migrate_external_execution_lookups()
        approval_id = args.approve_and_resume or args.resume_approved
        grant = None
        if approval_id is not None:
            pending = hitl_store.get_request(approval_id)
            if pending is None or pending.run_id != run_id:
                raise ValueError("approval_id 不属于当前 run")
            if args.approve_and_resume is not None:
                grant = hitl_store.approve(approval_id, principal=principal)
            else:
                grant = hitl_store.get_grant(approval_id)
                if grant is None:
                    raise ValueError("审批尚未 approved，不能恢复")
        # Registry 的 dispatch/query 与主图 Checkpoint 共用同一 PostgreSQL
        # 适配器；进程退出后外部仿真事实和 Effect Ledger 因而不会各自丢失。
        registry = build_tool_registry(
            settings=settings,
            snapshot_provider=snapshot_provider,
            execution_store=checkpoint_store,
            principal=principal,
            security_required=True,
        )
        runner = PEVRGraphRunner(
            provider,
            registry=registry,
            snapshot_provider=snapshot_provider,
            checkpoint_store=checkpoint_store,
            hitl_store=hitl_store,
            security_required=True,
        )
        request = PEVRRequest(
            run_id=run_id,
            raw_request=args.request,
            environment_ref=args.environment_ref,
            seed=args.seed,
            principal=principal,
            approval_grant=grant,
        )
        try:
            result = runner.run(request)
        except PEVRInterrupt as exc:
            checkpoint = checkpoint_store.load_checkpoint(run_id)
            payload = {
                "schema_version": "p013-secure-cli.v1",
                "run_id": run_id,
                "status": "waiting_approval",
                "principal_subject": principal.subject,
                "principal_role": principal.role.value,
                "interrupt": exc.interrupt.model_dump(mode="json"),
                "checkpoint_sequence": checkpoint.checkpoint_sequence if checkpoint else None,
                "evidence_refs": [
                    f"approval://{exc.interrupt.approval_id}/pending",
                    f"checkpoint://{exc.interrupt.checkpoint_id}",
                ],
                "legacy_lookup_migration": migration,
            }
            _write_output(args.output, payload)
            print(json.dumps(payload, ensure_ascii=False))
            return 3
    finally:
        database_runtime.dispose()
    _write_output(args.output, result.model_dump(mode="json"))
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
                "principal_subject": report.principal_subject,
                "approval_id": report.approval_id,
                "approval_checkpoint_id": report.approval_checkpoint_id,
                "legacy_lookup_migration": migration,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
