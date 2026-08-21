"""P0-16 PostgreSQL HITL 审批适配器。

审批请求和签名票据复用 P0-06 已迁移的 approvals 表，不另造绕过事务边界的
进程内状态。request_snapshot 保存完整 HITL 契约与 grant，审批决定在行锁内
完成；PEVR 恢复时再次读取数据库并核对签名、计划摘要和 Validator 摘要。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hmac
from typing import Mapping

from sqlalchemy.exc import IntegrityError

from agent.runtime.hitl import (
    ApprovalGrant,
    HITLRequest,
    HITLStatus,
    HITLStoreProtocol,
    sign_approval_grant,
)
from agent.security import Principal, authorize_operator
from services.application.exceptions import PersistenceConflictError
from services.persistence import ApprovalRecord, ApprovalRepository, SessionFactory


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """为生产审批使用带时区时间，测试可注入固定时钟。"""

    return datetime.now(timezone.utc)


class PostgresHITLStore(HITLStoreProtocol):
    """以 approvals 表提供可跨进程恢复的 HITL 存储。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        signing_secret: str,
        clock: Clock = _utc_now,
    ) -> None:
        if not isinstance(signing_secret, str) or len(signing_secret) < 32:
            raise ValueError("HITL signing_secret 至少需要 32 个字符")
        self._session_factory = session_factory
        self._secret = signing_secret
        self._clock = clock

    def request_approval(self, request: HITLRequest) -> HITLRequest:
        """幂等创建 pending 行；相同 ID 若绑定不同事实则拒绝覆盖。"""

        try:
            with self._session_factory() as session:
                with session.begin():
                    repository = ApprovalRepository(session)
                    previous = repository.get(request.approval_id, for_update=True)
                    if previous is not None:
                        existing = self._request_from_record(previous)
                        if (
                            existing.model_dump(mode="json", exclude={"status"})
                            != request.model_dump(mode="json", exclude={"status"})
                        ):
                            raise ValueError("approval_id 已绑定另一组审批事实")
                        return existing
                    record = ApprovalRecord(
                        approval_id=request.approval_id,
                        run_id=request.run_id,
                        task_record_id=None,
                        task_id=request.task_id,
                        plan_version=request.plan_version,
                        status=HITLStatus.PENDING.value,
                        required_role=request.required_role.value,
                        requested_by=request.requested_by,
                        decided_by=None,
                        reason=request.reason,
                        decision_comment=None,
                        request_snapshot={
                            "hitl_request": request.model_dump(mode="json"),
                            "grant": None,
                        },
                        requested_at=request.requested_at,
                        decided_at=None,
                        expires_at=request.expires_at,
                    )
                    repository.add(record)
                    session.flush()
                    return request.model_copy(deep=True)
        except IntegrityError as exc:
            raise PersistenceConflictError("HITL 审批请求写入冲突") from exc

    request = request_approval

    def get_request(self, approval_id: str) -> HITLRequest | None:
        """读取审批生命周期，供恢复入口区分 rejected/expired 与 pending。"""

        with self._session_factory() as session:
            record = ApprovalRepository(session).get(approval_id)
            return self._request_from_record(record) if record is not None else None

    def approve(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        """锁定 pending 行并由真实 operator 签发一次批准票据。"""

        authorize_operator(principal)
        current = now or self._clock()
        with self._session_factory() as session:
            with session.begin():
                repository = ApprovalRepository(session)
                record = repository.get(approval_id, for_update=True)
                if record is None:
                    raise KeyError("审批请求不存在")
                if record.status == HITLStatus.APPROVED.value:
                    grant = self._grant_from_record(record)
                    if grant is None or grant.approved_by != principal.subject:
                        raise PermissionError("审批已经由另一主体决定")
                    return grant
                if record.status != HITLStatus.PENDING.value:
                    raise PermissionError("审批请求不是 pending 状态")
                if record.expires_at is not None and record.expires_at <= current:
                    record.status = HITLStatus.EXPIRED.value
                    session.flush()
                    raise PermissionError("审批请求已过期")
                request = self._request_from_record(record)
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
                grant = grant.model_copy(
                    update={"signature": sign_approval_grant(grant, self._secret)}
                )
                record.status = HITLStatus.APPROVED.value
                record.decided_by = principal.subject
                record.decided_at = current
                record.request_snapshot = {
                    "hitl_request": request.model_dump(mode="json"),
                    "grant": grant.model_dump(mode="json"),
                }
                session.flush()
                return grant.model_copy(deep=True)

    def reject(
        self,
        approval_id: str,
        *,
        principal: Principal,
        now: datetime | None = None,
    ) -> HITLRequest:
        """锁定 pending 行并记录不可恢复的 rejected 终态。"""

        authorize_operator(principal)
        current = now or self._clock()
        with self._session_factory() as session:
            with session.begin():
                repository = ApprovalRepository(session)
                record = repository.get(approval_id, for_update=True)
                if record is None:
                    raise KeyError("审批请求不存在")
                if record.status != HITLStatus.PENDING.value:
                    raise PermissionError("审批请求不是 pending 状态")
                if record.expires_at is not None and record.expires_at <= current:
                    record.status = HITLStatus.EXPIRED.value
                    session.flush()
                    raise PermissionError("审批请求已过期")
                request = self._request_from_record(record)
                record.status = HITLStatus.REJECTED.value
                record.decided_by = principal.subject
                record.decided_at = current
                record.request_snapshot = {
                    "hitl_request": request.model_dump(mode="json"),
                    "grant": None,
                }
                session.flush()
                return request.model_copy(update={"status": HITLStatus.REJECTED})

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
        """恢复前从数据库重读并拒绝脱离审批事实的票据。"""

        authorize_operator(principal)
        current = now or self._clock()
        with self._session_factory() as session:
            record = ApprovalRepository(session).get(grant.approval_id)
            if record is None or record.status != HITLStatus.APPROVED.value:
                raise PermissionError("审批票据不是数据库中的 approved 票据")
            stored = self._grant_from_record(record)
            if stored is None or stored.model_dump(mode="json") != grant.model_dump(mode="json"):
                raise PermissionError("审批票据与数据库记录不一致")
            if stored.expires_at <= current:
                raise PermissionError("审批票据已过期")
            if not hmac.compare_digest(stored.signature, sign_approval_grant(stored, self._secret)):
                raise PermissionError("审批票据签名无效")
            if (
                stored.run_id != run_id
                or stored.task_id != task_id
                or stored.plan_version != plan_version
                or stored.plan_digest != plan_digest
                or stored.validator_digest != validator_digest
            ):
                raise PermissionError("审批票据与当前计划或 Validator 结果不一致")
            return stored.model_copy(deep=True)

    @staticmethod
    def _request_from_record(record: ApprovalRecord) -> HITLRequest:
        """把 JSONB 中的请求重新走 Pydantic 校验，再映射数据库状态。"""

        snapshot = record.request_snapshot
        payload = snapshot.get("hitl_request", snapshot)
        if not isinstance(payload, Mapping):
            raise ValueError("approvals.request_snapshot 缺少 hitl_request")
        request = HITLRequest.model_validate(payload)
        try:
            status = HITLStatus(record.status)
        except ValueError as exc:
            raise ValueError("数据库审批状态非法") from exc
        return request.model_copy(update={"status": status})

    @staticmethod
    def _grant_from_record(record: ApprovalRecord) -> ApprovalGrant | None:
        """只接受 snapshot 中完整的 grant，不从 decided_by 等零散字段拼票据。"""

        payload = record.request_snapshot.get("grant")
        if not isinstance(payload, Mapping):
            return None
        return ApprovalGrant.model_validate(payload)


__all__ = ["PostgresHITLStore"]
