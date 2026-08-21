# P0-13 PEVR 正常闭环

本工作包只交付成功路径，固定状态图为：

```text
guard → understand → retrieve → plan → validate → execute → verify → finish
```

## 运行边界

- `understand` 和 `plan/verify/finish` 复用 P0-05 的具名 Prompt 节点与预算门禁。
- `retrieve` 通过 P0-12 `retrieve_knowledge` 获取带版本、ACL 和 citation 的 RAG 证据。
- `plan` 只生成四个 DAG 任务：`allocate_tasks → plan_multi_amr_routes → validate_fleet_plan → dispatch_simulation`。
- Planner 候选生成后立即使用同一个确定性 PEVR Validator 检查；若首个候选不合法，
  只允许一次带明确错误的语义修复调用。第二个候选仍不合法就终止，绝不自动改写 seed、
  环境、订单或工具参数来“修绿”。
- `validate` 首轮使用确定性 `validate_normal_pevr_plan`；重规划后的 v2+ 走 `validate_replanned_pevr_plan`。只有无错误结果才能进入 Executor。
- `execute` 只通过 P0-12 `ToolRegistry` 调用 Hungarian、A*、P0-10 Validator 和 Python 仿真。
- `dispatch_simulation` 的 ToolSpec 仍要求审批；发布 CLI 已删除 `--approve-dispatch`。生产入口必须使用已验签 operator 与 HITL `ApprovalGrant`，Planner 不能自行批准。
- `verify` 只接受仿真订单全部完成且 Observation 验证为真；中断恢复与局部重规划由 P0-14
  的运行时适配器承接，复杂异常分类和确定终止策略见 P0-15 专题；身份、ACL、HITL
  中断和恢复前审批核验见 [docs/P016_SECURITY.md](P016_SECURITY.md)；自动补偿工具仍不在
  P0 范围内。

Planner 的 `JsonValue` 在本地 Fast 模型上可能被写成有限的 `{type,value}` 或固定事实引用。兼容层只还原白名单原语、两个正式数据流 `$ref` 和固定执行事实；不执行表达式，未知引用仍由 Validator 拒绝。

## 真实 Fast 验收

先启动固定 Fast（发布入口）：

```powershell
.\scripts\start_local.ps1 -StartFast
```

首次运行必须停在 `waiting_approval`（退出码 3），再用同一 `run_id` 与 `approval_id` 批准恢复：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' scripts\run_p013_e2e.py `
  --run-id p020-release-hitl-1-20260821-2028 `
  --jwt-token-file .\tmp\operator.jwt `
  --output tmp\p013_e2e_wait.json
& 'E:\Anaconda\envs\torch128\python.exe' scripts\run_p013_e2e.py `
  --run-id p020-release-hitl-1-20260821-2028 `
  --jwt-token-file .\tmp\operator.jwt `
  --approve-and-resume <approval_id> `
  --output tmp\p013_e2e_result.json
```

2026-08-21 收口用三个独立 `run_id` 连续实测 HITL：

- 模型：`qwen3.6-fast`，代理 `http://127.0.0.1:8080/v1`，llama-server `18080`，IQ4_NL，ctx `16384`。
- 输入：`请把 MAT-001 从 P1 运到 S3，并在截止时间前完成。`
- `p020-release-hitl-1-20260821-2028`、`...-2-...`、`...-3-...` 均为先 waiting 再 `completed`，阶段顺序均为固定 8/8，Approval=1、Effect=1。
- 每次工具都是 5/5 成功（RAG 1 次 + Planner DAG 4 次），计划版本 `1`、Validator
  错误数 `0`、仿真 `completed`、`ORDER-001` 完成、结束时间 `120`。
- 三次均为 4 次模型调用，说明首个计划候选已经合法；若触发唯一一次语义修复，指标会
  准确记为 5，而不是固定写死为 4。
- 工具总耗时分别为 `13215 ms`、`7642 ms`、`7512 ms`。输出报告写入系统临时目录，
  不作为仓库源码交付物。验收后模型进程已停止，8080 已确认释放。
- Smart 没有启动；选择 Smart 的预检实测在网络访问前返回
  `MODEL_PROFILE_DISABLED`。历史 Smart P0-05 结果 2/5，不能视为通过。
- 已知风险：P0-04 `TransportOrder` 没有重量字段，执行期 `payload_kg` 固定为 `1.0kg`。

完整机器报告位于 `tmp/p013_e2e_result.json`（属于自动生成物，不登记为源码职责）。报告字段包括 `citations`、`plan_version`、`tool_evidence`、`metrics`、`budget_usage` 和 `unresolved_risks`。

## 单元与回归

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p013_pevr.py -q -p no:cacheprovider
& 'E:\Anaconda\envs\torch128\python.exe' scripts\export_schemas.py
& '.\scripts\run_smoke.ps1'
```

P0-13 单测覆盖完整八阶段 mock 闭环、未授权数据流、可信审批门禁、`JsonValue` 包装与
固定事实别名解析，以及以下关键反例：首个非法计划仅修复一次、第二个非法计划停止、
Validator 工具返回 success 但业务 invalid 时不 dispatch、路线超时时不调用 Validator/
dispatch、仿真 ToolResult success 但订单 blocked 时不宣布完成。

`scripts/run_p013_e2e.py` 会创建 `PostgresRuntimeStore(session_factory)`，并把同一 Store
同时注入 ToolRegistry 和 PEVR Runner，使 Checkpoint、Effect Ledger 和外部仿真状态都
能跨进程恢复。直接构造 Runner 且不传 Store 时仍可用于 P0-13 单元级纯内存成功路径。
P0-14 的恢复顺序和失败边界见 [docs/P014_CHECKPOINT.md](P014_CHECKPOINT.md)。

## P0-17 Trace 关联

每次 PEVR 请求可带 trace_id；省略时由 Runner 生成并在恢复时从 Checkpoint 复用。
PEVRRunResult 和 PEVRRunReport 都返回该关联键，trace_events 按序记录八个图节点、
具名 Prompt 的 prompt_id/prompt_version、模型 served alias、Token 增量、延迟、工具
版本、输入/输出摘要、错误和 evidence_refs。详细 Trace 字段、失败事件和受控验证报告
见 [P017_TRACE_VERIFICATION.md](P017_TRACE_VERIFICATION.md)；本节为文档更新，无核心代码注释需求。
