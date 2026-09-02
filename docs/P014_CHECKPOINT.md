# P0-14 Checkpoint、幂等与局部重规划

P0-14 把 P0-13 的固定 PEVR 图接到 PostgreSQL 可恢复边界。它不改变固定节点顺序，
也不把 LLM 变成状态机或副作用的最终裁决者。生产组装使用
`services.application.PostgresRuntimeStore`；单元测试使用同契约的
`InMemoryRuntimeStore`。

## 1. 持久化对象

Checkpoint 使用 P0-06 `runs.run_state_snapshot` 保存完整 JSON 化 `PEVRGraphState`，
并在同一事务中更新 `runs.status/plan_version/current_task_id`、首次出现的
`plans/tasks` 快照和 `events(checkpoint.saved)`。`tasks.status/effect_id` 是可查询索引，
也在保存快照的同一事务中同步；计划版本自身保持不可变。

Effect Ledger 复用 `effects`，同时记录 `tool_calls` 的预留和完成结果。副作用身份唯一由：

```text
run_id + plan_version + task_id
```

这个三元组决定。`make_effect_idempotency_key()` 对其规范 JSON 数组做 SHA-256，并返回
`p014:<digest>`；不能用简单冒号拼接，否则合法 ID 自身含冒号时可能碰撞。数据库仍把
三列分别保存并施加唯一约束。`call_id` 只用于工具审计和外部调用关联，不能改变业务
副作用身份。并发预留的失败方读取唯一约束赢家，不会再次进入 handler。

## 2. 正常执行顺序

```text
读取/创建 run
  → 保存阶段 Checkpoint
  → 对带副作用任务 INSERT reserved Effect Ledger
  → 调用工具/仿真
  → 先独立提交 external_execution 快照及 digest
  → UPDATE completed Effect Ledger
  → 保存任务完成 Checkpoint
  → verify/finish
```

预留事务不包住长时间工具调用；因此进程可以在 `reserved` 和外部完成之间退出。
`PostgresRuntimeStore.put()` 会在仿真 handler 返回前按业务键锁定对应 Effect 行，先把
外部快照和摘要提交；即使进程在随后 `complete_effect()` 之前被杀，新进程也有独立的
外部事实可核对。纯读取/确定性工具不需要写 Effect Ledger，但仍可收到统一业务键供
`ToolResult` 审计和进程内重复调用使用。

## 3. 重启恢复顺序

1. 新进程先按 `run_id` 读取 PostgreSQL Checkpoint，并重新校验 JSON 契约、原始请求、
   `environment_ref` 和 seed。未知状态键、缺少必需对象、伪装成 list 的值、损坏的列表
   项、Trace 重复/逆序或 tool result/task id 数量不一致都会整体拒绝，不能静默丢弃。
2. 对 `reserved/completed/reconciled` Effect Ledger 逐条调用外部状态核对器。默认的
   仿真适配器只读调用 `query_execution_state`；没有可靠结果时返回 `unknown`。
3. 只有外部明确 `completed`，且外部 effect ID、工具名、业务键、规范化输入 digest、
   输出 digest 与账本/`ToolResult` 全部一致时才复用，并写为 `reconciled`；任何不一致都
   转安全重规划。`reserved + not_found` 才允许使用原业务键继续。
4. 外部 `in_progress`、未知、账本已完成但外部查不到时转安全重规划；外部明确失败时
   写 `compensation_required` 后停止。P0-14 不假造补偿工具，补偿动作留给 P0-15 或
   人工流程。
5. 恢复图阶段会跳过已完成阶段；执行节点按 `completed_task_ids` 跳过已完成任务，
   若完成任务没有 Checkpoint/Effect Ledger 结果则直接失败，绝不通过重派发补齐。
6. 终态 Checkpoint 仍先经过外部状态核对，核对通过后才直接返回报告。因此“旧快照为
   completed”本身不能绕过真实仿真/工具查询。

## 4. 局部重规划

`build_task_resource_provenance()` 从已成功的 allocation/route `ToolResult` 和受校验地图
快照构建每个任务真实使用的 AMR、cell、edge、通道和工位集合；不能只看 Planner 的
静态参数，因为 route/dispatch 参数可能只含上游 `$ref`。`LocalReplanner.analyze()` 再将
`amr:...`、`channel:...`、`cell:x,y`、`edge:x1,y1->x2,y2`、`workstation:...`、
`tool:...` 和 `task:...` 标签与这些 provenance 精确匹配，禁止执行自然语言或任意
表达式。影响随后沿依赖关系向后传播，避免复用旧路线、验证结果或派发计划。

