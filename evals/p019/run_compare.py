"""P0-19 策略对照实验 CLI。

默认运行三种策略的独立离线对照；``--mode replay`` 才消费已有 P0-18 源报告做可视化投影。
不会启动 Fast/Smart 模型，也不会改写 P0-18 数据集或工具能力。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_SOURCE_REPORT_PATH
from .reporting import run_and_write


def build_parser() -> argparse.ArgumentParser:
    """构造只允许固定配置/输出目录的入口。"""

    parser = argparse.ArgumentParser(description="运行 P0-19 三策略对照")
    parser.add_argument("--mode", choices=("independent", "replay"), default="independent")
    parser.add_argument("--source-report", type=Path, default=DEFAULT_SOURCE_REPORT_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/p019_strategy_compare"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行 P0-19 并打印可自动化读取的产物路径和 Smart 状态。"""

    args = build_parser().parse_args(argv)
    report, paths = run_and_write(
        source_report=args.source_report,
        config=args.config,
        output_dir=args.output_dir,
        mode=args.mode,
    )
    print(
        json.dumps(
            {
                "status": report.status,
                "report_id": report.report_id,
                "report_digest": report.report_digest,
                "execution_mode": report.execution_mode.value,
                "source_report": str(args.source_report.resolve()) if args.mode == "replay" else None,
                "strategies": [item.strategy.value for item in report.strategies],
                "raw_result_count": len(report.raw_results),
                "smart": report.smart_comparison.model_dump(mode="json"),
                "json_report": str(paths[0].resolve()),
                "markdown_report": str(paths[1].resolve()),
                "raw_jsonl": str(paths[2].resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
