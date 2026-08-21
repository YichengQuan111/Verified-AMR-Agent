# P0-18：60 例自动评测

P0-18 提供统一的 `EvalHarness`、固定 60 例数据集和一条命令入口。评测集只用于
验收，不进入训练、微调或 Prompt 示例；每个结果都保留预期终态、观察终态、Trace、
证据、失败原因和副作用 ID。

## 一键运行

在仓库根目录执行：

```powershell
.\scripts\run_p018_eval.ps1
```

也可以显式指定项目解释器和输出目录：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m evals.p018.run_eval `
  --output-dir .\tmp\p018_eval
```

命令成功返回退出码 `0`，并生成：

- `tmp/p018_eval/p018_eval.json`：完整机器可读报告，包含 60 个逐例结果和复现指纹；
- `tmp/p018_eval/p018_eval.md`：由同一个 `EvalReport` 渲染的人读报告。

退出码 `2` 表示意外失败、指标不符合预期或任一零容忍项非零。安全反例正确返回
`denied`/`blocked` 不算 Harness 失败，但仍会写入报告的负向案例章节。

## 固定数据集

数据集文件为 `evals/p018/dataset.json`，数据集 ID 为 `amr-p018-60`，版本为
`p0-18.v1`。契约在加载阶段强制 `purpose=evaluation_only`、`is_training_data=false`、
60 个唯一 `case_id` 和唯一 seed，配额固定如下：

| 分区 | 例数 | 覆盖内容 |
| --- | ---: | --- |
| `normal_order_charging` | 25 | 正常订单、依赖、路线/充电与副作用幂等 |
| `rag_permission_approval` | 10 | RAG 命中、引用、ACL、权限与审批 |
| `exception_local_replan` | 10 | 低电量、离线、封路、占用、超时、不可行、状态冲突、有限局部重规划 |
| `verification` | 5 | 固定 CTest、pytest、仿真以及 Trace/报告完整性 |
| `prompt_injection_security` | 10 | Prompt Injection、越权工具、跨角色泄漏、Shell/SQL/代码选择器、审批绕过 |

固定环境引用为 `warehouse_v1@seed-v1`，地图、AMR、订单分别来自：

- `domains/amr_warehouse/data/warehouse_v1.json`
- `domains/amr_warehouse/data/amrs_v1.json`
- `domains/amr_warehouse/data/orders_seed_v1.json`

## 复现边界

`evals/p018/config.json` 是版本为 `p0-18.v1` 的执行配置。每次报告记录以下内容的
SHA-256、大小或版本：

- 数据集、配置、地图、AMR、订单和五份 P0-05 Prompt；
- 每例固定 seed 及 seed 摘要；
- 运行时实际注册的九个 P0-12 `ToolSpec` 名称/版本；
- Fast Profile：`qwen3.6-fast`、Qwen3.6、GGUF、上下文 8192、temperature 0、关闭 reasoning；
- Smart Profile：`qwen3.8-smart`，保持 `enabled=false`，不启动；
- Git revision/dirty 状态、Python/platform 和评测配置版本。

默认 `execution_mode=offline_deterministic_oracle`：Harness 使用冻结 fixture、真实的
P0-15 故障分类/恢复、P0-16 授权/HITL、P0-17 Trace/受控验证组件，不调用在线模型，
因此报告中的 `agent.model_call_count` 应为 `0`。这保证离线验收可复现，也明确不能把
本结果误写成在线 LLM 质量验收。P0-07 原有的 20 例实时 PostgreSQL/Qdrant RAG 评测
仍保留在 `evals/rag/`，不被本离线固定 fixture 冒充。

## 指标与零容忍门槛

JSON/Markdown 报告按 Agent、RAG、AMR、安全、恢复、验证六个域汇总，并保留类别通过率、
负向观察数、Trace 完整率、引用/ACL、路线安全、局部重规划/重试、审批恢复、验证失败
定位等指标。七个零容忍计数必须全部为 `0`：

`vertex_collision_count`、`edge_collision_count`、`forbidden_zone_entry_count`、
`low_battery_violation_count`、`role_leak_count`、`duplicate_side_effect_count`、
`approval_bypass_count`。

其中路线逐步检查顶点和交换边，固定地图约束检查禁行区/禁行边和电量安全阈值；安全
分区检查 ACL、工具角色、执行选择器和审批票据；恢复分区检查 Effect Ledger/副作用 ID
唯一性。Harness 根据逐例事实重新汇总，不能从期望值直接填充零容忍指标。

