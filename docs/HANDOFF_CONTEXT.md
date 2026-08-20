# AMR Agent 项目交接上下文

最后更新：2026-08-19  
当前已完成：P0-00、P0-01、P0-02、P0-03、P0-04、P0-05、P0-06  
当前下一步：P0-07 实现文档解析、分块、混合检索与 ACL

## 1. 文档用途与维护要求

这是跨任务、跨 Agent 的唯一交接入口，用于保存后续步骤真正需要复用的事实、接口、验证结果和限制。它不替代正式技术路线；如有冲突，以用户指令、`docs/scope.md` 和 `docs/AMR_Agent_P0技术路线与实施ToDo.docx` 为准。

每次推进工作包后必须更新：完成状态、公共契约、设计决策、验证命令及结果、服务状态、已知限制、下游依赖和“当前下一步”。

## 2. 固定 Scope 与系统边界

- 地图：30 × 20 二维栅格，每格 1 m。
- AMR：4 台同构差速驱动机器人。
- 固定资源：6 个取货点、6 个交付点、2 个充电站。
- 订单主链：`pickup → transport → dropoff`。
- LLM 负责自然语言理解、结构化任务规划、受控工具选择和异常处置建议。
- 确定性程序负责分配、路径、冲突处理、约束验证和仿真推进。
- LLM 不能直接控制底盘，也不能绕过 Validator 宣布计划合法。
- P0 不包含 ROS 2、Gazebo、真实底盘、CBS/ECBS、MILP、MCP、多 LLM Agent、Redis、Celery、Kubernetes 或任意代码执行 Sandbox。

## 3. 当前工程与环境

### 3.1 固定路径

| 项目 | 路径或地址 |
|---|---|
| 项目根目录 | `C:\Users\QYC\Documents\AMR_Agent` |
| Python | `E:\Anaconda\envs\torch128\python.exe`（Python 3.12.13） |
| CMake | `E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe` |
| Ninja | `E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe` |
| MSVC 初始化 | `E:\BuildingTools\Common7\Tools\VsDevCmd.bat` |
| Fast 模型脚本 | `E:\Llama.cpp\start-qwen3.6-agent.cmd` |
| Smart 模型脚本 | `E:\Llama.cpp\start-qwen3.8-agent.cmd` |
| 模型 API | `http://127.0.0.1:8080/v1` |
| FastAPI | `http://127.0.0.1:8000` |
| PostgreSQL | `localhost:5432` / database `amr_agent` |
| Qdrant | `http://localhost:6333` |

### 3.2 当前外部状态

- 2026-08-19 完成最新 P0-05 2-shot 在线验收后，本次启动的 Fast Qwen 已关闭，8080 已确认释放；Smart 没有启动。
- PostgreSQL 17 与 Qdrant 容器在 P0-06 最终验收后均保持运行；PostgreSQL 已迁移到 `0001_p006_core (head)`，public schema 包含 8 张核心表和辅助表 `alembic_version`。后续任务开始时仍应重新运行 `docker compose ps`，不要把本条当作永久在线保证。
- 根目录截至 2026-08-19 未检测到 `.git`，当前不是 Git 仓库；后续如需版本控制应由用户明确决定初始化时点。

## 4. 已完成能力与关键决策

### 4.1 P0-00：范围与种子数据

- `docs/scope.md`、`docs/scope_changes.md`、`docs/backlog.md` 已存在。
- 地图、AMR 和订单种子位于 `domains/amr_warehouse/data/`。
- 当前地图 JSON 中障碍、窄通道、单向边和临时封路数组为空；P0-04 通过 `environment_ref` 引用完整地图快照，只在 `TaskConstraints.blocked_cells` 内嵌本次任务的临时封闭坐标。

### 4.2 P0-01：工程骨架

- Python 包骨架：`apps`、`agent`、`domains`、`services`、`evals`、`tests`。
- 分层配置入口：`services/config/settings.py`。
- 配置优先级：代码默认值 → `config/default.toml` → 环境 TOML → 显式 TOML → 环境变量。
- 结构化日志入口：`services/observability/logging.py`。
- C++17/CMake/CTest 骨架：顶层 `CMakeLists.txt` 和 `services/planner_cpp/`。
- MSVC 必须使用 `/utf-8`，否则 UTF-8 中文注释可能被代码页 936 错误解析。
- 统一离线验收命令：`.\scripts\run_smoke.ps1`。

### 4.3 P0-02：本地模型配置

- Fast alias：`qwen3.6-fast`，上下文 8192，并发 1。
- Smart alias：`qwen3.8-smart`，上下文 12288，并发 1。
- 两个模型共用 8080，一次只能启动一个。
- 模型服务保留在 Windows 主机，不进入 Docker Compose。

### 4.4 P0-03：统一模型网关

