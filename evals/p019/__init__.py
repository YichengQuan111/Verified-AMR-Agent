"""P0-19 策略对照实验的契约、轨迹回放器和报告入口。"""

from .contracts import (
    P019ExecutionMode,
    P019Report,
    P019Strategy,
    ResourceObservation,
    SmartComparisonStatus,
    StrategyCaseResult,
    StrategySummary,
)
from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_SOURCE_REPORT_PATH, load_config
from .independent import run_independent_comparison
from .replay import compare_source_report, load_source_report, run_comparison

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SOURCE_REPORT_PATH",
    "P019ExecutionMode",
    "P019Report",
    "P019Strategy",
    "ResourceObservation",
    "SmartComparisonStatus",
    "StrategyCaseResult",
    "StrategySummary",
    "compare_source_report",
    "load_config",
    "load_source_report",
    "run_comparison",
    "run_independent_comparison",
]
