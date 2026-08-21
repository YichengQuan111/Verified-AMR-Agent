"""FastAPI 路由到应用 Service 的依赖入口。"""

from fastapi import Depends, Header, HTTPException, Request

from agent.security import AuthenticationError, JWTAuthenticator, Principal, authorize_operator
from services.application import DocumentService, PostgresHITLStore, PostgresRuntimeStore, RunService


def get_run_service(request: Request) -> RunService:
    """取得应用生命周期中唯一配置的运行 Service。"""

    return request.app.state.run_service


def get_document_service(request: Request) -> DocumentService:
    """取得应用生命周期中唯一配置的文档 Service。"""

    return request.app.state.document_service


def get_checkpoint_store(request: Request) -> PostgresRuntimeStore:
    """取得 P0-14 PostgreSQL Checkpoint/Effect Ledger 适配器。"""

    return request.app.state.checkpoint_store


def get_hitl_store(request: Request) -> PostgresHITLStore:
    """取得跨进程保存 pending/approved 状态的 HITL Store。"""

    return request.app.state.hitl_store


def get_current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """校验 Bearer JWT；不接受 body/query 中的 subject 或 role。"""

    authenticator: JWTAuthenticator = request.app.state.authenticator
    try:
        return authenticator.authenticate_authorization_header(authorization)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_operator_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """人工审批、上传和创建运行等写入口的统一 operator 门禁。"""

    try:
        authorize_operator(principal)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "OPERATOR_REQUIRED", "message": str(exc)},
        ) from exc
    return principal


__all__ = [
    "get_checkpoint_store",
    "get_current_principal",
    "get_document_service",
    "get_hitl_store",
    "get_operator_principal",
    "get_run_service",
]