主要入口：`services/model_gateway/provider.py`。

稳定边界：

- 业务层依赖 `ModelProviderProtocol`，不接触 GGUF、llama.cpp 启动参数或 OpenAI SDK 客户端。
- `startup()` 请求 `/v1/models`，要求只暴露一个模型且 alias 精确匹配。
- OpenAI SDK 隐式重试关闭；连接超时和生成超时分开配置。
- `ChatMessage` 只允许 `system`、`user`、`assistant`，请求接口不接受 tools、文件、Shell 或任意透传参数。
- Fast 固定关闭思考、预算 0；Smart 开启思考、预算 512 Token。
- `generate_structured()` 从 Pydantic 模型导出 JSON Schema，并用同一模型验证结果。
- 首次 Schema 失败时最多修复一次；第二次失败抛出 `StructuredOutputError`，禁止无限循环。
- FastAPI 默认在 lifespan 启动阶段执行模型门禁；模型不可达或 alias 错误会阻止 Uvicorn 接受请求。
- 仅隔离测试允许设置 `MODEL_GATEWAY_VALIDATE_ON_STARTUP=false`。

### 4.5 P0-04：领域数据契约

八个核心 Pydantic v2 模型已经完成，公共入口如下：

| 核心契约 | Python 入口 | 主要职责 |
|---|---|---|
| `TaskContract` | `agent.planning.TaskContract` | 冻结目标、订单、环境快照、约束、完成条件、风险、审批和硬预算。 |
| `AMRState` | `domains.amr_warehouse.AMRState` | 表达一台 AMR 的位置、朝向、电量、载荷、任务、健康和连接状态。 |
| `TransportOrder` | `domains.amr_warehouse.TransportOrder` | 表达运输订单、时间窗、优先级和订单前置关系。 |
| `PlanTask` | `agent.planning.PlanTask` | 表达计划 DAG 中的单个工具步骤及参数、预算、风险、状态和证据。 |
| `ToolSpec` | `agent.tools.ToolSpec` | 声明白名单工具的封闭输入/输出 Schema、角色、超时、副作用和错误类别。 |
| `ToolResult` | `agent.tools.ToolResult` | 统一工具成功/失败载荷、时间、错误、证据和幂等副作用 ID。 |
| `Observation` | `agent.runtime.Observation` | 记录工具、仿真、验证、人工或系统产生的结构化状态证据。 |
| `RunState` | `agent.runtime.RunState` | 聚合合同、计划、AMR、订单、观测、进度和重规划计数，作为未来 Checkpoint 状态。 |

所有核心模型及其嵌套 Pydantic 模型都继承 `extra="forbid"` 的基类，同时启用默认值校验和赋值校验；未声明字段不会被静默忽略。

### 4.6 P0-05：Context Engineering

公共入口统一从 `agent.context` 导入。五个 Prompt 均为独立 UTF-8 Markdown 2-shot 模板，由注册表绑定稳定 ID、版本和 Pydantic 输出模型：

| 节点函数 | Prompt ID / 版本 | 输出模型 | 已提交 Schema |
|---|---|---|---|
| `understand_goal()` | `amr.p005.understand_goal` / `1.1.0` | `TaskContract` | `TaskContract.schema.json` |
| `plan_tasks()` | `amr.p005.plan_tasks` / `1.1.0` | `PlanTasksOutput` | `PlanTasksOutput.schema.json` |
| `verify_observation()` | `amr.p005.verify_observation` / `1.1.0` | `ObservationVerification` | `ObservationVerification.schema.json` |
| `replan()` | `amr.p005.replan` / `1.1.0` | `ReplanOutput` | `ReplanOutput.schema.json` |
| `compose_report()` | `amr.p005.compose_report` / `1.1.0` | `FinalReport` | `FinalReport.schema.json` |

每份 Prompt 都有“职责”“禁止事项”“两个示例（2-shot）”“输出要求”四节，并包含恰好两组虚构的“输入摘要 → 合法 JSON 输出”。两组示例覆盖互补边界，例如低风险/高风险审批、正常 DAG/审批门禁、验证通过/局部重规划、自动恢复/人工接管、全部完成/未完成报告。示例明确禁止复制其中的 ID、事实和证据。

`PromptDefinition.validated_examples()` 按固定标记解析两组示例：输入必须是 JSON 对象，示例输出还必须通过该节点绑定的实时 Pydantic 模型。示例数量、编号、JSON 或 Schema 任一不合法都会在模型调用前失败。`render_system_prompt()` 继续直接调用 `model_json_schema()` 注入实时输出 Schema；Prompt 语义改变后版本统一从 `1.0.0` 升为 `1.1.0`。静态示例属于 system Prompt，不是用户历史；每次调用仍只构造一条 system 和一条当前 user 上下文消息。

