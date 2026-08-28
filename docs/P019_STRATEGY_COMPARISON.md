# P0-19：三策略完整在线闭环对照

P0-19 现在支持 `online_fast_three_strategy_closed_loop`：固定 Workflow、有界 ReAct、生产
PEVR 分别真实执行与 P0-18 完整在线闭环**完全相同**的 `amr-p018-60` 固定 60 例，共
180 个 strategy-case。三者共用 Qwen3.6 Fast 制品、P0-18 在线配置、加难地图、seed、
Prompt、ToolSpec、JWT/HITL 和计分器；按 Latin-square 交错顺序串行运行，避免固定策略总在
冷启动或热启动位置。

离线 `offline_independent_oracle` 仍保留作契约回归，`offline_trace_replay` 只作轨迹可视化；
二者不能替代本页的真实在线结果。

## 三种策略边界

| 策略 | 在线控制语义 | 生产影响 |
| --- | --- | --- |
| `fixed_workflow` | 复用同一 PEVR 图、节点和工具，但关闭故障恢复；不 retry、不 replan。 | 仅评测 Harness。 |
| `react` | 同样关闭生产恢复器；仅在确定性安全门允许时，让 Fast 输出一次严格的 `retry/stop` 结构化决定，最多 retry 1 次、replan 0 次。 | 仅评测 Harness；不保存原始思维链。 |
| `pevr` | 使用现有生产 PEVR 图和默认恢复预算，未修改恢复器、Prompt、工具或 Validator。 | 唯一生产主链。 |

ReAct 的模型调用前安全门固定要求：故障可重试、工具幂等，且无副作用，或已证明副作用未发生。
条件不满足时直接安全停止，模型无权放宽这个门禁。

## 运行与续跑

前置条件与 P0-18 在线评测一致：项目 `.env` 已加载，Qwen3.6 Fast 在
`127.0.0.1:8080` 可用，PostgreSQL/Qdrant 健康；Smart 必须保持禁用。

```powershell
.\scripts\run_p019_compare.ps1 `
  -Mode online `
  -OutputDir tmp\p019_online_strategy_compare
```

长任务会逐条写入 `p019_online_progress.jsonl` 和不可变运行 manifest。中断后使用同一目录：

```powershell
.\scripts\run_p019_compare.ps1 `
  -Mode online `
  -OutputDir tmp\p019_online_strategy_compare `
  -Resume
```

`-Resume` 会校验数据集、配置和 180 条调度摘要；任一变化都 fail closed，只跳过已完整落盘的
`strategy + case_id`，从首个缺失条目继续。

## 2026-08-27 实测结果

- 报告：`p019-online-45906c9d5366a0e9`
- digest：`45906c9d5366a0e9a04f47315481a0da854aa609e39cd777ffce360486ed0de3`
- 状态：`passed`，表示 180/180 已执行、公平性门禁通过、七项零容忍全为 0；**不表示每个正向 case 都完成**。
- 数据集：`evals/p018/dataset.json`，SHA-256=`3a8a8d799a68cedd58ea674c02f1e9a433f16b708c50e4ce9c085a7df4ee3368`。
- P0-18 在线配置 SHA-256：`fc6945218fa22ac64d6eef5ff57414fe987269680c051a0ba98717d5b5ffc5ef`。
- 三策略各 60 个唯一 case，case ID 集与 P0-18 数据集完全一致；进度与原始 JSONL 均为 180 行。

任务完成率的分母是 44 个期望正向终态 case；全例预期符合率还包含正确
`denied/blocked/failed` 的负向 case。异常终态正确率固定覆盖 10 个异常 case。

| 策略 | 全例预期符合 | 任务完成 | 计划合法 | 异常终态正确 | 成功重规划 | 模型调用 | Token | 墙钟 P50 / P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed | 53/60（88.33%） | 37/44（84.09%） | 34/34 | 3/10 | 0/1 | 118 | 763,108 | 43.63 s / 93.73 s |
| ReAct | 54/60（90.00%） | 38/44（86.36%） | 35/35 | 4/10 | 0/2 | 123 | 783,192 | 43.82 s / 83.04 s |
| PEVR | 59/60（98.33%） | 43/44（97.73%） | 35/35 | 9/10 | 5/7 | 132 | 842,143 | 45.25 s / 98.24 s |

这里的“异常终态正确”按异常用例最终 `expected_outcome == observed_outcome` 重算。现有生成报告的
`recovery_terminal_correct_count` 对 PEVR 为 10/10，是因为评测侧 `_exception_recovery_ok`
把 `p018-exception-004` 已执行 retry/replan 且生成 v2 视为“恢复动作成立”；但该例最终
`observed_outcome=failed`，不符合期望的 `completed`，因此不能计入最终终态正确率。原始运行产物
保持不变，本页与 README 采用严格终态口径 9/10。

三策略累计逐例墙钟分别为 2,354,760.858 ms、2,346,645.932 ms、2,536,300.553 ms。
进程级采样峰值 RSS 分别为 16,166.121 MB、16,181.176 MB、16,172.645 MB；报告中的
GPU 峰值均为 0.0 MB。该资源口径包含评测进程、子进程和 8080/18080 服务，只能作本机近似，
不能解释成单节点因果成本。

Fixed 未符合预期的 7 例均在异常集；ReAct 未符合预期 6 例，比 Fixed 多恢复
`p018-exception-005`。PEVR 唯一未符合预期的是 `p018-exception-004`（工位占用）。本轮
ReAct 控制器只在安全门允许的 2 个 case 上调用，共 482 Token，均输出 `retry`；其他不安全或
不适用故障在模型前停止。Trace 只保存 action、reason code、简短 observation summary 和安全事实，
`raw_chain_of_thought_stored=false`。

## 公平性与 PEVR 主链影响

最终报告中的 `same_dataset`、`same_tools`、`same_prompts`、`same_config`、`same_model` 均为
`true`，`react_production_path_touched=false`。在线适配为各策略分配独立 run/trace/principal
身份，并完整保留 P0-17 Trace 的时间、Token、错误、Prompt/模型/工具版本和 metadata。

这次改动**不改变 PEVR 主链的功能结果逻辑**：生产 PEVR 图、恢复器和默认预算保持原样。
PEVR 的 59/60、43/44、异常最终终态 9/10 是一次新的真实在线随机样本；
telemetry 身份和 Trace 完整度因评测隔离而变化，不能要求 report digest 与历史运行相同。

## 产物

- `tmp/p019_online_strategy_compare/p019_strategy_comparison.json`：完整报告及 180 条源 Trace。
- `tmp/p019_online_strategy_compare/p019_strategy_comparison.md`：同一报告对象渲染的汇总。
- `tmp/p019_online_strategy_compare/p019_raw_trajectories.jsonl`：每行一个 strategy-case。
- `tmp/p019_online_strategy_compare/{fixed_workflow,react,pevr}/`：三套独立 P0-18 在线子报告。
- `tmp/p019_online_strategy_compare/p019_online_progress.jsonl`：可恢复进度；属于运行产物，不是源码。

## 限制

- 本次是单机、单模型、单 GPU 的一次运行，没有重复试验或置信区间；`temperature=0.1` 仍可能跨次波动。
- 沿用 P0-18 原分流：正常/充电/可恢复异常/审批走共享八阶段图；RAG、权限、安全、验证类走同一 live sidecar，因此不是所有 60 例都经过 dispatch。
- 有界 ReAct 只研究故障边界上的一次安全重试，不代表开放式、任意工具调用的通用 ReAct Agent。
- 三策略串行交错，不代表三路并行吞吐。Smart 仍为 `deferred`，未启动、未测试。
