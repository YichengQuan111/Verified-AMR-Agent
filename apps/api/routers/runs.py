"""运行、计划、SSE 事件与审批 HTTP 路由。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, status
from sse_starlette.sse import EventSourceResponse

from apps.api.dependencies import get_run_service
from apps.api.schemas import ApprovalDecisionRequest, CreateRunRequest
from services.application import ApprovalView, PlanView, RunService, RunView


router = APIRouter(prefix="/agent/runs", tags=["runs"])


@router.post("", response_model=RunView, status_code=status.HTTP_201_CREATED)
def create_run(
    request: CreateRunRequest,
    service: RunService = Depends(get_run_service),
) -> RunView:
    """创建持久化运行；业务事务全部由 RunService 负责。"""

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
) -> RunView:
    """从 PostgreSQL 查询运行状态，而不是读取进程内字典。"""

    return service.get_run(run_id)


@router.get("/{run_id}/plan", response_model=PlanView)
def get_plan(
    run_id: str,
    plan_version: int | None = Query(default=None, ge=1),
    service: RunService = Depends(get_run_service),
) -> PlanView:
    """查询最新或指定版本计划。"""

    return service.get_plan(run_id, plan_version=plan_version)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    service: RunService = Depends(get_run_service),
) -> EventSourceResponse:
    """把已持久化事件按序输出为有限 SSE 快照。"""

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
) -> ApprovalView:
    """提交人工审批决定；Router 不直接修改 approvals 或 runs。"""

    return service.decide_approval(
        run_id,
        decision=request.decision,
        decided_by=request.decided_by,
        comment=request.comment,
        task_id=request.task_id,
    )


__all__ = ["router"]
