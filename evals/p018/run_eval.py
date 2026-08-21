"""P0-18 60 例评测 CLI。

默认只接受仓库内固定 dataset/config，并把 JSON 与 Markdown 报告写到 ``tmp``。
退出码 0 表示 60 例都符合预期且七个零容忍项全为 0；退出码 2 表示 Harness
真实观察到意外失败或安全违规。负向安全案例正确返回 denied/blocked 不会导致
退出码 2，但会在报告中保留失败轨迹和原因。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_DATASET_PATH
from .reporting import write_report
from .runner import DEFAULT_OUTPUT_DIR, run_harness


def build_parser() -> argparse.ArgumentParser:
    """构造不暴露任意命令、脚本或测试表达式的固定参数入口。"""

    parser = argparse.ArgumentParser(description="运行 P0-18 固定 60 例自动评测")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--verification-timeout", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行 Harness、落盘双格式报告并返回可自动化判断的退出码。"""

    args = build_parser().parse_args(argv)
    report = run_harness(
        dataset_path=args.dataset,
        config_path=args.config,
        verification_timeout_seconds=args.verification_timeout,
    )
    json_path, markdown_path = write_report(report, output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "status": report.status,
                "report_id": report.report_id,
                "report_digest": report.report_digest,
                "case_count": report.metrics.case_count,
                "evaluation_pass_count": report.metrics.evaluation_pass_count,
                "zero_tolerance": report.metrics.zero_tolerance.model_dump(mode="json"),
                "json_report": str(json_path.resolve()),
                "markdown_report": str(markdown_path.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
