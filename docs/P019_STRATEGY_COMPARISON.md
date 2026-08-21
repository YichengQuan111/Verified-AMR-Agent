# P0-19：策略对照实验

P0-19 对固定的 P0-18 `amr-p018-60` 做三种策略**独立离线执行**：固定 Workflow（无恢复额度）、
ReAct（最多 1 次幂等 retry、无 replan）和 PEVR（完整 P0-15 预算）。三者使用同一数据集、
Prompt/ToolSpec 版本和 `qwen3.6-fast` 身份指纹，但各自产生 run/trace，终态不得从另一策略复制。
同源 `offline_trace_replay` 仍可作为步数可视化，不能再称为发布验收。

## 运行

默认独立对照（不要求先有 P0-18 源报告）：

```powershell
.\scripts\run_p019_compare.ps1 -OutputDir tmp\p019_strategy_compare
```

仅可视化投影时显式指定 replay：

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir tmp\p018_eval_final
.\scripts\run_p019_compare.ps1 -Mode replay -SourceReport tmp\p018_eval_final\p018_eval.json
```

默认输出：

- `tmp/p019_strategy_compare/p019_strategy_comparison.json`：完整报告，含三策略共 180 条逐例结果、原始 P0-18 case、源 Trace 和策略投影；
- `tmp/p019_strategy_compare/p019_raw_trajectories.jsonl`：一行一个策略-case，便于逐例复核；
- `tmp/p019_strategy_compare/p019_strategy_comparison.md`：由同一 Pydantic 报告对象渲染的汇总表和结论。

入口不会启动 Fast 或 Smart。默认执行模式是 `offline_independent_oracle`：三种策略各自
跑完同一 60 例，异常恢复额度不同，因此 Workflow 不应再与 PEVR 复制同一终态。
`--mode replay` 才是同源投影。Token/墙钟仍未观测。

## 公平性口径

1. 三种策略各覆盖相同的 60 个唯一 `case_id`，使用同一数据集/Prompt/ToolSpec/Fast alias；
2. 每条策略结果保存自己的 Trace、终态、错误和安全计数，不得从另一策略拷贝 `observed_outcome`；
3. ReAct 仍不接入生产主链；
4. Smart 状态固定为 `deferred`、`started=false`、`completed=false`。

## 2026-08-21 历史 Replay 结果（已废止为发布验收）

下表来自同源 Trace Replay，三种策略数字相同是投影造成的，不能再引用为策略质量结论。
独立对照的实际数字以本步重新运行的 JSON 为准。

| 策略 | 说明 |
| --- | --- |
| fixed_workflow | 无恢复额度；期望低于 PEVR 的异常完成率 |
| react | 最多 1 次 retry、无 replan；介于 Workflow 与 PEVR 之间 |
| pevr | 完整恢复预算；离线 oracle 下应保持 60/60 契约符合 |

## 结论与限制

独立对照用于证明恢复策略可分，不是在线 Fast 质量或墙钟性能证据。P0-18 源模式没有模型
调用、Token 或资源采样。Smart 对照仍延期。正式演示视频尚未交付。