`StandalonePromptNode` 只依赖 `ModelProviderProtocol`，没有导入 LangGraph。上述五个具名函数均可用 Fake Provider 单独运行；P0-13 接入状态图时只能包装这些入口，不应复制 Prompt、摘要或预算逻辑。

### 4.7 P0-06：FastAPI 与 PostgreSQL

固定分层为 `Router → Service → Repository → SQLAlchemy ORM → PostgreSQL`。Router 不执行 SQL，Repository 不调用 `commit/rollback`，跨表事务由 Service 的 `session.begin()` 统一划定。

数据库迁移版本为 `0001_p006_core`，核心表严格保留：

1. `runs`
2. `plans`
3. `tasks`
4. `tool_calls`
5. `effects`
6. `approvals`
7. `events`
8. `documents`

唯一辅助表为 Alembic 标准 `alembic_version`，原因是记录已应用 revision；它不保存业务数据。迁移脚本只提供 `upgrade/current/check`，首个 revision 的 `downgrade()` 明确抛错，禁止自动删除核心表。

数据布局采用“高频查询字段关系化 + 完整快照 JSONB”：`status/run_id/plan_version/tool_name/sequence_no/checksum` 等独立成列，`TaskContract/RunState/PlanTasksOutput/PlanTask/tool_arguments/ToolResult/审批请求/事件载荷/文档元数据` 保存 JSONB 快照；文档正文使用 BYTEA。所有业务外键均为 `ON DELETE RESTRICT`。

正式 HTTP 接口：

| 方法 | 路径 | 持久化行为 |
|---|---|---|
| POST | `/agent/runs` | 原子创建 run、首事件及可选合同审批。 |
| GET | `/agent/runs/{run_id}` | 从 PostgreSQL 恢复运行。 |
| GET | `/agent/runs/{run_id}/plan` | 查询最新或指定版本计划。 |
| GET | `/agent/runs/{run_id}/events` | 以 SSE 返回已持久化事件快照。 |
| POST | `/agent/runs/{run_id}/approve` | 原子保存决定、运行状态与事件。 |
| POST | `/documents` | 保存文档 ACL/版本/校验和、元数据与正文。 |
| GET | `/documents/{document_id}` | 查询不含正文的文档元数据。 |
| POST | `/evals/runs` | 创建 `run_kind=eval` 的持久化运行，不执行评测。 |

`RunService.create_run()` 明确执行 `INSERT runs → flush → INSERT events → flush`。真实 PostgreSQL 测试用已存在的 `event_id` 让第二次 INSERT 失败，SQL 监听确认两条 INSERT 均实际发出且顺序正确；异常后查询新 `run_id` 不存在，证明第一条 INSERT 被同一事务回滚。

## 5. 最近验证证据

### 5.1 离线统一回归

命令：

```powershell
.\scripts\run_smoke.ps1
```

P0-06 最新结果：

- 环境与直接依赖锁：全部匹配。
- Python：99/99 通过；P0-06 新增 6 个离线 ORM/迁移/API 边界测试和 4 个真实 PostgreSQL 集成测试，全部通过。既有 P0-04/P0-05 测试保持通过。
- C++：构建成功，CTest 1/1 通过。
- C++ `__cplusplus`：201703（C++17）。
- Alembic：重复 `upgrade` 成功，当前 revision 为 `0001_p006_core (head)`；8 张核心表缺失数 0，辅助表只有 `alembic_version`。
- ORM/数据库逐表字段比对：差异为空；集成测试清理后 8 张业务表均为 0 条测试数据。

公共 Schema 导出命令：

```powershell
E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py
```

结果：命令退出码为 0，P0-04 八个核心 Schema 与 P0-05 四个新增输出 Schema，共 12 个文件成功生成；测试还会校验提交的 JSON 与当前 `model_json_schema()` 完全一致。

### 5.2 真实模型验证

- Fast `qwen3.6-fast`：最新 alias/版本门禁通过；基础结构化请求 20/20 通过；版本 `1.1.0` 的五个 2-shot 节点 5/5 通过。25 次请求全部首次生成成功，没有触发 Schema 修复。
- 五节点本次实际总 Token：14,837；分别为 `understand_goal=3,734`、`plan_tasks=3,577`、`verify_observation=1,811`、`replan=3,336`、`compose_report=2,379`。这些是本次固定样例的 llama.cpp usage，不是未来生产预算常量。
- Smart `qwen3.8-smart`：alias/版本门禁通过，512 Token 思考预算下结构化请求 1/1 通过。
- API 曾在隔离端口实际由 Uvicorn 启动，`/health` 返回 HTTP 200；进程已关闭。

## 6. P0-04 已固定的公共约定

### 6.1 表示方式与单位

