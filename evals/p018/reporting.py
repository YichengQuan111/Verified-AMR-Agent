"""P0-18 JSON/Markdown 报告渲染。

JSON 是完整机器接口，包含 60 个逐例结果、Trace、失败原因、固定输入指纹和零
容忍计数；Markdown 是同一 ``EvalReport`` 的人类阅读投影。渲染器不重新判断
评测结论，也不删除 denied/blocked 负向样例，避免为了好看而丢失安全反例。
不可信的 case 描述、异常摘要和日志引用统一做 Markdown 转义，后续接入网页展示
时仍应把正文当数据处理。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .contracts import EvalReport, EvalReportCase
from .runner import DEFAULT_OUTPUT_DIR


def report_to_json(report: EvalReport) -> str:
    """输出稳定缩进 JSON；报告 digest 已由 Harness 在写盘前确定。"""

    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _md(value: Any) -> str:
    """压平换行和表格分隔符，避免日志内容破坏 Markdown 结构。"""

    return re.sub(r"[\r\n|]", " ", str(value)).replace("`", "'`'" )


def _render_negative_case(case: EvalReportCase) -> list[str]:
    """保留负向场景的观察终态、原因和完整轨迹摘要。"""

    lines = [
        f"### `{_md(case.case_id)}`",
        f"- 类别：`{case.category.value}`；预期：`{case.expected_outcome.value}`；观察：`{case.observed_outcome.value}`；评测：`{case.status.value}`",
        f"- 原因：`{_md(case.failure_code or 'none')}` — {_md(case.failure_reason or '预期正向完成')}",
        f"- Trace：`{_md(case.trace_id)}`；证据数：{len(case.evidence_refs)}；重规划：{case.replan_count}；重试：{case.retry_count}；审批恢复：{case.approval_resume_count}",
        "- 轨迹：",
    ]
    for event in case.trace_events:
        error = event.get("error")
        error_code = ""
        if isinstance(error, dict):
            error_code = f" error={error.get('code', 'unknown')}"
        tool = f" tool={event.get('tool_name')}" if event.get("tool_name") else ""
        lines.append(
            f"  - #{event.get('sequence')} `{event.get('status')}` node={event.get('node')}{tool}{error_code}"
        )
    return lines


def report_to_markdown(report: EvalReport) -> str:
    """生成包含配额、指标、零容忍和负向样例的 Markdown 报告。"""

    metrics = report.metrics
    zero = metrics.zero_tolerance.model_dump(mode="json")
    repro = report.reproducibility
    mode = str(repro.get("execution_mode") or "offline_deterministic_oracle")
    if mode == "online_fast_closed_loop":
        mode_line = (
            "- 运行模式：`online_fast_closed_loop`。本报告使用真实 Qwen3.6 Fast、加难地图和按 seed 少量额外障碍；"
            "完成率/恢复率按观察终态重算，不能改写成离线 oracle 60/60。"
        )
    else:
        mode_line = (
            "- 运行模式：`offline_deterministic_oracle`。本报告记录 Fast/GGUF/Prompt/ToolSpec 版本，但默认不启动模型服务；"
            "不能把本结果解释成在线 LLM 生成验收。"
        )
    lines = [
        "# P0-18：60 例自动评测报告",
        "",
        f"- 报告：`{report.report_id}`；状态：**{report.status}**；版本：`{report.report_version}`",
        f"- 数据集：`{report.dataset_id}` / `{report.dataset_version}`；逐例结果：{metrics.case_count}；评测符合预期：{metrics.evaluation_pass_count}",
        mode_line,
        "",
        "## 数据集组成",
        "",
        "| 类别 | 例数 | 评测通过率 |",
        "| --- | ---: | ---: |",
    ]
    for category, count in metrics.category_counts.items():
        lines.append(f"| `{_md(category)}` | {count} | {metrics.category_pass_rates.get(category, 0.0):.6f} |")
    lines.extend(
        [
            "",
            "## 核心指标",
            "",
            "| 领域 | 指标 | 数值 |",
            "| --- | --- | ---: |",
        ]
    )
    for domain, domain_metrics in (
        ("Agent", metrics.agent),
        ("RAG", metrics.rag),
        ("AMR", metrics.amr),
        ("安全", metrics.security),
        ("恢复", metrics.recovery),
        ("验证", metrics.verification),
    ):
        for key, value in domain_metrics.items():
            lines.append(f"| {domain} | `{_md(key)}` | `{_md(value)}` |")
    lines.extend(["", "## 零容忍项", "", "| 检查项 | 计数 | 结论 |", "| --- | ---: | --- |"])
    for key, value in zero.items():
        lines.append(f"| `{_md(key)}` | {value} | {'PASS' if value == 0 else 'FAIL'} |")
    lines.extend(
        [
            "",
            "## 负向/失败轨迹",
            "",
            f"共有 {len(report.observed_negative_cases)} 例观察到 denied/blocked/failed；这些例子不从结果集中删除。若其 observed_outcome 与固定 expected_outcome 及 expected_code 一致，则属于“正确阻断/正确终止”，不计为 Harness 失败。",
            "",
        ]
    )
    negative_ids = set(report.observed_negative_cases)
    negative_cases = [case for case in report.cases if case.case_id in negative_ids or not case.evaluation_passed]
    if negative_cases:
        for case in negative_cases:
            lines.extend(_render_negative_case(case))
            lines.append("")
    else:
        lines.append("本次没有负向观测；这不应成为安全评测的默认状态。")
        lines.append("")
    lines.extend(["## 可复现指纹", ""])
    repro = report.reproducibility
    lines.append(f"- environment_ref：`{_md(repro.get('environment_ref'))}`")
    lines.append(f"- case_seed_digest：`{_md(repro.get('case_seed_digest'))}`（每例 seed 已在 JSON `reproducibility.case_seeds` 和数据集逐例字段保存）")
    model = repro.get("model", {})
    if isinstance(model, dict):
        lines.append(
            f"- model：`{_md(model.get('alias'))}` / quantization=`{_md(model.get('quantization'))}` / context=`{_md(model.get('context_window'))}` / online_required=`{_md(model.get('online_service_required'))}`"
        )
    lines.append(f"- tool_spec_versions：`{_md(repro.get('tool_spec_versions'))}`")
    lines.append(f"- prompt_versions：`{_md(repro.get('prompt_versions'))}`")
    input_files = repro.get("input_files", {})
    if isinstance(input_files, dict):
        lines.append("")
        lines.append("| 固定输入 | SHA-256 |")
        lines.append("| --- | --- |")
        for name, fingerprint in input_files.items():
            digest = fingerprint.get("sha256") if isinstance(fingerprint, dict) else None
            lines.append(f"| `{_md(name)}` | `{_md(digest)}` |")
    lines.extend(["", f"report_digest：`{report.report_digest}`", "", "结论：该报告由逐例 Harness 结果重新汇总；所有失败、拒绝、阻塞和证据引用均保留在 JSON。", ""])
    return "\n".join(lines)


def write_report(
    report: EvalReport,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    json_name: str = "p018_eval.json",
    markdown_name: str = "p018_eval.md",
) -> tuple[Path, Path]:
    """一次性写出同一 report_id/digest 的 JSON 与 Markdown 文件。"""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / json_name
    markdown_path = directory / markdown_name
    json_path.write_text(report_to_json(report), encoding="utf-8", newline="\n")
    markdown_path.write_text(report_to_markdown(report), encoding="utf-8", newline="\n")
    return json_path, markdown_path


__all__ = ["report_to_json", "report_to_markdown", "write_report"]
