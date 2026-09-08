"""从不可改写的实验原始报告生成模型对比，单列空答案消耗和共同成功样本。

报告读取器不启动模型、不修改评分，避免把“HTTP 请求发生过”与现有 Harness 的
“成功进入 Trace 的模型调用”混为一谈，也避免用快速失败宣称任务加速。
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from evals.perf.llm36 import LLM_CASE_IDS
from evals.perf.veryfast import CONFIG_PATH, baseline, read_json, stats, write_json
from evals.p018.reproducibility import sha256_file


def summarize_cases(report):
    """终态正确性按 expected==observed 独立重算，不把恢复动作正确混作任务完成。"""
    cases = report["cases"]
    llm = [c for c in cases if c["case_id"] in LLM_CASE_IDS]
    exception = [c for c in cases if c["case_id"].startswith("p018-exception-")]
    return {"case_count": len(cases), "passed": sum(c["evaluation_passed"] for c in cases),
            "llm_case_count": len(llm), "llm_passed": sum(c["evaluation_passed"] for c in llm),
            "exception_terminal_correct": sum(c["expected_outcome"] == c["observed_outcome"] for c in exception),
            "llm_wall_ms": stats([c["metrics"]["wall_clock_ms"] for c in llm]),
            "all_wall_ms": stats([c["metrics"]["wall_clock_ms"] for c in cases]),
            "positive_completed": report["metrics"]["agent"].get("task_completion_count"),
            "positive_count": report["metrics"]["agent"].get("positive_case_count"),
            "harness_model_calls": report["metrics"]["agent"].get("model_call_count"),
            "failure_codes": dict(Counter(c.get("failure_code") for c in cases if not c["evaluation_passed"]))}


def summarize_requests(rows):
    """HTTP 200 空答案仍计请求和 token；无 reasoning token 字段时只报字符数。"""
    responses = [r for r in rows if r["status"] == "response"]
    timing = [r["timings"] for r in responses if r.get("timings")]
    return {"http_attempts": len(rows), "http_responses": len(responses),
            "transport_errors": sum(r["status"] == "error" for r in rows),
            "empty_final_content": sum(not (r.get("content") or "").strip() for r in responses),
            "finish_reasons": dict(Counter(r.get("finish_reason") for r in responses)),
            "reasoning_observed_responses": sum(r.get("reasoning_chars", 0) > 0 for r in responses),
            "prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens") or 0 for r in responses),
            "output_tokens_including_reasoning": sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in responses),
            "prefill_ms": stats([r["prompt_ms"] for r in timing]),
            "decode_tps": stats([r["predicted_per_second"] for r in timing]),
            "http_wall_ms": stats([r["wall_ms"] for r in rows]),
            "ttft_ms": None, "ttft_status": "non_streaming_response"}


def build_report(directory):
    """按 case_id 配对，只在两侧都通过时给任务速度比；无配对时明确不可计算。"""
    config = read_json(CONFIG_PATH)
    old = baseline(config)
    path = directory / "pevr60/p018_online_eval.json"
    new = read_json(path)
    import json
    rows = [json.loads(line) for line in (directory / "pevr60/requests.jsonl").read_text(encoding="utf-8").splitlines()]
    old_index = {c["case_id"]: c for c in old["cases"]}
    pairs = [c for c in new["cases"] if c["case_id"] in LLM_CASE_IDS and c["evaluation_passed"] and old_index[c["case_id"]]["evaluation_passed"]]
    old_ms = sum(old_index[c["case_id"]]["metrics"]["wall_clock_ms"] for c in pairs)
    new_ms = sum(c["metrics"]["wall_clock_ms"] for c in pairs)
    result = {"baseline": summarize_cases(old), "veryfast": summarize_cases(new),
              "requests": summarize_requests(rows), "paired_llm_successes": len(pairs),
              "paired_case_ids": [c["case_id"] for c in pairs],
              "paired_wall_speedup": old_ms / new_ms if new_ms else None,
              "selection": read_json(directory / "selected.json"),
              "zero_tolerance": new["metrics"]["zero_tolerance"],
              "baseline_sha256": config["baseline_sha256"], "veryfast_sha256": sha256_file(path)}
    write_json(directory / "analysis.json", result)
    a, b, q = result["baseline"], result["veryfast"], result["requests"]
    speed = f"{result['paired_wall_speedup']:.3f}×" if result["paired_wall_speedup"] is not None else "不可计算（无共同成功 LLM 案例）"
    text = f"""# VeryFast 与 Qwen3.6 历史 PEVR 对照

固定 16K、4096 单请求输出上限、120s 单次生成超时、原业务累计预算和同一 60 例。VeryFast 开思考（不单独截断），Qwen 历史关思考。

| 指标 | Qwen3.6 历史 | VeryFast 本次 |
|---|---:|---:|
| 全例符合预期 | {a['passed']}/60 | {b['passed']}/60 |
| 固定 LLM 案例符合预期 | {a['llm_passed']}/36 | {b['llm_passed']}/36 |
| 正向任务完成 | {a['positive_completed']}/{a['positive_count']} | {b['positive_completed']}/{b['positive_count']} |
| 异常终态与预期相同 | {a['exception_terminal_correct']}/10 | {b['exception_terminal_correct']}/10 |
| LLM 案例墙钟 p50 | {a['llm_wall_ms']['p50']/1000:.2f}s | {b['llm_wall_ms']['p50']/1000:.2f}s |
| LLM 案例墙钟 p95 | {a['llm_wall_ms']['p95']/1000:.2f}s | {b['llm_wall_ms']['p95']/1000:.2f}s |
| Harness Trace 调用数 | {a['harness_model_calls']} | {b['harness_model_calls']} |

共同成功 LLM 案例：{len(pairs)}；配对墙钟加速比：{speed}。全量墙钟包括失败，不能直接解释为有效任务加速。

本次实际 HTTP 请求 {q['http_attempts']} 次，收到响应 {q['http_responses']} 次，传输错误 {q['transport_errors']} 次；空最终答案 {q['empty_final_content']} 次；finish_reason={q['finish_reasons']}。思考被实际观测到的响应 {q['reasoning_observed_responses']} 次；输出 token（含思考）{q['output_tokens_including_reasoning']}。这些实际请求不能被 Harness 在未成功落 Trace 时的较小调用数替代。

七项零容忍：{result['zero_tolerance']}。安全零容忍为零不等于所有任务完成。TTFT 未测，非流式不可用 Prefill 回填。

失败码统计：{b['failure_codes']}。完整案例、最终响应、预算耗尽证据见 pevr60/。

这是单轮历史对照；模板/tokenizer、运行时和当前新增 STL gate 存在差异。只对本次 Q4_K_M 制品、硬件、预算和任务范围成立。未重跑 Qwen、未启用 Smart。

Qwen 报告 SHA-256：{result['baseline_sha256']}。
VeryFast 报告 SHA-256：{result['veryfast_sha256']}。
"""
    (directory / "MODEL_COMPARISON.md").write_text(text, encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_report(args.output.resolve())
    print(f"VeryFast {result['veryfast']['passed']}/60, HTTP={result['requests']['http_attempts']}")


if __name__ == "__main__":
    main()