- 坐标统一为 `position: {"x": int, "y": int}`，从 0 开始；`x` 为 0～29，`y` 为 0～19。AMR 种子已经从顶层 `x/y` 迁移到此格式。
- `heading` 单位为度，只允许 `0 / 90 / 180 / 270`。
- `battery` 和 `PlanTask.energy_budget` 单位为百分比，范围 0～100。
- `AMRState.load` 和 `TaskConstraints.maximum_load_kg` 单位为千克，均不得为负，最大载荷必须大于 0。
- `TransportOrder.release_time/deadline` 是非负仿真秒，且 `deadline > release_time`。
- `PlanTask.time_budget` 和 `ExecutionBudgets.max_total_seconds` 单位为秒。
- `ToolResult.started_at/finished_at`、`Observation.observed_at`、`RunState.created_at/updated_at` 必须是带时区时间。
- ID 当前使用非空字符串；数据库 UUID/业务 ID 的最终选型必须在 P0-06 映射时保持 JSON 兼容，不能无版本地改变字段类型。
- `TaskContract.schema_version` 当前固定为字符串 `"1.0"`。

### 6.2 枚举值

- AMR 任务状态：`IDLE`、`TO_PICKUP`、`LOADING`、`TO_DROPOFF`、`UNLOADING`、`TO_CHARGE`、`CHARGING`、`OFFLINE`。
- 健康状态：`HEALTHY`、`DEGRADED`、`FAULT`；连接状态：`ONLINE`、`DEGRADED`、`OFFLINE`。
- 风险：`low`、`medium`、`high`、`critical`；高/严重风险必须要求 operator 审批。
- 回退策略：`retry`、`replan`、`fallback`、`human`、`fatal`。
- PlanTask 状态：`pending`、`ready`、`running`、`waiting_approval`、`completed`、`failed`、`cancelled`。
- ToolResult 状态：`success`、`failed`、`timeout`、`denied`。
- Observation 来源：`tool`、`simulator`、`validator`、`human`、`system`；状态：`ok`、`warning`、`error`、`blocked`。
- RunState 状态：`created`、`planning`、`validating`、`executing`、`waiting_approval`、`replanning`、`completed`、`failed`、`cancelled`。

### 6.3 九个工具名与顶层参数白名单

P0-04 只定义下列契约，没有实现工具。`PlanTask` 会在创建时拒绝未知工具、缺少的必填参数和白名单之外的参数。

| 工具 | 必填顶层参数 | 可选顶层参数 |
|---|---|---|
| `retrieve_knowledge` | `query` | `top_k`、`role_scope`、`document_ids` |
| `get_fleet_state` | `environment_ref` | `amr_ids` |
| `allocate_tasks` | `order_ids`、`environment_ref` | `amr_ids` |
| `plan_multi_amr_routes` | `assignments`、`environment_ref` | `blocked_cells`、`max_time` |
| `validate_fleet_plan` | `plan`、`environment_ref` | `ruleset_version` |
| `dispatch_simulation` | `plan`、`seed` | `until_time` |
| `query_execution_state` | `run_id` | `task_ids`、`amr_ids` |
| `run_verification_suite` | `suite_id` | `run_id`、`case_ids` |
| `request_approval` | `run_id`、`task_id`、`reason` | `expires_at` |

`ToolSpec.input_schema` 与 `output_schema` 都必须是 `type=object`、包含 `properties` 且明确设置 `additionalProperties=false`。P0-12 实现注册表时还要按 JSON Schema 校验每个参数的内部类型和范围。

### 6.4 Validator 与 DAG 规则

- 领域：坐标边界、电量/载荷/优先级范围、取货点不等于交付点、订单时间窗、重复/自依赖。
- 合同：订单 ID 唯一、订单依赖已知且无环、完成条件/缺失信息去重、高风险审批、封闭栅格去重、重规划上限最多 2 次。
- 计划：PlanTask 直接依赖/条件/证据去重、自依赖、工具名、必填/越权参数和高风险审批。
- 工具：输入/输出 Schema 封闭、角色/错误类别去重、viewer 不可执行副作用工具、成功/失败载荷一致、时间先后和证据去重。
- 观测：来源与 ToolResult 一致、违规与状态一致、blocked 必须触发重规划或人工处理。
- 运行态：任务/AMR/订单/观测 ID 唯一，订单与合同一致，任务依赖已知且无环，当前/完成/失败任务一致，目标 AMR 和观测归属存在，时间、终态和重规划预算一致。

DAG 使用 `agent.planning.dag.topological_sort()` 中的 Kahn 算法：计算每个节点入度，把全部零入度节点放入最小堆，逐个移除并降低后继入度；处理节点数小于总节点数时，剩余正入度节点即处于循环中。最小堆和排序后的后继保证相同输入得到稳定拓扑顺序。

### 6.5 已提交 JSON Schema

