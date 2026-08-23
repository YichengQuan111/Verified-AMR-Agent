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

P0-19 默认对固定 Workflow、ReAct、PEVR 做 `offline_independent_oracle` 独立执行；
同源 `offline_trace_replay` 仅可视化。ReAct 不接入生产主链；Token/资源未观测。Smart 延期。

```powershell
.\scripts\run_p019_compare.ps1
```

完整契约、指标口径、原始结果、汇总表和当前结论见
[docs/P019_STRATEGY_COMPARISON.md](../docs/P019_STRATEGY_COMPARISON.md)。
