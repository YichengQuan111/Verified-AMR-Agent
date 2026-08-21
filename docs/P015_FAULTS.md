# P0-15 故障分类与终止策略

P0-15 将工具、Validator、仿真器和 Checkpoint 恢复边界的异常收敛为稳定的
`FaultCategory`，再由固定策略表产生 `retry`、`replan`、`fallback`、`human` 或
`fatal`。分类和预算控制不执行工具，也不替换 P0-14 Effect Ledger；副作用身份仍
由 `run_id + plan_version + task_id` 的规范业务键决定。

## 1. 稳定分类表

| 类别/稳定码 | 典型事实 | 首次动作 | 额度耗尽后的动作 | 确定终止 |
|---|---|---:|---:|---|
| `low_battery` | 电量低于新任务阈值、安全余量或临界电量 | `replan` | `human` | 最多两次局部重规划，仍不能安排则人工 |
| `amr_offline` | AMR 不可用、连接丢失或离线 | `replan` | `human` | 隔离离线 AMR；不能把未知连接状态当作可用 |
| `channel_closed` | 封闭 cell/edge、禁行边、单向约束或临时封路 | `replan` | `human` | 只绕开受影响未完成子图，并重新过 Validator |
| `workstation_occupied` | 工位容量已满或工位被占用 | `retry` | `replan` | 先有限重试，再重排工位子图；重规划也耗尽则人工 |
| `tool_timeout` | 工具超时或明确 `timed_out` | `retry` | `fallback` | 仅无副作用且幂等，或已由外部核对明确 `not_found` 时允许重试 |
| `plan_infeasible` | Validator/路径规划确认计划无解或业务后置条件失败 | `replan` | `human` | 不放宽安全门禁；局部替换仍无解则人工 |
| `state_conflict` | Checkpoint、Effect、幂等键或外部身份冲突 | `human` | `human` | 不猜测、不重放、不自动补偿，立即人工核对 |
| `unknown` | 未纳入契约的异常 | `fatal` | `fatal` | fail closed，禁止自动恢复 |

分类优先级为：状态冲突、超时、资源事实、计划不可行、未知。底层 `raw_code` 保留
在 `FaultSignal` 中用于追溯，策略只依赖稳定的枚举值；错误载荷中可验证的 AMR、任务、
工位、cell 和有向 edge 会转换为 `AffectedEntitySet`，供 P0-14 `LocalReplanner` 精确
计算失效集合。

## 2. 有限预算与终止

`ExecutionBudgets` 的默认恢复额度为 `max_replans=2`、`max_retries=2`；合同可收紧，
但 `max_replans` 不能超过 2，`max_retries` 不能超过 4。P0-13 入口还固定限制总时长
300 秒、输入 Token 30000、输出 Token 5000 和工具步骤 8。`BudgetUsage` 同时累计
输入/输出 Token、工具步骤、时长、重规划和重试；恢复侧把失败工具本身计入工具步骤和
耗时，因此失败重试不能绕过总预算。

每次决策严格按以下顺序执行：

1. 先检查总步骤、时长和 Token 硬上限；已达到上限立即 `fallback`。
2. 再按类别策略检查幂等、副作用和对应额度。
3. `replan` 只能增加一个计划版本；第二个局部版本之后再次失败进入 `human`。
4. 同一 `fault_id` 的终态决策幂等复用；非终态重复故障代表上一次有限动作仍失败，
   会继续消耗下一次额度，不能永远返回同一个 `retry/replan`。
5. `fallback`、`human`、`fatal` 均为终态；`RunState.current_task_id` 清空，终态为
   `FAILED`，并写入 `FaultRecord`。Checkpoint 恢复会再次校验预算、故障 ID 唯一性和
   状态计数，损坏或超限载荷直接拒绝。

## 3. 与 P0-14 的恢复边界

PEVR 固定八节点图不会让模型动态插入循环。`PEVRExecutionError` 保留原有
`stage/code/message` 契约，并附带 P0-15 `fault`；调用方使用
`PEVRGraphRunner.classify_failure()` 或 `FaultRecoveryController.handle_failure()`
取得确定动作。

- `retry` 必须沿原 P0-14 业务幂等键执行；副作用超时/失败且没有明确外部
  `not_found` 时直接 `human`，禁止盲目再次派发。
- `replan` 必须调用 `LocalReplanner.analyze()`，只失效受影响且未完成的任务，保留已
  完成任务及其 `effect_id`；替换计划使用新 task ID、版本严格加一，并重新通过完整
  PEVR Validator。不能直接接受模型给出的全图替换或 route-only 计划。
- 重规划后的 `RunState` 由 `FaultRecoveryController.apply_replan()` 写入
  `REPLANNING`、版本和故障记录，再用同一个 Checkpoint Store 保存；旧 Effect Ledger
  行不删除、不重置、不复制。
- P0-15 不假造补偿工具。Effect 为 `compensation_required`、外部状态未知或身份冲突
  时，终止动作是人工处理，后续补偿必须拥有独立审批和审计契约。

## 4. 验证入口

专项测试覆盖七类分类、重试/重规划耗尽、同一故障重复失败、外部副作用超时、局部替换
保留完成 effect，以及内存 Checkpoint 模拟重启。完整回归还必须包含 P0-12 工具、P0-13
PEVR、P0-14 Checkpoint/Replanner 和 PostgreSQL 集成测试；`scripts/run_smoke.ps1`
是阶段验收入口。Fast/Smart 模型服务不属于本模块的自动启动步骤，在线验证时仍遵守
当前 Smart 禁用门禁。

