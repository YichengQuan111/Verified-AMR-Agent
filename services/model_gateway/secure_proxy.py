"""Fast 模型 loopback 前置代理：统一鉴权、禁用浏览器跨域并转发到 llama.cpp。"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


MAX_REQUEST_BYTES = 4 * 1024 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def create_proxy_app(
    *,
    api_key: str,
    backend_url: str = "http://127.0.0.1:18080",
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """创建无 UI、无 CORS 的严格 Bearer 代理。

    llama.cpp 当前会让 ``/v1/models`` 匿名可读，即使 completion 已启用 key。
    因此发布端口不能直接暴露后端；本代理对 health/models/completion 使用完全相同
    的认证规则。任何带 Origin 的浏览器请求一律拒绝，P0 不提供浏览器模型面板。
    """

    if len(api_key) < 32 or api_key == "dummy":
        raise ValueError("Fast proxy api_key 至少需要 32 字符且不能为 dummy")
    normalised_backend = backend_url.rstrip("/")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """代理拥有唯一 AsyncClient，并在退出时可靠释放连接池。"""

        application.state.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(600.0, connect=3.0),
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await application.state.client.aclose()

    application = FastAPI(
        title="AMR Fast Model Secure Proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def forward(path: str, request: Request) -> Response:
        """先做 Origin/凭据/大小门禁，再以服务端 key 转发白盒 HTTP 请求。"""

        if request.headers.get("origin") is not None:
            # 不返回 Access-Control-Allow-Origin；恶意网页无法借用户浏览器读取模型。
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "cors_origin_denied"}},
            )
        authorization = request.headers.get("authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(supplied, api_key)
        ):
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={"error": {"code": "invalid_api_key"}},
            )

        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "request_too_large"}},
            )
        forwarded_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower()
            not in HOP_BY_HOP_HEADERS
            | {"host", "content-length", "authorization", "origin"}
        }
        # 后端也启用同一随机 key；即使本机进程误连 18080，completion 仍会拒绝。
        forwarded_headers["authorization"] = f"Bearer {api_key}"
        client: httpx.AsyncClient = request.app.state.client
        upstream = await client.request(
            method=request.method,
            url=f"{normalised_backend}/{path}",
            params=request.query_params,
            headers=forwarded_headers,
            content=body,
        )
        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower()
            not in HOP_BY_HOP_HEADERS
            | {"content-length", "content-encoding", "access-control-allow-origin"}
        }
        response_headers["X-Content-Type-Options"] = "nosniff"
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=None,
        )

    return application


def main() -> int:
    """从进程 secret 启动固定 loopback 代理；不接受命令行 key，避免进程列表泄漏。"""

    api_key = os.environ.get("FAST_MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    backend_url = os.environ.get("FAST_MODEL_BACKEND_URL", "http://127.0.0.1:18080")
    app = create_proxy_app(api_key=api_key, backend_url=backend_url)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8080,
        access_log=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_REQUEST_BYTES", "create_proxy_app", "main"]
