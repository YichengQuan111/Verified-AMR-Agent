"""P0-06 HTTP 请求体契约；响应直接复用 Service 视图。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.planning import TaskContract


class ApiRequest(BaseModel):
    """所有 API 请求都拒绝未声明字段。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class CreateRunRequest(ApiRequest):
    """用已经通过 P0-04 校验的 TaskContract 创建持久化运行。"""

    task_contract: TaskContract
    prompt_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=32)
    context_digest: str | None = Field(default=None, min_length=1, max_length=64)
    run_state_snapshot: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_prompt_identity(self) -> "CreateRunRequest":
        """Prompt ID 与版本必须成对出现，避免无法复现的半份审计信息。"""

        if (self.prompt_id is None) != (self.prompt_version is None):
            raise ValueError("prompt_id 与 prompt_version 必须同时提供或同时省略")
        return self


class ApprovalDecisionRequest(ApiRequest):
    """人工审批决定；P0 只接受明确批准或拒绝。"""

    decision: Literal["approved", "rejected"]
    # 兼容旧客户端的可选回显字段；服务端始终以验签 Principal.subject 为准，
    # 不允许它冒充另一个审批人。
    decided_by: str | None = Field(default=None, min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=2000)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreateEvalRunRequest(ApiRequest):
    """创建评测类型运行，但不在 P0-06 内执行评测套件。"""

    task_contract: TaskContract
    suite_id: str = Field(min_length=1, max_length=128)
    case_ids: list[str] = Field(min_length=1)
    # 保留旧客户端字段但不把它作为身份来源；路由使用签名 Principal.subject。
    requested_by: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_case_ids(self) -> "CreateEvalRunRequest":
        """同一评测用例不能在一个运行中重复。"""

        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("case_ids 不能重复")
        return self


__all__ = [
    "ApprovalDecisionRequest",
    "ApiRequest",
    "CreateEvalRunRequest",
    "CreateRunRequest",
]
