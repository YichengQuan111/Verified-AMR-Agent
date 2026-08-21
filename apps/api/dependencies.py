"""FastAPI 路由到应用 Service 的依赖入口。"""

from fastapi import Request

from services.application import DocumentService, PostgresRuntimeStore, RunService


def get_run_service(request: Request) -> RunService:
    """取得应用生命周期中唯一配置的运行 Service。"""

    return request.app.state.run_service


def get_document_service(request: Request) -> DocumentService:
    """取得应用生命周期中唯一配置的文档 Service。"""

    return request.app.state.document_service


def get_checkpoint_store(request: Request) -> PostgresRuntimeStore:
    """取得 P0-14 PostgreSQL Checkpoint/Effect Ledger 适配器。"""

    return request.app.state.checkpoint_store


__all__ = ["get_checkpoint_store", "get_document_service", "get_run_service"]
