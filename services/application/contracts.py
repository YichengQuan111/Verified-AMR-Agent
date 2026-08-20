"""Service 与 Router 之间使用的严格 Pydantic 视图契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.context import PlanTasksOutput
from agent.planning import TaskContract
from agent.tools import UserRole


class ApplicationContract(BaseModel):
    """应用层契约拒绝未知字段，避免 API 响应形状无意漂移。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class RunView(ApplicationContract):
    """运行的关系化索引字段与原始 TaskContract 快照。"""

    run_id: str
    run_kind: Literal["agent", "eval"]
    status: str
    contract_id: str | None
    environment_ref: str | None
    plan_version: int = Field(ge=0)
    current_task_id: str | None
    prompt_id: str | None
    prompt_version: str | None
    context_digest: str | None
    task_contract: TaskContract
    run_state_snapshot: dict[str, JsonValue]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class PlanView(ApplicationContract):
    """一个计划版本及其通过 Pydantic 验证的完整 DAG 快照。"""

    plan_id: str
    run_id: str
    plan_version: int = Field(ge=1)
    status: str
    trigger_observation_id: str | None
    reason: str | None
    plan: PlanTasksOutput
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EventView(ApplicationContract):
    """可用于审计或 SSE 输出的持久化运行事件。"""

    event_id: str
    run_id: str
    sequence_no: int = Field(ge=1)
    event_type: str
    node_name: str | None
    task_id: str | None
    severity: str
    payload: dict[str, JsonValue]
    created_at: AwareDatetime


class ApprovalView(ApplicationContract):
    """人工审批请求与决定结果；不依赖数据库 ORM 对象。"""

    approval_id: str
    run_id: str
    task_id: str | None
    plan_version: int = Field(ge=0)
    status: Literal["pending", "approved", "rejected"]
    required_role: str
    requested_by: str
    decided_by: str | None
    reason: str
    decision_comment: str | None
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None
    expires_at: AwareDatetime | None


class DocumentMetadataInput(ApplicationContract):
    """文档上传时进入 JSONB 快照的完整、无二进制元数据。"""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    role_scope: list[UserRole] = Field(min_length=1)
    source: str = Field(min_length=1, max_length=256)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_roles(self) -> "DocumentMetadataInput":
        """同一角色只保存一次，避免 ACL 查询和审计出现歧义。"""

        if len(self.role_scope) != len(set(self.role_scope)):
            raise ValueError("role_scope 不能包含重复角色")
        return self


class DocumentView(ApplicationContract):
    """文档查询响应；正文留在 Service 内，不在元数据接口中回传。"""

    document_id: str
    filename: str
    content_type: str
    status: str
    version: str
    role_scope: list[UserRole]
    source: str
    checksum: str
    size_bytes: int = Field(gt=0)
    storage_uri: str | None
    metadata: dict[str, JsonValue]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    indexed_at: AwareDatetime | None


class StoredDocument(ApplicationContract):
    """Service 内部读取结果；供 P0-07 索引器取得原始字节。"""

    metadata: DocumentView
    content: bytes


__all__ = [
    "ApplicationContract",
    "ApprovalView",
    "DocumentMetadataInput",
    "DocumentView",
    "EventView",
    "PlanView",
    "RunView",
    "StoredDocument",
]
