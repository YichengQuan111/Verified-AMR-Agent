# P0-19：三策略完整在线闭环对照

P0-19 当前发布口径是 `online_fast_three_strategy_closed_loop` / **`p0-19.online.v2`**：
固定 Workflow、**独立 ReAct 循环**、生产 PEVR 分别真实执行与 P0-18 完整在线闭环相同的
`amr-p018-60` 固定 60 例，共 180 个独立 strategy-case。

三者只共享策略无关前置：

```text
Guard/Auth → Understand → 初次 Retrieve → 冻结共同初始事实
                                          ├─ Fixed Workflow 图
                                          ├─ Independent ReAct Loop
                                          └─ Production PEVR
```

共用 Qwen3.6 Fast 制品、P0-18 在线配置、加难地图、seed、初次 Retrieve、ToolSpec、
JWT/HITL、Validator 和预算包络。ReAct 控制 Prompt（`amr.eval.p019.react_agent` / `2.1.0`，前置共享 system 前缀）
与 PEVR 的 `amr.p005.*@1.2.0` 不同，因此 **`same_prompts=false`**。按 Latin-square 交错串行运行。

`p0-19.online.v1` 把异常后一次 `retry/stop` 误称为 ReAct，并复用 `PEVRGraphRunner`；该实现
与其指标已作废，不能 `--resume` 进 v2，也不得再作为当前结果引用。

离线 `offline_independent_oracle` 仍保留作恢复额度契约回归；其中名为 `react` 的槽位是
`max_retries=1` 遗留夹具，不是独立 ReAct Agent。`offline_trace_replay` 只作轨迹可视化。

## 三种策略边界

| 策略 | 在线控制语义 | 生产影响 |
| --- | --- | --- |
| `fixed_workflow` | 共享前置后进入固定八阶段图；关闭故障恢复，不 retry、不 replan。 | 仅评测 Harness。 |
| `react` | 共享前置后进入独立 `decide → guard → act → observe → terminal_check` 循环；不实例化 `PEVRGraphRunner`，不生成四任务 DAG，本轮不重复检索。 | 仅评测 Harness；不保存原始思维链。 |
| `pevr` | 共享前置后进入生产 PEVR 图和默认恢复预算。 | 唯一生产主链。 |

独立 ReAct 的确定性门禁（模型无权放宽）：ToolSpec/JSON Schema、角色、JWT Principal、
白名单、工具依赖、幂等与未知副作用、Validator digest、HITL 签名审批、Effect Ledger、
以及 Token/时间/工具步/循环上限。`dispatch_simulation` 必须同时满足最近一次
`validate_fleet_plan` 成功、计划 digest 一致、审批票据绑定 run/task/plan/digest、同一
Effect 最多一次。模型 `finish` 只表示请求完成，终态由确定性检查确认。

异常指标：

- `recovery_terminal_correct_count`：严格 `expected_outcome == observed_outcome`
- `successful_recovery_count`：确实发生恢复动作且最终完成

禁止把“执行过 retry/replan”计为最终终态正确。

## 运行与续跑

前置条件与 P0-18 在线评测一致：项目 `.env` 已加载，Qwen3.6 Fast 在
`127.0.0.1:8080` 可用，PostgreSQL/Qdrant 健康；Smart 必须保持禁用。必须不带 `-Resume`
启动新的 v2 进度；旧 v1 manifest 会被拒绝。

```powershell
.\scripts\run_p019_compare.ps1 `
  -Mode online `
  -OutputDir tmp\p019_online_strategy_compare
```

长任务会逐条写入 `p019_online_progress.jsonl` 和含 `config_version` /
`react_runner_version` 的运行 manifest。仅当 manifest 与当前 v2 身份完全一致时，
才允许对同一目录 `-Resume`。

## 当前实测结果

报告 `p019-online-5bf27026e1607cfe`，digest
`5bf27026e1607cfebf641d285221fdda978fe5b97c5bc6959968ff6e8d82f456`，版本
`p0-19.online.v2`，模式 `online_fast_three_strategy_closed_loop`，状态 **passed**。
180/180 独立身份；`react_uses_pevr_runner=false`；七项零容忍均为 0。
数据集 SHA-256=`3a8a8d799a68cedd58ea674c02f1e9a433f16b708c50e4ce9c085a7df4ee3368`，
P0-18 在线配置 SHA-256=`fc6945218fa22ac64d6eef5ff57414fe987269680c051a0ba98717d5b5ffc5ef`，
P0-19 在线配置 SHA-256=`c077dfcf4f40e8c0c6176bdbd8df0482305326385ce5e597721eeece7f62da48`。
异常终态已按 `expected_outcome == observed_outcome` 复核：3/10、6/10、9/10。

| 策略 | 全例预期符合 | 任务完成 | 计划合法 | 异常终态正确 | 成功恢复 | 模型调用 | Token | 墙钟 P50/P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed_workflow | 52/60 | 36/44 | 33/36 | 3/10 | 0/1 | 117 | 757143 | 57768 / 87715 |
| react | 46/60 | 30/44 | 24/32 | 6/10 | 4/4 | 326 | 888837 | 49098 / 84508 |
| pevr | 59/60 | 43/44 | 34/37 | 9/10 | 6/7 | 132 | 841688 | 59156 / 95214 |

PEVR 唯一未符合预期仍为 `p018-exception-004`（工具步预算耗尽）。ReAct 低于 Fixed/PEVR
是独立循环的真实结果，不是 v1 一次 retry 的 54/60。ReAct 主路径 Trace 不含
`plan_tasks`/`PEVRGraphRunner` 节点；正常例可见多轮 `react_decide`/`react_act`/`react_observe`。

## 公平性

在线 v2 报告记录：

- `same_shared_context_contract`
- `same_initial_retrieval`
- `same_tools`
- `same_safety_gates`
- `same_budget_envelope`
- `same_model`
- `strategy_prompt_versions`
- `react_uses_pevr_runner=false`
- `react_production_path_touched=false`
- `same_prompts=false`（控制 Prompt 按策略不同，这是事实而不是实验失败）

## 产物

- `tmp/p019_online_strategy_compare/p019_strategy_comparison.json`：完整报告及 180 条源 Trace。
- `tmp/p019_online_strategy_compare/p019_strategy_comparison.md`：同一报告对象渲染的汇总。
- `tmp/p019_online_strategy_compare/p019_raw_trajectories.jsonl`：每行一个 strategy-case。
- `tmp/p019_online_strategy_compare/{fixed_workflow,react,pevr}/`：三套独立 P0-18 在线子报告。
- `tmp/p019_online_strategy_compare/p019_online_progress.jsonl`：可恢复进度；属于运行产物，不是源码。

## 限制

- 本次是单机、单模型、单 GPU 的一次运行，没有重复试验或置信区间；`temperature=0.1` 仍可能跨次波动。
- 沿用 P0-18 分流：正常/充电/可恢复异常/审批走各策略主路径；RAG、权限、安全、验证类走同一 live sidecar。
- 本轮 ReAct 在共享初次 Retrieve 后禁止再检索；自主重复检索是另一实验。
- 独立 ReAct 仍只能使用白名单工具，不是任意代码/Shell Agent。
- 三策略串行交错，不代表三路并行吞吐。Smart 仍为 `deferred`，未启动、未测试。
