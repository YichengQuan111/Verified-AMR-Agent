"""P0-16 人工审批、interrupt 和可恢复 Checkpoint 契约。

HITL 是一个 fail-closed 的小状态机：请求只能由已通过 Schema 和确定性 Validator
的计划产生，审批只能由 operator 身份完成，恢复前还要重新核对请求摘要、计划摘要
和 Validator 摘要。审批结果通过 HMAC 签名并与存储中的记录比对，因而不能用一个
手写的 ``approved=True`` 或篡改后的 JSON 绕过工具权限和验证器。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
from threading import RLock
from typing import Any, Callable, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent.security.contracts import Principal
from agent.security.rbac import authorize_operator
from agent.tools.contracts import UserRole


class HITLContract(BaseModel):
    """HITL 持久化对象共同使用的严格配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)


class HITLReason(str, Enum):
    """必须停下来等待人的四类安全触发原因。"""

    HIGH_PRIORITY_OVERRIDE = "high_priority_override"
    HUMAN_TAKEOVER = "human_takeover"
    HIGH_RISK_WRITE = "high_risk_write"
    FAULT_RECOVERY = "fault_recovery"


class HITLStatus(str, Enum):
    """审批状态机；不存在自动从 pending 变 approved 的转移。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class HITLRequest(HITLContract):
    """一次绑定到具体 Checkpoint、计划和 Validator 结果的审批请求。"""

    schema_version: Literal["1.0"] = "1.0"
    approval_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    requested_by: str = Field(min_length=1, max_length=128)
    required_role: Literal[UserRole.OPERATOR] = UserRole.OPERATOR
    reason_code: HITLReason
    reason: str = Field(min_length=1, max_length=2000)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_status: Literal["valid"] = "valid"
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at: AwareDatetime
    expires_at: AwareDatetime
    status: HITLStatus = HITLStatus.PENDING

    @model_validator(mode="after")
    def validate_request(self) -> "HITLRequest":
        """审批期限和摘要必须是可验证的正向事实。"""

        if self.expires_at <= self.requested_at:
            raise ValueError("审批 expires_at 必须晚于 requested_at")
        expected = canonical_hitl_digest(
            {
                "approval_id": self.approval_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "plan_version": self.plan_version,
                "requested_by": self.requested_by,
                "reason_code": self.reason_code.value,
                "checkpoint_id": self.checkpoint_id,
                "plan_digest": self.plan_digest,
                "validator_digest": self.validator_digest,
            }
        )
        if self.request_digest != expected:
            raise ValueError("HITL request_digest 与请求事实不一致")
        return self


class ApprovalGrant(HITLContract):
    """审批通过后由存储层签发的、只能用于同一计划/Validator 的授权票据。"""

    schema_version: Literal["1.0"] = "1.0"
    approval_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_role: Literal[UserRole.OPERATOR] = UserRole.OPERATOR
    decision: Literal["approved"] = "approved"
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_status: Literal["valid"] = "valid"
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_expiry(self) -> "ApprovalGrant":
        """票据过期后即使签名正确也不能恢复副作用。"""

        if self.expires_at <= self.approved_at:
            raise ValueError("审批票据 expires_at 必须晚于 approved_at")
        return self


class HITLInterrupt(HITLContract):
    """写入 Checkpoint 的暂停事实；恢复入口可据此定位审批。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    reason_code: HITLReason
    status: Literal["waiting_approval"] = "waiting_approval"
    created_at: AwareDatetime
    expires_at: AwareDatetime


class HITLInterruptError(RuntimeError):
    """图已保存暂停 Checkpoint，调用方应审批后使用同一 run_id 恢复。"""

    def __init__(self, interrupt: HITLInterrupt) -> None:
        super().__init__(
            f"run {interrupt.run_id} 在 task {interrupt.task_id} 等待审批 "
            f"({interrupt.approval_id})"
        )
        self.interrupt = interrupt


