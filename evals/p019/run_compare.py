"""P0-19 离线、Replay 与真实 Fast 三策略对照 CLI。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_ONLINE_CONFIG_PATH, DEFAULT_SOURCE_REPORT_PATH
from .reporting import run_and_write


def build_parser() -> argparse.ArgumentParser:
    """构造只允许固定配置/输出目录的入口。"""

    parser = argparse.ArgumentParser(description="运行 P0-19 三策略对照")
    parser.add_argument("--mode", choices=("independent", "replay", "online"), default="independent")
    parser.add_argument("--source-report", type=Path, default=DEFAULT_SOURCE_REPORT_PATH)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/p019_strategy_compare"))
    parser.add_argument("--verification-timeout", type=float, default=120.0)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行 P0-19 并打印可自动化读取的产物路径和 Smart 状态。"""

    args = build_parser().parse_args(argv)
    config = args.config or (DEFAULT_ONLINE_CONFIG_PATH if args.mode == "online" else DEFAULT_CONFIG_PATH)
    report, paths = run_and_write(
        source_report=args.source_report,
        config=config,
        output_dir=args.output_dir,
        mode=args.mode,
        resume=args.resume,
        verification_timeout_seconds=args.verification_timeout,
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
