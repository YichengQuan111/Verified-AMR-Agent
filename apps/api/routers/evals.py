"""P0-06 评测运行持久化入口。"""

from fastapi import APIRouter, Depends, status

from agent.security import Principal
from apps.api.dependencies import get_operator_principal, get_run_service
from apps.api.schemas import CreateEvalRunRequest
from services.application import RunService, RunView


router = APIRouter(prefix="/evals/runs", tags=["evals"])


@router.post("", response_model=RunView, status_code=status.HTTP_201_CREATED)
def create_eval_run(
    request: CreateEvalRunRequest,
    service: RunService = Depends(get_run_service),
    principal: Principal = Depends(get_operator_principal),
) -> RunView:
    """创建 run_kind=eval 的运行，不越界实现评测执行器。"""

    return service.create_eval_run(
        request.task_contract,
        suite_id=request.suite_id,
        case_ids=request.case_ids,
        requested_by=principal.subject,
    )


__all__ = ["router"]