`scripts/export_schemas.py` 对每个模型直接调用 `model.model_json_schema()`，以 UTF-8、`indent=2`、`ensure_ascii=False`、LF 换行写入：

- `docs/schemas/TaskContract.schema.json`
- `docs/schemas/AMRState.schema.json`
- `docs/schemas/TransportOrder.schema.json`
- `docs/schemas/PlanTask.schema.json`
- `docs/schemas/ToolSpec.schema.json`
- `docs/schemas/ToolResult.schema.json`
- `docs/schemas/Observation.schema.json`
- `docs/schemas/RunState.schema.json`

## 7. P0-04 / P0-05 对后续步骤的影响

| 后续工作包 | 需要从 P0-04 复用的信息 |
|---|---|
| P0-05 Context Engineering | TaskContract、PlanTask、Observation 和 RunState 的精确 JSON Schema；Prompt 不应重复定义字段。 |
| P0-06 API/PostgreSQL | 已完成；关系化字段与 JSONB 快照策略见第 4.7 节。 |
| P0-07 RAG | ToolResult/Observation 中的引用格式，以及文档 ACL/版本字段需要兼容。 |
| P0-08～P0-10 C++ | TransportOrder、坐标、AMR 状态、计划和错误证据需要稳定 JSON stdin/stdout 契约。 |
| P0-11 仿真 | AMR 状态枚举、时间、电量、位置和事件字段必须与 RunState/Observation 一致。 |
| P0-12 工具注册表 | ToolSpec、ToolResult、工具名、权限、超时和错误分类直接成为注册表公共契约。 |
| P0-13 主闭环 | LangGraph State 必须复用 RunState，不应另造不兼容状态对象。 |

### 7.1 P0-05 上下文边界

- `summarize_run_state()` 最多保留三条最新 `ObservationDigest`，只包含结论、来源、时间和 evidence refs；不会复制 `state_delta`、`ToolResult.output` 或更早轨迹。
- `NodeContext` 没有 history 字段，并递归拒绝 `history/messages/full_history/trajectory/trace/observations/run_state/state_delta/tool_result` 的常见命名变体。
- 两组静态 2-shot 示例只存在于 system Prompt，带有“虚构、禁止复制”的边界说明；当前请求仍是唯一 user 消息，不会把历史对话伪装成第三个示例。
- 当前必要工具结果只能作为 `tool_evidence` 进入，RAG 只能作为 `rag_evidence` 进入；每条都必须携带 `source_id`、`source_version`、`observed_at`、`collected_at` 和 `citation`。
- RAG 在 Prompt 中明确标为不可信参考文本，其中的角色变更、代码执行或绕过规则指令一律不能执行。
- 动态状态使用 `DynamicStateSnapshot` 或 `StateSummary`，两者均带版本和观测/摘要时间；状态摘要还带 `run_id`、`plan_version`、`state_updated_at` 和 `environment_ref`。

### 7.2 P0-05 预算与确定性路由

`BudgetSnapshot` 同时携带合同上限、累计用量、捕获时间以及动态计算的剩余 input/output Token、tool steps、seconds 和 replans。调用前门禁固定如下：

| 条件 | 路由 | reason_code |
|---|---|---|
| 估算输入 Token 超过剩余 | `fallback` | `INPUT_TOKEN_BUDGET_EXCEEDED` |
| 请求输出 Token 超过剩余 | `fallback` | `OUTPUT_TOKEN_BUDGET_EXCEEDED` |
| plan/verify/replan 所需工具步数已耗尽 | `fallback` | `TOOL_BUDGET_EXHAUSTED` |
| 总时间已耗尽 | `fallback` | `TIME_BUDGET_EXHAUSTED` |
| replan 次数已耗尽 | `human` | `REPLAN_BUDGET_EXHAUSTED` |

输入 Token 的离线估算规则固定为 ASCII 约 4 字符/Token、非 ASCII 1 字符/Token；估算对象包含实时 Schema、两组静态示例和当前有限上下文，因此 2-shot 增加的成本仍受原有输入预算门禁约束。真实模型返回后还会用累计 `total_usage` 和实测耗时做第二次门禁。估算不是精确 tokenizer 结果，因此 P0-17 指标必须以模型实际 usage 为准。

`ModelProviderProtocol.generate_structured()` 新增关键字参数 `max_output_tokens` 和 `timeout_seconds`。两者只能缩小进程级配置，不能放宽；首次生成与唯一一次 Schema 修复共享这份总 Token/时间额度。`StructuredGeneration.total_usage` 累加所有尝试，`call` 仍保留最终一次响应证据。

### 7.3 后续接入要求

