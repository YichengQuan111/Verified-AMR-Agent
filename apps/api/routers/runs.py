"""运行、计划、SSE 事件与审批 HTTP 路由。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sse_starlette.sse import EventSourceResponse

from agent.runtime.hitl import ApprovalGrant, HITLRequest
from agent.security import Principal
from apps.api.dependencies import (
    get_current_principal,
    get_hitl_store,
    get_operator_principal,
    get_run_service,
)
from apps.api.schemas import ApprovalDecisionRequest, CreateRunRequest
from services.application import (
    ApprovalView,
    PlanView,
    PostgresHITLStore,
    RunService,
    RunView,
)


router = APIRouter(prefix="/agent/runs", tags=["runs"])


@router.post("", response_model=RunView, status_code=status.HTTP_201_CREATED)
def create_run(
    request: CreateRunRequest,
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_operator_principal),
) -> RunView:
    """创建持久化运行；业务事务全部由 RunService 负责。"""

    del principal
    return service.create_run(
        request.task_contract,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        context_digest=request.context_digest,
        run_state_snapshot=request.run_state_snapshot,
    )


@router.get("/{run_id}", response_model=RunView)
def get_run(
    run_id: str,
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_current_principal),
) -> RunView:
    """从 PostgreSQL 查询运行状态，而不是读取进程内字典。"""

    del principal
    return service.get_run(run_id)


@router.get("/{run_id}/plan", response_model=PlanView)
def get_plan(
    run_id: str,
    plan_version: int | None = Query(default=None, ge=1),
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_current_principal),
) -> PlanView:
    """查询最新或指定版本计划。"""

    del principal
    return service.get_plan(run_id, plan_version=plan_version)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_current_principal),
) -> EventSourceResponse:
    """把已持久化事件按序输出为有限 SSE 快照。"""

    del principal
    # SQLAlchemy 同步会话放到工作线程，避免阻塞 FastAPI 的事件循环。
    events = await asyncio.to_thread(
        service.list_events,
        run_id,
        after_sequence=after_sequence,
    )

    async def event_source() -> AsyncIterator[dict[str, str]]:
        """每条 SSE data 都是同一个 EventView JSON 契约。"""

        for event in events:
            yield {
                "id": str(event.sequence_no),
                "event": event.event_type,
                "data": json.dumps(
                    event.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }

    return EventSourceResponse(event_source())


@router.post("/{run_id}/approve", response_model=ApprovalView)
def approve_run(
    run_id: str,
    request: ApprovalDecisionRequest,
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_operator_principal),
) -> ApprovalView:
    """提交人工审批决定；Router 不直接修改 approvals 或 runs。"""

    if request.decided_by is not None and request.decided_by != principal.subject:
        raise HTTPException(
            status_code=403,
            detail={"code": "DECIDER_IMPERSONATION", "message": "decided_by 必须等于签名主体"},
        )
    return service.decide_approval(
        run_id,
        decision=request.decision,
        decided_by=principal.subject,
        comment=request.comment,
        task_id=request.task_id,
        principal=principal,
    )


def _get_hitl_request_for_run(
    store: PostgresHITLStore,
    *,
    run_id: str,
    approval_id: str,
) -> HITLRequest:
    """读取并绑定 run-scoped HITL 请求，避免 approval_id 跨运行复用或泄漏。"""

    request = store.get_request(approval_id)
    if request is None or request.run_id != run_id:
        # 不区分“审批不存在”和“属于另一个运行”，避免枚举审批事实。
        raise HTTPException(status_code=404, detail={"code": "HITL_NOT_FOUND", "message": "审批不存在"})
    return request


@router.post(
    "/{run_id}/hitl/{approval_id}/approve",
    response_model=ApprovalGrant,
)
def approve_hitl(
    run_id: str,
    approval_id: str,
    store: PostgresHITLStore = Depends(get_hitl_store),
    principal: Principal = Depends(get_operator_principal),
) -> ApprovalGrant:
    """由签名 operator 批准安全 PEVR 的 pending HITL 请求并签发 grant。"""

    _get_hitl_request_for_run(store, run_id=run_id, approval_id=approval_id)
    try:
        return store.approve(approval_id, principal=principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "HITL_NOT_FOUND", "message": "审批不存在"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"code": "HITL_NOT_PENDING", "message": str(exc)}) from exc


@router.post(
    "/{run_id}/hitl/{approval_id}/reject",
    response_model=HITLRequest,
)
def reject_hitl(
    run_id: str,
    approval_id: str,
    store: PostgresHITLStore = Depends(get_hitl_store),
    principal: Principal = Depends(get_operator_principal),
) -> HITLRequest:
    """由签名 operator 拒绝 HITL 请求；rejected 是不可恢复终态。"""

    _get_hitl_request_for_run(store, run_id=run_id, approval_id=approval_id)
    try:
        return store.reject(approval_id, principal=principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "HITL_NOT_FOUND", "message": "审批不存在"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"code": "HITL_NOT_PENDING", "message": str(exc)}) from exc


__all__ = ["router"]
