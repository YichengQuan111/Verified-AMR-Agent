# P0-12 九个白名单工具

P0-12 的唯一运行入口是 `agent.tools.build_tool_registry()` 返回的封闭注册表。
注册表只包含下面九个 `ToolName`；`services.amr_simulator.FaultInjection`、Shell、
SQL、任意路径和任意 HTTP 都不属于正常 Agent 工具。

## 统一调用闭环

一次调用固定经过：

```text
ToolName
  → ToolSpec 名称/角色/副作用/超时门禁
  → TOOL_ARGUMENT_POLICIES 顶层键门禁
  → Pydantic input Schema 与跨字段校验
  → 固定 handler（必要时固定 C++ exe + JSON stdin/stdout）
  → Pydantic output Schema
  → ToolResult 状态、错误、证据、digest、effect_id 和幂等缓存
```

`ToolResult` 对所有工具统一记录 `call_id`、工具版本、调用角色、开始/结束时间、
耗时、输入/输出 SHA-256、`evidence_refs`、`effect_id` 和错误分类。P0-12 生成的
结果还在 `audit_metadata` 中记录 `preflight_validated`、幂等性、副作用和请求摘要。
每个 `ToolSpec.error_categories` 都覆盖执行器可能统一产生的 `timeout`、`conflict`
和 `internal`，不能让静态声明少于真实失败面；预检失败会明确记录
`preflight_validated=false`。

输入/输出 Schema 由 `scripts/export_schemas.py` 从运行时 Pydantic 模型导出到
`docs/schemas/`，不能手写一份与代码分叉的 JSON。

## 工具清单

| 工具 | 输入顶层字段 | 输出 | 角色 | 超时 | 幂等/副作用 | 错误重点 |
|---|---|---|---|---:|---|---|
| `retrieve_knowledge` | `query`, `top_k?`, `role_scope?`, `document_ids?` | `RetrievalResponse` | viewer/operator | 15s | 是 / 否 | 参数、ACL、RAG 不可用 |
| `get_fleet_state` | `environment_ref`, `amr_ids?` | `FleetStateOutput` | viewer/operator | 5s | 是 / 否 | 参数、快照不存在/不可用 |
| `allocate_tasks` | `order_ids`, `environment_ref`, `amr_ids?` | `AllocationResponse` | operator | 10s | 是 / 否 | 参数、ID 不存在、C++ 不可用 |
| `plan_multi_amr_routes` | `assignments`, `environment_ref`, `blocked_cells?`, `max_time?` | `RoutePlanResponse` | operator | 20s | 是 / 否 | 参数、无安全路线、C++ 不可用 |
| `validate_fleet_plan` | `plan`, `environment_ref`, `ruleset_version?` | `ValidationResponse` | viewer/operator | 10s | 是 / 否 | 参数、`unsafe_plan`、C++ 不可用 |
| `dispatch_simulation` | `plan`, `seed`, `until_time?` | `SimulationResult` | operator | 30s | 是 / 是 | 未验证计划、Validator 超时、仿真故障 |
| `query_execution_state` | `run_id`, `task_ids?`, `amr_ids?` | `ExecutionStateOutput` | viewer/operator | 5s | 是 / 否 | 状态不存在、筛选 ID 不存在 |
| `run_verification_suite` | `suite_id`, `run_id?`, `trace_id?`, `case_ids?` | `VerificationSuiteOutput`（逐 case 状态、失败类型、证据位置、JSON/Markdown 报告） | operator | 120s | 是 / 是 | 固定套件/Case 不合法、超时、入口不可用 |
| `request_approval` | `run_id`, `task_id`, `reason`, `expires_at?` | `ApprovalRequestOutput` | operator | 5s | 是 / 是 | 参数、审批存储冲突/不可用 |

`requires_approval=true` 的 `dispatch_simulation` 是声明性安全元数据：它表达
该工具必须由上游计划审批门禁控制；P0-12 不接受把 `approved`、`skip_validation`
或 `llm_valid` 塞进计划绕过 P0-10。`request_approval` 只创建 `pending` 请求，
不会自动批准；真正的批准决定继续使用 P0-06 的人工审批入口。

## 固定 C++ 边界

Python 适配器 `agent.tools.cpp_client.FixedCppJsonClient` 只允许以下固定程序和
固定参数：

| 工具 | 固定程序 | 固定参数 |
|---|---|---|
| `allocate_tasks` | `build/cpp/services/planner_cpp/task_allocator_cli.exe` | `--algorithm hungarian` |
| `plan_multi_amr_routes` | `build/cpp/services/planner_cpp/route_planner_cli.exe` | `--algorithm astar` |
| `validate_fleet_plan` | `build/cpp/services/planner_cpp/fleet_plan_validator_cli.exe` | `--validate` |

