"""P0-16 JWT 验签边界。

这里采用固定 HS256 算法和固定 issuer/audience。算法、签发者、受众、subject、
role、iat、exp 任一项不满足要求都 fail closed；任何失败只返回稳定错误，不把
JWT 原文、密钥或内部异常带给上层。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import jwt
from jwt import InvalidTokenError

from agent.security.contracts import Principal
from agent.tools.contracts import UserRole


class AuthenticationError(ValueError):
    """令牌不能证明调用主体身份。"""


class JWTAuthenticator:
    """验证并解析 AMR Agent API 的 HS256 JWT。"""

    ALGORITHM = "HS256"

    def __init__(
        self,
        secret: str,
        *,
        issuer: str = "amr-agent",
        audience: str = "amr-agent-api",
        leeway_seconds: int = 0,
    ) -> None:
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("JWT secret 至少需要 32 个字符")
        if not issuer.strip() or not audience.strip():
            raise ValueError("JWT issuer 和 audience 不能为空")
        if isinstance(leeway_seconds, bool) or leeway_seconds < 0 or leeway_seconds > 300:
            raise ValueError("JWT leeway_seconds 必须在 0..300 内")
        self._secret = secret
        self.issuer = issuer
        self.audience = audience
        self.leeway_seconds = leeway_seconds

    def authenticate_token(self, token: str) -> Principal:
        """验签并把 JWT claims 收敛成不可变语义上的 ``Principal``。

        PyJWT 的 ``algorithms`` 参数是硬白名单；同时显式要求关键 claims，
        防止无 exp 的长寿命令牌或缺 role 的半身份进入业务层。
        """

        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("认证令牌缺失")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != self.ALGORITHM:
                raise AuthenticationError("不支持的 JWT 算法")
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self.ALGORITHM],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_seconds,
                options={"require": ["sub", "role", "iat", "exp", "iss", "aud"]},
            )
        except AuthenticationError:
            raise
        except (InvalidTokenError, TypeError, ValueError, OverflowError) as exc:
            raise AuthenticationError("JWT 无效或已过期") from exc

        if not isinstance(payload, Mapping):
            raise AuthenticationError("JWT claims 格式无效")
        try:
            subject = payload["sub"]
            role = UserRole(payload["role"])
            issued_at = self._claim_time(payload["iat"])
            expires_at = self._claim_time(payload["exp"])
            principal = Principal(
                subject=subject,
                role=role,
                issuer=self.issuer,
                audience=self.audience,
                token_id=payload.get("jti"),
                issued_at=issued_at,
                expires_at=expires_at,
                auth_method="jwt",
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise AuthenticationError("JWT 身份字段无效") from exc
        if principal.expires_at is not None and principal.issued_at is not None:
            if principal.expires_at <= principal.issued_at:
                raise AuthenticationError("JWT 生命周期无效")
        return principal

    def authenticate_authorization_header(self, value: str | None) -> Principal:
        """只接受 ``Bearer <JWT>``，拒绝 Basic、空值和多余片段。"""

        if not isinstance(value, str):
            raise AuthenticationError("需要 Bearer 认证")
        parts = value.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthenticationError("需要 Bearer 认证")
        return self.authenticate_token(parts[1])

    def issue_token(
        self,
        *,
        subject: str,
        role: UserRole,
        ttl_seconds: int = 900,
        now: datetime | None = None,
        token_id: str | None = None,
    ) -> str:
        """为本地测试/受信任启动脚本签发短期令牌。

        业务 API 不暴露此方法；生产系统应由外部身份服务签发 JWT。保留它是为了
        让安全测试使用同一验签路径，而不是构造无法代表真实边界的伪 Principal。
        """

        if not isinstance(role, UserRole):
            role = UserRole(role)
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("JWT ttl_seconds 必须在 1..86400 内")
        issued = now or datetime.now(timezone.utc)
        if issued.tzinfo is None:
            raise ValueError("JWT now 必须带时区")
        expires = issued + timedelta(seconds=ttl_seconds)
        claims: dict[str, Any] = {
            "sub": subject,
            "role": role.value,
            "iss": self.issuer,
            "aud": self.audience,
            "iat": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
        }
        if token_id is not None:
            claims["jti"] = token_id
        return jwt.encode(claims, self._secret, algorithm=self.ALGORITHM)

    @staticmethod
    def _claim_time(value: Any) -> datetime:
        """把 NumericDate 变成带时区时间，拒绝 bool/字符串绕过。"""

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("JWT 时间 claim 必须是 NumericDate")
        return datetime.fromtimestamp(value, tz=timezone.utc)


__all__ = ["AuthenticationError", "JWTAuthenticator"]
