"""P0-06 FastAPI Router 汇总入口。"""

from apps.api.routers.documents import router as documents_router
from apps.api.routers.evals import router as evals_router
from apps.api.routers.runs import router as runs_router

__all__ = ["documents_router", "evals_router", "runs_router"]
