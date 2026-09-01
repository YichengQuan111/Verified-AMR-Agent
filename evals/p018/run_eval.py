"""P0-18 60 例评测 CLI。

默认只接受仓库内固定 dataset/config，并把 JSON 与 Markdown 报告写到 ``tmp``。
离线模式退出码 0 表示 60 例都符合预期且七个零容忍项全为 0。在线闭环退出码 0
表示 60 例均已执行且零容忍项为 0，完成率允许低于 100%。退出码 2 表示 Harness
崩溃、零容忍非零或离线契约失败。负向安全案例正确返回 denied/blocked 不会单
独导致退出码 2。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.perf.ttft_provider import eval_ttft_requested

from .dataset import DEFAULT_CONFIG_PATH, DEFAULT_DATASET_PATH, load_config
from .reporting import write_report
from .runner import DEFAULT_OUTPUT_DIR, run_harness


def build_parser() -> argparse.ArgumentParser:
    """构造不暴露任意命令、脚本或测试表达式的固定参数入口。"""

    parser = argparse.ArgumentParser(description="运行 P0-18 固定 60 例自动评测")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--verification-timeout", type=float, default=120.0)
    parser.add_argument(
        "--measure-ttft",
        action="store_true",
        help="仅在线模式：用评测专用 stream=true 探针记录 TTFT；默认关闭，生产网关仍非流式",
    )
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help="仅在线模式：只跑固定 36 个 LLM 例；不是正式 P0-18 60 例发布报告",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行 Harness、落盘双格式报告并返回可自动化判断的退出码。"""

    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if str(config.get("execution_mode")) == "online_fast_closed_loop":
        from .online import run_online_harness

        report = run_online_harness(
            dataset_path=args.dataset,
            config_path=args.config,
            verification_timeout_seconds=args.verification_timeout,
            output_dir=args.output_dir,
            measure_ttft=eval_ttft_requested(measure_ttft=bool(args.measure_ttft)),
            llm_only=bool(args.llm_only),
        )
        json_name, markdown_name = "p018_online_eval.json", "p018_online_eval.md"
    else:
        if args.measure_ttft or args.llm_only:
            print("[p018] --measure-ttft/--llm-only 只作用于在线闭环，离线 oracle 已忽略", flush=True)
        report = run_harness(
            dataset_path=args.dataset,
            config_path=args.config,
            verification_timeout_seconds=args.verification_timeout,
        )
        json_name, markdown_name = "p018_eval.json", "p018_eval.md"
    json_path, markdown_path = write_report(
        report,
        output_dir=args.output_dir,
        json_name=json_name,
        markdown_name=markdown_name,
    )
    payload = {
        "status": report.status,
        "report_id": report.report_id,
        "report_digest": report.report_digest,
        "execution_mode": str(config.get("execution_mode")),
        "case_count": report.metrics.case_count,
        "evaluation_pass_count": report.metrics.evaluation_pass_count,
        "zero_tolerance": report.metrics.zero_tolerance.model_dump(mode="json"),
        "json_report": str(json_path.resolve()),
        "markdown_report": str(markdown_path.resolve()),
    }
    agent = report.metrics.agent
    if "task_completion_rate" in agent:
        payload["task_completion_rate"] = agent.get("task_completion_rate")
        payload["task_completion_count"] = agent.get("task_completion_count")
        payload["positive_case_count"] = agent.get("positive_case_count")
        payload["model_call_count"] = agent.get("model_call_count")
    recovery = report.metrics.recovery
    if "recovery_rate" in recovery:
        payload["recovery_rate"] = recovery.get("recovery_rate")
        payload["recovery_terminal_correct_count"] = recovery.get("recovery_terminal_correct_count")
    if str(config.get("execution_mode")) == "online_fast_closed_loop":
        payload["measure_ttft"] = eval_ttft_requested(measure_ttft=bool(args.measure_ttft))
        payload["llm_only"] = bool(args.llm_only)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if report.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
