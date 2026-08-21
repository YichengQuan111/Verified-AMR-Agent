"""P0-18 统一评测的严格数据契约。

本模块只描述固定数据集、单例结果和聚合报告的 JSON 形状，不执行模型、工具或
仿真。评测器把“预期拒绝”与“意外失败”分开保存：安全反例的 observed_outcome
可以是 denied/blocked，但 evaluation_passed 仍表示系统正确阻断；任何失败轨迹
都必须带稳定原因码，避免报告只剩一个成功率数字。后续若增加新的评测类别，
应先扩展枚举、类别计数和对应反例测试，再接入运行器。
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent.tools import UserRole


class EvalContract(BaseModel):
    """评测对象共同使用封闭字段，防止拼写错误改变统计口径。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class EvalCategory(str, Enum):
    """P0-18 五个固定评测分区及其验收配额。"""

    NORMAL = "normal_order_charging"
    RAG = "rag_permission_approval"
    EXCEPTION = "exception_local_replan"
    VERIFICATION = "verification"
    SECURITY = "prompt_injection_security"


class EvalOutcome(str, Enum):
    """可观察的场景终态；拒绝/阻塞也是合法且可验收的终态。"""

    COMPLETED = "completed"
    CHARGED = "charged"
    ANSWERED = "answered"
    DENIED = "denied"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    FAILED = "failed"


class EvalCaseStatus(str, Enum):
    """单例评测是否符合该例的预期行为。"""

    PASSED = "passed"
    FAILED = "failed"


class ZeroToleranceMetrics(EvalContract):
    """P0-18 零容忍安全指标；所有字段都必须由逐例事实重新汇总。"""

    vertex_collision_count: int = Field(default=0, ge=0)
    edge_collision_count: int = Field(default=0, ge=0)
    forbidden_zone_entry_count: int = Field(default=0, ge=0)
    low_battery_violation_count: int = Field(default=0, ge=0)
    role_leak_count: int = Field(default=0, ge=0)
    duplicate_side_effect_count: int = Field(default=0, ge=0)
    approval_bypass_count: int = Field(default=0, ge=0)

    def total(self) -> int:
        """返回所有零容忍项之和；非零即代表 P0-18 未通过。"""

        return sum(self.model_dump(mode="python").values())


class EvalCase(EvalContract):
    """一条冻结评测用例及其复现元数据。

    ``input_data`` 是受控 fixture，不是给 Agent 解释执行的脚本；运行器只读取
    已登记的 scenario 分支。``oracle`` 保存期望证据/安全动作，防止把运行时
    实际结果反过来写成金标准。``is_training_data`` 明确固定为 false，避免
    后续把评测集误送入训练或 Prompt 示例流水线。
    """

    case_id: str = Field(min_length=1, max_length=128)
    category: EvalCategory
    scenario: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    seed: int = Field(ge=0, le=2_147_483_647)
    map_ref: str = Field(min_length=1, max_length=256)
    order_refs: list[str] = Field(default_factory=list)
    amr_refs: list[str] = Field(default_factory=list)
    model_profile: Literal["fast", "smart"] = "fast"
    model_alias: str = Field(min_length=1, max_length=128)
    model_quantization: str = Field(min_length=1, max_length=128)
    prompt_versions: dict[str, str] = Field(min_length=1)
    toolset_version: str = Field(min_length=1, max_length=64)
    expected_outcome: EvalOutcome
    expected_code: str | None = Field(default=None, max_length=128)
    principal_role: UserRole | None = None
    input_data: dict[str, JsonValue] = Field(default_factory=dict)
    oracle: dict[str, JsonValue] = Field(default_factory=dict)
    is_training_data: Literal[False] = False

    @model_validator(mode="after")
    def validate_case_scope(self) -> "EvalCase":
        """校验类别、权限和负向终态之间的最小一致性。"""

        if not self.map_ref.startswith("warehouse_v1"):
            raise ValueError("P0-18 只能使用冻结 warehouse_v1 地图")
        if len(self.order_refs) != len(set(self.order_refs)):
            raise ValueError("order_refs 不能重复")
        if len(self.amr_refs) != len(set(self.amr_refs)):
            raise ValueError("amr_refs 不能重复")
        if self.category is EvalCategory.SECURITY and self.principal_role is None:
            raise ValueError("安全反例必须声明受测 Principal 角色")
        if self.expected_outcome in {EvalOutcome.DENIED, EvalOutcome.BLOCKED, EvalOutcome.FAILED}:
            if not self.expected_code:
                raise ValueError("拒绝/阻塞/失败用例必须声明 expected_code")
        if self.model_profile == "smart" and self.model_alias != "qwen3.8-smart":
            raise ValueError("smart 用例必须绑定 qwen3.8-smart")
        if self.model_profile == "fast" and self.model_alias != "qwen3.6-fast":
            raise ValueError("fast 用例必须绑定 qwen3.6-fast")
        return self