- P0-06 的 `runs` 已保存 `prompt_id/prompt_version/context_digest`，事件 JSONB 可保存节点 route/reason_code、预算与证据来源；P0-13 写事件时必须使用这些位置，不要新增完整对话副本作为业务真相。
- P0-07 检索结果必须转换为 `ContextEvidence(source_type="rag")`；P0-12 工具结果必须转换为 `ContextEvidence(source_type="tool")`。
- P0-13 直接调用五个具名函数，并把 `NodeExecutionResult.route` 映射到状态图边；不能让 LLM 自己决定是否忽略预算门禁。
- P0-14 重规划必须用 `ReplanOutput` 的版本加一、保留/失效/替换任务集合，并继续受最多两次重规划约束。

## 8. 已知限制与注意事项

- P0-06 API/Service 已实际消费 `TaskContract`、`PlanTasksOutput` 和 `PlanTask` 并把快照写入 PostgreSQL；C++、仿真器和工具注册表的跨语言/跨服务兼容性仍需后续工作包验证。
- 完整 `warehouse_v1.json` 通过 `environment_ref` 引用，不属于本次要求的八个核心 Schema；本次只验证 AMR/订单种子和所有内嵌 `GridPosition`。P0-09 如需独立地图输入 Schema，应在不改变现有坐标表示的前提下补充。
- 九个工具目前只有名称和顶层参数契约，没有任何工具实现、JSON Schema 执行器或角色认证；这些分别属于 P0-12 和 P0-16。
- C++ 目前只有工程冒烟程序，没有规划算法。
- API 已有第 4.7 节的 8 个业务接口，但没有身份认证/授权中间件；文档 `role_scope` 只是已持久化 ACL 元数据，真正的访问判定属于 P0-07/P0-16。
- PostgreSQL 已接入业务仓储层；Qdrant 仍未被业务检索代码使用，属于 P0-07。
- `requirements.lock` 锁定的是直接依赖，不是完整传递依赖快照。
- `docs/P001_P003_FILE_GUIDE.md` 是 P0-01/P0-03 历史基线；后续文件职责统一登记在 `docs/FILE_PURPOSES.md`。
- P0-05 的 2-shot Prompt 已在 Fast Qwen 上完成一轮五节点真实冒烟，并覆盖结构与关键业务事实；每节点目前只有一个固定在线样例，不能把 5/5 外推为复杂场景成功率。Smart Profile 的版本 `1.1.0` 五节点测试也尚未执行。
- 文档当前只以 `status=stored` 保存原始字节和元数据，未做解析、分块、向量化、BM25、RRF 或 Qdrant 写入；这些属于 P0-07。
- `tool_calls/effects` 已有表和 Repository，但没有真实工具执行或副作用逻辑；RAG、C++ 算法、仿真、真实工具、LangGraph 主闭环和评测执行器均尚未实现，即没有实现任何 P0-07+ 功能。

## 9. 交接更新模板

后续每完成一个工作包，在本文件末尾增加或更新以下信息：

```text
### YYYY-MM-DD · P0-XX
- 完成：
- 未完成：
- 新增/变化的公共契约：
- 关键设计决策及原因：
- 验证命令与结果：
- 外部服务当前状态：
- 已知限制/风险：
- 下一步直接需要的信息：
```

## 10. 交接日志

### 2026-08-19 · 仓库级治理规则

- 完成：新增 `AGENTS.md`、长期文件职责登记表和本交接上下文。
- 未完成：没有推进新的业务工作包；当前下一步仍为 P0-04。
- 公共契约变化：无。
- 关键决策：今后每次推进必须同时写中文注释、文件作用和下游交接信息。
- 验证：本步仅新增/修改 Markdown 文档，无运行时代码变化；沿用最近一次统一回归 Python 16/16、CTest 1/1 的结果，不将其冒充为本步重新执行。
- 下一步直接需要的信息：见第 6～7 节。

### 2026-08-19 · P0-04 领域数据契约

- 完成：实现八个严格 Pydantic 核心模型、嵌套支持模型、确定性 DAG 校验、九工具顶层参数白名单、Schema 导出器、八个已提交 Schema、AMR 种子格式迁移和 46 个专项正反例。
- 未完成：没有实现 Prompt、Context Engineering、数据库/API 业务接口、C++ 算法、仿真器、工具注册表或 LangGraph 主闭环，即没有实现任何 P0-05+ 功能。
- 公共契约变化：统一坐标为嵌套 `position`；固定字段、单位、枚举、状态一致性和工具顶层参数，详见第 6 节。
- 关键决策：所有模型拒绝额外字段；Schema 只从 `model_json_schema()` 生成；DAG 使用不依赖 NetworkX 的 Kahn 算法，使校验规则便于 Python/C++ 对照复现。
- 验证命令与结果：`python scripts/export_schemas.py` 成功输出 8 个文件；`python -m pytest -q` 为 62/62；`.\scripts\run_smoke.ps1` 环境锁全部匹配、Python 62/62、CTest 1/1。
- 外部服务当前状态：P0-04 不依赖外部服务，本次没有启动或重新检查模型、PostgreSQL、Qdrant；沿用第 3.2 节状态说明。
- 已知限制/风险：跨语言与跨服务消费者尚未实现，完整地图没有纳入八个核心 Schema；这不阻塞 P0-04 的本地验收。
- 下一步直接需要的信息：推进 P0-05 时导入第 4.5 节的公共模型，读取 `docs/schemas/` 中的合同/计划/观测/运行态 Schema，并严格复用第 6 节的预算、风险和字段语义。

