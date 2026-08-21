"""P0-19 策略对照实验 CLI。

默认读取已存在的 P0-18 真实源报告；不会启动 Fast/Smart 模型、不会运行任意命令，
也不会改写 P0-18 数据集或工具能力。缺少源报告时先执行 P0-18 一键评测，再重新
调用本入口。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_SOURCE_REPORT_PATH
from .reporting import run_and_write


def build_parser() -> argparse.ArgumentParser:
    """构造只允许固定源报告/配置/输出目录的入口。"""

    parser = argparse.ArgumentParser(description="运行 P0-19 三策略同源 Trace Replay")
    parser.add_argument("--source-report", type=Path, default=DEFAULT_SOURCE_REPORT_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/p019_strategy_compare"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行 P0-19 并打印可自动化读取的产物路径和 Smart 状态。"""

    args = build_parser().parse_args(argv)
    report, paths = run_and_write(source_report=args.source_report, config=args.config, output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "status": report.status,
                "report_id": report.report_id,
                "report_digest": report.report_digest,
                "source_report": str(args.source_report.resolve()),
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
