"""P0-06 真实 PostgreSQL 事务、恢复与 API 接口测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, inspect, select

from agent.context import PlanTasksOutput
from agent.planning import TaskContract
from agent.tools import UserRole
from apps.api.main import create_app
from services.application import (
    DocumentMetadataInput,
    DocumentService,
    PersistenceConflictError,
    RunService,
)
from services.config.settings import AppSettings
from services.persistence import (
    ApprovalRecord,
    Base,
    DatabaseRuntime,
    DocumentRecord,
    EffectRecord,
    EventRecord,
    PlanRecord,
    RunRecord,
    SessionFactory,
    TaskRecord,
    ToolCallRecord,
    create_database_runtime,
)
from tests.unit.test_p004_contracts import plan_task_payload, task_contract_payload


@pytest.fixture(scope="module")
def database_runtime() -> Iterator[DatabaseRuntime]:
    """连接已经迁移的本地 PostgreSQL；缺表时测试必须失败而不是伪通过。"""

    runtime = create_database_runtime(AppSettings().database)
    existing = set(inspect(runtime.engine).get_table_names(schema="public"))
    assert set(Base.metadata.tables).issubset(existing), "请先执行 P0-06 Alembic upgrade"
    yield runtime
    runtime.dispose()


def _unique_contract(*, approval_required: bool = False) -> TaskContract:
    """生成不会与其他测试共享业务 ID 的合法合同。"""

    payload = task_contract_payload()
    suffix = uuid4().hex[:12]
    payload["contract_id"] = f"CONTRACT-{suffix}"
    payload["orders"][0]["order_id"] = f"ORDER-{suffix}"
    payload["orders"][0]["material_id"] = f"MAT-{suffix}"
    if approval_required:
        payload["risk_level"] = "high"
        payload["approval"] = {
            "required": True,
            "reason": "高风险测试合同需要人工确认",
            "required_role": "operator",
        }
    return TaskContract.model_validate(payload)


def _cleanup_runs(session_factory: SessionFactory, run_ids: list[str]) -> None:
    """仅清理本测试明确记录的运行行，不删除表或其他用户数据。"""

    if not run_ids:
        return
    with session_factory() as session:
        with session.begin():
            # 顺序按外键从叶子回到 runs；目标始终由测试生成的精确 ID 限定。
            session.execute(delete(EffectRecord).where(EffectRecord.run_id.in_(run_ids)))
            session.execute(delete(ApprovalRecord).where(ApprovalRecord.run_id.in_(run_ids)))
            session.execute(delete(EventRecord).where(EventRecord.run_id.in_(run_ids)))
            session.execute(delete(ToolCallRecord).where(ToolCallRecord.run_id.in_(run_ids)))
            session.execute(delete(TaskRecord).where(TaskRecord.run_id.in_(run_ids)))
            session.execute(delete(PlanRecord).where(PlanRecord.run_id.in_(run_ids)))
            session.execute(delete(RunRecord).where(RunRecord.run_id.in_(run_ids)))


def _cleanup_documents(
    session_factory: SessionFactory,
    document_ids: list[str],
) -> None:
    """仅删除本测试创建的文档行。"""

    if not document_ids:
        return
    with session_factory() as session:
        with session.begin():
            session.execute(
                delete(DocumentRecord).where(DocumentRecord.document_id.in_(document_ids))
            )


def test_run_and_event_real_insert_failure_rolls_back_run(
    database_runtime: DatabaseRuntime,
) -> None:
    """证明第二个 INSERT 失败后，第一个已经发出的 INSERT runs 被撤销。"""

    session_factory = database_runtime.session_factory
    engine = database_runtime.engine
    seed_run_id = f"run_seed_{uuid4().hex}"
    failed_run_id = f"run_rollback_{uuid4().hex}"
    duplicate_event_id = f"event_duplicate_{uuid4().hex}"
    contract = _unique_contract()
    now = datetime.now(timezone.utc)

    emitted_inserts: list[str] = []

    def capture_insert(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """只记录本测试关心的两张表，参数和 DSN 不写入日志。"""

        del connection, cursor, parameters, context, executemany
        normalised = " ".join(statement.lower().split())
        if normalised.startswith("insert into runs"):
            emitted_inserts.append("runs")
        elif normalised.startswith("insert into events"):
            emitted_inserts.append("events")

    def collision_id(kind: str) -> str:
        """只让事件主键撞车，run_id 保持全新。"""

        if kind == "run":
            return failed_run_id
        if kind == "event":
            return duplicate_event_id
        raise AssertionError(f"不应请求额外 ID: {kind}")

    try:
        with session_factory() as session:
            with session.begin():
                session.add(
                    RunRecord(
                        run_id=seed_run_id,
                        run_kind="agent",
                        status="created",
                        contract_id=contract.contract_id,
                        environment_ref=contract.environment_ref,
                        plan_version=0,
                        task_contract_snapshot=contract.model_dump(mode="json"),
                        run_state_snapshot={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                # seed event 依赖 seed run；显式 flush 使测试准备阶段不依赖 ORM
                # 对两个无 relationship 对象的插入排序。
                session.flush()
                session.add(
                    EventRecord(
                        event_id=duplicate_event_id,
                        run_id=seed_run_id,
                        sequence_no=1,
                        event_type="test.seeded",
                        severity="info",
                        payload={},
                        created_at=now,
                    )
                )

        event.listen(engine, "before_cursor_execute", capture_insert)
        try:
            service = RunService(session_factory, identifier_factory=collision_id)
            with pytest.raises(PersistenceConflictError, match="整体回滚"):
                service.create_run(contract)
        finally:
            event.remove(engine, "before_cursor_execute", capture_insert)

        assert emitted_inserts == ["runs", "events"]
        with session_factory() as session:
            # 新 run 不存在，证明不是“捕获异常后把第一步提交了”。
            assert session.get(RunRecord, failed_run_id) is None
            # 故障注入用的已提交事件仍存在，证明冲突确实来自 events 主键。
            assert session.get(EventRecord, duplicate_event_id) is not None
    finally:
        # 即使断言或被测代码出现意外异常，也只按精确测试 ID 清理行。
        _cleanup_runs(session_factory, [failed_run_id, seed_run_id])


def test_run_approval_plan_and_restart_recovery(
    database_runtime: DatabaseRuntime,
) -> None:
    """验证跨 Service 实例恢复、审批、计划/任务快照与事件序号。"""

    session_factory = database_runtime.session_factory
    created_ids: list[str] = []
    try:
        contract = _unique_contract(approval_required=True)
        created = RunService(session_factory).create_run(contract)
        created_ids.append(created.run_id)
        assert created.status == "waiting_approval"

        # 新 Service 实例模拟 API 进程重启；状态仍从 PostgreSQL 读取。
        recovered_service = RunService(session_factory)
        assert recovered_service.get_run(created.run_id).task_contract == contract
        assert [item.sequence_no for item in recovered_service.list_events(created.run_id)] == [1, 2]

        approval = recovered_service.decide_approval(
            created.run_id,
            decision="approved",
            decided_by="operator-test",
            comment="测试批准",
        )
        assert approval.status == "approved"
        assert recovered_service.get_run(created.run_id).status == "created"

        plan_payload = {
            "plan_version": 1,
            "tasks": [plan_task_payload(task_id=f"TASK-{uuid4().hex[:10]}")],
            "planning_assumptions": ["测试环境快照可用"],
            "unresolved_risks": [],
        }
        plan = PlanTasksOutput.model_validate(plan_payload)
        saved = recovered_service.save_plan(created.run_id, plan, reason="集成测试")
        assert saved.plan == plan
        assert recovered_service.get_plan(created.run_id).plan_version == 1

        with session_factory() as session:
            task = session.scalar(
                select(TaskRecord).where(TaskRecord.run_id == created.run_id)
            )
            assert task is not None
            assert task.tool_name == "get_fleet_state"
            assert task.plan_version == 1
            assert task.task_snapshot["task_id"] == plan.tasks[0].task_id
            sequences = list(
                session.scalars(
                    select(EventRecord.sequence_no)
                    .where(EventRecord.run_id == created.run_id)
                    .order_by(EventRecord.sequence_no)
                )
            )
            assert sequences == [1, 2, 3, 4]
    finally:
        _cleanup_runs(session_factory, created_ids)


def test_document_survives_new_service_instance(
    database_runtime: DatabaseRuntime,
) -> None:
    """文档正文与关系化元数据可由新 Service 实例读取。"""

    session_factory = database_runtime.session_factory
    document_ids: list[str] = []
    try:
        content = "P0-06 文档内容".encode("utf-8")
        created = DocumentService(session_factory).create_document(
            DocumentMetadataInput(
                filename="guide.txt",
                content_type="text/plain",
                version="1.0",
                role_scope=["viewer", "operator"],
                source="integration-test",
                metadata={"language": "zh-CN"},
            ),
            content,
        )
        document_ids.append(created.document_id)
        recovered = DocumentService(session_factory).get_document(created.document_id)
        assert recovered.content == content
        assert recovered.metadata.checksum == created.checksum
        assert recovered.metadata.metadata == {"language": "zh-CN"}
    finally:
        _cleanup_documents(session_factory, document_ids)


def test_all_p006_http_interfaces_use_real_postgres(
    database_runtime: DatabaseRuntime,
) -> None:
    """覆盖正式路线列出的运行、计划、SSE、审批、文档与评测入口。"""

    session_factory = database_runtime.session_factory
    run_ids: list[str] = []
    document_ids: list[str] = []
    settings = AppSettings()
    settings.model_gateway.validate_on_startup = False
    application = create_app(settings=settings, session_factory=session_factory)
    try:
        with TestClient(application) as client:
            # P0-16 后业务路由统一要求签名身份；沿用 operator 主体覆盖原有
            # P0-06 事务契约，而不把测试改成绕过认证的特殊入口。
            auth_headers = {
                "Authorization": (
                    "Bearer "
                    + application.state.authenticator.issue_token(
                        subject="api-operator",
                        role=UserRole.OPERATOR,
                    )
                )
            }
            contract = _unique_contract(approval_required=True)
            create_response = client.post(
                "/agent/runs",
                json={"task_contract": contract.model_dump(mode="json")},
                headers=auth_headers,
            )
            assert create_response.status_code == 201
            run_id = create_response.json()["run_id"]
            run_ids.append(run_id)

            assert client.get(f"/agent/runs/{run_id}", headers=auth_headers).status_code == 200
            event_response = client.get(f"/agent/runs/{run_id}/events", headers=auth_headers)
            assert event_response.status_code == 200
            assert event_response.headers["content-type"].startswith("text/event-stream")
            assert "event: run.created" in event_response.text

            approval_response = client.post(
                f"/agent/runs/{run_id}/approve",
                json={
                    "decision": "approved",
                    "decided_by": "api-operator",
                    "comment": "API 集成测试批准",
                },
                headers=auth_headers,
            )
            assert approval_response.status_code == 200
            assert approval_response.json()["status"] == "approved"

            plan = PlanTasksOutput.model_validate(
                {
                    "plan_version": 1,
                    "tasks": [plan_task_payload(task_id=f"TASK-{uuid4().hex[:10]}")],
                    "planning_assumptions": [],
                    "unresolved_risks": [],
                }
            )
            RunService(session_factory).save_plan(run_id, plan)
            plan_response = client.get(f"/agent/runs/{run_id}/plan", headers=auth_headers)
            assert plan_response.status_code == 200
            assert plan_response.json()["plan_version"] == 1

            upload_response = client.post(
                "/documents",
                files={"file": ("知识.txt", "安全规则".encode("utf-8"), "text/plain")},
                data={
                    "version": "1.0",
                    "role_scope": "viewer,operator",
                    "source": "api-test",
                    "metadata_json": '{"category":"safety"}',
                },
                headers=auth_headers,
            )
            assert upload_response.status_code == 201
            document_id = upload_response.json()["document_id"]
            document_ids.append(document_id)
            assert client.get(f"/documents/{document_id}", headers=auth_headers).status_code == 200

            eval_contract = _unique_contract()
            eval_response = client.post(
                "/evals/runs",
                json={
                    "task_contract": eval_contract.model_dump(mode="json"),
                    "suite_id": "p006-interface",
                    "case_ids": ["case-001"],
                    "requested_by": "pytest",
                },
                headers=auth_headers,
            )
            assert eval_response.status_code == 201
            assert eval_response.json()["run_kind"] == "eval"
            run_ids.append(eval_response.json()["run_id"])
    finally:
        _cleanup_documents(session_factory, document_ids)
        _cleanup_runs(session_factory, run_ids)