class HITLStoreProtocol(Protocol):
    """PEVR 所需的最小审批持久化协议。"""

    def request_approval(self, request: HITLRequest) -> HITLRequest:
        ...

    def approve(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        ...

    def reject(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> HITLRequest:
        ...

    def verify_grant(
        self,
        grant: ApprovalGrant,
        *,
        principal: Principal,
        run_id: str,
        task_id: str,
        plan_version: int,
        plan_digest: str,
        validator_digest: str,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        ...

    def get_request(self, approval_id: str) -> HITLRequest | None:
        ...


def canonical_hitl_digest(value: Any) -> str:
    """对 HITL 事实计算不含空白和随机顺序的 SHA-256 摘要。"""

    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sign_approval_grant(grant: ApprovalGrant, secret: str) -> str:
    """用与内存/ PostgreSQL 适配器相同的字段全集签名审批票据。"""

    if not isinstance(secret, str) or len(secret) < 16:
        raise ValueError("HITL signing secret 至少需要 16 个字符")
    payload = grant.model_dump(mode="json", exclude={"signature"})
    return hmac.new(
        secret.encode("utf-8"),
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    """递归转换枚举、Pydantic 和时间，拒绝把对象 repr 当成安全摘要。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class InMemoryHITLStore:
    """线程安全的审批适配器，供本地运行和单测使用。

    生产服务可以实现 ``HITLStoreProtocol`` 并把相同的请求/票据字段存入
    PostgreSQL；本适配器刻意不提供自动批准或“管理员万能”分支。
    """

    def __init__(self, *, signing_secret: str = "p016-local-hitl-signing-secret") -> None:
        if not isinstance(signing_secret, str) or len(signing_secret) < 16:
            raise ValueError("HITL signing_secret 至少需要 16 个字符")
        self._secret = signing_secret.encode("utf-8")
        self._requests: dict[str, HITLRequest] = {}
        self._grants: dict[str, ApprovalGrant] = {}
        self._lock = RLock()

    def request_approval(self, request: HITLRequest) -> HITLRequest:
        """按 approval_id 幂等创建 pending 请求；不同事实不能复用同一 ID。"""

        if request.required_role is not UserRole.OPERATOR or request.validator_status != "valid":
            raise ValueError("HITL 请求必须绑定 operator 和 valid Validator 结果")
        with self._lock:
            previous = self._requests.get(request.approval_id)
            if previous is not None:
                if previous.model_dump(mode="json") != request.model_dump(mode="json"):
                    raise ValueError("approval_id 已绑定另一组审批事实")
                return previous.model_copy(deep=True)
            self._requests[request.approval_id] = request.model_copy(deep=True)
            return request.model_copy(deep=True)

    # 短别名方便 API/测试调用，但核心仍走严格 request_approval。
    request = request_approval

    def get_request(self, approval_id: str) -> HITLRequest | None:
        """返回审批请求副本，避免调用方修改存储中的状态。"""

        with self._lock:
            value = self._requests.get(approval_id)
            return value.model_copy(deep=True) if value is not None else None

    def approve(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        """只接受 operator 决定，并签发绑定原始摘要的批准票据。"""

        authorize_operator(principal)
        current = now or datetime.now(timezone.utc)
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                raise KeyError("审批请求不存在")
            if request.expires_at <= current:
                raise PermissionError("审批请求已过期")
            existing = self._grants.get(approval_id)
            if existing is not None:
                if existing.approved_by != principal.subject:
                    raise PermissionError("审批已经由另一主体决定")
                return existing.model_copy(deep=True)
            if request.status is not HITLStatus.PENDING:
                raise PermissionError("审批请求不是 pending 状态")
            grant = ApprovalGrant(
                approval_id=request.approval_id,
                run_id=request.run_id,
                task_id=request.task_id,
                plan_version=request.plan_version,
                approved_by=principal.subject,
                request_digest=request.request_digest,
                plan_digest=request.plan_digest,
                validator_digest=request.validator_digest,
                approved_at=current,
                expires_at=request.expires_at,
                signature="0" * 64,
            )
            grant = grant.model_copy(update={"signature": self._sign(grant)})
            # HITLRequest 的 status 是不可变审批事实；更新必须重新构造，不能只靠
            # setattr 绕过模型的 pending 约束。
            self._requests[approval_id] = request.model_copy(
                update={"status": HITLStatus.APPROVED},
            )
            self._grants[approval_id] = grant
            return grant.model_copy(deep=True)

    def reject(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> HITLRequest:
        """拒绝审批并保持不可恢复的终态；拒绝不会触发任何工具调用。"""

        authorize_operator(principal)
        current = now or datetime.now(timezone.utc)
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                raise KeyError("审批请求不存在")
            if request.expires_at <= current:
                raise PermissionError("审批请求已过期")
            if request.status is not HITLStatus.PENDING:
                raise PermissionError("审批请求不是 pending 状态")
            rejected = request.model_copy(update={"status": HITLStatus.REJECTED})
            self._requests[approval_id] = rejected
            return rejected.model_copy(deep=True)

    def verify_grant(
        self,
        grant: ApprovalGrant,
        *,
        principal: Principal,
        run_id: str,
        task_id: str,
        plan_version: int,
        plan_digest: str,
        validator_digest: str,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        """恢复前重新检查存储状态、签名、主体、计划和 Validator 摘要。"""

        authorize_operator(principal)
        current = now or datetime.now(timezone.utc)
        if grant.expires_at <= current:
            raise PermissionError("审批票据已过期")
        with self._lock:
            request = self._requests.get(grant.approval_id)
            stored = self._grants.get(grant.approval_id)
            if request is None or stored is None or request.status is not HITLStatus.APPROVED:
                raise PermissionError("审批票据不是存储中的 approved 票据")
            if stored.model_dump(mode="json") != grant.model_dump(mode="json"):
                raise PermissionError("审批票据与存储记录不一致")
            if not hmac.compare_digest(grant.signature, self._sign(grant)):
                raise PermissionError("审批票据签名无效")
            expected = {
                "run_id": run_id,
                "task_id": task_id,
                "plan_version": plan_version,
                "plan_digest": plan_digest,
                "validator_digest": validator_digest,
            }
            actual = {
                "run_id": grant.run_id,
                "task_id": grant.task_id,
                "plan_version": grant.plan_version,
                "plan_digest": grant.plan_digest,
                "validator_digest": grant.validator_digest,
            }
            if actual != expected or grant.request_digest != request.request_digest:
                raise PermissionError("审批票据与当前计划或 Validator 结果不一致")
            return grant.model_copy(deep=True)

    def _sign(self, grant: ApprovalGrant) -> str:
        """签名时排除 signature 自身，避免循环并确保字段全集参与签名。"""

        return sign_approval_grant(grant, self._secret.decode("utf-8"))


class HITLController:
    """把高优先级覆盖、人工接管和写操作统一转成 pending interrupt。"""

    def __init__(
        self,
        store: HITLStoreProtocol,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int = 900,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("HITL ttl_seconds 必须在 1..86400 内")
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl_seconds = ttl_seconds

    def request_interrupt(
        self,
        *,
        run_id: str,
        task_id: str,
        plan_version: int,
        requested_by: str,
        reason_code: HITLReason,
        reason: str,
        checkpoint_id: str,
        plan_digest: str,
        validator_digest: str,
    ) -> HITLInterrupt:
        """先记录 pending 审批，再返回可写入 Checkpoint 的 interrupt。"""

        now = self.clock()
        request = build_hitl_request(
            run_id=run_id,
            task_id=task_id,
            plan_version=plan_version,
            requested_by=requested_by,
            reason_code=reason_code,
            reason=reason,
            checkpoint_id=checkpoint_id,
            plan_digest=plan_digest,
            validator_digest=validator_digest,
            now=now,
            ttl_seconds=self.ttl_seconds,
        )
        stored = self.store.request_approval(request)
        return HITLInterrupt(
            run_id=stored.run_id,
            task_id=stored.task_id,
            approval_id=stored.approval_id,
            checkpoint_id=stored.checkpoint_id,
            reason_code=stored.reason_code,
            created_at=stored.requested_at,
            expires_at=stored.expires_at,
        )


def build_hitl_request(
    *,
    run_id: str,
    task_id: str,
    plan_version: int,
    requested_by: str,
    reason_code: HITLReason,
    reason: str,
    checkpoint_id: str,
    plan_digest: str,
    validator_digest: str,
    now: datetime,
    ttl_seconds: int = 900,
) -> HITLRequest:
    """由运行时统一生成审批请求，避免不同入口遗漏摘要或期限校验。"""

    if isinstance(ttl_seconds, bool) or ttl_seconds <= 0 or ttl_seconds > 86400:
        raise ValueError("HITL ttl_seconds 必须在 1..86400 内")
    approval_id = "approval_" + uuid4().hex
    digest = canonical_hitl_digest(
        {
            "approval_id": approval_id,
            "run_id": run_id,
            "task_id": task_id,
            "plan_version": plan_version,
            "requested_by": requested_by,
            "reason_code": reason_code.value,
            "checkpoint_id": checkpoint_id,
            "plan_digest": plan_digest,
            "validator_digest": validator_digest,
        }
    )
    return HITLRequest(
        approval_id=approval_id,
        run_id=run_id,
        task_id=task_id,
        plan_version=plan_version,
        requested_by=requested_by,
        reason_code=reason_code,
        reason=reason,
        checkpoint_id=checkpoint_id,
        plan_digest=plan_digest,
        validator_digest=validator_digest,
        request_digest=digest,
        requested_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


__all__ = [
    "ApprovalGrant",
    "HITLInterrupt",
    "HITLInterruptError",
    "HITLReason",
    "HITLRequest",
    "HITLStatus",
    "HITLStoreProtocol",
    "HITLController",
    "InMemoryHITLStore",
    "build_hitl_request",
    "canonical_hitl_digest",
    "sign_approval_grant",
]