### 2026-08-19 · P0-05 Context Engineering

- 完成：实现五份独立且版本化的 Prompt、四个新增输出模型、实时 Schema 注入、有限状态摘要器、来源/版本/时间标记、预算快照、调用前后门禁和五个脱离 LangGraph 的节点函数。
- 未完成：没有实现 API/PostgreSQL、RAG、真实工具、LangGraph 主闭环或后续工作包；没有启动真实 Qwen 做 Prompt 语义成功率测试。
- 公共契约变化：新增 `agent.context` 公共入口；结构化模型调用增加只能收紧的 `max_output_tokens`/`timeout_seconds`，并通过 `StructuredGeneration.total_usage` 返回包含 Schema 修复在内的总用量。
- 关键设计决策：模型只看到 system + 当前有限上下文；完整 RunState 仅在内存中由摘要器消费；来源证据必须显式分区；预算是否允许调用由确定性代码决定，而不是由 Prompt 自行判断。
- 验证命令与结果：`python scripts/export_schemas.py` 成功生成 12 个 Schema；P0-05 专项 pytest 25/25；全量 pytest 89/89；`.\scripts\run_smoke.ps1` 环境锁全部匹配、Python 89/89、CTest 1/1。
- 外部服务当前状态：本步没有启动模型、PostgreSQL 或 Qdrant；沿用第 3.2 节状态说明。
- 已知限制/风险：Token 前置估算是确定性近似，实际值靠调用后 usage 复核；真实模型对五份复杂 Schema 的成功率尚未验证；跨服务持久化和 LangGraph 路由尚属后续步骤。
- 下一步直接需要的信息：P0-06 设计 runs/plans/tasks/tool_calls/events 表时读取第 4.6、7.1～7.3 节，预留 Prompt 版本、上下文摘要、来源版本、预算用量和 route/reason_code 的审计字段。

### 2026-08-19 · P0-05 2-shot 提示词升级

- 完成：把 `understand_goal`、`plan_tasks`、`verify_observation`、`replan`、`compose_report` 从 zero-shot 统一升级为 2-shot；每份模板加入两组互补的虚构输入/合法输出，并增加加载期数量、JSON 和 Pydantic 输出校验。
- 未完成：没有启动真实 Qwen 做版本 `1.1.0` 的在线语义评测；没有推进 P0-06 或任何后续功能。
- 公共契约变化：五个 Prompt 的 ID 与输出模型不变，语义版本统一由 `1.0.0` 升为 `1.1.0`；新增 `PromptDefinition.validated_examples()`，业务 JSON Schema 没有变化。
- 关键设计决策：示例内嵌在 system Prompt 中，避免把静态教学样例当成聊天历史；每个输出都用实时响应模型验证，防止 Prompt 示例与 Pydantic 契约漂移；示例也明确禁止复制虚构事实。
- 验证命令与结果：P0-05 专项 pytest 25/25；Schema 导出成功生成 12 个文件；`run_smoke.ps1` 环境锁全部匹配、Python 89/89、CTest 1/1。
- 外部服务当前状态：本步没有启动模型、PostgreSQL 或 Qdrant；沿用第 3.2 节状态说明。
- 已知限制/风险：2-shot 会增加输入 Token，但现有离线估算和确定性门禁会把完整 system Prompt 计入预算；真实 Qwen 的格式成功率和语义质量仍需后续在线评测。
- 下一步直接需要的信息：P0-06 持久化模型调用审计时记录新的 prompt_version=`1.1.0`；未来修改示例必须保持恰好两组，并通过对应 Pydantic 模型。

### 2026-08-19 · P0-05 Fast Qwen 在线验收

