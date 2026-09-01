# LLM 延迟指标契约（TTFT / Prefill / E2E）

本文件是仓库内 LLM 调用延迟的唯一口径说明。实现入口：

```powershell
E:\Anaconda\envs\torch128\python.exe -m evals.perf benchmark --repeats 2 --output tmp\ttft_benchmark.json
E:\Anaconda\envs\torch128\python.exe -m evals.perf parse-log --log tmp\llama-server.err.log
E:\Anaconda\envs\torch128\python.exe -m evals.perf restate-legacy
E:\Anaconda\envs\torch128\python.exe -m evals.perf summarize-pevr-llm --report tmp\p018_pevr_llm36_20260901\p018_online_eval.json --log-offset 9398
E:\Anaconda\envs\torch128\python.exe -m evals.perf pevr-ttft
E:\Anaconda\envs\torch128\python.exe -m evals.perf compare-cache --output-root tmp\p018_pevr_llm36_ttft_cache_20260901
```

PEVR 案例级 TTFT 需要显式打开评测探针（生产 `ModelProvider` 仍是 `stream=false`）。默认只打印命令，不会开跑：

```powershell
E:\Anaconda\envs\torch128\python.exe -m evals.p018.run_eval --config evals\p018\online_config.json --output-dir tmp\p018_pevr_ttft --measure-ttft
```

36 LLM 例有/无缓存对照（不是正式 60 例发布报告）：

```powershell
E:\Anaconda\envs\torch128\python.exe -m evals.perf compare-cache --output-root tmp\p018_pevr_llm36_ttft_cache_20260901
```

或 `python -m evals.perf pevr-ttft --run` 跑完整 60 例。产物是 `pevr_ttft_metrics.json` 与 `pevr_ttft_samples.jsonl`。


等价脚本：`scripts/run_ttft_benchmark.py`、`scripts/parse_llama_timing.py`。

生产业务路径 `services.model_gateway.provider.ModelProvider` **保持** `"stream": False`。不要为了测 TTFT 改生产契约。

## 三个指标

| 指标 | 定义 | 时钟 | 单位 | 是否含排队/网络 |
|---|---|---|---|---|
| **TTFT** | 客户端发出 HTTP 请求 → 收到第一个**非空生成文本** SSE `delta.content` | `time.perf_counter()` | ms | 是（本机排队、鉴权代理、网络、服务端排队、Prefill，直到首 token 发出） |
| **Prefill** | llama.cpp 最终 `prompt eval time`（`timings.prompt_ms` 或日志同名字段） | 服务端计算时长 | ms | 否。只含未命中 KV 的 Prompt token 计算 |
| **E2E** | 客户端发出请求 → 流式响应完全结束 | `time.perf_counter()` | ms | 是。Benchmark 路径是**单次模型调用**，不是 P0-18 案例墙钟 |

成功请求上必须满足 `TTFT <= E2E`。TTFT 与 Prefill **没有**固定大小关系：时钟边界不同，Prefill 不含客户端网络，TTFT 含排队且以首个生成 token 为准。

## 明确禁止的做法

1. 用 llama.cpp `prompt processing, progress = 1.00` 当作 Prefill 结束或 TTFT 终点。该字段只保留两位小数，并且在**下一批** Prompt token 处理前打印；`(N-4)/N` 已可能显示成 `1.00`。
2. 没有 progress 日志时令 `TTFT = prompt eval time`。progress 缺失也可能只是没达到约 3 秒打印阈值，**不等于** prefix KV 命中。
3. 用 Prefill 回填缺失的 TTFT，或把 Prefill 加速比标成 TTFT 加速比。
4. 把 PEVR 案例 `wall_clock_ms`（含 C++/仿真）标成单次模型 E2E。
5. 把 warmup、breaker、超时、HTTP 失败、不完整流、并发错配样本送进百分位。

无法取得真实 TTFT 时，输出 `ttft_ms=null` 和 `ttft_missing_reason`，例如 `non_streaming_response`、`legacy_log_only_no_client_clock`、`timeout`。

## 样本关联

Benchmark **串行**、`parallel_slots=1`。每次请求：

- 生成 `request_id`，放在 HTTP 头 `X-AMR-Perf-Request-Id`（不写入 Prompt，以免破坏前缀 KV）。
- 若提供 `tmp/llama-server.err.log`，在请求前后按文件偏移读取增量；增量里必须恰好一条 `prompt eval time`，否则 Prefill 记为 `mismatched_server_log`。
- 进程内 `SerialRequestGuard` 拒绝重叠请求。

## 旧数据如何引用

`tmp/p018_pevr_cache_compare_20260831/llm_only_cache_metrics.json` 里的 **TTFT 数字不可用于正式结论**（`progress=1.00` 提前约 110–264 ms，且与 Prefill 混用）。

**仍可保留：**

- Prefill p50 无缓存 4829.7 ms / 有缓存 3349.2 ms，约 **1.44×**
- Token 命中率 0% / 43.2%
- 36 个 LLM 案例 PEVR 墙钟 p50 约 **1.10×**

用 `python -m evals.perf restate-legacy` 生成带 `ttft_status=invalid_do_not_cite` 的副本，不覆盖原始 JSON。

## 2026-09-01 流式 TTFT 对照（可引用）

证据：`tmp/p018_pevr_llm36_ttft_cache_20260901/llm36_ttft_cache_compare.json`（SHA-256=`3D683EED…2B00CE`）。只跑 36 个 LLM 例，评测探针 `stream=true`，**不是**正式 P0-18 60 例分数。`filled_from_prefill=false`，未使用 `progress=1.00`。

| 指标 | 无缓存 | 有缓存 | 无/有 |
|---|---:|---:|---:|
| TTFT p50（首个非空 SSE delta） | 5099.6 ms | 3299.4 ms | **1.546×** |
| Prefill p50（`timings.prompt_ms`） | 4997.1 ms | 3176.4 ms | 1.573× |
| 单次模型 E2E p50 | 20165.3 ms | 14139.0 ms | 1.426× |
| 案例墙钟 p50（含 C++/仿真） | 72.6 s | 62.4 s | 1.164× |
| 缓存命中率 | 0.0% | 44.6% | — |
| 有效 TTFT 样本 | 132 | 132 | — |

不要用本表 Prefill 1.57× 覆盖上一节 8/31 非流式日志 Prefill 1.44×。两侧通过 35/36，失败例均为 `p018-exception-004`。