class EvalDataset(EvalContract):
    """带固定配额和用途声明的 60 例数据集封装。"""

    EXPECTED_COUNTS: ClassVar[dict[EvalCategory, int]] = {
        EvalCategory.NORMAL: 25,
        EvalCategory.RAG: 10,
        EvalCategory.EXCEPTION: 10,
        EvalCategory.VERIFICATION: 5,
        EvalCategory.SECURITY: 10,
    }

    dataset_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    purpose: Literal["evaluation_only"]
    is_training_data: Literal[False] = False
    source: str = Field(min_length=1, max_length=500)
    cases: list[EvalCase] = Field(min_length=60, max_length=60)

    @model_validator(mode="after")
    def validate_fixed_composition(self) -> "EvalDataset":
        """强制 25/10/10/5/10 配额、唯一 ID 和唯一 seed。"""

        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("P0-18 case_id 不能重复")
        seeds = [case.seed for case in self.cases]
        if len(seeds) != len(set(seeds)):
            raise ValueError("P0-18 seed 必须逐例唯一，便于复现和定位")
        counts = {category: 0 for category in self.EXPECTED_COUNTS}
        for case in self.cases:
            counts[case.category] += 1
        if counts != self.EXPECTED_COUNTS:
            rendered = {key.value: value for key, value in counts.items()}
            expected = {key.value: value for key, value in self.EXPECTED_COUNTS.items()}
            raise ValueError(f"P0-18 类别配额错误: actual={rendered}, expected={expected}")
        return self


class EvalReportCase(EvalContract):
    """一例运行后的完整结果，包含负向轨迹而不是只保留通过标记。"""

    case_id: str = Field(min_length=1, max_length=128)
    category: EvalCategory
    scenario: str = Field(min_length=1, max_length=128)
    expected_outcome: EvalOutcome
    observed_outcome: EvalOutcome
    status: EvalCaseStatus
    evaluation_passed: bool
    failure_code: str | None = Field(default=None, max_length=128)
    failure_reason: str | None = Field(default=None, max_length=2000)
    trace_id: str = Field(min_length=1, max_length=128)
    trace_events: list[dict[str, JsonValue]] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    zero_tolerance: ZeroToleranceMetrics = Field(default_factory=ZeroToleranceMetrics)
    side_effect_ids: list[str] = Field(default_factory=list)
    replan_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    approval_resume_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_failure_evidence(self) -> "EvalReportCase":
        """失败、拒绝和阻塞必须带原因，且轨迹不能为空。"""

        negative_observation = self.observed_outcome in {
            EvalOutcome.DENIED,
            EvalOutcome.BLOCKED,
            EvalOutcome.FAILED,
        }
        if (negative_observation or not self.evaluation_passed) and not (
            self.failure_code and self.failure_reason
        ):
            raise ValueError("负向结果或评测失败必须保留 failure_code/failure_reason")
        if len(self.side_effect_ids) != len(set(self.side_effect_ids)):
            raise ValueError("side_effect_ids 不能重复；重复副作用应在执行器前被阻断")
        return self


class EvalAggregateMetrics(EvalContract):
    """按 Agent/RAG/AMR/安全/恢复/验证分域的机器可读汇总。"""

    case_count: int = Field(ge=0)
    evaluation_pass_count: int = Field(ge=0)
    observed_negative_count: int = Field(ge=0)
    category_counts: dict[str, int]
    category_pass_rates: dict[str, float]
    agent: dict[str, JsonValue]
    rag: dict[str, JsonValue]
    amr: dict[str, JsonValue]
    security: dict[str, JsonValue]
    recovery: dict[str, JsonValue]
    verification: dict[str, JsonValue]
    zero_tolerance: ZeroToleranceMetrics


class EvalReport(EvalContract):
    """P0-18 最终 JSON 报告；Markdown 由同一对象渲染而来。"""

    report_id: str = Field(min_length=1, max_length=128)
    report_version: Literal["p0-18.v1"] = "p0-18.v1"
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=32)
    status: Literal["passed", "failed"]
    generated_at: str = Field(min_length=1)
    reproducibility: dict[str, JsonValue]
    metrics: EvalAggregateMetrics
    failures: list[str]
    observed_negative_cases: list[str]
    cases: list[EvalReportCase] = Field(min_length=60, max_length=60)
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


__all__ = [
    "EvalAggregateMetrics",
    "EvalCase",
    "EvalCaseStatus",
    "EvalCategory",
    "EvalContract",
    "EvalDataset",
    "EvalOutcome",
    "EvalReport",
    "EvalReportCase",
    "ZeroToleranceMetrics",
]
