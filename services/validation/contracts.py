"""P0-17 验证日志、证据位置和报告使用的严格数据契约。

验证结论必须来自固定入口的真实退出码和其输出日志；本模块只保存已经解析的
结构化事实，不接受命令、脚本或路径字段，避免报告层再次形成任意执行面。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationContract(BaseModel):
    """验证层所有模型的共同边界；未知字段必须在解析入口被拒绝。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class VerificationFailureType(str, Enum):
    """日志中可稳定识别的失败类型；未知失败保持 ``unknown`` 而不猜测。"""

    NONE = "none"
    ASSERTION = "assertion"
    SCHEMA = "schema"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    UNSAFE_PLAN = "unsafe_plan"
    SIMULATION_BLOCKED = "simulation_blocked"
    TOOL_ERROR = "tool_error"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class VerificationEvidenceLocation(ValidationContract):
    """一条可跳回原始 stdout/stderr 或仿真快照的证据位置。"""

    source: Literal["stdout", "stderr", "simulation", "metadata"]
    line: int | None = Field(default=None, ge=1)
    citation: str = Field(min_length=1, max_length=512)
    excerpt: str = Field(min_length=1, max_length=512)


class ParsedVerificationCase(ValidationContract):
    """日志解析后的单个 case 事实，供工具输出和报告共同复用。"""

    case_id: str = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "timeout"]
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_type: VerificationFailureType = VerificationFailureType.NONE
    task_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=64)
    parameters_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_locations: list[VerificationEvidenceLocation] = Field(default_factory=list)
    summary: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def validate_execution_fact(self) -> "ParsedVerificationCase":
        """让 case 状态只能由退出码/timeout 事实支持，拒绝伪造通过结论。"""

        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed case 必须来自 exit_code=0")
        if self.status == "failed" and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("failed case 必须来自非零 exit_code")
        if self.status == "timeout" and self.exit_code is not None:
            raise ValueError("timeout case 的 exit_code 必须为 null")
        if self.status == "passed" and self.failure_type is not VerificationFailureType.NONE:
            raise ValueError("passed case 不能携带失败类型")
        if self.status == "timeout" and self.failure_type is not VerificationFailureType.TIMEOUT:
            raise ValueError("timeout case 必须使用 timeout 失败类型")
        if self.status == "failed" and self.failure_type is VerificationFailureType.NONE:
            raise ValueError("failed case 必须携带失败类型")
        return self


class VerificationReport(ValidationContract):
    """由逐 case 真实结果汇总出的机器可读报告，不接受外部结论字段。"""

    schema_version: Literal["1.0"] = "1.0"
    report_id: str = Field(min_length=1, max_length=128)
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=128)
    status: Literal["passed", "failed", "timeout"]
    case_count: int = Field(gt=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    cases: list[ParsedVerificationCase] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_fact(self) -> "VerificationReport":
        """报告计数和结论必须能由逐 case 的真实执行状态重算。"""

        passed = sum(item.status == "passed" for item in self.cases)
        failed = sum(item.status == "failed" for item in self.cases)
        timed_out = sum(item.status == "timeout" for item in self.cases)
        expected_status = "timeout" if timed_out else "failed" if failed else "passed"
        if self.case_count != len(self.cases):
            raise ValueError("report.case_count 必须等于 cases 数量")
        if (self.passed_count, self.failed_count, self.timeout_count) != (passed, failed, timed_out):
            raise ValueError("report 计数与逐 case 状态不一致")
        if self.status != expected_status:
            raise ValueError("report.status 不能脱离逐 case 真实结果")
        return self


__all__ = [
    "ParsedVerificationCase",
    "ValidationContract",
    "VerificationEvidenceLocation",
    "VerificationFailureType",
    "VerificationReport",
]
