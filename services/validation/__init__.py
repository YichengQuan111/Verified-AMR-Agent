"""受控验证适配器、日志解析和证据报告的稳定入口。"""

from services.validation.contracts import (
    ParsedVerificationCase,
    VerificationEvidenceLocation,
    VerificationFailureType,
    VerificationReport,
)
from services.validation.log_parser import VerificationLogParser
from services.validation.reporting import VerificationReportGenerator

__all__ = [
    "ParsedVerificationCase",
    "VerificationEvidenceLocation",
    "VerificationFailureType",
    "VerificationLogParser",
    "VerificationReport",
    "VerificationReportGenerator",
]