`apply()` 的边界是：

- 已完成节点原样保留，包含原 `effect_id` 和证据引用；
- 只移除受影响的未完成节点；保留但未完成的节点重置为 `pending`；
- 替换任务必须使用新 ID，依赖只能指向保留节点或替换子图；
- `new_plan_version == previous_plan_version + 1`；构造后不仅重做 Pydantic/DAG 校验，
  还必须带合同、工具规格和 seed 重新通过完整 PEVR Validator。只给一条 route 替换链
  即使拓扑合法也会被拒绝，替换子图仍须形成 `allocate→route→validate→dispatch`；
- `apply_to_run_state()` 将状态置为 `replanning`，保留已完成任务，递增 `replan_count`，
  由调用方随后写新版本 Checkpoint；旧版本 Effect Ledger 永不删除。

## 5. 验证入口

```powershell
python -m pytest tests\unit\test_p014_checkpoint.py -q -p no:cacheprovider
python -m pytest tests\unit\test_p014_replanner.py -q -p no:cacheprovider
python -m pytest tests\integration\test_p014_postgres.py -q -p no:cacheprovider
```

真实 PostgreSQL 集成测试只清理自身生成的 `run_id`，不使用 SQLite 冒充数据库，也不
删表。其中一个测试在 external snapshot 已提交、Effect 尚未完成的精确窗口调用
`os._exit(73)` 杀死子进程，再用全新的 Engine/Runner 恢复；断言 dispatch handler 调用
总数仍为 1、Effect 只有一行、外部结果被核对复用。

2026-08-21 阶段修复专项结果：P0-14 单元与 PostgreSQL 集成合计 `24 passed`；完整
`run_smoke.ps1` 为 Python `178 passed, 1 warning`、CTest `34/34 passed`。
统一验收仍使用 `scripts/run_smoke.ps1`；`build/`、`__pycache__/`、`.pytest_cache/` 和
`tmp/` 是自动生成物，不属于源码交付。

## 6. P0-14 限制

- P0-14 提供补偿状态和安全停机边界，但没有注册任意补偿工具；自动补偿策略属于 P0-15。
- 单独构造 `ToolRegistry` 时，仿真执行状态和审批仍默认是进程内适配器。真实 PEVR CLI
  已把 `PostgresRuntimeStore` 同时注入运行图和 execution store；其他未来副作用工具仍
  必须提供各自可靠的外部状态适配器，未知状态不会被推断为 `not_found`。
- 历史 P0-13 正常闭环测试夹具仍显式使用 `max_replans=0`；P0-15 的恢复合同默认
  `max_replans=2`（上限仍为 2），并通过 `FaultRecoveryController` 控制局部版本数。
  LocalReplanner 契约支持后续合同显式收紧预算；RunState 会拒绝超过合同上限的版本更新。

## 7. P0-15 消费边界

P0-15 从本专题复用 `RecoveryAssessment`、Effect Ledger 业务键、完成任务锚点和
`LocalReplanner` 的完整复验，不在 P0-14 中重新执行副作用。故障分类、重试/重规划额度、
终止动作和 `FaultRecord` 见 [`P015_FAULTS.md`](P015_FAULTS.md)。

## 8. P0-16 安全恢复边界

安全 PEVR 的 waiting Checkpoint 还必须保存 `HITLInterrupt`、当前 Principal 关联的
审批定位、已完成工具证据和计划/Validator 摘要。恢复不能只看 Checkpoint 中的布尔批准
字段，而要从 HITL Store 重新核对 operator、签名、期限、请求状态、计划版本和摘要；
票据或计划任一事实漂移时，在 handler 前拒绝继续。完整 RBAC、ACL、Prompt Injection
和审批状态机见 [`P016_SECURITY.md`](P016_SECURITY.md)。

## P0-17 Trace 持久化边界

P0-17 的 graph_state 还包含 trace_id 和严格递增的 trace_events；TraceEvent 记录节点、
任务、模型/Prompt/工具版本、Token、延迟、错误、摘要和 evidence_refs。PostgresRuntimeStore
将其以 trace.node/trace.model/trace.tool/trace.verification 事件写入既有 events 表，
不新增表或 migration。理解节点在 runs 行创建前产生的首条模型事件会暂存，ensure_run
建立外键后补写；同一 run 不允许混用 trace_id。详细验证 suite、日志解析和 JSON/Markdown
报告见 P017_TRACE_VERIFICATION.md；本节为文档更新，无核心代码注释需求。
