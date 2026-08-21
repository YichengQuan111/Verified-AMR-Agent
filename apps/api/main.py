"""FastAPI 应用工厂、P0 健康检查与 P0-06 Router 装配入口。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from apps.api.routers import documents_router, evals_router, runs_router
from services.application import (
    ApplicationError,
    DocumentService,
    PostgresRuntimeStore,
    RunService,
)
from services.config import AppSettings, load_settings
from services.model_gateway.exceptions import ModelGatewayError
from services.model_gateway.provider import ModelProvider
from services.model_gateway.protocols import ModelProviderProtocol
from services.observability import configure_logging, get_logger
from services.persistence import (
    DatabaseRuntime,
    SessionFactory,
    create_database_runtime,
)


def create_app(
    settings: AppSettings | None = None,
    model_provider: ModelProviderProtocol | None = None,
    session_factory: SessionFactory | None = None,
) -> FastAPI:
    """创建 FastAPI 实例。

    三个依赖都允许由测试注入；生产运行不传参数时会加载真实配置、模型 Provider
    和 PostgreSQL 会话工厂。创建 Engine 是惰性的，健康检查不会因此连接数据库。
    """

    resolved_settings = settings or load_settings()
    provider = model_provider or ModelProvider(resolved_settings.model_gateway)
    owned_database_runtime: DatabaseRuntime | None = None
    resolved_session_factory = session_factory
    if resolved_session_factory is None:
        owned_database_runtime = create_database_runtime(resolved_settings.database)
        resolved_session_factory = owned_database_runtime.session_factory

    run_service = RunService(resolved_session_factory)
    document_service = DocumentService(resolved_session_factory)
    # P0-14 的运行图通过此共享 Store 注入 Checkpoint/Effect Ledger；这里仅组装
    # PostgreSQL 边界，不在 API 进程启动时主动创建表或执行工具副作用。
    checkpoint_store = PostgresRuntimeStore(resolved_session_factory)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """管理 API 从启动到关闭的整个生命周期。"""

        configure_logging(resolved_settings.logging)
        logger = get_logger(__name__, component="api")
        application.state.settings = resolved_settings
        application.state.model_provider = provider
        application.state.session_factory = resolved_session_factory
        application.state.run_service = run_service
        application.state.document_service = document_service
        application.state.checkpoint_store = checkpoint_store
        try:
            if resolved_settings.model_gateway.validate_on_startup:
                # ModelProvider 是同步 SDK；to_thread 避免在等待模型服务时阻塞
                # FastAPI/asyncio 的事件循环。异常不在这里吞掉，Uvicorn 因而会拒绝启动。
                await asyncio.to_thread(provider.startup)
            logger.info(
                "api_started",
                environment=resolved_settings.app.environment,
                model_validation=resolved_settings.model_gateway.validate_on_startup,
            )
            # yield 之前是启动阶段，之后是关闭阶段。
            yield
            logger.info("api_stopped")
        finally:
            # 仅释放本应用自己创建的连接池；测试注入的 Engine 由测试负责关闭。
            if owned_database_runtime is not None:
                owned_database_runtime.dispose()

    application = FastAPI(
        title="AMR Agent API",
        version=resolved_settings.app.version,
        lifespan=lifespan,
    )

    @application.exception_handler(ApplicationError)
    async def application_error_handler(
        request: Request,
        exc: ApplicationError,
    ) -> JSONResponse:
        """把稳定应用异常映射成 JSON，避免泄漏数据库内部细节。"""

        del request
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @application.get("/health")
    def health() -> dict[str, str | bool | None]:
        """轻量进程健康检查，不额外请求模型服务。"""

        version = provider.version_record
        return {
            "status": "ok",
            "service": resolved_settings.app.name,
            "version": resolved_settings.app.version,
            "environment": resolved_settings.app.environment,
            "model_validated": version is not None,
            "model_alias": version.served_alias if version is not None else None,
        }

    @application.get("/health/model")
    async def model_health() -> dict[str, object]:
        """主动重新探测模型；稳定网关异常统一转换成 HTTP 503。"""

        try:
            result = await asyncio.to_thread(provider.health_check)
        except ModelGatewayError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return result.model_dump(mode="json")

    application.include_router(runs_router)
    application.include_router(documents_router)
    application.include_router(evals_router)

    return application


# Uvicorn 使用 ``apps.api.main:app`` 导入这个实例。
app = create_app()
