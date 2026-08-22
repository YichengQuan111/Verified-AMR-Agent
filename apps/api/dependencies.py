"""FastAPI 路由到应用 Service 的依赖入口。"""

from fastapi import Depends, Header, HTTPException, Request

from agent.security import AuthenticationError, JWTAuthenticator, Principal, authorize_operator
from agent.tools import UserRole
from services.application import DocumentService, PostgresHITLStore, PostgresRuntimeStore, RunService
from services.demo import ControlledLauncher, ControlledNLRunner, WarehouseDemoService
from services.model_gateway.protocols import ModelProviderProtocol


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


def get_demo_service(request: Request) -> WarehouseDemoService:
    """惰性构造演示编排服务；测试用 dependency_overrides 注入坏计划变体。

    惰性构造避免 create_app 在任何请求前扫描 C++ build 目录；服务本身
    无状态（每次请求重读固定 seed），单实例缓存到 app.state 是安全的。
    """

    service = getattr(request.app.state, "demo_service", None)
    if service is None:
        service = WarehouseDemoService()
        request.app.state.demo_service = service
    return service


def get_model_provider(request: Request) -> ModelProviderProtocol:
    """取得应用生命周期中唯一的模型 Provider（create_app 已装配到 app.state）。"""

    return request.app.state.model_provider


def get_demo_launcher(request: Request) -> ControlledLauncher:
    """惰性构造受控启动器；进程句柄必须跨请求保留，因此缓存到 app.state。"""

    launcher = getattr(request.app.state, "demo_launcher", None)
    if launcher is None:
        launcher = ControlledLauncher()
        request.app.state.demo_launcher = launcher
    return launcher


def get_demo_nl_runner(request: Request) -> ControlledNLRunner:
    """惰性构造自然语言闭环运行器；进程句柄与槽位必须跨请求保留。

    token_factory 绑定 app.state.authenticator：每次拉起 PEVR CLI 现铸一枚
    1 小时 operator JWT（subject=demo-nl-runner），浏览器与长期令牌文件都不参与。
    """

    runner = getattr(request.app.state, "demo_nl_runner", None)
    if runner is None:
        authenticator: JWTAuthenticator = request.app.state.authenticator
        runner = ControlledNLRunner(
            token_factory=lambda: authenticator.issue_token(
                subject="demo-nl-runner",
                role=UserRole.OPERATOR,
                ttl_seconds=3600,
            )
        )
        request.app.state.demo_nl_runner = runner
    return runner


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
    "get_demo_launcher",
    "get_demo_nl_runner",
    "get_demo_service",
    "get_document_service",
    "get_hitl_store",
    "get_model_provider",
    "get_operator_principal",
    "get_run_service",
]
