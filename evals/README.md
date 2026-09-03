# Evaluations

本目录保存版本化 P0 正常、故障、权限、安全和策略对比评测。评测代码不得把故障注入能力暴露给正常 Agent 工具注册表。

P0-07 的固定 20 例仓储 RAG 数据位于 `rag/cases.json`，执行入口为：

```powershell
python -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

需要同时从 6 份 frozen Markdown 重建 PostgreSQL/Qdrant 索引时增加 `--rebuild-index`。报告固定输出 Recall@K、MRR、Section Recall@K、Precision@K、nDCG@K、Citation、answerability 与 ACL；新增的 Precision/nDCG 可用 `--min-precision-at-k`、`--min-ndcg-at-k` 显式设门禁。指标定义、ACL/拒答边界和当前实测见 `docs/RAG.md`。

## P0-18 统一 60 例评测

P0-18 的固定数据集位于 `p018/dataset.json`，严格包含 25 例正常订单/充电、10 例
RAG/权限/审批、10 例异常/局部重规划、5 例 CTest/pytest/仿真验证和 10 例
Prompt Injection/越权/审批绕过。Harness 会记录固定地图、订单、seed、模型/量化、
Prompt、九个 ToolSpec、配置版本和 Git 指纹，并输出全部逐例 Trace（包括正确拒绝和
意外失败）。

一条命令运行：

```powershell
.\scripts\run_p018_eval.ps1
```

报告写入 `tmp/p018_eval/p018_eval.json` 和 `tmp/p018_eval/p018_eval.md`。默认是
`offline_deterministic_oracle`，不启动模型服务；七个零容忍指标（顶点/边碰撞、禁行区、
低电量、跨角色泄漏、重复副作用、审批绕过）必须全部为 0。详细契约、指标和限制见
`docs/P018_EVAL.md`。

### 真实 Fast 在线闭环（加难地图，独立于离线 60/60）

生产 `warehouse_v1.json` 不变。在线模式使用 `warehouse_v1_hard` 货架墙（通道对齐工位行）+ 每例 2 个
seed 通道障碍，调用真实 Qwen3.6 Fast。完成率按观察终态记录，不预先调百分比。
演示页 `/demo` 使用同一张加难图，但额外障碍是固定 2 格且保持全部 P→S 走廊连通，避免任意自然语言选点无解。

```powershell
.\scripts\run_p018_online_eval.ps1
```

报告默认写入 `tmp/p018_online_eval/`。2026-08-23 当前引用写入
`tmp/p018_online_eval_ease_verifier/`（`p018-online-fa1d397a8f60ad17`，43/44、10/10、充电 5/5 charged、正常订单 20/20）。
旧 `tmp/p018_online_eval_recovery/`（`p018-online-fad484647c97878f`，22/44、9/10）只作对照。退出码 0 只保证
60 例已执行且零容忍为 0。实测率见 `docs/P018_EVAL.md` 与 `docs/RESUME_FACTS.md`。

## P0-19 策略对照实验

P0-19 保留 `offline_independent_oracle` 契约回归和只作可视化的 `offline_trace_replay`，并新增
`online_fast_three_strategy_closed_loop` / `p0-19.online.v2`。在线模式在共享 Guard、Understand、
初次 Retrieve 之后，让 Plan-and-Execute 图、独立 ReAct 循环、生产 PEVR 图分别执行同一 60 例，共 180 条。
`plan_execute` 与 `pevr` 同图同 Prompt，只差 `fault_recovery_enabled`，二者构成 verify→replan 环的消融；`react` 是唯一跨范式对照。
ReAct 不得调用 `PEVRGraphRunner`，本轮不重复检索，不保存原始思维链。旧 v1 一次 retry 适配器已作废。
Smart 继续延期。

```powershell
.\scripts\run_p019_compare.ps1 -Mode online -OutputDir tmp\p019_online_strategy_compare
```

不要用旧 v1 progress `--resume`。当前默认结果已覆盖写入上述目录：报告
`p019-online-5bf27026e1607cfe`，ReAct/Plan-and-Execute/PEVR 全例符合 46/60、52/60、59/60，
零容忍全 0。完整契约见
[docs/P019_STRATEGY_COMPARISON.md](../docs/P019_STRATEGY_COMPARISON.md)。

## P1-1 STL 与规则验证器布尔一致性核对

`evals/stl_consistency/harness.py` 不依赖模型：用正式 `ToolRegistry`（生产 Hungarian + A*，
P0-18 加难地图、每例 seed 障碍与电量/release 覆盖）为 P0-18 中会经过运输主链的 32 个用例生成
真实计划，再施加 13 种确定性变异（超载、低电、封路格/边、单向逆行、deadline/release、截断路径、
空闲 AMR 闯入、安全距离 2、无路线前置）和 6 个合成多车冲突场景，对每个计划分别跑
`fleet_plan_validator_cli --validate` 与 `--validate --stl-spec`，逐公式比对
“公式违反 ⟺ 对应规则错误码出现”。任何不一致都以非零退出码失败。

```powershell
.\scripts\run_stl_consistency.ps1
```

报告写入 `tmp/stl_consistency/stl_consistency.{json,md}`。2026-09-03 实测：453 个计划
（32 基础 + 415 变异 + 6 合成）计划级一致 453/453，3171 次公式级核对 0 不一致，
STL 单次增量 +1.2 ms。契约与语义见 [docs/P1_STL_VALIDATOR.md](../docs/P1_STL_VALIDATOR.md)。

## LLM 延迟 Benchmark（TTFT / Prefill / E2E）

TTFT 必须用流式首 token 测量，不能用 llama.cpp `progress=1.00` 或 Prefill 回填。
生产 `ModelProvider` 仍是 `stream=false`。契约见 [docs/LLM_LATENCY_METRICS.md](../docs/LLM_LATENCY_METRICS.md)。

```powershell
python -m evals.perf benchmark --repeats 2 --output tmp\ttft_benchmark.json
python -m evals.perf benchmark --compare-cache --repeats 2
python -m evals.perf restate-legacy
python -m evals.perf summarize-pevr-llm --report tmp\p018_pevr_llm36_20260901\p018_online_eval.json --log-offset 9398
python -m evals.perf pevr-ttft
python -m evals.perf compare-cache --output-root tmp\p018_pevr_llm36_ttft_cache_20260901
```

36 LLM 例有/无 `cache_prompt` 对照须显式 `--measure-ttft --llm-only`（不是正式 60 例发布报告）。2026-09-01 证据在 `tmp/p018_pevr_llm36_ttft_cache_20260901/`。

P0-18 在线 60 例若要记录真实 TTFT，须显式 `--measure-ttft`（默认关闭，生产网关仍非流式）：

```powershell
.\scripts\run_p018_online_eval.ps1 -OutputDir tmp\p018_pevr_ttft -MeasureTtft
```
