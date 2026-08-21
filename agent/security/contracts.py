"""P0-16 已验证身份的严格契约。

``Principal`` 是权限判断的唯一身份来源。调用方可以携带 JWT，但不能通过请求体、
Prompt、检索正文或工具参数重新声明角色；这些非身份数据在进入授权层前已经被
视为不可信输入。
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent.tools.contracts import UserRole


class SecurityContract(BaseModel):
    """安全边界对象共同使用的封闭 Pydantic 配置。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class Principal(SecurityContract):
    """由签名令牌或受信任内部适配器产生的调用主体。

    ``role`` 只在 JWT 验签成功后从令牌读取；它不能由 API body 或 Agent 自己
    传入。时间字段用于把身份生命周期带入审计，不作为权限提升的可选开关。
    """

    subject: str = Field(min_length=1, max_length=128)
    role: UserRole
    issuer: str = Field(default="amr-agent", min_length=1, max_length=128)
    audience: str = Field(default="amr-agent-api", min_length=1, max_length=128)
    token_id: str | None = Field(default=None, min_length=1, max_length=128)
    issued_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    auth_method: Literal["jwt", "internal"] = "jwt"
