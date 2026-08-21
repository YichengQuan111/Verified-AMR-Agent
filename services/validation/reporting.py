"""P0-17 验证报告生成器。

报告的 status、计数和结论全部从解析后的逐 case 结果重算；调用方不能传入一段
“通过”文字覆盖真实非零退出码。JSON 用于机器消费，Markdown 用于人工审阅，二者
共享同一 report_digest 和 evidence_refs。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any

from pydantic import BaseModel

from services.validation.contracts import (
    ParsedVerificationCase,
    VerificationReport,
)


def _canonical_json(value: Any) -> str:
    """以固定 JSON 表示生成报告身份，避免键顺序改变报告 digest。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class VerificationReportGenerator:
    """从真实 case 结果构造 JSON/Markdown 验证报告。"""

    def build(
        self,
        *,
        suite_id: str,
        run_id: str | None,
        trace_id: str | None,
        cases: Iterable[ParsedVerificationCase | Mapping[str, Any]],
        started_at: datetime,
        finished_at: datetime,
    ) -> VerificationReport:
        """重算汇总字段并拒绝空 case，防止“没有执行测试”被报告为通过。"""

        parsed: list[ParsedVerificationCase] = []
        for item in cases:
            if isinstance(item, ParsedVerificationCase):
                parsed.append(item)
            elif isinstance(item, BaseModel):
                parsed.append(ParsedVerificationCase.model_validate(item.model_dump(mode="json")))
            else:
                parsed.append(ParsedVerificationCase.model_validate(item))
        if not parsed:
            raise ValueError("验证报告不能没有实际执行的 case")
        passed = sum(item.status == "passed" for item in parsed)
        failed = sum(item.status == "failed" for item in parsed)
        timed_out = sum(item.status == "timeout" for item in parsed)
        status = "timeout" if timed_out else "failed" if failed else "passed"
        evidence_refs = list(
            dict.fromkeys(
                ref
                for item in parsed
                for ref in item.evidence_refs
            )
        )
        identity_payload = {
            "suite_id": suite_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "status": status,
            "cases": [item.model_dump(mode="json") for item in parsed],
        }
        report_digest = sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()
        report_id = f"verification-{report_digest[:24]}"
        report_refs = [
            *evidence_refs,
            f"report://{report_id}/json",
            f"report://{report_id}/markdown",
        ]
        report = VerificationReport(
            report_id=report_id,
            report_digest=report_digest,
            suite_id=suite_id,
            run_id=run_id,
            trace_id=trace_id,
            status=status,
            case_count=len(parsed),
            passed_count=passed,
            failed_count=failed,
            timeout_count=timed_out,
            cases=parsed,
            evidence_refs=list(dict.fromkeys(report_refs)),
        )
        # 生成时间只做完整性校验，不参与 status 重算，不能借时间字段伪造通过结论。
        if finished_at < started_at:
            raise ValueError("验证报告 finished_at 不能早于 started_at")
        return report

    @staticmethod
    def to_json(report: VerificationReport) -> str:
        """输出稳定缩进 JSON；不添加未经契约验证的字段。"""

        return report.model_dump_json(indent=2)

    @staticmethod
    def to_markdown(report: VerificationReport) -> str:
        """生成包含失败类型、任务/工具和证据位置的人工审阅表。"""

        lines = [
            f"# Verification Report: {report.suite_id}",
            "",
            f"- Status: `{report.status}`",
            f"- Report ID: `{report.report_id}`",
            f"- Report digest: `{report.report_digest}`",
            f"- Run ID: `{report.run_id or '-'}`",
            f"- Trace ID: `{report.trace_id or '-'}`",
            f"- Cases: {report.case_count} (passed={report.passed_count}, failed={report.failed_count}, timeout={report.timeout_count})",
            "",
            "| Case | Status | Failure type | Task | Tool | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for case in report.cases:
            evidence = "; ".join(case.evidence_refs[:4]) or "-"
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md(case.case_id),
                        _md(case.status),
                        _md(case.failure_type.value),
                        _md(case.task_id or "-"),
                        _md(case.tool_name or "-"),
                        _md(evidence),
                    )
                )
                + " |"
            )
            if case.status != "passed":
                lines.append("")
                lines.append(f"**{case.case_id}**: {_md(case.summary or '无摘要')}")
                for location in case.evidence_locations:
                    lines.append(f"- `{location.citation}` — {_md(location.excerpt)}")
        lines.extend(["", "结论：该状态由固定验证入口退出码和逐 case 解析结果重算。"])
        return "\n".join(lines) + "\n"


def _md(value: str) -> str:
    """转义 Markdown 表格中的不可信日志摘要。"""

    return re.sub(r"[\r\n|]", " ", str(value))


__all__ = ["VerificationReportGenerator"]
