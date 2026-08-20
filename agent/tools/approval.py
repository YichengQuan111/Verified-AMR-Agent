"""审批请求的确定性内存封装。

P0-12 只负责创建 pending 审批请求，不能自动批准、执行 Shell/SQL 或修改底盘。
请求以业务字段 hash 作为稳定 approval/effect ID；同一输入重复请求返回同一条
记录，避免重试制造多个待审批事项。P0-06/P0-16 可在不改变工具契约的前提下
把协议实现替换为 PostgreSQL/HITL 存储。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import RLock

from agent.tools.schemas import ApprovalRequestOutput, RequestApprovalInput


class ApprovalStoreProtocol:
    """审批存储的最小同步接口。"""

    def request(self, payload: RequestApprovalInput) -> ApprovalRequestOutput:  # pragma: no cover - protocol body
        raise NotImplementedError


class InMemoryApprovalStore(ApprovalStoreProtocol):
    """P0-12 默认审批存储；不把 pending 请求误当作批准结果。"""

    def __init__(self) -> None:
        self._values: dict[str, ApprovalRequestOutput] = {}
        self._lock = RLock()

    def request(self, payload: RequestApprovalInput) -> ApprovalRequestOutput:
        """按规范化请求 hash 幂等创建 pending 记录。"""

        canonical = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = sha256(canonical.encode("utf-8")).hexdigest()
        approval_id = f"approval-{digest[:24]}"
        effect_id = f"effect-{digest[:24]}"
        with self._lock:
            existing = self._values.get(approval_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            created_at = datetime.now(timezone.utc)
            result = ApprovalRequestOutput(
                approval_id=approval_id,
                effect_id=effect_id,
                run_id=payload.run_id,
                task_id=payload.task_id,
                reason=payload.reason,
                status="pending",
                requested_at=created_at,
                expires_at=payload.expires_at,
            )
            self._values[approval_id] = result
            return result.model_copy(deep=True)


__all__ = ["ApprovalStoreProtocol", "InMemoryApprovalStore"]
