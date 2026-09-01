"""``python -m evals.perf`` 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def _dump(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from evals.perf.benchmark import run_benchmark

    report = run_benchmark(
        repeats=args.repeats,
        max_tokens=args.max_tokens,
        compare_cache=args.compare_cache,
        llama_log_path=None if args.no_llama_log else Path(args.llama_log),
    )
    _dump(Path(args.output) if args.output else None, report)
    valid = 0
    for phase in report.get("phases") or []:
        valid += int(((phase.get("summary") or {}).get("sample_counts") or {}).get("valid") or 0)
    return 0 if valid > 0 else 2


def _cmd_parse_log(args: argparse.Namespace) -> int:
    from evals.perf.legacy import summarize_log_prefill_only

    text = Path(args.log).read_text(encoding="utf-8", errors="replace")
    payload = summarize_log_prefill_only(text, min_prompt_eval_tokens=args.min_prompt_tokens)
    _dump(Path(args.output) if args.output else None, payload)
    return 0


def _cmd_summarize_pevr_llm(args: argparse.Namespace) -> int:
    from evals.perf.llm36 import load_eval_report, load_ttft_samples, read_log_delta, summarize_pevr_llm_report

    report = load_eval_report(args.report)
    log_text = None
    if args.log:
        log_path = Path(args.log)
        if log_path.is_file():
            log_text = read_log_delta(log_path, byte_offset=args.log_offset)
    cache_prompt = None
    if args.cache_prompt == "true":
        cache_prompt = True
    elif args.cache_prompt == "false":
        cache_prompt = False
    ttft_samples = None
    sample_path = Path(args.ttft_samples) if args.ttft_samples else Path(args.report).with_name(
        "pevr_ttft_metrics.json"
    )
    if sample_path.is_file():
        ttft_samples = load_ttft_samples(sample_path)
    payload = summarize_pevr_llm_report(
        report,
        log_text=log_text,
        log_byte_offset=args.log_offset,
        cache_prompt=cache_prompt,
        ttft_samples=ttft_samples,
    )
    _dump(Path(args.output) if args.output else None, payload)
    llm_count = int(payload.get("llm_case_count") or 0)
    expected = int(payload.get("expected_llm_case_count") or 36)
    return 0 if llm_count == expected else 2


def _cmd_pevr_ttft(args: argparse.Namespace) -> int:
    """打印或执行 PEVR 在线 TTFT 探针。无 ``--run`` 时绝不启动评测。"""

    command = (
        "& 'E:\\Anaconda\\envs\\torch128\\python.exe' -u -m evals.p018.run_eval `\n"
        "  --config evals\\p018\\online_config.json `\n"
        f"  --output-dir {args.output_dir} `\n"
        "  --verification-timeout 120 `\n"
        "  --measure-ttft"
    )
    print(
        "评测专用 TTFT 路径：生产 ModelProvider 仍是 stream=false。\n"
        "默认不启动实测。需要跑完整 60 例时加上 --run，或复制下列命令：\n\n"
        f"{command}\n",
        flush=True,
    )
    if not args.run:
        return 0
    from evals.p018.online import run_online_harness
    from evals.p018.reporting import write_report

    output_dir = Path(args.output_dir)
    report = run_online_harness(
        config_path=PROJECT_ROOT / "evals" / "p018" / "online_config.json",
        verification_timeout_seconds=120.0,
        output_dir=output_dir,
        measure_ttft=True,
    )
    write_report(
        report,
        output_dir=output_dir,
        json_name="p018_online_eval.json",
        markdown_name="p018_online_eval.md",
    )
    return 0 if report.status == "passed" else 2


def _cmd_compare_cache(args: argparse.Namespace) -> int:
    from evals.perf.cache_compare import run_llm36_cache_compare

    payload = run_llm36_cache_compare(output_root=Path(args.output_root))
    comparison = payload.get("comparison") or {}
    speedups = comparison.get("ttft_prefill_model_e2e") or {}
    if speedups.get("ttft_speedup_uses_prefill") is True:
        return 2
    return 0


def _cmd_restate_legacy(args: argparse.Namespace) -> int:
    from evals.perf.legacy import load_legacy_metrics, restated_legacy_metrics

    legacy = load_legacy_metrics(args.input)
    log_text = None
    if args.log:
        log_path = Path(args.log)
        if log_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
    payload = restated_legacy_metrics(legacy, log_text=log_text)
    _dump(Path(args.output) if args.output else None, payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AMR LLM 延迟 Benchmark：TTFT 只来自流式首 token，不用 progress=1.00。"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bench = sub.add_parser("benchmark", help="对 Fast 发出串行流式请求并统计 TTFT/Prefill/E2E")
    bench.add_argument("--repeats", type=int, default=2)
    bench.add_argument("--max-tokens", type=int, default=16)
    bench.add_argument("--compare-cache", action="store_true")
    bench.add_argument("--output", default=str(PROJECT_ROOT / "tmp" / "ttft_benchmark.json"))
    bench.add_argument(
        "--llama-log",
        default=str(PROJECT_ROOT / "tmp" / "llama-server.err.log"),
    )
    bench.add_argument("--no-llama-log", action="store_true")
    bench.set_defaults(func=_cmd_benchmark)

    parse_log = sub.add_parser("parse-log", help="只从 llama.cpp 日志提取 Prefill，TTFT 保持缺失")
    parse_log.add_argument("--log", default=str(PROJECT_ROOT / "tmp" / "llama-server.err.log"))
    parse_log.add_argument("--min-prompt-tokens", type=int, default=200)
    parse_log.add_argument("--output")
    parse_log.set_defaults(func=_cmd_parse_log)

    restate = sub.add_parser("restate-legacy", help="作废旧 TTFT，保留 Prefill/E2E/命中率")
    restate.add_argument(
        "--input",
        default=str(
            PROJECT_ROOT / "tmp" / "p018_pevr_cache_compare_20260831" / "llm_only_cache_metrics.json"
        ),
    )
    restate.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / "tmp"
            / "p018_pevr_cache_compare_20260831"
            / "llm_only_cache_metrics.ttft_withdrawn.json"
        ),
    )
    restate.add_argument("--log", default=str(PROJECT_ROOT / "tmp" / "llama-server.err.log"))
    restate.set_defaults(func=_cmd_restate_legacy)

    llm36 = sub.add_parser(
        "summarize-pevr-llm",
        help="从完整 60 例 P0-18 在线报告抽出 36 个 LLM 案例；TTFT 保持缺失",
    )
    llm36.add_argument("--report", required=True)
    llm36.add_argument("--output")
    llm36.add_argument("--log", default=str(PROJECT_ROOT / "tmp" / "llama-server.err.log"))
    llm36.add_argument("--log-offset", type=int, default=0)
    llm36.add_argument(
        "--cache-prompt",
        choices=("true", "false", "unknown"),
        default="true",
    )
    llm36.add_argument(
        "--ttft-samples",
        help="pevr_ttft_metrics.json 或 jsonl；省略时若报告旁存在同名文件则自动读取",
    )
    llm36.set_defaults(func=_cmd_summarize_pevr_llm)

    pevr_ttft = sub.add_parser(
        "pevr-ttft",
        help="打印 PEVR 在线 TTFT 探针命令；加 --run 才真正开跑（默认不跑）",
    )
    pevr_ttft.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "tmp" / "p018_pevr_ttft"),
    )
    pevr_ttft.add_argument(
        "--run",
        action="store_true",
        help="真正启动完整 60 例在线 PEVR + TTFT 探针；默认只打印命令",
    )
    pevr_ttft.set_defaults(func=_cmd_pevr_ttft)

    compare = sub.add_parser(
        "compare-cache",
        help="36 LLM 例 PEVR 有/无 cache_prompt 对照；TTFT 走流式探针，会实际开跑",
    )
    compare.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / "tmp" / "p018_pevr_llm36_ttft_cache_20260901"),
    )
    compare.set_defaults(func=_cmd_compare_cache)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
