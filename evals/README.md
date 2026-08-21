# Evaluations

本目录保存版本化 P0 正常、故障、权限、安全和策略对比评测。评测代码不得把故障注入能力暴露给正常 Agent 工具注册表。

P0-07 的固定 20 例仓储 RAG 数据位于 `rag/cases.json`，执行入口为：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

需要同时从 6 份 frozen Markdown 重建 PostgreSQL/Qdrant 索引时增加 `--rebuild-index`。指标定义、ACL/拒答边界和当前实测见 `docs/RAG.md`。

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

## P0-19 策略对照实验

P0-19 复用 P0-18 的 60 例、真实逐例 Trace、Prompt/ToolSpec/配置指纹和
`qwen3.6-fast` 身份，对固定 Workflow、ReAct、PEVR 做同源 `offline_trace_replay`。
ReAct 只作测评投影，不接入生产主链；Token/资源没有源样本时报告为未观测。Smart
`qwen3.8-smart` 当前因速度问题延期，未启动、未测试，不阻塞 P0-19。

```powershell
.\scripts\run_p019_compare.ps1
```

完整契约、指标口径、原始结果、汇总表和当前结论见
[docs/P019_STRATEGY_COMPARISON.md](../docs/P019_STRATEGY_COMPARISON.md)。
