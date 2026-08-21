from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from services.model_gateway.secure_proxy import MAX_REQUEST_BYTES, create_proxy_app


API_KEY = "proxy-unit-secret-0123456789-0123456789"


def _backend(request: httpx.Request) -> httpx.Response:
    """Fake 后端同时确认代理没有丢失服务端 Bearer 凭据。"""

    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    return httpx.Response(
        200,
        json={"path": request.url.path, "method": request.method},
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _client() -> TestClient:
    """所有请求都留在进程内，不需要真实 llama.cpp。"""

    app = create_proxy_app(
        api_key=API_KEY,
        transport=httpx.MockTransport(_backend),
    )
    return TestClient(app)


def test_models_and_completion_both_require_exact_bearer_key() -> None:
    with _client() as client:
        for path in ("/v1/models", "/v1/chat/completions"):
            assert client.get(path).status_code == 401
            assert (
                client.get(path, headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            response = client.get(
                path,
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            assert response.status_code == 200
            assert response.json()["path"] == path


def test_every_browser_origin_is_rejected_without_cors_headers() -> None:
    with _client() as client:
        response = client.options(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_proxy_removes_backend_cors_header_and_bounds_request_body() -> None:
    with _client() as client:
        valid = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            content=b"{}",
        )
        too_large = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            content=b"x" * (MAX_REQUEST_BYTES + 1),
        )

    assert valid.status_code == 200
    assert "access-control-allow-origin" not in valid.headers
    assert valid.headers["x-content-type-options"] == "nosniff"
    assert too_large.status_code == 413
