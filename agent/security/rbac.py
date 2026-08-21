"""P0-16 两级 RBAC、文档 ACL 和检索范围门禁。"""

from __future__ import annotations

from collections.abc import Iterable

from agent.security.contracts import Principal
from agent.tools.contracts import ToolSpec, UserRole


class AuthorizationError(PermissionError):
    """已认证主体没有执行目标动作的权限。"""

    def __init__(self, message: str, *, code: str = "permission_denied") -> None:
        super().__init__(message)
        self.code = code


def authorize_operator(principal: Principal) -> None:
    """要求签名身份是 operator；viewer 不可通过参数升级。"""

    if principal.role is not UserRole.OPERATOR:
        raise AuthorizationError("该操作需要 operator 角色", code="operator_required")


def authorize_tool(principal: Principal, spec: ToolSpec) -> None:
    """按注册 ToolSpec 执行工具级角色门禁。"""

    if principal.role not in spec.allowed_roles:
        raise AuthorizationError(
            f"角色 {principal.role.value} 无权调用 {spec.tool_name.value}",
            code="tool_role_not_allowed",
        )


def authorize_document(principal: Principal, document_roles: Iterable[UserRole | str]) -> None:
    """要求文档 ACL 明确包含主体角色，禁止用更高角色读取低权限文档。"""

    allowed = {item if isinstance(item, UserRole) else UserRole(item) for item in document_roles}
    if principal.role not in allowed:
        raise AuthorizationError("主体无权读取该文档", code="document_acl_denied")


def assert_retrieval_scope(
    principal: Principal,
    requested_scope: UserRole | None,
) -> UserRole:
    """把检索范围绑定到主体角色；viewer 请求 operator 范围直接拒绝。"""

    scope = requested_scope or principal.role
    if scope is UserRole.OPERATOR and principal.role is UserRole.VIEWER:
        raise AuthorizationError("viewer 不能请求 operator 文档范围", code="rag_role_scope_escalation")
    return scope


__all__ = [
    "AuthorizationError",
    "assert_retrieval_scope",
    "authorize_document",
    "authorize_operator",
    "authorize_tool",
]