- 完成：使用项目固定 Fast 配置启动 `qwen3.6-fast`，通过 `/health`、单模型 alias 门禁、20 次基础结构化输出和五个 P0-05 2-shot 独立节点在线测试；新增可重复执行的五节点冒烟脚本。
- 未完成：没有测试 Smart Profile 的五个版本 `1.1.0` Prompt，没有扩大固定在线样例集，也没有推进任何 P0-06+ 功能。
- 公共契约变化：业务 Pydantic/JSON Schema、Prompt ID 与版本均未变化；新增命令行入口 `python scripts/smoke_p005_prompts.py --profile fast`。
- 关键设计决策：每个节点使用独立有限上下文，不串联前一节点输出或完整历史；除结构校验外增加关键语义断言，专门检测 2-shot 示例污染、工具越权和版本/事实偏移。
- 验证命令与结果：网关预检通过；`smoke_llm_structured.py --iterations 20` 为 20/20；`smoke_p005_prompts.py --profile fast` 为 5/5；25 次均 `attempts=1`，没有 Schema 修复。首次离线回归因受管环境拒绝默认 Temp/`.pytest_cache` 而出现 3 个 fixture setup error、86 个通过；项目内临时目录策略的首版又因漏建父目录得到同样 3 个 setup error。补齐父目录后，未设置额外环境变量直接运行 `run_smoke.ps1`，最终 pytest 89/89、CTest 1/1，环境锁全部匹配。
- 外部服务当前状态：本次 llama.cpp 进程 PID 20624 已关闭，8080 无监听；服务关闭前 `/health` 正常、仅暴露 `qwen3.6-fast`，日志未检出 ERROR/FATAL 行。
- 已知限制/风险：五节点各只有一个固定在线样例；真实总 Token 14,837 只代表本次输入。llama.cpp 日志提示服务未设置 API key 且 CORS 允许全部来源，但当前仅绑定 `127.0.0.1`；未来若扩大监听地址必须先补鉴权和 CORS 收敛。
- 下一步直接需要的信息：后续修改 Prompt 后应先运行 25 个 P0-05 离线用例，再启动对应 Profile 执行本脚本；本条记录的是当时状态，P0-06 现已完成，当前下一步以文件顶部为准。

### 2026-08-19 · P0-06 FastAPI 与 PostgreSQL

- 完成：实现 Router → Service → Repository → SQLAlchemy ORM → PostgreSQL 分层；创建并迁移 `runs/plans/tasks/tool_calls/effects/approvals/events/documents` 8 张核心表；实现运行创建/恢复、计划查询、SSE 事件、审批、文档上传/查询和评测运行入口；新增前向迁移、稳定异常、应用视图及数据库说明。
- 未完成：没有实现文档解析/分块/RAG、ACL 执行中间件、真实工具调用与副作用、C++ 算法、仿真、LangGraph 主闭环或评测套件执行器，即没有实现任何 P0-07+ 功能。
- 新增/变化的公共契约：数据库 revision=`0001_p006_core`；8 表字段/索引/约束见 `services/persistence/models.py` 与 `docs/DATABASE.md`；新增 `RunView/PlanView/EventView/ApprovalView/DocumentView`、`RunService`、`DocumentService`；新增第 4.7 节列出的 8 个 HTTP 接口。
- 关键设计决策及原因：高频查询/评测字段独立关系化，复杂 Pydantic 对象保存 JSONB 快照；文档正文使用 BYTEA；所有业务外键 RESTRICT；Repository 无事务提交；Service 显式 flush 父子写入；迁移只向前且禁止自动删表。这样兼顾查询能力、完整复现和审计安全，不会过度正规化。
- 事务回滚证据：真实 PostgreSQL 故障注入监听到 SQL 顺序严格为 `INSERT runs`、`INSERT events`；第二条因重复 `event_id` 抛出 `IntegrityError` 后，新 `run_id` 查询为空，种子 event 仍存在，证明第一条已执行 INSERT 被同一事务撤销。
- 验证命令与结果：`python scripts/migrate_database.py upgrade/current` 成功；8 核心表缺失 0、辅助表只有 `alembic_version`；ORM/数据库列差异为空；P0-06 专项 10/10；最终 `run_smoke.ps1` 环境锁全匹配、pytest 99/99、CTest 1/1。测试后 8 张业务表行数均为 0。
- 外部服务当前状态：PostgreSQL 17 与 Qdrant 容器仍运行；PostgreSQL 当前为 revision head，表保留；Qwen 没有启动，8080 延续 P0-05 验收后的关闭状态。
- 已知限制/风险：SSE 只输出请求时已有事件的有限快照；公开 API 尚无认证；文档正文当前限制 10 MiB 并存于 PostgreSQL；`tool_calls/effects` 尚无真实写入者。Alembic 首次执行日志的 revision 中文说明曾受终端代码页影响显示乱码，但 revision 内容、后续 `current` 输出和迁移结果均正常。
- 下一步直接需要的信息：P0-07 应通过 `DocumentService.get_document()` 取得正文，通过 `documents.role_scope/version/source/checksum/status/indexed_at/metadata_snapshot` 复用 ACL、版本和索引状态；写 Qdrant 后应在新前向迁移或既有字段中更新状态，不能删除/替换 8 张核心表。检索结果必须转换为 P0-05 `ContextEvidence(source_type="rag")` 并保留 source/version/time/citation。
