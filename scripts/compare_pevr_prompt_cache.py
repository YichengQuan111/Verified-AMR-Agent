"""对照生产 PEVR 在 P0-18 固定 60 例上有/无 llama.cpp ``cache_prompt`` 的墙钟。

只跑 ``OnlineControlStrategy.PEVR``，不跑 Fixed/ReAct，也不改 P0-19 三策略契约。
每阶段用独立子进程加载配置，避免 ``load_settings()`` 把上一阶段的开关带进下一阶段。
阶段之间发送与 AMR 前缀无关的 breaker 请求，避免槽内残留 KV 污染关缓存组。
本脚本不改仓库默认生成超时；超时本身计入该例墙钟。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent.context.prompt_registry import P005_PROMPT_VERSION
from agent.context.shared_prefix import SHARED_PREFIX_ID, SHARED_PREFIX_VERSION, shared_system_prefix_digest
from services.config import load_settings
from services.model_gateway import ChatMessage, ModelProvider

PYTHON_EXE = Path(os.environ.get("AMR_PYTHON_EXE") or r"E:\Anaconda\envs\torch128\python.exe")
ONLINE_CONFIG = PROJECT_ROOT / "evals" / "p018" / "online_config.json"


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位；空列表返回 0。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return round(ordered[low] * (1.0 - frac) + ordered[high] * frac, 1)


def summarize_pevr_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """从 P0-18 在线报告逐例重算墙钟分布；不信任报告顶层预填数字。"""

    walls = [float(item.get("metrics", {}).get("wall_clock_ms") or 0.0) for item in cases]
    passed = sum(1 for item in cases if item.get("evaluation_passed") is True)
    model_calls = sum(int(item.get("metrics", {}).get("model_call_count") or 0) for item in cases)
    return {
        "case_count": len(cases),
        "evaluation_pass_count": passed,
        "model_call_count": model_calls,
        "wall_ms_sum": round(sum(walls), 1),
        "wall_ms_mean": round(sum(walls) / len(walls), 1) if walls else 0.0,
        "wall_ms_p50": percentile(walls, 50),
        "wall_ms_p95": percentile(walls, 95),
        "wall_ms_max": round(max(walls), 1) if walls else 0.0,
    }


def paired_wall_comparison(
    off_cases: list[dict[str, Any]],
    on_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """按 case_id 对齐两侧墙钟；缺失配对不进入加速比。"""

    on_map = {str(item.get("case_id")): item for item in on_cases}
    pairs: list[dict[str, Any]] = []
    off_sum = 0.0
    on_sum = 0.0
    for off in off_cases:
        case_id = str(off.get("case_id"))
        on = on_map.get(case_id)
        if on is None:
            continue
        off_ms = float(off.get("metrics", {}).get("wall_clock_ms") or 0.0)
        on_ms = float(on.get("metrics", {}).get("wall_clock_ms") or 0.0)
        off_sum += off_ms
        on_sum += on_ms
        pairs.append(
            {
                "case_id": case_id,
                "category": off.get("category"),
                "cache_off_wall_ms": off_ms,
                "cache_on_wall_ms": on_ms,
                "speedup": round(off_ms / max(on_ms, 0.001), 3),
                "cache_off_passed": off.get("evaluation_passed"),
                "cache_on_passed": on.get("evaluation_passed"),
                "cache_off_model_calls": off.get("metrics", {}).get("model_call_count"),
                "cache_on_model_calls": on.get("metrics", {}).get("model_call_count"),
            }
        )
    return {
        "pairs": len(pairs),
        "wall_ms_sum_cache_off": round(off_sum, 1),
        "wall_ms_sum_cache_on": round(on_sum, 1),
        "wall_speedup_off_over_on": round(off_sum / max(on_sum, 0.001), 3) if pairs else None,
        "calls": pairs,
    }


def _print(message: str) -> None:
    print(message, flush=True)


def _break_slot_cache() -> dict[str, Any]:
    """发送与 AMR 共享前缀无关的短请求，打断槽内可复用 KV。"""

    environment = dict(os.environ)
    environment["LLM_PROFILE"] = "fast"
    environment.pop("LLM_MODEL", None)
    settings = load_settings(environ=environment, load_dotenv_file=True)
    provider = ModelProvider(settings.model_gateway)
    provider.startup()
    started = time.perf_counter()
    call = provider.generate_text(
        [
            ChatMessage(
                role="system",
                content=f"CACHE-BREAKER {uuid.uuid4().hex} 只回复两个字母 OK。",
            ),
            ChatMessage(role="user", content="OK"),
        ]
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    usage = call.usage
    return {
        "kind": "breaker",
        "wall_ms": round(wall_ms, 1),
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "cached_input_tokens": None if usage is None else usage.cached_input_tokens,
    }


def _run_pevr_phase(*, name: str, cache_enabled: bool, output_dir: Path) -> dict[str, Any]:
    """子进程跑完整 P0-18 在线 PEVR 60 例，stdout 实时转发。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LLM_PROFILE"] = "fast"
    env.pop("LLM_MODEL", None)
    env["LLM_PROMPT_CACHE_ENABLED"] = "true" if cache_enabled else "false"
    env["PYTHONUNBUFFERED"] = "1"
    command = [
        str(PYTHON_EXE),
        "-u",
        "-m",
        "evals.p018.run_eval",
        "--config",
        str(ONLINE_CONFIG),
        "--output-dir",
        str(output_dir),
        "--verification-timeout",
        "120",
    ]
    _print(
        f"\n=== {name} cache_prompt={cache_enabled} output={output_dir} ==="
    )
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), env=env, check=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    report_path = output_dir / "p018_online_eval.json"
    if not report_path.is_file():
        raise RuntimeError(f"{name} 未写出 {report_path}，退出码 {completed.returncode}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = list(report.get("cases") or [])
    if len(cases) != 60:
        raise RuntimeError(f"{name} 例数是 {len(cases)}，不是 60")
    summary = summarize_pevr_cases(cases)
    _print(
        f"=== {name} done exit={completed.returncode} elapsed_ms={elapsed_ms} "
        f"pass={summary['evaluation_pass_count']}/60 "
        f"wall_sum_ms={summary['wall_ms_sum']} p50={summary['wall_ms_p50']} "
        f"p95={summary['wall_ms_p95']} ==="
    )
    return {
        "name": name,
        "cache_prompt": cache_enabled,
        "exit_code": completed.returncode,
        "phase_elapsed_ms": elapsed_ms,
        "report_id": report.get("report_id"),
        "report_digest": report.get("report_digest"),
        "report_status": report.get("status"),
        "report_path": str(report_path),
        "summary": summary,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / "tmp" / "p018_pevr_cache_compare"),
    )
    args = parser.parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    warmup_settings = load_settings()
    if warmup_settings.model_gateway.profile != "fast":
        raise RuntimeError("对照必须使用 Fast Profile")
    ModelProvider(warmup_settings.model_gateway).startup()
    breaker_before_off = _break_slot_cache()
    off_phase = _run_pevr_phase(
        name="cache_off",
        cache_enabled=False,
        output_dir=output_root / "cache_off",
    )
    breaker_before_on = _break_slot_cache()
    on_phase = _run_pevr_phase(
        name="cache_on",
        cache_enabled=True,
        output_dir=output_root / "cache_on",
    )
    paired = paired_wall_comparison(off_phase["cases"], on_phase["cases"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "pevr",
        "dataset_id": "amr-p018-60",
        "prompt_version_p005": P005_PROMPT_VERSION,
        "shared_prefix_id": SHARED_PREFIX_ID,
        "shared_prefix_version": SHARED_PREFIX_VERSION,
        "shared_prefix_sha256": shared_system_prefix_digest(),
        "note": (
            "只比较生产 PEVR 的 60 例墙钟；Fixed/ReAct 未跑。"
            "全量加速比含 sidecar/安全门禁短例；LLM 主链例见 paired.calls。"
        ),
        "breaker_before_cache_off": breaker_before_off,
        "breaker_before_cache_on": breaker_before_on,
        "phases": [
            {key: value for key, value in off_phase.items() if key != "cases"},
            {key: value for key, value in on_phase.items() if key != "cases"},
        ],
        "comparison": {
            "cache_off": off_phase["summary"],
            "cache_on": on_phase["summary"],
            "paired": {
                "pairs": paired["pairs"],
                "wall_ms_sum_cache_off": paired["wall_ms_sum_cache_off"],
                "wall_ms_sum_cache_on": paired["wall_ms_sum_cache_on"],
                "wall_speedup_off_over_on": paired["wall_speedup_off_over_on"],
            },
        },
        "paired_cases": paired["calls"],
    }
    output_path = output_root / "p018_pevr_cache_compare.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _print("\n=== pevr cache comparison ===")
    _print(json.dumps(report["comparison"], ensure_ascii=False, indent=2))
    _print(f"wrote {output_path}")
    if off_phase["summary"]["case_count"] != 60 or on_phase["summary"]["case_count"] != 60:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
