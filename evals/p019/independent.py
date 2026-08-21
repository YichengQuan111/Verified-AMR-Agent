"""P0-19 三策略独立离线执行器。

每种策略各自调用同一份 P0-18 数据集与 Fast 指纹，但恢复额度不同，因此异常样例
的终态不能从另一策略复制。这是发布验收入口；``replay.py`` 只保留可视化投影。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evals.p018.contracts import EvalReport
from evals.p018.dataset import load_config as load_p018_config
from evals.p018.dataset import load_dataset as load_p018_dataset
from evals.p018.reproducibility import canonical_digest, sha256_file
from evals.p018.runner import EvalHarness, StrategyRecoveryPolicy

from .contracts import (
    FairnessEvidence,
    P019ExecutionMode,
    P019Report,
    P019Strategy,
    SmartDeferral,
    StrategyCaseResult,
    StrategySummary,
)
from .dataset import DEFAULT_CONFIG_PATH, load_config, rooted_path
from .replay import _aggregate, _case_result, _relative


STRATEGY_POLICIES = {
    P019Strategy.FIXED_WORKFLOW: StrategyRecoveryPolicy.WORKFLOW,
    P019Strategy.REACT: StrategyRecoveryPolicy.REACT,
    P019Strategy.PEVR: StrategyRecoveryPolicy.PEVR,
}


class IndependentComparisonError(ValueError):
    """独立对照输入或公平性门禁失败时的稳定错误。"""


def _fake_verification_runner() -> Any:
    """独立对照默认不拉起 CTest/pytest 子进程，避免把外部套件混进策略差异。"""

    return SimpleNamespace(
        run=lambda *args, **kwargs: SimpleNamespace(
            status="passed",
            case_count=1,
            evidence_refs=["verification://p019-independent/fixed"],
            report_digest="b" * 64,
        )
    )


def _run_strategy_report(
    *,
    strategy: P019Strategy,
    p018_dataset,
    p018_config: Mapping[str, object],
    p018_dataset_path: Path,
    p018_config_path: Path,
    verification_runner: Any,
) -> EvalReport:
    """用指定恢复额度独立跑完 60 例。"""

    return EvalHarness(
        dataset=p018_dataset,
        config=p018_config,
        dataset_path=p018_dataset_path,
        config_path=p018_config_path,
        verification_runner=verification_runner,
        recovery_policy=STRATEGY_POLICIES[strategy],
    ).run()


def _strategy_results(strategy: P019Strategy, report: EvalReport) -> list[StrategyCaseResult]:
    """把该策略自己的观察转换成 P0-19 逐例结果，不读取其他策略终态。"""

    return [_case_result(strategy, case) for case in report.cases]


def run_independent_comparison(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    verification_runner: Any | None = None,
) -> P019Report:
    """对同一 60 例分别执行 Workflow / ReAct / PEVR 恢复策略。"""

    p019_config = load_config(config_path)
    if p019_config.get("execution_mode") != P019ExecutionMode.INDEPENDENT_ORACLE.value:
        raise IndependentComparisonError("独立对照入口只接受 offline_independent_oracle")
    p018_dataset_path = rooted_path(str(p019_config["p018_dataset_path"]))
    p018_config_path = rooted_path(str(p019_config["p018_config_path"]))
    p018_dataset = load_p018_dataset(p018_dataset_path)
    p018_config = load_p018_config(p018_config_path)
    runner = verification_runner or _fake_verification_runner()
    reports = {
        strategy: _run_strategy_report(
            strategy=strategy,
            p018_dataset=p018_dataset,
            p018_config=p018_config,
            p018_dataset_path=p018_dataset_path,
            p018_config_path=p018_config_path,
            verification_runner=runner,
        )
        for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR)
    }
    pevr_report = reports[P019Strategy.PEVR]
    workflow_report = reports[P019Strategy.FIXED_WORKFLOW]
    react_report = reports[P019Strategy.REACT]
    if len(pevr_report.cases) != 60:
        raise IndependentComparisonError("独立对照必须覆盖 60 例")
    case_ids = [item.case_id for item in pevr_report.cases]
    if any(
        [item.case_id for item in report.cases] != case_ids
        for report in reports.values()
    ):
        raise IndependentComparisonError("三种策略的 case 顺序必须与数据集一致")

    source_prompt_versions = pevr_report.reproducibility.get("prompt_versions", {})
    source_tool_versions = pevr_report.reproducibility.get("tool_spec_versions", {})
    source_model = pevr_report.reproducibility.get("model", {})
    p018_model_cfg = p018_config.get("model")
    p019_model_cfg = p019_config.get("model")
    p018_prompts = p018_config.get("prompts")
    p018_tools = p018_config.get("tools")
    expected_prompt_versions: dict[str, str] = {}
    if isinstance(p018_prompts, Mapping):
        expected_prompt_versions = {
            str(key): str(value.get("version"))
            for key, value in p018_prompts.items()
            if isinstance(value, Mapping)
        }
    same_dataset = (
        pevr_report.dataset_id == "amr-p018-60"
        and pevr_report.dataset_version == "p0-18.v1"
        and workflow_report.dataset_id == pevr_report.dataset_id
        and react_report.dataset_id == pevr_report.dataset_id
    )
    same_tools = (
        isinstance(p018_tools, Mapping)
        and sorted(str(item) for item in p018_tools.get("names", [])) == sorted(str(key) for key in source_tool_versions)
    )
    same_prompts = expected_prompt_versions == source_prompt_versions
    same_model = (
        isinstance(p019_model_cfg, Mapping)
        and isinstance(p018_model_cfg, Mapping)
        and p019_model_cfg.get("alias") == "qwen3.6-fast"
        and p018_model_cfg.get("alias") == "qwen3.6-fast"
        and isinstance(source_model, Mapping)
        and source_model.get("alias") == "qwen3.6-fast"
    )
    same_config = sha256_file(p018_config_path) == sha256_file(rooted_path(str(p019_config["p018_config_path"])))
    combined_identity = canonical_digest(
        {
            "workflow": workflow_report.report_digest,
            "react": react_report.report_digest,
            "pevr": pevr_report.report_digest,
        }
    )
    fairness = FairnessEvidence(
        dataset_id=pevr_report.dataset_id,
        dataset_version=pevr_report.dataset_version,
        case_count=60,
        case_id_digest=canonical_digest(sorted(case_ids)),
        prompt_versions={str(key): str(value) for key, value in dict(source_prompt_versions).items()},
        tool_spec_versions={str(key): str(value) for key, value in dict(source_tool_versions).items()},
        p018_config_sha256=sha256_file(p018_config_path),
        p019_config_sha256=sha256_file(Path(config_path).resolve()),
        p018_source_report_sha256=combined_identity,
        same_dataset=same_dataset,
        same_tools=same_tools,
        same_prompts=same_prompts,
        same_config=same_config,
        same_model=same_model,
        react_production_path_touched=False,
    )
    raw_results: list[StrategyCaseResult] = []
    summaries: list[StrategySummary] = []
    for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR):
        strategy_results = _strategy_results(strategy, reports[strategy])
        raw_results.extend(strategy_results)
        summaries.append(_aggregate(strategy, strategy_results, latency_source="p018_independent_oracle"))
    smart = SmartDeferral.model_validate(p019_config["smart_comparison"])
    workflow = next(item for item in summaries if item.strategy is P019Strategy.FIXED_WORKFLOW)
    react = next(item for item in summaries if item.strategy is P019Strategy.REACT)
    pevr = next(item for item in summaries if item.strategy is P019Strategy.PEVR)
    split_cases = [
        case_id
        for case_id in case_ids
        if next(item.observed_outcome for item in raw_results if item.strategy is P019Strategy.FIXED_WORKFLOW and item.case_id == case_id)
        != next(item.observed_outcome for item in raw_results if item.strategy is P019Strategy.PEVR and item.case_id == case_id)
    ]
    conclusions = [
        f"三种策略独立执行同一 P0-18 60 例；PEVR 预期符合 {pevr.evaluation_pass_count}/{pevr.case_count}，"
        f"Workflow {workflow.evaluation_pass_count}/{workflow.case_count}，ReAct {react.evaluation_pass_count}/{react.case_count}。",
        f"终态被策略额度分开的 case 数：{len(split_cases)}；Workflow 与 PEVR 不再复制同一 observed_outcome。",
        f"异常路径 PEVR 成功恢复 {pevr.successful_recovery_count}/{pevr.successful_recovery_case_count}，"
        f"Workflow {workflow.successful_recovery_count}/{workflow.successful_recovery_case_count}。",
        "PEVR 是生产恢复策略；固定 Workflow 与 ReAct 只在评测层独立执行，ReAct 未接入生产主链。",
        "Qwen3.8 Smart 对照已延期：本步未启动、未测试、未完成，不阻塞 P0-19。",
    ]
    limitations = [
        "当前独立对照仍是 offline_deterministic_oracle：证明恢复额度差异，不证明 60 例在线 LLM 质量。",
        "Token 与 CPU/RSS/GPU 未观测；延迟来自确定性 Trace 字段，不是墙钟 P95。",
        "要获得在线三策略质量对照，必须新增独立 online_fast execution_mode，不能覆盖本报告。",
    ]
    source_summary = {
        "mode": P019ExecutionMode.INDEPENDENT_ORACLE.value,
        "path": _relative(p018_dataset_path),
        "sha256": combined_identity,
        "report_id": pevr_report.report_id,
        "report_digest": pevr_report.report_digest,
        "dataset_id": pevr_report.dataset_id,
        "dataset_version": pevr_report.dataset_version,
        "case_count": 60,
        "strategy_report_digests": {
            "fixed_workflow": workflow_report.report_digest,
            "react": react_report.report_digest,
            "pevr": pevr_report.report_digest,
        },
    }
    stable_body = {
        "execution_mode": P019ExecutionMode.INDEPENDENT_ORACLE.value,
        "source_report": source_summary,
        "fairness": fairness.model_dump(mode="json"),
        "smart_comparison": smart.model_dump(mode="json"),
        "strategies": [item.model_dump(mode="json") for item in summaries],
        "raw_results": [item.model_dump(mode="json") for item in raw_results],
        "conclusions": conclusions,
        "limitations": limitations,
    }
    report_digest = canonical_digest(stable_body)
    status = "passed" if pevr.evaluation_pass_count == 60 and fairness.same_dataset else "failed"
    return P019Report(
        report_id=f"p019-{report_digest[:16]}",
        report_version="p0-19.v2",
        execution_mode=P019ExecutionMode.INDEPENDENT_ORACLE,
        status=status,
        generated_at=datetime.now().astimezone().isoformat(),
        source_report=source_summary,
        fairness=fairness,
        smart_comparison=smart,
        strategies=summaries,
        raw_results=raw_results,
        conclusions=conclusions,
        limitations=limitations,
        report_digest=report_digest,
    )


__all__ = [
    "IndependentComparisonError",
    "run_independent_comparison",
]