## 失败轨迹

`EvalReport.cases` 始终包含全部 60 例。对于预期拒绝、预期阻塞或预期失败，
`evaluation_passed=true` 只表示系统正确执行了该负向契约，`observed_outcome` 仍保留
`denied`、`blocked` 或 `failed`，并写入 `failure_code`、`failure_reason`、Trace 和证据。
对于意外失败，报告状态为 `failed`，同样保留完整轨迹，方便后续定位；不会用删除失败
样例的方式提高成功率。

## 当前验收结果

2026-08-21 发布审计确认：旧 `tmp/p018_eval_final` / `tmp/p018_eval_p020_final` 的 60/60
**不能作为发布验收**，因为当时 runner 不消费 `case.oracle`，注入样例也未把攻击文本送进
Prompt。本步已改为独立 `evals/p018/oracle.py`：未知键 fail closed，`must_fail` 与重复副作用
突变必须失败；注入文本进入 `plan_tasks` 的不可信 RAG 上下文。

P0-18 仍然是 `offline_deterministic_oracle`。新的 60/60 只证明离线契约/安全/恢复回归，
`model_call_count=0`，不能写成 60 例在线 Fast 质量分。重新生成的 report digest 以本步实际
命令为准，废止引用 `p018-415f0b3f59574772`。

P0-18 专项测试为：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest `
  tests\unit\test_p018_eval.py -q -p no:cacheprovider
```

全量项目门禁仍使用：

```powershell
.\scripts\run_smoke.ps1
```

## Fast 在线补充验证

2026-08-21 另外启动了固定 Fast 脚本并进行真实模型调用，Smart 未启动：

- `check_model_gateway.py --profile fast`：alias 门禁通过，served alias 为 `qwen3.6-fast`；
- `smoke_llm_structured.py`：20/20 结构化输出通过；
- `smoke_p005_prompts.py --profile fast`：五个 P0-05 节点 5/5 通过，均未触发修复；
- `run_p013_e2e.py --approve-dispatch`：在 `plan_tasks` 失败，模型返回请求 9086 tokens
  超过服务 `--ctx-size 8192`，退出码为 1，未生成在线 E2E 报告。

Fast 服务已在测试后停止，8080 无监听。该失败不改变 P0-18 离线 60 例的结果，但说明
在线 PEVR 需要先压缩计划节点上下文或重新评估固定窗口，不能把离线 oracle 或单节点通过
当作在线全链路通过。

随后用户将外部 Fast 服务上下文改为 `--ctx-size 16384` 并复测：网关通过、结构化输出
20/20、P0-05 五节点 5/5，P0-13 真实在线闭环退出码 0，8/8 阶段、4 次模型调用、5/5
工具成功、Validator error=0、仿真 `completed`、`ORDER-001` 完成，在线报告为
`tmp/p013_e2e_model_test_16k.json`。本次只更新在线服务事实；P0-18 离线配置仍明确记录
`context_window=8192`，不能把两种执行配置混为同一份复现结果。

为排除固定 `run_id` 恢复旧 Checkpoint 的影响，随后使用全新
`p013-e2e-fast-16k-fresh-20260821` 从头执行 P0-13；结果仍为退出码 `0`、8/8 阶段、4
次模型调用、5/5 工具成功、Validator error=0、仿真完成、`ORDER-001` 完成，报告位于
`tmp/p013_e2e_model_test_16k_fresh.json`。该 fresh run 是 16K 在线全链路通过的主要证据。

## P0-20 最终复测记录

为收口部署与交付，实际执行：

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p018_eval_p020_final
```

退出码为 0，60/60 通过；报告为 `tmp/p018_eval_p020_final/p018_eval.json`、
`report_id=p018-415f0b3f59574772`、`report_digest=415f0b3f59574772ad71df36591628f756a79dd26224d356d5f7fb1afb0f4585`。
本次 `model_call_count=0`、正确负向观察 16 例，RAG/Agent/AMR/安全/恢复/验证指标与七项
zero tolerance 结果均保持既有口径：所有通过率为 1.0，七项零容忍均为 0。该报告仍是
`offline_deterministic_oracle`，不能写成 60 例在线 Fast 模型质量验收；在线 Fast 证据另见
`docs/TEST_REPORT.md`。
