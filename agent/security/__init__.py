"""P0-16 身份、授权与安全边界的稳定导出。

安全模块不依赖 FastAPI 或具体数据库；运行时、工具注册表和 API 只消费这里
定义的已验证身份与授权结果，避免每一层各自解释 ``role`` 字符串。
"""

from agent.security.auth import AuthenticationError, JWTAuthenticator
from agent.security.contracts import Principal, SecurityContract
from agent.security.rbac import (
    AuthorizationError,
    authorize_document,
    authorize_operator,
    authorize_tool,
    assert_retrieval_scope,
)

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "JWTAuthenticator",
    "Principal",
    "SecurityContract",
    "assert_retrieval_scope",
    "authorize_document",
    "authorize_operator",
    "authorize_tool",
]