请求通过 UTF-8 JSON stdin 传递，响应从 stdout 解析；适配器显式使用
`subprocess.run(..., shell=False, cwd=<仓库根目录>, timeout=...)`，并遵守 4 MiB
输入上限。C++ 退出码 `2` 映射为 `invalid_argument`，进程超时映射为 `timeout`，
缺少固定程序映射为可重试的 `unavailable`；业务 `status=infeasible` 或
Validator `status=invalid` 不会被当作成功计划，分别映射为 `unsafe_plan`。

三个算法工具的成功输出也收紧为固定实现：分配只接受
`algorithm=hungarian`，路线只接受 `algorithm=astar`，Validator 的
`status/valid/error_count/errors` 必须互相一致。工具边界不会接受 C++ baseline
标识伪装成生产结果。

受控验证同样只运行代码中声明的固定 argv。CTest/PowerShell 从可信进程环境的
`PATH` 解析为绝对程序路径，不接受工具参数传入 executable、cwd 或命令文本，也不
硬编码某台开发机的盘符；P0-12 安全用例使用固定 pytest node id，避免模糊 `-k`
筛选出零条测试却被误读为已执行。

路线工具只调用 A*，不把 Dijkstra 当作隐式回退；Validator 始终独立重算，不能
信任 P0-08/P0-09 的审计字段或模型声明。

## 重复调用与失败行为

- P0-14 运行图为带副作用任务传入统一业务键。键只由
  `run_id + plan_version + task_id` 三元组决定，但具体字符串是规范 JSON 数组的
  SHA-256 摘要（`p014:<digest>`），避免 ID 本身含冒号时出现分隔符碰撞。该业务键优先于
  易变的 `call_id` 参与缓存和 in-flight 协调，并与 PostgreSQL Effect Ledger 保持一致。
  没有传入业务键的既有 P0-12 调用继续使用 call_id 语义。
- `call_id` 相同且工具、角色、规范化输入相同：若首次调用仍在执行，后到调用等待
  同一个 in-flight 结果；完成后返回第一次完整 `ToolResult`，handler 和副作用均只执行一次。
- `call_id` 相同但工具、角色或输入不同：无业务键时返回
  `conflict/call_id_reused_with_different_request`；业务键复用到不同请求时返回
  `conflict/idempotency_key_reused_with_different_request`。
- 未提供 `call_id`：由工具名、角色和参数生成稳定 `call-<digest>`；同一纯输入因此仍可重放。
- 无法序列化为有限 JSON 的参数在 handler 前失败，且不写幂等 ledger；这种请求没有
  稳定规范指纹，不能借复用 call_id 覆盖此前合法结果。
- 参数/权限/输出 Schema 失败：handler 不执行，返回 `failed`；权限失败返回 `denied`。
- 超过 `ToolSpec.timeout_seconds`：返回 `timeout` 和 `timeout` 错误分类。C++/验证
  子进程使用比外层门限少 1 秒的固定 timeout，先回收子进程再由执行器封装结果；
  Python handler 收到协作取消信号，仿真/审批等副作用写入前会再次检查，超时完成的
  结果不会写入状态存储。
- `ToolResult.status=success` 表示工具调用完成，不等同于业务计划合法；例如仿真
  `SimulationResult.status=blocked/timeout` 仍会作为成功生成的仿真证据返回，P0-10
  非法计划则是 `unsafe_plan` 工具失败。

## 环境和持久化边界

默认环境 Provider 只读取仓库提交且通过 `WarehouseMap` 公共契约校验的
`warehouse_v1` seed 文件，`environment_ref` 只是快照身份，不会被拼成路径。单独构造
注册表时，执行状态和审批默认是进程内确定性适配器；真实 PEVR CLI 会把同一个
`PostgresRuntimeStore` 同时注入 ToolRegistry 与运行图。`dispatch_simulation` 在 handler
返回图节点之前，先把外部仿真快照、业务键和 digest 独立提交到对应 Effect 行；进程随后
被杀时，新进程仍能通过 `query_execution_state` 核对并复用，而不会二次 dispatch。
工具参数不能选择数据库 DSN、表或文件路径。

`dispatch_simulation` 的结果以确定性 simulation ID 登记，随后可以用
`query_execution_state` 查询。地图快照会合并 seed 的静态 `obstacles` 和
`temporary_blocked_cells`；仿真结果中的订单使用 `order_id` 响应 `task_ids` 筛选，
未知 ID 必须失败，不能返回未过滤快照。

RAG 的候选阶段继续复用 P0-07 ACL；工具返回前还会复核 query、role_scope、top_k、
逐条文档角色和 document_ids。任何后端越界结果均以
`retrieval_output_scope_violation` 失败且不返回正文，形成工具层的输出侧熔断。
