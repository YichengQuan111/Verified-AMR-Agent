"""P0-19 原始轨迹、汇总表和结论报告渲染器。

JSON 是完整机器复核接口，包含 180 条策略结果、P0-18 原始 Trace 和所有可观测性
标记；JSONL 便于逐行审阅。Markdown 只投影同一个 ``P019Report``，不重新计算或
删除负向案例，避免展示层改变实验结论。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .contracts import P019Report, P019Strategy, StrategySummary
from .independent import run_independent_comparison
from .online import run_online_comparison
from .replay import run_comparison


def report_to_json(report: P019Report) -> str:
    """以稳定缩进输出完整 P0-19 报告。"""

    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def raw_results_to_jsonl(report: P019Report) -> str:
    """一行一个策略-case，保留完整源 Trace 和策略投影。"""

    return "".join(
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        for item in report.raw_results
    )


def _md(value: Any) -> str:
    """把不可信摘要压平成 Markdown 单元格，避免日志破坏表格。"""

    return re.sub(r"[\r\n|]", " ", str(value)).replace("`", "'")


def _rate(numerator: int, denominator: int) -> str:
    """统一显示计数和比例，保留零分母语义。"""

    if denominator == 0:
        return "N/A"
    return f"{numerator}/{denominator} ({numerator / denominator:.2%})"


def _token_text(summary: StrategySummary) -> str:
    """Token 未观测时显示 N/A，而不是把 0 写成资源结论。"""

    token = summary.token_usage
    return f"{token.total_tokens}（未观测）" if not token.observed else str(token.total_tokens)


def _resource_text(summary: StrategySummary) -> str:
    """资源未观测时明确原因。"""

    resource = summary.resource
    if not resource.observed:
        return "未观测"
    return f"RSS峰值 {resource.peak_rss_mb} MB / GPU峰值 {resource.peak_gpu_memory_mb} MB"


def report_to_markdown(report: P019Report) -> str:
    """生成包含公平性、三策略汇总、原始证据和 Smart 状态的 Markdown。"""

    summaries = {item.strategy: item for item in report.strategies}
    online_mode = report.execution_mode.value == "online_fast_three_strategy_closed_loop"
    if online_mode:
        model_note = "本次真实在线调用，同一 Fast 制品身份经三套 Harness 分别预检"
        evidence_note = "三策略各自的真实在线 Trace 与实际控制事件"
        # 在线模式没有单一源报告文件，path 指向固定数据集；必须配对 dataset_sha256，
        # 不能把三份策略报告的组合身份误标成数据集文件哈希。
        source_label = "P0-18 固定数据集"
        source_sha256 = report.source_report.get("dataset_sha256")
    elif report.execution_mode.value == "offline_independent_oracle":
        model_note = "独立离线对照"
        evidence_note = "三策略各自的离线 Trace"
        source_label = "P0-18 源报告"
        source_sha256 = report.source_report.get("sha256")
    else:
        model_note = "本次 replay 不新增在线模型调用"
        evidence_note = "同源 Trace 与派生控制投影"
        source_label = "P0-18 源报告"
        source_sha256 = report.source_report.get("sha256")
    lines = [
        "# P0-19：策略对照实验报告",
        "",
        f"- 报告：`{report.report_id}`；状态：**{report.status}**；版本：`{report.report_version}`",
        f"- 执行模式：`{report.execution_mode.value}`；身份：`{_md(report.source_report.get('report_id'))}` / `{_md(report.source_report.get('report_digest'))}`",
        f"- 模型：`qwen3.6-fast`（{model_note}）",
        "",
        "## 公平性门禁",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| 同一数据集/60例 | `{report.fairness.same_dataset}`；`{report.fairness.case_id_digest}` |",
        f"| 同一共享前置契约 | `{report.fairness.same_shared_context_contract}` |",
        f"| 同一初次 Retrieve | `{report.fairness.same_initial_retrieval}` |",
        f"| 同一 ToolSpec 版本 | `{report.fairness.same_tools}`；`{_md(report.fairness.tool_spec_versions)}` |",
        f"| 同一安全门禁 | `{report.fairness.same_safety_gates}` |",
        f"| 同一预算包络 | `{report.fairness.same_budget_envelope}` |",
        f"| 控制 Prompt 是否相同 | `{report.fairness.same_prompts}`；策略 Prompt=`{_md(report.fairness.strategy_prompt_versions or report.fairness.prompt_versions)}` |",
        f"| 同一 P0-18 配置 | `{report.fairness.same_config}`；SHA-256=`{report.fairness.p018_config_sha256}` |",
        f"| P0-19 策略配置 | `{report.report_version}`；SHA-256=`{report.fairness.p019_config_sha256}` |",
        f"| 同一 Qwen3.6 Fast | `{report.fairness.same_model}`；`qwen3.6-fast` |",
        f"| ReAct 是否调用 PEVR 图 | `{report.fairness.react_uses_pevr_runner}` |",
        f"| ReAct 是否触碰生产主链 | `{report.fairness.react_production_path_touched}` |",
        "",
        "## 汇总表",
        "",
        "任务完成率按固定数据集的正向 case 计算；全例预期符合率也包含正确 denied/blocked 的负向 case。计划合法率只统计实际出现 plan/validate 证据的 case；异常恢复率覆盖固定异常集，成功恢复率只表示最终完成的异常路径。",
        "",
        "| 策略 | 全例预期符合率 | 任务完成率 | 计划合法率 | 异常终止正确率 | 成功重规划率 | 工具错误/意外 | 步数均值/P95 | Token | Trace 延迟 P95 | 资源 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR):
        summary = summaries[strategy]
        lines.append(
            "| {name} | {accuracy} | {completion} | {plan} | {recovery} | {successful} | {errors} | {mean:.2f}/{p95:.2f} | {tokens} | {latency:.2f} ms | {resource} |".format(
                name=strategy.value,
                accuracy=_rate(summary.evaluation_pass_count, summary.case_count),
                completion=_rate(summary.task_completion_count, summary.positive_case_count),
                plan=_rate(summary.plan_legal_count, summary.plan_evaluated_count),
                recovery=_rate(summary.recovery_terminal_correct_count, summary.recovery_case_count),
                successful=_rate(summary.successful_recovery_count, summary.successful_recovery_case_count),
                errors=f"{summary.tool_error_count}/{summary.unexpected_tool_error_count}",
                mean=summary.step_count_mean,
                p95=summary.step_count_p95,
                tokens=_token_text(summary),
                latency=summary.latency.p95_case_ms,
                resource=_resource_text(summary),
            )
        )
    pevr = summaries[P019Strategy.PEVR]
    react = summaries[P019Strategy.REACT]
    lines.extend(
        [
            "",
            "## 零容忍项",
            "",
            "| 策略 | 顶点/边冲突 | 禁行区 | 低电量 | 角色泄漏 | 重复副作用 | 审批绕过 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for strategy in (P019Strategy.FIXED_WORKFLOW, P019Strategy.REACT, P019Strategy.PEVR):
        zero = summaries[strategy].zero_tolerance
        lines.append(
            f"| {strategy.value} | {zero.vertex_collision_count + zero.edge_collision_count} | {zero.forbidden_zone_entry_count} | {zero.low_battery_violation_count} | {zero.role_leak_count} | {zero.duplicate_side_effect_count} | {zero.approval_bypass_count} |"
        )
    lines.extend(
        [
            "",
            "## 原始结果与复核",
            "",
            f"- 完整 JSON（含 180 条策略-case、源 case、源 Trace、{evidence_note}）：`p019_strategy_comparison.json`。",
            f"- JSONL 原始轨迹（每行一个策略-case）：`p019_raw_trajectories.jsonl`。",
            f"- {source_label}：`{_md(report.source_report.get('path'))}`，SHA-256=`{_md(source_sha256)}`。",
            (
                "- 在线独立 ReAct 只保存短 decision_summary、动作、Observation 和安全事实，不保存原始思维链；Fixed/PEVR 保存各自实际控制事实。"
                if report.execution_mode.value == "online_fast_three_strategy_closed_loop"
                else "- Replay 模式的 ReAct think/act/observe 仅是可视化投影，不代表新的在线调用。"
            ),
            "",
            "## 结论",
            "",
        ]
    )
    lines.extend(f"- {_md(item)}" for item in report.conclusions)
    lines.extend(["", "## Smart 对照状态", "", f"- 状态：**{report.smart_comparison.status.value}**；alias=`{report.smart_comparison.alias}`；请求案例：{report.smart_comparison.requested_case_count}。", f"- 已启动：`{report.smart_comparison.started}`；已完成：`{report.smart_comparison.completed}`。", f"- 原因：{_md(report.smart_comparison.reason)}", f"- Backlog：`{_md(report.smart_comparison.backlog_key)}`。", "", "## 限制", ""])
    lines.extend(f"- {_md(item)}" for item in report.limitations)
    lines.extend(["", f"report_digest：`{report.report_digest}`", ""])
    return "\n".join(lines)


def write_report(report: P019Report, *, output_dir: str | Path) -> tuple[Path, Path, Path]:
    """同时写 JSON、Markdown 和 JSONL，三者共享同一 report digest。"""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "p019_strategy_comparison.json"
    markdown_path = directory / "p019_strategy_comparison.md"
    raw_path = directory / "p019_raw_trajectories.jsonl"
    json_path.write_text(report_to_json(report), encoding="utf-8", newline="\n")
    markdown_path.write_text(report_to_markdown(report), encoding="utf-8", newline="\n")
    raw_path.write_text(raw_results_to_jsonl(report), encoding="utf-8", newline="\n")
    return json_path, markdown_path, raw_path


def run_and_write(
    *,
    source_report: str | Path | None = None,
    config: str | Path,
    output_dir: str | Path,
    mode: str = "independent",
    resume: bool = False,
    verification_timeout_seconds: float = 120.0,
) -> tuple[P019Report, tuple[Path, Path, Path]]:
    """CLI/测试共用入口；在线模式额外支持逐 case 安全恢复。"""

    if mode == "replay":
        if source_report is None:
            raise ValueError("Trace Replay 模式必须提供 source_report")
        report = run_comparison(source_report_path=source_report, config_path=config)
    elif mode == "online":
        report = run_online_comparison(
            config_path=config,
            output_dir=output_dir,
            verification_timeout_seconds=verification_timeout_seconds,
            resume=resume,
        )
    elif mode == "independent":
        report = run_independent_comparison(config_path=config)
    else:
        raise ValueError(f"未知 P0-19 mode: {mode}")
    return report, write_report(report, output_dir=output_dir)


__all__ = ["raw_results_to_jsonl", "report_to_json", "report_to_markdown", "run_and_write", "write_report"]
