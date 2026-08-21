"""CTest、pytest 和仿真 stdout/stderr 的确定性结构化解析器。

解析器只读取已经由固定验证入口采集的文本和退出码，不执行日志中的任何内容。
每条失败最多保留少量带行号摘要，并同时生成 ``log://`` 引用，使报告既能定位
失败又不会把整份日志重新注入模型或错误载荷。
"""

from __future__ import annotations

import json
import re
from typing import Any

from services.validation.contracts import (
    ParsedVerificationCase,
    VerificationEvidenceLocation,
    VerificationFailureType,
)


_TOOL_NAMES = (
    "retrieve_knowledge",
    "get_fleet_state",
    "allocate_tasks",
    "plan_multi_amr_routes",
    "validate_fleet_plan",
    "dispatch_simulation",
    "query_execution_state",
    "run_verification_suite",
    "request_approval",
)
_TASK_PATTERN = re.compile(
    r"(?:task(?:[_ -]?id)?|order(?:[_ -]?id)?)\s*[:=]\s*([A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)
_DIGEST_PATTERN = re.compile(
    r"(?:input|parameter|parameters|args?|request)[_ -]?digest\s*[:=]\s*([0-9a-f]{64})",
    re.IGNORECASE,
)
_LOCATION_PATTERN = re.compile(r"(?:position|cell|coordinate)\s*[:=]\s*([^,;]+)", re.IGNORECASE)


class VerificationLogParser:
    """把固定验证入口的实际输出转换成 ``ParsedVerificationCase``。"""

    def parse(
        self,
        *,
        suite_id: str,
        case_id: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        duration_ms: int,
        timed_out: bool = False,
        entry_kind: str = "test",
        stdout_digest: str,
        stderr_digest: str,
    ) -> ParsedVerificationCase:
        """只依据退出状态和日志文本产出结果；没有输出时也保留元数据证据。"""

        status = "timeout" if timed_out else "passed" if exit_code == 0 else "failed"
        combined = f"{stdout}\n{stderr}"
        failure_type = (
            VerificationFailureType.TIMEOUT
            if timed_out
            else self._failure_type(combined, entry_kind=entry_kind)
            if status == "failed"
            else VerificationFailureType.NONE
        )
        task_id, tool_name, parameters_digest = self._related_entities(combined)
        if entry_kind == "simulation" and tool_name is None:
            tool_name = "dispatch_simulation"
        simulation_refs = self._simulation_evidence(
            suite_id=suite_id,
            case_id=case_id,
            stdout=stdout,
            entry_kind=entry_kind,
        )
        locations = self._locations(
            suite_id=suite_id,
            case_id=case_id,
            stdout=stdout,
            stderr=stderr,
            status=status,
            failure_type=failure_type,
            entry_kind=entry_kind,
        )
        refs = list(dict.fromkeys([*simulation_refs, *(item.citation for item in locations)]))
        if not refs:
            refs = [f"log://{suite_id}/{case_id}/metadata"]
            locations = [
                VerificationEvidenceLocation(
                    source="metadata",
                    citation=refs[0],
                    excerpt=(
                        "fixed entry exited with code "
                        f"{exit_code}" if status != "timeout" else "fixed entry timed out"
                    ),
                )
            ]
        summary = self._summary(status, failure_type, stdout, stderr)
        return ParsedVerificationCase(
            case_id=case_id,
            status=status,
            exit_code=exit_code,
            duration_ms=max(0, duration_ms),
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            failure_type=failure_type,
            task_id=task_id,
            tool_name=tool_name,
            parameters_digest=parameters_digest,
            evidence_refs=refs,
            evidence_locations=locations,
            summary=summary,
        )

    @staticmethod
    def _failure_type(text: str, *, entry_kind: str) -> VerificationFailureType:
        """按最具体到最宽泛的顺序匹配，未知文本不能被伪装成通过。"""

        lower = text.lower()
        if any(token in lower for token in ("timeout", "timed out", "超时")):
            return VerificationFailureType.TIMEOUT
        if any(token in lower for token in ("permission_denied", "permission denied", "unauthorized", "越权")):
            return VerificationFailureType.PERMISSION
        if any(token in lower for token in ("schema", "validationerror", "json schema")):
            return VerificationFailureType.SCHEMA
        if any(token in lower for token in ("unsafe_plan", "infeasible", "不安全计划", "不可行")):
            return VerificationFailureType.UNSAFE_PLAN
        if entry_kind == "simulation" and any(
            token in lower for token in ("blocked", "simulation blocked", "仿真阻塞")
        ):
            return VerificationFailureType.SIMULATION_BLOCKED
        if any(token in lower for token in ("tool_error", "tool failed", "工具失败")):
            return VerificationFailureType.TOOL_ERROR
        if any(token in lower for token in ("assertionerror", "assertion failed", "assert failed")):
            return VerificationFailureType.ASSERTION
        if any(token in lower for token in ("fatal", "no such file", "cannot open", "collection error")):
            return VerificationFailureType.INFRASTRUCTURE
        if any(token in lower for token in ("failed", "failure", "error", "失败")):
            return VerificationFailureType.UNKNOWN
        return VerificationFailureType.INFRASTRUCTURE

    @staticmethod
    def _related_entities(text: str) -> tuple[str | None, str | None, str | None]:
        """从日志中的显式字段提取任务、工具和输入 digest，不解析任意表达式。"""

        task_match = _TASK_PATTERN.search(text)
        task_id = task_match.group(1) if task_match else None
        tool_name = next((name for name in _TOOL_NAMES if name in text), None)
        digest_match = _DIGEST_PATTERN.search(text)
        parameters_digest = digest_match.group(1) if digest_match else None
        return task_id, tool_name, parameters_digest

    @staticmethod
    def _simulation_evidence(
        *, suite_id: str, case_id: str, stdout: str, entry_kind: str
    ) -> list[str]:
        """从合法 JSON 仿真快照提取已有 evidence_refs；失败 JSON 不会抛弃日志位置。"""

        if entry_kind != "simulation":
            return []
        try:
            payload = json.loads(stdout.strip())
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        refs: list[str] = []
        raw_refs = payload.get("evidence_refs")
        if isinstance(raw_refs, list):
            refs.extend(item for item in raw_refs if isinstance(item, str) and item.strip())
        simulation_id = payload.get("simulation_id")
        if isinstance(simulation_id, str) and simulation_id:
            refs.append(f"simulation://{simulation_id}")
        refs.append(f"simulation-log://{suite_id}/{case_id}")
        return list(dict.fromkeys(refs))

    @staticmethod
    def _locations(
        *,
        suite_id: str,
        case_id: str,
        stdout: str,
        stderr: str,
        status: str,
        failure_type: VerificationFailureType,
        entry_kind: str,
    ) -> list[VerificationEvidenceLocation]:
        """保留最多四行可复核摘要；证据引用包含流和原始行号。"""

        candidates: list[VerificationEvidenceLocation] = []
        keywords = (
            "failed",
            "failure",
            "error",
            "assert",
            "timeout",
            "blocked",
            "infeasible",
            "passed",
            "通过",
            "失败",
            "超时",
        )
        for source, text in (("stdout", stdout), ("stderr", stderr)):
            for line_number, raw_line in enumerate(text.splitlines(), start=1):
                line = raw_line.strip()
                lower = line.lower()
                if not line:
                    continue
                selected = any(keyword in lower or keyword in line for keyword in keywords)
                if status != "passed" and source == "stderr":
                    selected = True
                if status == "passed" and not selected:
                    continue
                if not selected:
                    continue
                excerpt = line[:512]
                citation = f"log://{suite_id}/{case_id}/{source}#L{line_number}"
                candidates.append(
                    VerificationEvidenceLocation(
                        source=source,
                        line=line_number,
                        citation=citation,
                        excerpt=excerpt,
                    )
                )
                if len(candidates) >= 4:
                    return candidates
        if not candidates:
            candidates.append(
                VerificationEvidenceLocation(
                    source="simulation" if entry_kind == "simulation" else "metadata",
                    citation=f"log://{suite_id}/{case_id}/metadata",
                    excerpt=(
                        f"status={status}; failure_type={failure_type.value}"
                    ),
                )
            )
        return candidates

    @staticmethod
    def _summary(status: str, failure_type: VerificationFailureType, stdout: str, stderr: str) -> str:
        """报告只携带稳定短摘要，避免把完整测试日志写进 ToolResult。"""

        if status == "passed":
            return "验证入口退出码为 0"
        source = (stderr or stdout).strip().splitlines()
        detail = next((line.strip() for line in source if line.strip()), "无日志摘要")
        return f"{failure_type.value}: {detail[:440]}"


__all__ = ["VerificationLogParser"]
