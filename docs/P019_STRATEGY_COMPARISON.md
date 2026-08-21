# P0-19：策略对照实验

P0-19 对固定的 P0-18 `amr-p018-60` 做三策略同源 Trace Replay：固定 Workflow、ReAct 和
PEVR。三者读取同一份 P0-18 逐例结果、地图/AMR/订单、Prompt 版本、九个 ToolSpec 版本、
配置和 `qwen3.6-fast` 身份指纹；ReAct 只在评测层展开 `think → act → observe` 控制步，
不接入生产主链，也不增加任何工具能力。

## 运行

先生成或确认 P0-18 源报告，再运行：

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir tmp\p018_eval_final
.\scripts\run_p019_compare.ps1 -SourceReport tmp\p018_eval_final\p018_eval.json
```

默认输出：

- `tmp/p019_strategy_compare/p019_strategy_comparison.json`：完整报告，含三策略共 180 条逐例结果、原始 P0-18 case、源 Trace 和策略投影；
- `tmp/p019_strategy_compare/p019_raw_trajectories.jsonl`：一行一个策略-case，便于逐例复核；
- `tmp/p019_strategy_compare/p019_strategy_comparison.md`：由同一 Pydantic 报告对象渲染的汇总表和结论。

入口不会启动 Fast 或 Smart，也不接受 executable、Shell、脚本或任意数据集选择器。
当前执行模式是 `offline_trace_replay`：`qwen3.6-fast` 是复用的 P0-18 固定配置/身份，
本步新增模型调用数为 0。

## 公平性口径

运行前必须通过以下门禁：

1. 三种策略各覆盖相同的 60 个唯一 `case_id`，源报告必须通过 P0-18 digest、60 例、失败列表和七项零容忍校验；
2. Prompt 版本、ToolSpec 名称/版本、P0-18 配置 SHA-256、数据集 SHA-256 和 Fast alias 必须与源报告一致；
3. 每条策略结果保存源 case、源 Trace、策略 Trace 投影、错误、负向终态、副作用 ID 和安全计数；
4. 固定 Workflow 与 PEVR 保留源事件，ReAct 只增加派生控制步，不重新调用工具、不重写观察终态；
5. Smart 状态固定为 `deferred`、`started=false`、`completed=false`。它不启动、不测试，不阻塞 P0-19。

## 指标定义

- 任务完成率：44 个正向 case 中 `completed/charged/answered/verified` 的比例；另报 60 例全局预期符合率，正确 `denied/blocked` 不被当作失败删除；
- 计划合法率：出现 `plan/validate` Trace 的 case 中，`validate=completed` 且零容忍安全事实为零的比例；
- 异常终止/恢复正确率：10 个异常 case 中 `recovery_terminal_correct=1` 的比例；
- 成功重规划率：最终完成的异常路径中 `recovery_replan_success=1` 的比例；
- 工具错误：`tool`/`verification` 事件状态为 `failed/timeout/denied` 的总数；意外错误另计，预期安全拒绝和状态阻塞仍保留；
- 步数：固定 Workflow/PEVR 为源 Trace 事件数；ReAct 为源事件展开后的 think/act/observe 控制步数，派生增加量单独保存；
- Token：只从真实 `model` Trace 汇总；没有样本时 `observed=false`，不能把 0 写成模型消耗；
- 延迟/P95：从源 `TraceEvent.latency_ms` 汇总逐例总延迟，P95 使用 `statistics.quantiles(..., method="inclusive")`；P0-18 的值是确定性 Trace 时间，不是墙钟；
- 资源：只接受 Trace metadata 中显式 CPU/RSS/GPU 样本，没有样本时报告“未观测”。

## 2026-08-21 实测结果

源报告：`p018-6e52da4252d83a14`，`report_digest=6e52da4252d83a147d48ce27db4932ab5288d72045db27c0a287b416a56fa3d8`，
源文件 SHA-256=`0df9ea4ab21a3df5a912b6c77221623cb5dbfc1948532cbb23501312541126f7`。
P0-19 报告：`p019-baf5fb7ee1177042`，`report_digest=baf5fb7ee117704238f4ecc56a952aab127fad78e63d4de62afbb5b78f67849c`。
P0-19 config SHA-256=`9d23d3af71718399af48e7f551f2285249c1dcd1b719336cde1897e6e7c32b31`；三策略各 60 例，公平性门禁全部通过。

| 策略 | 全例预期符合率 | 任务完成率 | 计划合法率 | 异常终止正确率 | 成功重规划率 | 工具错误/意外 | 步数均值/P95 | Token | Trace 延迟 P95 | 资源 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| fixed_workflow | 60/60 (100%) | 44/44 (100%) | 33/33 (100%) | 10/10 (100%) | 8/8 (100%) | 15/0 | 5.82/14 | 0（未观测） | 70 ms | 未观测 |
| react | 60/60 (100%) | 44/44 (100%) | 33/33 (100%) | 10/10 (100%) | 8/8 (100%) | 15/0 | 12.15/28 | 0（未观测） | 70 ms | 未观测 |
| pevr | 60/60 (100%) | 44/44 (100%) | 33/33 (100%) | 10/10 (100%) | 8/8 (100%) | 15/0 | 5.82/14 | 0（未观测） | 70 ms | 未观测 |

七项 P0-18 零容忍项在三种投影中均为 0：顶点冲突、边冲突、禁行区进入、低电量越界、
角色泄漏、重复副作用和审批绕过。源轨迹中 15 个工具/验证错误均属于预期拒绝或阻塞，
意外工具错误为 0；所有 16 个负向 case 均保留在原始报告和 P0-19 JSON 中。

## 结论与限制

在这份同源离线回放中，三种策略的任务/计划/恢复/安全结果完全相同；ReAct 只呈现出更高
的派生控制步开销（均值 12.15、P95 28，相对 PEVR 5.82、14）。这不是新的在线模型质量
或墙钟性能证据，因此不能据此宣称 ReAct 在真实 Fast 服务上更慢或 PEVR 在模型质量上更优。

P0-18 的源模式没有在线模型调用、Token usage 或资源采样；报告明确记录了这一限制，
没有把缺失值填成零。若要完成真实在线三策略对照，后续必须新增独立的 `online_fast`
适配器、统一采样器和在线验收门槛，并保留本报告不变。

Qwen3.8 Smart 的 15 例对照已延期而非完成：当前因速度问题硬禁用，本步未启动、未测试，
延期项为 `P0-19-SMART-COMPARISON`，已写入 Backlog。

## P0-20 最终复测记录

本次部署收口使用新的 P0-18 源报告执行：

```powershell
.\scripts\run_p019_compare.ps1 `
  -SourceReport .\tmp\p018_eval_p020_final\p018_eval.json `
  -OutputDir .\tmp\p019_strategy_compare_p020_final
```

退出码为 0，输出 180 条逐例策略结果；报告为
`tmp/p019_strategy_compare_p020_final/p019_strategy_comparison.json`、
`report_id=p019-26947b55d9054d8d`、
`report_digest=26947b55d9054d8de3f5e204b203b24fb54c3e4b57b519df3c265afd0eace8e6`。
fixed Workflow、ReAct、PEVR 仍各为 60/60 预期符合；Smart 仍为
`started=false/completed=false/status=deferred`。本次仍是 `offline_trace_replay`，不能改写
为在线三策略模型/Token/资源对照。
