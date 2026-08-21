# P0-20：3 分钟演示脚本

本脚本只演示已经实现的 P0 主链。演示前先按 [`SERVICES_STARTUP.md`](SERVICES_STARTUP.md) 启动 Compose 栈和宿主机 Fast；Fast 使用 `qwen3.6-fast`，Smart 不启动。若现场模型冷启动较慢，使用已生成的真实 JSON 报告展示证据，不把短冒烟输出代替 PEVR。

## 0:00–0:20：服务与边界

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
.\scripts\start_local.ps1
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\check_model_gateway.py --profile fast
docker compose ps
```

口播：Compose 只承载 API、PostgreSQL、Qdrant；Qwen3.6 Fast、Embedding、C++ CLI 和仿真仍在 Windows 宿主机。`/health` 代表 API 进程健康；模型是否可用必须由宿主机网关预检确认。

## 0:20–1:20：自然语言订单到确定性执行

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\run_p013_e2e.py `
  --run-id p020-demo-live-20260821 `
  --approve-dispatch `
  --output .\tmp\p020_demo_live.json
```

该命令只有在 Fast 已通过网关预检且长上下文吞吐满足 P0-13 固定 300 秒预算时才算现场成功；
若现场 fresh run 返回非零，立即停止把它当作通过，改为展示 `docs/TEST_REPORT.md` 中三个正式
成功 run_id 的 PostgreSQL 事件/Trace 证据。不要复用旧 run_id 来掩盖失败。

现场按 JSON/终端依次指出：

1. 自然语言订单：`请把 MAT-001 从 P1 运到 S3，并在截止时间前完成。`
2. `guard → understand`：Fast 只负责理解并输出严格 `TaskContract`。
3. `retrieve`：本地 Embedding + Qdrant/BM25 返回带版本、ACL 和 citation 的 RAG 证据。
4. `plan`：模型输出受限 DAG；不是模型直接调用 Shell 或机器人。
5. `execute`：白名单工具依次调用 C++ Hungarian、C++ A*、C++ Validator 和 Python 离散仿真。

## 1:20–2:10：验证、异常恢复与安全边界

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest `
  tests\unit\test_p015_faults.py `
  tests\integration\test_p015_fault_recovery.py -q -p no:cacheprovider
```

口播：Validator 对完整计划重新检查硬约束；Observation 验证决定是否完成。低电量、AMR 离线、封路、工位占用、超时和无解会先进入固定分类表，再按有限 `retry/replan/fallback/human/fatal` 策略处理。局部重规划只替换受影响且未完成的 DAG 后继，已完成 Effect Ledger 不重复派发；预算耗尽或副作用状态未知时 fail closed 转人工。

## 2:10–2:40：证据报告

```powershell
$report = Get-Content .\tmp\p020_demo_live.json -Raw | ConvertFrom-Json
$report.report | Select-Object run_id,final_status,plan_version,completed_order_ids,metrics,tool_evidence,citations | ConvertTo-Json -Depth 8
```

展示 `final_status=completed`、固定 8 阶段、RAG citations、工具证据、Validator 错误数、仿真事件和 `ORDER-001` 完成。Trace/Event、Checkpoint、Effect Ledger 和报告共同构成可复核证据，不用模型自述“已完成”作为事实。

## 2:40–3:00：验收结论

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p018_eval_p020_final
.\scripts\run_p019_compare.ps1 `
  -SourceReport .\tmp\p018_eval_p020_final\p018_eval.json `
  -OutputDir .\tmp\p019_strategy_compare_p020_final
```

口播：P0-18 的 60 例是离线确定性 oracle，不是在线 LLM 质量分；P0-19 是同源 Trace Replay，不是三策略在线采样。简历和演示只引用 [`RESUME_FACTS.md`](RESUME_FACTS.md) 中已经实测的事实；Smart 仍是禁用/延期，不包装成对照完成。

## 真实连续闭环证据

正式 P0-13 审计已用以下三个独立 `run_id` 连续完成真实 Fast 闭环：

- `p014-fast-online-1-20260821-1037`
- `p014-fast-online-2-20260821-1038`
- `p014-fast-online-3-20260821-1039`

三次均为 8/8 阶段、5/5 工具、Validator error=0、仿真 `completed`、`ORDER-001` 完成、4 次模型调用。P0-20 新鲜重测若遇到宿主机 Fast 长上下文吞吐不足，必须在报告中另列失败原因，不得覆盖这组三次正式证据。
