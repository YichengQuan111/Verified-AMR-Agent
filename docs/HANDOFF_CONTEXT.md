# AMR Agent 项目交接上下文

最后更新：2026-08-22
当前实现里程碑记录：P0-00～P0-20 代码已落地。2026-08-21 原审计文档仍是 **FAIL** 快照；本步已启动 Qwen3.6 Fast 与 Compose，并实测 HITL 三连、全量 smoke、RAG holdout 与 P0-18/P0-19。**H06 演示视频仍为 0 个媒体文件**，未在真实 Fast dispatch 窗口再做 OS 强杀，七类异常仍以 FakeRegistry 生产图测试为准，因此发布 Verdict **仍不能写成 PASS**。同日按用户明确指令新增**演示 UI 扩展**（`/demo` 页面 + `/demo/*` API，可视化 `warehouse_v1` 地图与真实 C++ 链路仿真轨迹），该扩展偏离 `docs/scope.md` 的「完整前端 P0 外」排除项，差异已在本文件末条登记；它不是 P0 Release PASS，也不替代 H06。
当前下一步：由艺诚在 `http://127.0.0.1:8010/demo` 用真实 Fast 走闭环演示（任意自然语言 → waiting_approval → 页面匿名批准 → completed → 轨迹/报告）。**2026-08-22 用户明确指令：演示闭环完全不考虑安全，审批匿名、页面仅闭环链。** 轻量 `POST /demo/order` 接口保留给测试。H06 演示视频仍为 0。本地 Fast 启动已按 manifest `verify_sha256=false`。Smart 继续硬禁用。ORDER-002 `release_time` 语义冲突待裁决。

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
| Smart 模型脚本 | `E:\Llama.cpp\start-qwen3.8-agent.cmd`（暂时禁用，不得启动） |
| 模型 API | `http://127.0.0.1:8080/v1` |
| FastAPI | `http://127.0.0.1:8000` |
| PostgreSQL | `localhost:5432` / database `amr_agent` |
| Qdrant | `http://localhost:6333` |
| Embedding 模型 | `E:\Llama.cpp\Embedding`（Qwen3-Embedding-0.6B） |

### 3.2 当前外部状态

- 2026-08-20 P0-07 最终检查时 Docker Desktop 4.87 / Engine 29.7.2 可连接；PostgreSQL 17 与 Qdrant 1.19.0 容器正在运行，5432/6333 有监听。PostgreSQL 当前 revision 为 `0001_p006_core (head)`，8 张核心表完整，辅助表只有 `alembic_version`。
- Qdrant collection `amr_warehouse_knowledge` 当前存在 70 个 points；6 份 frozen 文档已在 PostgreSQL 中标为 `indexed` 并带 `indexed_at`。新会话仍必须重查服务，不能把本条当作永久在线保证。
- 本地 `E:\Llama.cpp\Embedding` 已实际离线加载；权重约 1.19 GB，SentenceTransformers 6.0.0 动态读得 dimension=1024，文档/query 编码均成功。模型、维度和 chunk 数没有写死在运行代码中。
- 2026-08-21 P0-20 最终状态：`docker compose ps` 显示 `amr-api`、`amr-postgres`、`amr-qdrant`
  均 healthy，API 8000、PostgreSQL 5432、Qdrant 6333/6334 保持运行；Fast 已按精确 PID 停止，
  8080 无监听；Smart launcher 未运行。API 重启后 `/health` 为 `model_validated=false`，无 Fast 时
  `/health/model` 实测 HTTP 503 `MODEL_CONNECTION_FAILED`。
- 2026-08-21 收口复验：Compose `amr-api`/`amr-postgres`/`amr-qdrant` 均为 healthy，端口只发布到 `127.0.0.1`。Fast 由 `scripts/start_fast_secure.ps1` 拉起：`127.0.0.1:18080` 为 llama-server，`127.0.0.1:8080` 为强制 Bearer 代理；`check_model_gateway.py --profile fast` 返回 alias `qwen3.6-fast`、IQ4_NL、ctx=16384、GGUF SHA-256=`B228C988…F337E6`。Smart launcher 未运行。本步结束时 Fast **仍在运行**，未执行停止。
- Git 仍在 `main`，基线提交为 `e6a4f07`；工作树含审计修复与本步启动器编码/超时修正。本次没有 stage、commit、reset 或删除用户变更。

## 4. 已完成能力与关键决策

### 4.1 P0-00：范围与种子数据

- `docs/scope.md`、`docs/scope_changes.md`、`docs/backlog.md` 已存在。
- 地图、AMR 和订单种子位于 `domains/amr_warehouse/data/`。
- 当前地图 JSON 含不干扰 ORDER-001 正常主链的非空障碍、窄通道、禁行/单向边和临时
  封路 fixture；`DefaultWarehouseSnapshotProvider` 必须先通过严格 `WarehouseMap` 公共契约
  解析。`environment_ref` 只标识快照，不参与路径拼接。

### 4.2 P0-01：工程骨架

- Python 包骨架：`apps`、`agent`、`domains`、`services`、`evals`、`tests`。
- 分层配置入口：`services/config/settings.py`。
- 配置优先级：代码默认值 → `config/default.toml` → 环境 TOML → 显式 TOML → 项目 `.env` 白名单键 → 进程环境变量。
- 结构化日志入口：`services/observability/logging.py`。
- C++17/CMake/CTest 骨架：顶层 `CMakeLists.txt` 和 `services/planner_cpp/`。
- MSVC 必须使用 `/utf-8`，否则 UTF-8 中文注释可能被代码页 936 错误解析。
- 统一离线验收命令：`.\scripts\run_smoke.ps1`。

### 4.3 P0-02：本地模型配置

- Fast Profile 配置：`qwen3.6-fast`，离线/配置契约 `context_window=8192`，并发 1；外部 Windows
  Fast 脚本本次实际命令行为 `--ctx-size 16384`，两者必须在报告中分开记录。
- Smart 保留 alias：`qwen3.8-smart`，上下文 12288，并发 1；当前 `enabled=false`，原因是
  最近一次真实 P0-05 五节点在线验收仅 2/5。
- 两个脚本共用 8080；当前只允许启动 Fast。Smart 必须等待用户明确指示并重新验收。
- 模型服务保留在 Windows 主机，不进入 Docker Compose。

### 4.4 P0-03：统一模型网关

主要入口：`services/model_gateway/provider.py`。

稳定边界：

- 业务层依赖 `ModelProviderProtocol`，不接触 GGUF、llama.cpp 启动参数或 OpenAI SDK 客户端。
- `startup()` 先检查 Profile `enabled`；Smart 会在任何网络调用前返回
  `MODEL_PROFILE_DISABLED`。启用的 Fast 才请求 `/v1/models` 并要求单模型 alias 精确匹配。
- OpenAI SDK 隐式重试关闭；连接超时和生成超时分开配置。
- `ChatMessage` 只允许 `system`、`user`、`assistant`，请求接口不接受 tools、文件、Shell 或任意透传参数。
- Fast 固定关闭思考、预算 0；Smart 的开启思考/预算 512 Token 参数仅为以后复验保留，
  当前不能进入生成调用。
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

### 4.8 P0-07：仓储 SOP RAG

公共入口统一从 `services.retrieval` 导入。完整链路已实现：

- `MarkdownDocumentLoader`：安全解析 YAML Front Matter、UTF-8、原始字节 SHA-256、frozen-only 和重复 doc ID 门禁。
- `MarkdownChunker`：优先按 H2 section 切分，只在超长 section 内按语义块二次拆分；只有问题无答案的 `RAG 示例问题` 不生成证据 chunk。
- `Embedder`：分别提供 `embed_documents()` / `embed_query()`，使用本地 Qwen3 query/document prompt，维度从实际模型动态读取。
- `QdrantVectorStore`：collection 默认 `amr_warehouse_knowledge`，cosine dense vector，完整 chunk payload，UUIDv5 point，支持 rebuild 和按 doc ID 替换；`role_scope`/`doc_id` 在 `query_filter` 中执行。
- `BM25Index`：使用 `jieba.lcut()` + `BM25Okapi`，先按角色/文档范围过滤语料，再评分。
- `HybridRetriever`：vector/BM25 归一化后按默认 0.5/0.5 融合，不实现 Reranker；结果返回完整引用和 hybrid/vector/BM25 数值。
- `RetrievalResponse`：top hybrid 低于 0.809 且 top raw vector 低于 0.499 时返回 `insufficient_evidence` 并强制 `results=[]`；阈值均可配置。
- `RetrievalResult.to_context_evidence()`：转换为 P0-05 `ContextEvidence(source_type="rag")`，chunk ID 作为唯一 source ID，文档身份/section/version/citation 保留在内容中。

索引器通过 `DocumentService.upsert_frozen_knowledge_document()` 同步 P0-06 `documents` 表，再用 `get_document()` 回读正文。只有 Qdrant 全批成功后才调用 `mark_documents_indexed()`；本步没有新增数据库 revision。

公共 JSON Schema 新增：`KnowledgeChunk.schema.json`、`RetrievalResult.schema.json`、`RetrievalResponse.schema.json`。运行入口：

```powershell
E:\Anaconda\envs\torch128\python.exe scripts\index_warehouse_knowledge.py
E:\Anaconda\envs\torch128\python.exe scripts\query_warehouse_knowledge.py "问题" --role viewer
E:\Anaconda\envs\torch128\python.exe -m evals.rag.run_eval --output tmp\p007_rag_eval.json
```

### 4.9 P0-08：C++ Hungarian 任务分配

公共 C++ 入口位于 `services/planner_cpp/include/task_allocator/task_allocator.hpp`，实现位于 `services/planner_cpp/src/task_allocator.cpp`。`allocate_hungarian()` 和 `allocate_nearest_idle()` 都复用 P0-04 的 AMR/订单字段语义；前者是生产算法，后者是独立最近空闲 baseline，不共享匹配结果或选择逻辑。

CLI 为 `task_allocator_cli`，默认或 `--algorithm hungarian` 执行生产算法，`--algorithm nearest_idle` 执行基线。请求顶层 `schema_version` 固定为 `"1.0"`，直接复用 `AMRState`、`TransportOrder`、嵌套 `position`，并新增本模块 envelope 字段：`location_positions`、`completed_order_ids`、显式五项 `weights` 和电量/速度/能耗 `config`。响应包含 `algorithm/status/assignments/cost_matrix/pair_evaluations/unassigned_orders/unassigned_amrs/total_cost`；不可行矩阵项使用 JSON 字符串 `"INF"`，每个组合提供稳定 `reason_codes`。

关键设计：

- 代价为 `wd*distance_to_pickup + wt*lateness_risk + wb*battery_risk + wl*load_penalty - wp*priority_bonus`，时间估算使用 Manhattan route 的 ceil 行驶时间；迟到风险进入代价但由 P0-10 Validator 做最终时间窗硬校验。
- 电量沿用冻结 30/20/10/15 规则：电量不高于 20% 的普通新任务、预计完成后低于 15% 安全余量、电量临界、非空闲/非健康/非在线、依赖未完成和缺少工位都使组合不可行。
- Hungarian 在真实矩阵外加 dummy 行/列，先最大化可行匹配数再最小化代价；所有 ID 先按字典序排序，等价代价稳定破平。P0-08 不处理障碍、单向边、实际路径和时空冲突，交给 P0-09/P0-10。
- JSON 编解码器为本模块严格子集实现，不引入 Anaconda/Boost 等偶然路径依赖；拒绝未知字段、重复键、非有限数值，CLI stdin 上限 4 MiB。

详细字段、原因码、退出码和示例见 `docs/TASK_ALLOCATOR.md`。P0-08 没有修改 P0-07 RAG payload、阈值、collection 或数据库 revision。

### 4.10 P0-09：C++ A* 与时空预约表

公共 C++ 入口位于 `services/planner_cpp/include/route_planner/route_planner.hpp`，实现位于 `services/planner_cpp/src/route_planner.cpp`；JSON 编解码位于 `route_planner/json_codec.hpp` 与 `route_json_codec.cpp`。`route_planner` 复用 P0-04 的 `AMRState`、`TransportOrder`、`GridPosition` 和 P0-08 的 `Location`，不读取 `environment_ref` 对应的任意文件路径，地图快照必须随请求内存传入。

公共 API 与固定决策：

- `plan_routes_astar()` 是生产入口，状态为 `(x,y,heading,t)`；前进、左/右转和等待都消耗一个离散时间步，`move_cost/turn_cost/wait_cost` 进入 `g` 代价。启发式固定为 Manhattan 距离乘移动代价，不把转向/等待估计塞进启发式。
- `plan_routes_dijkstra()` 是独立时间扩展 Dijkstra，使用自己的 `g` 开放表且 `h=0`，不调用 A*、不读取 A* 的开放/关闭集；它只能由调用方显式选择，不能作为生产失败 fallback。
- `ReservationTable` 用 `(cell,t)` 登记顶点，用有向 `(edge,t)` 登记运动边；`can_transition()` 同时拒绝正向重复边和反向交换边。路径终点保持预约到 `max_time`，避免后续路线穿过停靠 AMR。
- 多车按订单 `priority` 降序、`release_time` 升序、`order_id`、`amr_id` 稳定排序；每台车的完整 `pickup → dropoff` 路径成功后才写入预约表。任何车无安全路径时整体返回 `status=infeasible`，失败路线不填充 path。
- 地图硬约束包含边界、`blocked_cells`、`blocked_edges`、`one_way_edges` 和有限 `max_time`。CLI 顶层请求字段固定为 `schema_version/environment_ref/map_width/map_height/blocked_cells/blocked_edges/one_way_edges/amrs/orders/location_positions/assignments/completed_order_ids/start_time/max_time/costs`；`assignments` 可直接使用 P0-08 输出中的 `components` 快照，但路线会重新计算；详细 JSON 和退出码见 `docs/ROUTE_PLANNER.md`。
- 路由返回 `path` 的每个时刻、朝向、动作和累计 `g_cost`，并给出 `pickup_time/dropoff_time`；deadline 由 P0-10 Validator 做最终硬校验，本步不伪装为路线安全结论。

### 4.11 P0-10：C++ 车队计划验证器

- 完成：新增 `fleet_plan_validator` C++17 静态库、`fleet_plan_validator_cli` 和严格 JSON 编解码；验证器接收内存地图快照、P0-04 `AMRState`/`TransportOrder`、工位位置、执行期 `payload_kg` 和 P0-09 离散路径，独立重算完整计划，不读取 `environment_ref` 文件。
- 新增/变化的公共接口：`ValidatorConfig`、`FleetPlanRoute`、`FleetPlanRequest`、`ValidationEvidence`、`ValidationErrorDefinition`、`ValidationResult`、`error_dictionary()`、`validate_fleet_plan()`；规则版本固定为 `p0-10.v1`，结果 schema 版本为 `1.0`。JSON 顶层必填 `schema_version/environment_ref/map_width/map_height/blocked_cells/blocked_edges/one_way_edges/amrs/orders/location_positions/completed_order_ids/routes/start_time/max_time/config/workstation_capacities`，路线必填 `amr_id/order_id/payload_kg/pickup_time/dropoff_time/path`。
- 规则边界：确定性检查任务依赖 DAG、release/deadline 和声明时间、初始/取货后载荷、临界/普通新任务电量门槛和路径结束安全余量、边界/禁行区/禁行边/单向边、动作—位置—朝向一致性、工位容量、Manhattan 最小安全距离、逐时刻顶点冲突及反向交换边冲突；非正工位容量也会独立报错。路径终点从到达时刻保持占用至 `max_time`，与 P0-09 `ReservationTable` 一致。
- 证据契约：每个错误固定包含 `code/constraint/message` 以及任务/订单、相关任务/订单、AMR、相关 AMR、坐标、相关坐标、时间、相关时间、观测值、上限和路径索引字段；不适用字段序列化为 `null` 或空字符串。错误按稳定键排序，错误字典由 C++ 单一实现导出，不能由 Prompt 自行声明新规则。
- 安全边界：`status`、`reason_code`、`reason` 只作为 P0-09 审计字段；`llm_valid`、`skip_validation`、`approved` 等未知字段在 JSON 边界拒绝。业务非法计划以退出码 `0` 返回 `status=invalid`，参数/契约错误为 `2`，内部异常为 `3`；调用方必须检查 `valid/status/errors`，不能只看进程成功。
- 详细契约入口：`docs/FLEET_PLAN_VALIDATOR.md`；错误字典可通过 `fleet_plan_validator_cli.exe --error-dictionary` 导出。没有修改 P0-04 Pydantic Schema、数据库字段、Alembic revision 或模型服务。

### 4.12 P0-11：Python AMR 离散事件仿真

- 完成：新增 `services.amr_simulator`，接收 P0-10 同形状的 `SimulationPlan`，执行前通过固定 `build/cpp/services/planner_cpp/fleet_plan_validator_cli.exe --validate`；Validator 不是 `status=valid`、`valid=true` 且 `errors=[]` 时立即抛出 `PlanValidationError`/`ValidatorExecutionError`，不回退到 Python 自判。
- 新增公共接口：`AMRSimulator`、`DiscreteEventSimulator`、`simulate_plan()`、`SimulationPlan`、`FleetPlanRoute`、`RouteStep`、`SimulationResult`、`SimulationEvent`、`SimulatorConfig`、`ChargingStationSpec`、`SimulationOrderState`、`WorkstationState`、`ChargingStationState`、`FaultInjection`/`FaultType`，以及 Validator 客户端和异常。P0-09 的动作/时间字段保持 `start/move/turn_left/turn_right/wait`，P0-04 AMR 状态继续使用八个固定状态枚举。
- 状态与数据流：固定 `tick_seconds=1`，按原始 `path[*].time` 执行位置/朝向；`move` 使用 P0-10 同名能耗配置扣电；pickup/dropoff 在到达 tick 生成零时长 `LOADING`/`UNLOADING` 和工位事件；终点不释放、不瞬移。低电量已在充电站才进入 `CHARGING`，否则进入 `TO_CHARGE` 原地等待；故障安全停机为 `OFFLINE`。
- 观测/事件契约：每个 tick 生成一个 `agent.runtime.Observation(source=simulator)`，`state_delta` 同时包含 AMR、订单、工位和充电站快照；`SimulationEvent` 以仿真内单调序号生成 ID。`observed_at` 为固定 Unix epoch 加 tick 秒，不使用墙上时钟；事件和遍历均按稳定 ID 排序。
- 故障边界：`offline`、`battery_drain`、`stuck` 只能从仿真/Eval 参数传入，不写入 `agent.tools.ToolName` 或正常 ToolSpec；当前故障均安全停机，未完成订单变为 `blocked`，Observation 设置 `requires_replan=true`。若未来支持恢复，必须先扩展计划时间戳和证据契约。
- 专题入口：`docs/AMR_SIMULATOR.md`；专项测试：`tests/unit/test_p011_simulator.py`；新增 `docs/schemas/SimulationPlan.schema.json`、`SimulationEvent.schema.json`、`SimulationResult.schema.json`，由 `scripts/export_schemas.py` 统一生成。没有新增数据库 revision、常驻服务、ROS/真实底盘或 P0-12 工具注册。

## 5. 最近验证证据

### 5.1 离线统一回归

命令：

```powershell
.\scripts\run_smoke.ps1
```

P0-08 最终结果：

- 环境与直接依赖锁：`.\scripts\run_smoke.ps1` 全部匹配，MSVC/CMake/Ninja 路径和 Python 包版本门禁通过。
- Python：110/110 通过；P0-08 没有修改 Python 运行时代码或 Schema，P0-04～P0-07 回归保持通过。
- C++：在导入 `VsDevCmd.bat -arch=x64 -host_arch=x64` 后构建成功，CTest 7/7 通过；覆盖原有冒烟、正常、低电量、无可行、订单多于车辆、边界和 JSON 契约。
- C++ `__cplusplus`：201703（C++17）。
- P0-09 增量验证：在导入 `VsDevCmd.bat -arch=x64 -host_arch=x64` 后，`route_planner`/CLI/测试目标构建成功；专项 CTest 12/12 通过；完整 CTest 19/19 通过，覆盖障碍、边界、禁行边、单向边、等待、顶点冲突、交换边冲突、无解、Dijkstra、可复现性、性能和 JSON 契约。`route_planner_tests --case performance` 实测 `performance_ms=4`、`expanded_states=88`。
- P0-09 CLI 冒烟：`route_planner_cli --version` 输出 `0.1.0`/C++17；同一 5×3 请求的 A* 与 Dijkstra 都返回 complete，路径/代价一致，A* 扩展 5 个状态、Dijkstra 扩展 51 个状态。
- Alembic：重复 `upgrade` 成功，当前 revision 为 `0001_p006_core (head)`；8 张核心表缺失数 0，辅助表只有 `alembic_version`。
- Qdrant 健康门禁通过，正式 collection 存在；集成测试只删除自己的 UUID collection。PostgreSQL/Qdrant 测试行/点均按精确 ID 清理，6 份正式知识文档和 70 个正式 points 保留。
- 完成本步后外部状态复核：`docker compose ps` 显示 `amr-postgres`/`amr-qdrant` 运行，5432/6333 监听；8000/8080 未监听，未启动 FastAPI 或文本 Qwen。
- 唯一警告为 jieba 依赖内部使用已废弃 `pkg_resources` 的 DeprecationWarning，不影响本次结果。

公共 Schema 导出命令：

```powershell
E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py
```

结果：命令退出码为 0，P0-04 八个、P0-05 四个、P0-07 三个公共 Schema，共 15 个文件成功生成；测试校验提交 JSON 与当前 `model_json_schema()` 完全一致。

### 5.2 P0-07 真实 Embedding / Qdrant 评测

- `scripts/index_warehouse_knowledge.py`：6/6 frozen Markdown 成功，非 frozen 跳过 0，生成 70 个证据 chunks；实际 Embedding dimension=1024；PostgreSQL 同步与 Qdrant rebuild 成功。
- `python -m evals.rag.run_eval`：20 例中 17 可答、3 不可答；Recall@K=1.0，MRR=0.970588，Section Recall@K=1.0，Citation Correctness=1.0（88/88），Answerability Accuracy=1.0，ACL leak count=0。
- hybrid 单阈值对短改写不完全可分，因此保留配置化 vector 补充门禁：hybrid 未达标子集中，可答 top vector=0.597388，不可答为 0.320516～0.400358，建议中点=0.498873，默认取 0.499。
- 手工 CLI 复核：短改写“AMR 当前电量 25% 属于哪个区间？”为 `answerable`；viewer 查询 operator 审批策略和 Wi-Fi 密码均为 `insufficient_evidence`，结果正文数 0。

### 5.3 真实文本模型验证（历史 P0-05）

- Fast `qwen3.6-fast`：最新 alias/版本门禁通过；基础结构化请求 20/20 通过；版本 `1.1.0` 的五个 2-shot 节点 5/5 通过。25 次请求全部首次生成成功，没有触发 Schema 修复。
- 五节点本次实际总 Token：14,837；分别为 `understand_goal=3,734`、`plan_tasks=3,577`、`verify_observation=1,811`、`replan=3,336`、`compose_report=2,379`。这些是本次固定样例的 llama.cpp usage，不是未来生产预算常量。
- Smart `qwen3.8-smart`：alias/版本门禁通过，512 Token 思考预算下结构化请求 1/1 通过。
- API 曾在隔离端口实际由 Uvicorn 启动，`/health` 返回 HTTP 200；进程已关闭。

### 5.4 P0-10 C++ 验证器验收

- 构建：在同一 PowerShell 进程导入 `E:\BuildingTools\Common7\Tools\VsDevCmd.bat -arch=x64 -host_arch=x64` 后，`cmake --build build\cpp` 成功，生成 `fleet_plan_validator.lib`、`fleet_plan_validator_cli.exe` 和测试可执行文件。
- 专项 CTest：`ctest --test-dir build\cpp -R "^fleet_validator_" --output-on-failure` 实测 14/14 通过，覆盖合法计划、依赖、时间窗、载荷、电量、禁行区、工位容量、安全距离、顶点冲突、交换边冲突、路线几何、证据稳定性、JSON 旁路拒绝和错误字典。
- 完整 CTest：`ctest --test-dir build\cpp --output-on-failure` 实测 33/33 通过，P0-08/P0-09 回归保持通过。
- CLI 实测：`fleet_plan_validator_cli --version` 输出服务版本 `0.1.0`/C++17；合法样例输出 `error_count=0`、`status=valid`；`payload_kg=101` 的业务非法样例以退出码 `0` 返回 `load_capacity_exceeded`，证据含 `ORDER-01`、`AMR-01`、`{x:1,y:0}`、`time=1`、`observed=101`、`limit=100`；包含 `llm_valid` 的请求以退出码 `2` 返回 `invalid_json_or_contract`；`--error-dictionary` 成功输出 56 条机器可读定义。
- 最终统一回归：实际执行 `.\scripts\run_smoke.ps1` 退出码为 0，环境/依赖门禁通过，Alembic 为 `0001_p006_core (head)` 且核心表缺失 0，Qdrant collection 健康，pytest `110 passed, 1 warning`，完整 CTest `33/33`。唯一警告为既有 jieba `pkg_resources` 弃用警告，不影响结果。
- 环境与服务：P0-10 本身不依赖 FastAPI、文本 Qwen、PostgreSQL 或 Qdrant，本次没有启动模型/API；最终复核 `docker compose ps` 显示 `amr-postgres`/`amr-qdrant` 运行，5432/6333 监听，8000/8080 未监听。C++ 构建依赖固定 MSVC/CMake/Ninja 路径和 UTF-8 编译选项。
- 已知限制/风险：容量当前按同一工位 ID、同一离散 pickup/dropoff 事件时刻聚合，尚未表达 P0-11 的服务持续时间；载荷通过执行期 `payload_kg` 补充，尚未修改 P0-04 `TransportOrder`。验证器是库和固定 CLI，尚未注册为 P0-12 Python 工具。
- 下一步直接需要的信息：P0-11 已复用 `FleetPlanRequest`、`ValidationResult` 的时间/载荷/终点占用语义；P0-12 应把 `AMRSimulator` 包装成受控 `dispatch_simulation` 工具，固定传入已验证计划并把 `SimulationResult` 转换为 `ToolResult`/Observation，不能把 `FaultInjection` 注册为正常工具。

### 5.5 P0-11 Python 仿真验收

- 验证命令：`E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p011_simulator.py -q`，实际结果 `8 passed in 0.14s`；`compileall` 对 `services\amr_simulator` 与专项测试实际通过。
- 覆盖事实：正常路径的 `IDLE/TO_PICKUP/LOADING/TO_DROPOFF/UNLOADING` 迁移、订单 pickup/dropoff、按 move 扣电、工位服务计数；低电量充电的容量/速率/`CHARGING`→`IDLE`；无充电路径时 `TO_CHARGE` 原地等待；offline 与 battery_drain 安全停机、订单 blocked、Observation `requires_replan`；P0-10 非法 dropoff 时间戳前置拒绝；同 plan/seed 完整 JSON 重放一致；FaultType 不在 ToolName 白名单。
- 实际 CLI 门禁：专项测试通过真实 `build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe`；另以无订单 idle plan 实测 `status=valid/valid=true/errors=[]`，以错误 `dropoff_time` 实测返回 `dropoff_time_mismatch` 并由 Python 抛出 `PlanValidationError`。
- Schema 导出：`E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py` 实测退出码为 0，新增 3 份 P0-11 Schema，与 `model_json_schema()` 一致；全量 Schema 当前为 18 份。
- P0-09→P0-11 手工联调：实际调用 `build/cpp/services/planner_cpp/route_planner_cli.exe --algorithm astar` 得到 `route_status=complete`、`path_len=6`，将其原始 `path/pickup_time/dropoff_time` 注入 P0-10 request 后运行仿真，结果 `sim_status=completed`、`dropoff=5`、终点 `{x:6,y:1}`。
- 服务状态：P0-11 不启动常驻服务，不修改 PostgreSQL/Qdrant；最终 `.\scripts\run_smoke.ps1` 已实际退出码 0，环境/依赖门禁、PostgreSQL 迁移与 8 表检查、Qdrant 健康检查、pytest `118 passed, 1 warning`、CMake/Ninja 重建和 CTest `33/33` 全部通过。随后 `docker compose ps` 实测 `amr-postgres`/`amr-qdrant` 均 Up，5432/6333 监听；8000/8080 无监听；未启动模型/API。
- 已知限制：P0-10 当前只验证运输路线，仿真充电站由 `SimulatorConfig` 单独传入；没有未验证的充电移动路径，因此低电量且不在站点时只进入 `TO_CHARGE`，不会瞬移。当前故障均终止执行，不支持恢复后跳过时间戳路径；故障注入只供 Eval，不是 Agent 工具。
- 下一步直接需要的信息：P0-12 接入时固定调用 `AMRSimulator`/`simulate_plan`，将 Validator 失败映射为 `unsafe_plan`/`invalid_argument`，将 blocked Observation 转为受控 `ToolResult`，不得放宽 P0-10 前置门禁或把 FaultInjection 暴露给正常 Agent。

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

### 7.4 P0-08 直接复用与验收边界

- 直接复用 `TransportOrder`、`AMRState`、`GridPosition` 和当前嵌套 `position: {x,y}` 表示，不另造坐标或订单格式。
- Hungarian 代价至少包含到取货点距离、迟到风险、订单优先级、电量风险和当前负载；具体权重需进入显式配置/JSON 契约并有基线对照。
- 不可行 AMR—订单组合使用明确的 INF/不可行表示并返回原因，不能只给大代价后仍允许匹配。
- “最近空闲 AMR”只能作为独立正确性/策略基线，不得混入生产 Hungarian 实现。
- 交付物应包含 C++ 库/可执行程序、稳定 JSON stdin/stdout 契约和 CTest，覆盖正常、低电量、无可行分配和订单多于车辆。
- P0-08 不需要修改 P0-07 RAG payload、阈值或 collection；如需引用电量规则，使用冻结文档/契约，但算法合法性由确定性代码和测试负责。

## 8. 已知限制与注意事项

- P0-06 API/Service 已实际消费 `TaskContract`、`PlanTasksOutput` 和 `PlanTask` 并把快照写入 PostgreSQL；P0-12 已用固定 JSON 边界完成 C++、仿真器和工具注册表的集成回归。单独构造 ToolRegistry 时状态/审批仍默认进程内；真实 PEVR CLI 已将同一 `PostgresRuntimeStore` 注入 Checkpoint、Effect Ledger 和 dispatch 外部状态。外部身份真实性仍留给 P0-16。
- 完整 `warehouse_v1.json` 通过 `environment_ref` 标识，并由新增 `WarehouseMap` 支持契约严格校验；P0-09 的 C++ CLI 仍使用 `docs/ROUTE_PLANNER.md` 定义的跨语言严格地图 envelope，并要求调用方把已获取的地图快照显式传入，不能让 C++ 按 ref 自行读文件。
- 九个工具已经由 `build_tool_registry()` 实现，并具有 Pydantic/JSON Schema、角色门禁、超时、错误、审计和幂等执行器；外部身份认证与角色真实性仍属于 P0-16，不能让调用方自行声明 operator。
- P0-08 Hungarian、P0-09 A*、P0-10 独立 Validator 和 P0-11 Python 仿真均已通过 P0-12 固定适配器串联；baseline 算法与 Eval `FaultInjection` 没有进入正常工具表。
- P0-09 采用按优先级的 prioritized planning，不实现 Scope 明确排除的 CBS/ECBS；在固定顺序下若后续车辆无安全解会返回 infeasible，不会回溯重排或忽略冲突。Python seed 先经 `WarehouseMap` 校验，再按 `docs/ROUTE_PLANNER.md` 的严格 JSON envelope 适配给 C++；未分配 AMR 也会在完整时域占用初始 cell。
- P0-09 不直接控制底盘、不从 `environment_ref` 读取地图、不做最终业务验收；P0-10 负责静态计划安全门禁，P0-11 负责执行期状态推进/观测一致性，P0-12 负责受控工具接入。
- API 已有第 4.7 节的 8 个业务接口，但没有身份认证/授权中间件。P0-07 检索器会执行给定 `UserRole` 的 ACL，然而角色真实性仍由未来 P0-16 认证/授权层保证；外部调用方不能被允许自行声称 operator。
- PostgreSQL 已接入业务仓储层，Qdrant 已由 P0-07 正式使用；BM25 按 P0 Scope 保持进程内，重启时从同一 frozen 语料重建。
- `requirements.lock` 锁定的是直接依赖，不是完整传递依赖快照。
- `docs/P001_P003_FILE_GUIDE.md` 是 P0-01/P0-03 历史基线；后续文件职责统一登记在 `docs/FILE_PURPOSES.md`。
- P0-05 的 2-shot Prompt 已在 Fast Qwen 上完成五节点真实冒烟，并覆盖结构与关键业务事实；每节点目前只有一个固定在线样例，不能把 5/5 外推为复杂场景成功率。Smart Profile 版本 `1.1.0` 曾真实执行但只通过 2/5，现已硬禁用，不能写成“未测”或“已通过”。
- P0-07 的 20 例阈值只覆盖当前 6 份冻结语料和 Qwen3-Embedding-0.6B；开放域泛化尚未验证。语料、模型、prompt、chunking、权重或归一化改变后必须重新观察可答/不可答完整分布。
- P0-07 已注册为真实 `retrieve_knowledge` 工具，候选阶段 ACL 外还具有工具返回侧 role/document/top_k 熔断和 ToolResult 审计；调用者身份绑定仍属于 P0-16。
- P0 按正式 Scope 不实现 Reranker；当前 Citation Correctness 验证引用逐字段回指源 chunk，语义相关性由 Recall/MRR/section recall 分开衡量。
- `tool_calls/effects` 现在由 P0-14 `PostgresRuntimeStore` 在 Checkpoint 运行路径写入；副作用身份由 `run_id + plan_version + task_id` 三元组决定，字符串键为规范三元组 SHA-256，而非分隔符拼接。LangGraph 主闭环仍属于 P0-13，恢复/账本边界由 P0-14 扩展。

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

### 2026-08-20 · 新会话交接准备

- 完成：重写项目 README，使其覆盖固定 Scope、P0-00～06 状态、模型/上下文/API/数据库边界、接口、目录、启动验证命令和 P0-07 正式要求；新增 `docs/NEXT_SESSION_PROMPT.md`，作为下一 Codex 会话可直接复制的 P0-07 初始任务说明。
- 未完成：没有修改运行时代码、配置、数据库、Schema 或测试，也没有开始实现 P0-07；当前下一步仍为 P0-07，不能把本次文档整理视为工作包完成。
- 新增/变化的公共契约：无。新提示词只是交接与验收说明，不是新的业务 Schema 或 API 契约。
- 关键设计决策及原因：README 只保存稳定项目事实和最近验证基线，不把瞬时服务状态写成长期在线保证；提示词同时记录 2026-08-20 观察值并强制下一会话重新核验。P0-07 要求来自正式路线第 93～99 段，额外约束来自已经落地的 P0-05/P0-06 公共边界。
- 验证命令与结果：README 的 8 个相对链接全部存在，两份 Markdown 代码围栏均配对；`git diff --check` 通过；不依赖数据库的单元测试 94/94 通过。由于 Docker Engine 当前关闭，本次未运行需要真实 PostgreSQL 的 `run_smoke.ps1`，不得把 P0-06 的 99/99 历史基线冒充为本次全量回归。
- 外部服务当前状态：Docker API 不可连接；5432、6333、8080 均无监听；PostgreSQL、Qdrant、Fast/Smart Qwen 均未运行。本次没有擅自启动服务。
- 已知限制/风险：常见缓存位置未发现本地 Embedding 模型，但没有扫描所有磁盘；P0-07 必须先确定 model_id/版本/维度/归一化和获取方式。Git 状态会报告一个历史 pytest 畸形目录的权限警告，后续只能在解析绝对目标和确认它是生成物后再安全处理。README 与提示词为 Markdown，无 DOCX 布局变更，因此没有 DOCX 渲染 QA 需求。
- 下一步直接需要的信息：新会话复制 `docs/NEXT_SESSION_PROMPT.md` 中的完整提示词；先读 AGENTS/HANDOFF/正式路线、运行 `git status`、启动 Docker/PostgreSQL/Qdrant、检查迁移和本地 Embedding，再推进 P0-07。完成 P0-07 后必须更新/替换提示词中的瞬时状态与下一工作包信息。

### 2026-08-20 · P0-07 仓储 SOP RAG

- 完成：实现 frozen Markdown Loader、H2 section-aware Chunker、独立 Qwen3 Embedder、Qdrant 向量库、进程内 jieba/BM25、配置化混合检索、检索期 ACL、完整 Citation、证据不足拒答、PostgreSQL 文档状态同步、索引/查询 CLI，以及固定 20 例评测执行器；6 份知识文档形成 70 个正式 chunks/points。
- 未完成：按 P0 Scope 没有实现 Reranker；尚未把检索器注册成九工具白名单中的 `retrieve_knowledge`，没有接入公开 API 的已认证调用者角色，也没有开始 P0-08。文本生成 Fast/Smart Qwen 本步不需要且当前未运行。
- 新增/变化的公共契约：`KnowledgeDocument`、`LoadedCorpus`、`KnowledgeChunk`、`VectorSearchHit`、`BM25SearchHit`、`RetrievalResult`、`RetrievalResponse`、`RetrievalStatus`、`KnowledgeIndexReport`；`Embedder.embed_documents()` / `embed_query()`；`HybridRetriever.retrieve()`；`KnowledgeIndexer.rebuild()`；`DocumentService.upsert_frozen_knowledge_document()` / `mark_documents_indexed()`。新增 `KnowledgeChunk`、`RetrievalResult`、`RetrievalResponse` 三份 JSON Schema；数据库没有新增 revision，继续使用 `0001_p006_core`。
- 关键设计决策及原因：chunk 先按 H2 保留 section 语义，只有超长 section 才按语义块和句界二次拆分；仅含问题、不含答案的“RAG 示例问题”不作为证据。Qdrant 使用 payload filter 在召回阶段限制 `role_scope`，BM25 在建候选语料前限制角色，禁止先召回后隐藏。Embedding dimension 从本地模型动态读取为 1024；两路分数分别归一化后默认 0.5/0.5 融合，不加 Reranker。拒答阈值没有凭经验固定，而是保留配置并根据 20 例分布采用 hybrid 0.809 与 raw vector 0.499 的联合门禁；拒答响应强制清空 `results`。文档表先同步 frozen 内容，只有 Qdrant 全批成功后才原子标记 `indexed`。
- 验证命令与结果：实现前重新运行基线 pytest 99/99；P0-07 专项单元测试 9/9、真实 PostgreSQL/Qdrant 集成测试 2/2；`scripts/index_warehouse_knowledge.py` 成功索引 6/6 文档、70 chunks、dimension 1024；`python -m evals.rag.run_eval` 得到 Recall@K 1.0、MRR 0.970588、Section Recall@K 1.0、Citation Correctness 1.0（88/88）、Answerability Accuracy 1.0、ACL leak count 0。第一次最终 `run_smoke.ps1` 在测试前因已验证环境的 sentence-transformers 6.0.0 与历史 direct lock 5.5.1 不一致而正确失败；同步声明/锁文件后完整重跑成功：依赖全部匹配、pytest 110/110、CTest 1/1、迁移/8 表/Qdrant 门禁全部通过。`scripts/export_schemas.py` 成功导出并校验 15 份 Schema。
- 外部服务当前状态：Docker Desktop/Engine 正常；PostgreSQL 17 与 Qdrant 1.19.0 容器运行，5432/6333 监听；PostgreSQL revision 为 `0001_p006_core (head)`；Qdrant 正式 collection `amr_warehouse_knowledge` 有 70 points；本地 `E:\Llama.cpp\Embedding` 可离线加载并实测 dimension 1024；8000/8080 未监听，没有文本模型或 API 进程遗留。
- 已知限制/风险：角色 ACL 的执行逻辑已验证，但角色真实性必须由 P0-16 认证层绑定，不能让外部请求自行声明 operator。当前阈值只由 6 份冻结语料和 20 例校准；语料、模型、prompt、chunking、权重或分数归一化改变后必须重跑完整分布。BM25 是进程内索引，进程重启后需从同一 frozen 语料重建。jieba 产生一个来自其依赖内部 `pkg_resources` 的弃用警告，不影响结果。Git 仍会报告历史畸形 pytest 目录权限警告，本步没有删除或移动该不明目录。
- 下一步直接需要的信息：P0-08 直接复用 P0-04 的 `TransportOrder`、`AMRState`、`GridPosition` 和嵌套坐标；实现独立 Hungarian 与最近空闲基线，显式报告不可行组合及原因，并交付 C++ library/executable、JSON stdin/stdout 契约和 CTest。P0-08 不应修改 P0-07 payload、阈值、collection 或数据库 revision。

### 2026-08-20 · P0-08 C++ Hungarian 任务分配

- 完成：新增独立 `task_allocator` C++17 静态库、`task_allocator_cli` JSON stdin/stdout 可执行程序和 `nearest_idle_amr` 独立 baseline。Hungarian 支持矩形 AMR—订单矩阵，通过 dummy 行/列表示未匹配并优先最大化可行匹配数。
- 完成：代价显式包含到 pickup 距离、迟到风险、订单优先级奖励、电量风险和当前负载；电量沿用 30/20/10/15 冻结规则。不可行组合在内部使用 `1e12` INF，JSON 使用标准字符串 `"INF"`，并返回稳定 `reason_codes/reasons`。
- 完成：严格 JSON 请求 envelope 复用 P0-04 `AMRState`、`TransportOrder`、`GridPosition`，新增 `location_positions`、`completed_order_ids`、五项 `weights` 和显式 `config`；未知字段、重复键、非有限数字、越界坐标和 4 MiB 以上 stdin 被拒绝。公共契约详见 `docs/TASK_ALLOCATOR.md`。
- 未完成：没有实现 P0-09 A*、障碍/单向边、时空预约、P0-10 车队计划验证，也没有接入 P0-12 工具注册表或 Python 调用方；P0-08 只计算 Manhattan 分配代价，不声称路线合法。
- 关键设计决策及原因：不引用 Anaconda 中偶然存在的 Boost/JSON 库，使用本模块严格子集编解码器，避免 CMake 绑定个人 Python 环境；baseline 独立实现选择逻辑，不能作为生产 Hungarian 的隐式 fallback；按 ID 排序和微小 tie-break 保证跨次运行稳定。
- 验证命令与结果：基线 CMake 配置后直接 build 暴露未初始化 MSVC 环境；导入 `E:\BuildingTools\Common7\Tools\VsDevCmd.bat -arch=x64 -host_arch=x64` 后重新配置/构建成功。`ctest --test-dir build/cpp --output-on-failure` 实测 7/7 通过；覆盖 `planner_cpp_smoke`、正常、低电量、无可行、订单多于车辆、边界/依赖/重复 ID/依赖环和 JSON 契约。CLI stdin 实测正常 Hungarian、nearest_idle、未知字段错误和全 INF 响应均输出合法 JSON，`--version` 报告 `0.1.0`/C++17；随后执行完整 `.\scripts\run_smoke.ps1`，环境门禁、Python 110/110、迁移/Qdrant 检查和 CTest 7/7 全部通过。`scripts/export_schemas.py` 本步未重跑，因为没有修改 Pydantic Schema。
- 外部服务当前状态：P0-08 算法本身不依赖模型、FastAPI、PostgreSQL 或 Qdrant，也没有改变这些服务；统一 smoke 使用现有 PostgreSQL/Qdrant。结束复核显示 `amr-postgres`/`amr-qdrant` 运行且 5432/6333 监听，8000/8080 未监听。C++ 构建依赖固定 MSVC/CMake/Ninja 路径并需要先初始化开发环境。
- 已知限制/风险：JSON 编解码器是本模块严格子集，不是通用 JSON 库；后续若扩展请求字段必须同步文档、解析门禁和 CTest。分配器不处理实际障碍/路径冲突；`TransportOrder` 没有订单重量，当前负载只能作为代价和已超上限阻断，不能替代未来订单容量字段。
- 下一步直接需要的信息：P0-09 应从 `AllocationResult.assignments` 或 CLI `assignments` 读取 AMR/订单匹配，复用 `location_positions` 与 P0-04 嵌套坐标；路线合法性必须在 A*/Validator 中重新裁决，不能把 P0-08 的 Manhattan 距离当作可执行路径。启动下一步前先检查 `docs/TASK_ALLOCATOR.md`、P0-04 Schema 和当前 CTest 7/7 基线。

### 2026-08-20 · P0-09 C++ A* 与时空预约表

- 完成：新增 `route_planner` 静态库、`route_planner_cli` 和独立 `route_planner_tests`；实现 `(x,y,heading,t)` A*、前进/转向/等待代价、曼哈顿启发式、按优先级的多车规划、`(cell,t)` 顶点预约、`(edge,t)` 交换边预约、障碍/禁行边/单向边/边界硬约束和明确 `infeasible` 结果。
- 完成：实现完全独立的时间扩展 Dijkstra baseline；它不调用 A*，不作为生产失败 fallback。路径输出包含逐时刻位置/朝向/动作/累计代价，终点占用保持预约到 `max_time`。
- 新增/变化的公共接口：`RouteMap`、`RouteAssignment`、`RouteRequest`、`RouteStep`、`PlannedRoute`、`RoutePlanResult`、`RouteError`、`ReservationTable`、`plan_routes_astar()`、`plan_routes_dijkstra()`、`plan_multi_amr_routes()`；CLI `--algorithm astar|dijkstra`，JSON schema_version 固定为 `1.0`，assignment 可接收 P0-08 的可选 `components` 审计快照，具体字段见 `docs/ROUTE_PLANNER.md`。没有修改 P0-04 Pydantic Schema、数据库字段或迁移。
- 关键设计决策：路线输入必须携带内存地图快照，`environment_ref` 只作为审计标识，防止 C++ 从不受控路径读取或猜测环境；prioritized planning 固定使用 priority/release/ID tie-break，不实现 Scope 排除的 CBS/ECBS；deadline 仍交给 P0-10 最终 Validator。
- 验证命令与结果：`.\scripts\run_smoke.ps1` 实际通过，环境/依赖门禁、Alembic/Qdrant 检查通过，Python `110 passed, 1 warning`；CMake/C++ 构建成功；完整 `ctest --test-dir build/cpp --output-on-failure` 为 `19/19` 通过，其中 P0-09 专项为 `12/12`；性能场景实测 `performance_ms=4`、`expanded_states=88`。唯一警告是既有 jieba `pkg_resources` 弃用警告，不影响结果。
- 外部服务当前状态：本步没有启动模型、FastAPI、PostgreSQL 或 Qdrant；smoke 复核显示 PostgreSQL/Qdrant 容器与 5432/6333 正常，8000/8080 未监听。C++ 构建仍依赖固定 MSVC/CMake/Ninja 路径，必须先导入 `E:\BuildingTools\Common7\Tools\VsDevCmd.bat`。
- 已知限制/风险：当前是固定优先级顺序的 prioritized planning，后续车无安全路径直接整体不可行，不回溯换序；未实现 P0-10 的安全距离、工位容量、deadline/载荷/电量最终校验，未接入 P0-12 Python 工具注册表；当前没有独立地图 Pydantic Schema，任何跨语言消费者必须严格遵守 route JSON 文档。
- 下一步直接需要的信息：P0-10 应直接消费 `RoutePlanResult.routes[*].path` 和 `ReservationTable` 的冲突语义，再验证时间窗、工位容量、安全距离、电量等业务/运动规则；P0-12 适配时调用固定 `route_planner_cli.exe`，不要把 Dijkstra 当 fallback，也不要让 LLM 绕过 `status=infeasible`。

### 2026-08-20 · P0-10 C++ 车队计划验证器

- 完成：新增 `fleet_plan_validator` 静态库、`fleet_plan_validator_cli`、严格 JSON 编解码和 14 个正反例 CTest；独立检查任务依赖、时间窗、载荷、电量安全余量、禁行区/边、工位容量、Manhattan 安全距离、顶点冲突和交换边冲突。
- 未完成：没有实现 P0-12 工具注册或 Python 工具调用适配；没有修改 P0-04 Pydantic Schema、数据库字段、Alembic revision 或模型服务。工位容量当前只按同一离散事件时刻聚合，不表达未来服务持续时间。
- 新增/变化的公共契约：`ValidatorConfig`、`FleetPlanRoute`、`FleetPlanRequest`、`ValidationEvidence`、`ValidationErrorDefinition`、`ValidationResult`、`error_dictionary()`、`validate_fleet_plan()`；JSON schema_version=`1.0`，ruleset_version=`p0-10.v1`，路线必须携带执行期 `payload_kg`。完整字段见 `docs/FLEET_PLAN_VALIDATOR.md`。
- 关键设计决策及原因：验证器不读取 `environment_ref`、不信任 P0-09 status 或 LLM/Prompt 声明，先通过严格 JSON 白名单拒绝旁路字段，再对完整路径和全车队状态独立重算；终点保持占用到 `max_time`；错误按稳定键排序并通过单一 C++ 错误字典输出，确保相同请求产生相同证据。
- 验证命令与结果：导入 `VsDevCmd.bat -arch=x64 -host_arch=x64` 后 `cmake --build build\cpp` 成功；`ctest --test-dir build\cpp -R "^fleet_validator_" --output-on-failure` 为 14/14；`ctest --test-dir build\cpp --output-on-failure` 为 33/33。CLI `--version`、合法计划、LLM 旁路拒绝和 `--error-dictionary` 均已实测；业务非法计划返回退出码 0 且 `status=invalid`，契约错误返回退出码 2。
- 外部服务当前状态：P0-10 本身不依赖 FastAPI、文本 Qwen、PostgreSQL 或 Qdrant，本次没有启动模型/API；构建依赖固定 MSVC/CMake/Ninja 路径。统一 smoke 的服务状态需在最终执行时重新核验，不能沿用历史快照。
- 已知限制/风险：P0-04 `TransportOrder` 不含订单重量，当前由 `FleetPlanRoute.payload_kg` 明确补充；容量只覆盖 pickup/dropoff 同 tick 事件；CLI 仍未进入 P0-12 受控工具注册表。
- 下一步直接需要的信息：P0-12 应从 `services.amr_simulator` 稳定入口接入 `dispatch_simulation`，保留 P0-10 前置门禁、P0-09 原始路径、Observation/event evidence 和故障注入隔离；若引入服务持续时间/动态装卸，先同步公共契约、错误字典、文件职责、交接和反例测试。

### 2026-08-20 · P0-11 Python AMR 离散事件仿真

- 完成：实现 `services.amr_simulator` 的严格计划契约、固定 1 秒 tick 执行器、P0-10 固定 CLI 前置门禁、P0-04 Observation、结构化事件日志、订单/工位/充电站状态和 Eval 专用 `offline`/`battery_drain`/`stuck` 故障注入。
- 未完成：没有注册 `dispatch_simulation` 工具（属于 P0-12），没有实现未验证的去充电站路线、故障恢复、服务持续时间、Checkpoint 或 ROS/真实底盘。
- 新增/变化的公共契约：`SimulationPlan` 复用 P0-10 JSON 顶层字段；`RouteStep` 复用 P0-09 `time/action/heading/g_cost`；新增 `SimulationResult`、`SimulationEvent`、`SimulatorConfig`、充电/订单/工位状态和 `FaultInjection`；`Observation` 使用 `source=simulator`，固定 epoch 时间和 event evidence refs。没有新增数据库字段/revision或正常 ToolName。
- 关键设计决策及原因：验证器通过后直接按路径时间戳执行，不重算路线、不瞬移充电、不释放终点占用；pickup/dropoff 采用零时长事件以保持 P0-10 时间契约；故障均安全停机并要求重规划；事件 ID/Observation 时间不依赖墙上时钟，确保同一输入/seed 可重放。
- 验证命令与结果：`E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p011_simulator.py -q` 实测 `8 passed in 0.14s`；`E:\Anaconda\envs\torch128\python.exe -m compileall -q services\amr_simulator tests\unit\test_p011_simulator.py` 实测通过。测试使用真实 P0-10 CLI，覆盖正常运输、状态迁移、充电、低电量、离线/电量故障、非法时间戳、可复现性和工具白名单隔离。
- 外部服务当前状态：P0-11 不需要启动 Fast/Smart Qwen、FastAPI、PostgreSQL 或 Qdrant；Validator 使用已有本地 C++ 构建产物。最终完整 `run_smoke.ps1` 的环境/数据库/Qdrant/服务状态以最后一次实际输出为准。
- 已知限制/风险：充电站不在 P0-10 运输 envelope 中，由 `SimulatorConfig` 单独提供；低电量不在站点时只进入 `TO_CHARGE` 原地等待。当前 fault 是终止式安全停机，不能恢复后跳过既有路径；P0-10 工位容量仍是同 tick 零时长事件容量。
- 下一步直接需要的信息：P0-12 只能从 `services.amr_simulator` 稳定入口包装 `dispatch_simulation`，把 P0-10 失败映射为受控工具错误、把 Observation/event 作为证据返回，并继续隔离 FaultInjection。

### 2026-08-20 · P0-12 九个白名单工具

- 完成：新增 `agent.tools.build_tool_registry()` 和 `ToolRegistry/ToolExecutor`，注册恰好 `retrieve_knowledge`、`get_fleet_state`、`allocate_tasks`、`plan_multi_amr_routes`、`validate_fleet_plan`、`dispatch_simulation`、`query_execution_state`、`run_verification_suite`、`request_approval` 九个工具。每个工具均绑定实时 Pydantic 输入/输出 Schema、允许角色、超时、副作用/幂等声明、错误分类和审计字段。
- 完成：`ToolExecutor` 在 handler 前执行顶层键、Pydantic、角色和跨字段校验；handler 后执行输出 Schema 校验；统一生成 `ToolResult` 的版本、角色、input/output SHA-256、时间、耗时、证据引用、effect ID、错误和幂等缓存。相同 call_id + 工具/角色/规范化输入返回首次结果，复用不同请求返回 conflict，超时返回 timeout。
- 完成：新增固定 C++ JSON 客户端，只允许 `task_allocator_cli.exe --algorithm hungarian`、`route_planner_cli.exe --algorithm astar`、`fleet_plan_validator_cli.exe --validate`，使用 JSON stdin/stdout、4 MiB 限制、固定仓库 cwd、`shell=False` 和 subprocess timeout；无可行路线/Validator invalid 映射为 `unsafe_plan`，不把 Dijkstra 当隐式 fallback。
- 完成：`retrieve_knowledge` 延迟复用 P0-07 `HybridRetriever`；`get_fleet_state`、分配、A* 共享固定 warehouse seed 快照；`dispatch_simulation` 复用 P0-11 `AMRSimulator`、不暴露 FaultInjection，并将确定性 simulation ID 结果登记到状态存储；`query_execution_state` 可读取该结果；审批使用稳定 digest 创建 pending 请求；验证套件只允许固定 Python/CTest/Smoke suite/case。
- 未完成：默认状态/审批存储仍是进程内适配器，尚未接入 P0-06 PostgreSQL Checkpoint/HITL API；默认 RAG retriever 需要本地 Embedding、Qdrant 和 frozen 语料在线，工具层本身不启动这些服务；没有真实底盘/ROS 或本地 LLM 依赖。
- 新增/变化的公共契约：`ToolSpec.audit_fields`；`ToolResult.tool_version/principal_role/input_digest/output_digest/idempotency_key/audit_metadata`（均兼容旧结果，P0-12 新结果全量填写）；`agent.tools.schemas` 中九个输入模型、`FleetStateOutput`、`AllocationResponse`、`RoutePlanResponse`、`ValidationResponse`、`ExecutionStateOutput`、`VerificationSuiteOutput`、`ApprovalRequestOutput`；`FixedCppJsonClient`、`EnvironmentSnapshot`、`InMemoryExecutionStateStore`、`InMemoryApprovalStore` 和 `build_tool_registry`。新增/更新工具 Schema 已由 `scripts/export_schemas.py` 导出到 `docs/schemas/`，包括独立的 `FleetStateOutput.schema.json`；数据库没有新增字段或 Alembic revision。
- 关键设计决策：工具输入不接受 executable、command、path、DSN、Shell 或 faults；environment_ref 只匹配固定 seed 身份，不参与路径拼接。`dispatch_simulation` 标记 `requires_approval=true`，但 P0-12 只负责声明和请求 pending 审批，实际批准仍走 P0-06/P0-16；仿真 `SimulationResult.status=blocked/timeout` 是已完成的仿真证据，而 P0-10 invalid 是工具失败。默认快照/状态/审批依赖可在组装期替换，不进入 Agent 参数。
- 验证命令与结果：`E:\Anaconda\envs\torch128\python.exe -m compileall -q agent\tools` 通过；`E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py` 成功导出 34 份 Schema；P0-12 专项 `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p012_tools.py -q -p no:cacheprovider` 为 12 passed、1 warning；P0-04/P0-11/P0-12 联合回归 `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p004_contracts.py tests\unit\test_p011_simulator.py tests\unit\test_p012_tools.py -q -p no:cacheprovider` 为 66 passed、1 warning。另以实际固定 C++ 构建产物执行分配→A*→Validator→dispatch→query 集成脚本：allocation success/complete、route success/complete、validate success/valid、dispatch success（SimulationResult.status=timeout，窗口截断是预期）、query success/simulation。最后实际执行 `scripts\run_smoke.ps1` 退出码为 0：环境/依赖、PostgreSQL 迁移、Qdrant 检查通过，pytest 为 130 passed、1 warning，完整 CTest 为 33/33。
- 外部服务/构建状态：P0-12 工具层没有启动 Fast/Smart Qwen；RAG 只有实际调用 `retrieve_knowledge` 时才构造 Qdrant/Embedding。本次 smoke 实测 PostgreSQL 核心表缺失 0、Qdrant collection 健康；C++ 适配器依赖 `build\cpp\services\planner_cpp\` 三个固定 exe，CMake/Ninja 构建与 33 个 CTest 均已复核通过。
- 已知限制/风险：通用 Python timeout 在线程边界返回结果但不能强杀任意 Python handler；生产高风险外部调用仍依赖下层 C++/subprocess timeout，未来若接入异步/进程隔离应保留相同 ToolResult 语义。默认内存状态/审批在进程重启后丢失，P0-14/P0-16 接入持久化前不能宣称可恢复。
- 下一步直接需要的信息：P0-13 应直接消费 `build_tool_registry()` 和 `ToolResult`，在 guard/Planner 执行前检查 `ToolSpec.requires_approval`、`status`、`error.category` 和 Validator evidence；不得复制 C++/仿真器边界或把 FaultInjection 加回正常工具表。P0-06 PostgreSQL 审批/运行服务接入时，优先实现 `ExecutionStateStoreProtocol/ApprovalStoreProtocol` 的适配器并保留同一 digest/idempotency 语义。

### 2026-08-20 · P0-12 完成后严格工程审查

- 审查结论：`PASS WITH FIXES`。逐项对照正式路线后，九个 ToolName、注册表交付物、固定 C++ JSON 适配器、参数预检、超时/错误/审计/幂等契约和集成测试均达到 P0-12 门槛；没有引入本地 LLM 依赖，也没有把 `FaultInjection` 注册到正常工具表。
- 审查发现并修复：高严重度——ledger-only 查重存在并发竞态，同一 call_id 可同时进入 handler；RAG 工具无返回侧 ACL 熔断，错误后端可把越权正文交给 viewer。中严重度——不可序列化参数可借已有 call_id 覆盖合法 ledger；ToolSpec 未完整声明执行器实际可能产生的 timeout/conflict/internal；受控 `security` case 使用模糊 `-k` 时选中 0 条并退出 5；验证 runner 硬编码本机盘符；仿真状态 task_ids 未按 order_id 筛选。低严重度——A*/Hungarian 输出仍接受 baseline 名称、部分整数/空白/汇总字段不够严格、静态 obstacles 未合并、无效角色审计成 operator、每次调用重复构造默认快照 Provider。上述问题均已最小修复并有反例。
- 新增/变化的公共契约：同一请求的并发重复调用现在共享一个 in-flight 结果，handler/副作用只执行一次；不同请求复用 call_id 仍稳定 conflict；不可序列化请求不进入 ledger，因而不能污染已有结果。九个 ToolSpec 均声明公共 timeout/conflict/internal；`audit_metadata` 成为默认审计字段。RAG 返回侧复核 query/role/top_k/document ACL；`AllocationResponse.algorithm` 固定为 `hungarian`，`RoutePlanResponse.algorithm` 固定为 `astar`；A* `max_time<=2000`；Validator/验证套件汇总字段必须一致；默认地图合并静态和临时障碍。数据库字段、Alembic revision 和 ToolName 没有变化。
- 关键设计决策及原因：C++ 子进程 timeout 固定比外层 ToolSpec 少 1 秒，确保先回收进程再形成 ToolResult；通用 Python handler 使用协作取消事件，并在仿真/审批写入前复核，避免外层超时后的迟到副作用。验证程序只从可信进程 PATH 解析固定绝对 executable，工具参数仍不能提供 command/path/cwd。安全套件使用显式 pytest node id，不能依赖会产生空选集的表达式。
- 实际验证：`E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p012_tools.py -q -p no:cacheprovider` 为 `20 passed, 1 warning`；P0-04/P0-07/P0-11/P0-12 联合单测为 `83 passed, 1 warning`；`tests\integration\test_p007_rag_backends.py` 为 `2 passed, 1 warning`；直接 CTest 为 `33/33 passed`。经真实 `ToolRegistry` 调用 `run_verification_suite(p0_12)` 为 `4/4 passed`，调用 `run_verification_suite(p0_cpp)` 为 `1/1 passed`。最终 `scripts\run_smoke.ps1` 退出码 0：环境/依赖、PostgreSQL 迁移和 Qdrant 均 `ok`，Python `138 passed, 1 warning`，C++ `33/33 passed`。`compileall` 与 `git diff --check` 退出码均为 0；唯一 warning 是 jieba 依赖使用已弃用 `pkg_resources`，不是本次失败。
- 外部服务当前状态：最终 smoke 实测 PostgreSQL 核心表缺失 0、Qdrant collection `amr_warehouse_knowledge` 可用；没有启动或调用 Fast/Smart Qwen，也没有启动新的常驻服务。三个 C++ CLI 仍从仓库 `build\cpp\services\planner_cpp\` 固定位置调用。
- 已知限制/风险：Python 线程不能被解释器强杀，协作取消只能保证本仓库副作用 handler 不在超时后落账；未来新增 handler 必须响应同一取消门禁。默认执行状态和审批存储仍不可跨进程恢复；身份真实性仍依赖 P0-16。仓库既有 smoke/toolchain 脚本包含当前 Windows 构建环境路径，这是 P0-01 已登记的环境契约，P0-12 运行时适配器与受控验证器均未新增本机硬编码。
- P0-13 前阻塞项：无。P0-13 必须直接复用注册表和 ToolResult，在调度 `requires_approval` 工具前接入 guard，按 Validator evidence 判定业务合法性，并保持 FaultInjection、baseline 算法和任意命令参数不可达；不可把进程内幂等误称为重启恢复。

### 2026-08-20 · P0-13 PEVR 正常闭环

- 完成：新增固定八阶段 LangGraph 主图 `guard → understand → retrieve → plan → validate → execute → verify → finish`。`understand_goal`、`plan_tasks`、`verify_observation`、`compose_report` 复用 P0-05 命名节点；RAG、Hungarian、A*、P0-10 Validator、Python 仿真全部通过 P0-12 `ToolRegistry` 进入。
- 完成：Planner 只允许四任务链 `allocate_tasks → plan_multi_amr_routes → validate_fleet_plan → dispatch_simulation`；`validate_normal_pevr_plan` 在任何 Planner 工具执行前确定性检查拓扑、白名单参数、合同环境/订单、封路、deadline、`p0-10.v1`、请求 seed、审批声明、运行期证据和两个受控 `$ref`。
- 完成：`dispatch_simulation` 的 `ToolSpec.requires_approval=true` 在 guard 声明检查和 execute 调用前再次检查；只有 `PEVRRequest.approval_granted` 这一可信上层字段能放行，Planner/自然语言不能自行批准。当前 CLI 用 `--approve-dispatch` 显式提供该上下文。
- 完成：新增 `PEVRRequest`、`PEVRStage`、`PEVRTraceEvent`、`PEVRToolEvidence`、`PEVRMetrics`、`PEVRRunReport`、`PEVRRunResult` 和 `PEVRGraphState`。报告契约包含 `citations`、`plan_version`、`tool_evidence`、`metrics`、`budget_usage` 和 `unresolved_risks`；新增 `docs/schemas/PEVRRunReport.schema.json`。没有新增数据库字段、Alembic revision、ToolName 或 P0-12 输入/输出字段。
- 关键设计决策：继续复用 `RunState` 作为业务事实，LangGraph 只保存受控引用；P0-13 不启用 Checkpoint，P0-14 才接 PostgreSQL 恢复。Fast 本地多节点累计预算固定为 300 秒/30000 输入/5000 输出/8 工具步/0 重规划；本地网关输出上限为 4096，单次 context window 仍为 8192。为兼容本地模型对 `JsonValue` 的 `{type,value}` 和固定事实引用偏差，规范化层只解析原语、正式 `$ref` 和合同/请求白名单事实，未知形状不求值并交由 Validator 拒绝。
- 实际验证命令与结果：
  - `E:\Anaconda\envs\torch128\python.exe scripts\smoke_p005_prompts.py --profile fast`：5/5 通过，alias=`qwen3.6-fast`，Prompt version=`1.1.0`。
  - `E:\Anaconda\envs\torch128\python.exe scripts\run_p013_e2e.py --approve-dispatch --output tmp\p013_e2e_result.json`：真实 Fast E2E completed；8/8 阶段、5/5 工具成功、4 次模型调用、4 个计划任务、5 条 RAG 结果、Validator error=0、仿真 completed、ORDER-001 完成、结束时间 120；报告 run_id=`p013-e2e-b22ad4723f9e51bb9034`。
  - 同一命令用 run_id=`p013-e2e-repeat-2`、`p013-e2e-repeat-3` 连续复跑：两次均 completed，工具 5/5、仿真 completed、ORDER-001 完成，报告分别为 `tmp\p013_e2e_repeat2.json`、`tmp\p013_e2e_repeat3.json`。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p013_pevr.py -q -p no:cacheprovider`：5 passed、1 warning；`E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider`：143 passed、1 warning。
  - `E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py`：成功导出含 PEVR 报告的 35 份 Schema；`.\scripts\run_smoke.ps1`：退出码 0，Python 143 passed、1 warning，CTest 33/33。
  - `E:\Anaconda\envs\torch128\python.exe -m compileall -q agent\planning agent\runtime services\config scripts\run_p013_e2e.py tests\unit\test_p013_pevr.py` 与 `git diff --check`：均退出码 0。
- 真实报告关键指标：输入 Token `20602`、输出 Token `2699`、工具步 `5`、重规划 `0`、累计模型/工具耗时预算 `62.181s`/工具耗时 `7103ms`；citation 5 条，工具证据 5 条。唯一报告风险是 P0-04 `TransportOrder` 没有重量字段，执行期 `payload_kg` 固定为 `1.0kg`。
- 外部服务当前状态（本步结束实际核验）：`amr-postgres`、`amr-qdrant` 均 running；5432、6333/6334 正常监听，Qdrant collection=`amr_warehouse_knowledge` 可用；8080 和 8000 无监听。Fast 由 `E:\Llama.cpp\start-qwen3.6-agent.cmd` 启动用于验收，验收完成后已停止；Smart 未启动。C++ 三个固定 CLI 仍在 `build\cpp\services\planner_cpp\`，构建和 CTest 已通过。
- 已知限制/风险：当前只实现成功闭环，不处理复杂异常、局部重规划、Checkpoint 恢复或跨进程副作用恢复；默认执行状态/审批仍是进程内适配器，不能宣称重启恢复。`payload_kg=1.0` 是缺少订单重量字段的临时正常路径约束。模型输出规范化是受控兼容方案，未来若升级 Planner Schema 应优先改为明确 typed argument contract 并继续保留拒绝反例。
- 下一工作包直接复用：P0-14 应从 `PEVRGraphState`/`RunState`、`ToolResult`、`effect_id`/digest 和当前阶段 Trace 接入 PostgreSQL Checkpoint，恢复时不能重新执行已完成副作用；P0-15 在 `verify`/`execute` 失败边界增加确定性异常分类和局部重规划，但不得放松当前 P0-13 Validator、审批和白名单门禁。下一次启动前仍需重新核验 Docker、Qdrant、PostgreSQL 和模型 alias。

### 2026-08-20 · P0-14 Checkpoint、幂等与局部重规划

- 完成：新增 `agent.runtime.checkpoint` 运行时契约和恢复协调器；`CheckpointSnapshot` 保存 JSON 化 `PEVRGraphState`，`EffectLedgerEntry` 保存预留/完成/核对/失败/待补偿状态。审计修复后，`make_effect_idempotency_key(run_id, plan_version, task_id)` 固定返回规范三元组 SHA-256（`p014:<digest>`），禁止分隔符碰撞，也禁止用 attempt、call_id 或随机 UUID 代替。
- 完成：新增 `services.application.PostgresRuntimeStore`（别名 `PostgresCheckpointStore`），复用 P0-06 的 `runs.run_state_snapshot`、`plans`、`tasks`、`tool_calls`、`effects`、`events`。Checkpoint/计划/任务状态同步和事件在同一事务中提交；Effect 先独立提交 `reserved` 唯一行；审计修复后 dispatch 外部快照在 handler 返回前独立提交到同一 Effect 行，再更新 completed，唯一键并发冲突只读取赢家。
- 完成：`PEVRGraphRunner` 增加可选 `checkpoint_store` 和 `external_state_reconciler`。恢复先严格校验 run 请求/环境/seed/状态结构，再逐条查询真实仿真/工具状态；只有 effect/tool/key/input/output 身份全部一致的外部完成才复用并写 `reconciled`，明确 not_found 才可沿原键继续，未知/进行中/不一致转重规划，外部失败落 `compensation_required` 后停止。终态 Checkpoint 也必须先核对外部状态。
- 完成：`ToolRegistry.execute`/`ToolExecutor.execute` 接受 `idempotency_key`，传入业务键时缓存与 in-flight 协调按业务键工作；无键的既有 P0-12 调用继续按 call_id 兼容。PEVR 对真实副作用任务在调用前预留、完成后落账，恢复不会重新派发已核对的 dispatch。
- 完成：新增 `AffectedEntitySet`、`LocalReplanner`、`LocalReplanAnalysis/Result`。支持 AMR、命名/坐标通道、blocked cell/edge、工位、工具和任务标签；审计修复后从实际 allocation/route ToolResult 构建 provenance，直接影响只沿 DAG 传播到未完成后继，已完成节点和 `effect_id` 原样保留，替换任务用新 ID，计划版本恰好加一并重新通过完整 PEVR Validator；`apply_to_run_state()` 同步 `replanning` 状态和重规划计数。
- 完成：FastAPI 生命周期组装 `application.state.checkpoint_store`，新增 `get_checkpoint_store` 依赖入口；没有在启动时执行迁移或工具副作用。数据库没有新增 revision，继续使用 `0001_p006_core`。
- 未完成：P0-14 不提供任意自动补偿工具，也没有把局部重规划动态插入 P0-13 固定八节点图；它提供安全决策/状态边界和可调用的确定性 Replanner。单独构造 `ToolRegistry` 时 execution/approval 仍默认进程内；真实 PEVR CLI 已为 dispatch 注入 PostgreSQL 外部状态，未来其他副作用工具仍需各自可靠适配器。P0-13 正常合同的 `max_replans=0`。本次明确不推进 P0-15。
- 新增/变化的公共契约：`CheckpointSnapshot`、`EffectLedgerEntry`、`EffectLedgerStatus`、`ExternalExecutionSnapshot/Status`、`RecoveryDecision/Assessment`、`RuntimePersistenceProtocol`；`ToolRegistry/ToolExecutor.execute(..., idempotency_key=...)`；`AffectedEntitySet.channel_ids`、`LocalReplanner` 及 `apply_to_run_state()`；`PostgresRuntimeStore/PostgresCheckpointStore`；FastAPI `checkpoint_store` state/依赖。没有新增 Pydantic 数据库字段或 Alembic revision。
- 关键设计决策及原因：先 reserved 后外部调用，避免长事务；恢复必须先核对真实状态，未知不等价于 not_found；已完成任务缺少结果时直接停止，不用重复执行补齐；局部重规划只沿原 DAG 失效未完成后继，保留已完成节点/副作用作为只读锚点；补偿只落账为 required，不假造未定义的补偿接口。
- 实际验证命令与结果：
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p014_checkpoint.py -q -p no:cacheprovider`：`7 passed, 1 warning`。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p014_replanner.py -q -p no:cacheprovider`：`6 passed, 1 warning`。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration\test_p014_postgres.py -q -p no:cacheprovider`：`2 passed, 1 warning`，真实 PostgreSQL 跨 Store/Runner 实例读取 Checkpoint/Effect Ledger 并验证不重派 dispatch。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p006_persistence.py tests\unit\test_p012_tools.py tests\unit\test_p013_pevr.py tests\unit\test_p014_checkpoint.py tests\unit\test_p014_replanner.py -q -p no:cacheprovider`：`44 passed, 1 warning`。
  - `E:\Anaconda\envs\torch128\python.exe -m compileall -q agent\planning agent\runtime services\application apps\api tests\unit\test_p014_checkpoint.py tests\unit\test_p014_replanner.py`：退出码 `0`。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider`：`158 passed, 1 warning`。
  - `.\scripts\run_smoke.ps1`：退出码 `0`；环境/依赖门禁通过，PostgreSQL 8 张核心表缺失 `0`、Qdrant collection 健康，pytest `158 passed, 1 warning`，CTest `33/33 passed`。
- 外部服务当前状态：本步 PostgreSQL 集成测试已实际连接 `localhost:5432/amr_agent`；`migrate_database.py current` 为 `0001_p006_core (head)`，8 张核心表缺失 `0`；Docker `amr-postgres`/`amr-qdrant` 当前 running，5432/6333/6334 监听，8000/8080 无监听。没有启动新的常驻模型/API 进程；Checkpoint Store 只使用已注入的 SessionFactory。
- 已知限制/风险：真实 `query_execution_state` 目前只对 `dispatch_simulation` 提供默认只读核对；其他副作用工具必须注入专用 `ExternalStateReconciler`。Python handler 超时仍不能被线程强杀；外部身份与 operator 真实性仍由 P0-16 处理。`tasks` 状态同步依赖同一事务中的计划任务行，损坏/缺失会显式抛出而不猜测。
- 下一工作包直接复用的信息：P0-15 应消费 `RecoveryAssessment`/`COMPENSATION_REQUIRED`，为补偿建立独立、审批和审计完备的工具契约；在线异常分支应调用 `LocalReplanner.apply_model_output()`，先比较确定性影响集合再接受 LLM 替换任务，并保留当前 Validator、审批和 Effect Ledger 唯一键门禁。启动前以本文件顶部状态、`docs/P014_CHECKPOINT.md` 和实际数据库/模型健康检查为准。

### 2026-08-21 · P0-00～P0-14 阶段审计修复（当前有效状态）

- 范围：只修复阶段审计发现的缺口，没有实现、启动或验证 P0-15。Smart 按用户指示
  暂时禁用，等待日后明确指示；本条覆盖上方 P0-14 初次完成记录中的临时/旧语义。
- 模型门禁：`ModelProfileSettings` 新增 `enabled/disabled_reason`，默认 TOML 与代码
  fallback 均把 Smart 设为 `enabled=false`。`ModelProvider.startup()` 在构造任何
  `/v1/models` 或 completion 请求前抛出 `ModelProfileDisabledError`，稳定错误码为
  `MODEL_PROFILE_DISABLED`；环境变量只能选择 Profile/alias，不能覆盖 enabled。
- Fast 稳定性：Planner 首个候选立即经过正常确定性 PEVR Validator；只允许一次带错误
  反馈的语义修复，第二个候选仍非法即停止。模型调用数改为状态内真实累计，不再固定为 4。
  Validator ToolResult success 但业务 invalid、route timeout、仿真 blocked 等路径均在
  dispatch/finish 前失败。
- 公共契约：run ID 上限统一为 64，task/assignment ID 上限统一为 128，环境 ID 上限为
  256；`GridPosition` 的 x/y 使用 strict integer。新增 `WarehouseMap`、`WarehouseLocation`、
  `WarehouseEdge`、`NarrowAisle` 严格契约和 Schema；固定地图有非空但不阻断正常订单的
  obstacles/narrow aisles/blocked edges/one-way edges/temporary blocks。
- P0-09：未分配 AMR 从 start_time 到 max_time 持续预约初始 cell；新增
  `route_planner_idle_amr_reservation` 反例，防止路线穿过窄通道中的空闲车。
- P0-14 幂等：副作用身份仍严格是 `run_id + plan_version + task_id`，但字符串键改为
  规范 JSON 三元组 SHA-256（`p014:<digest>`），避免合法 ID 含冒号时碰撞。Effect Ledger
  与 ToolResult 都对 Pydantic 规范化后的工具参数求同一种 input digest；完成落账再次核对。
- P0-14 恢复：Checkpoint 恢复同时核对 request/environment/seed，未知键、坏列表项、
  Trace 逆序/重复、ToolResult 与 task ID 数量不一致均 fail closed 为 `checkpoint_corrupt`。
  外部 completed 只有 effect ID、tool、business key、input/output digest 全部相符才能
  `reconciled`；任一不一致都转安全重规划。
- P0-14 持久化：`PostgresRuntimeStore` 现在也实现 execution state store。真实 PEVR CLI
  把同一 Store 注入 ToolRegistry 和 Runner；dispatch handler 返回前先按业务键锁定
  Effect 行并独立提交 `external_execution` 快照/digest。真实子进程在此后、Effect 完成
  前 `os._exit(73)`，新 Engine/Runner 恢复时实测不再次调用 dispatch，Effect 仍只有一行。
- P0-14 局部重规划：从成功 allocation/route ToolResult 和地图构建真实 AMR/cell/edge/
  channel/workstation provenance，影响只传播到未完成后继；完成节点/effect 保留。新版本
  必须带原合同、九工具规格和 seed 重新通过完整 `allocate→route→validate→dispatch`
  PEVR Validator，route-only 替换即使 DAG 合法也会被拒绝。
- 工程修复：`run_smoke.ps1`/`check_environment.py` 支持通过参数或 `AMR_*` 环境变量覆盖
  Python/CMake/Ninja/MSVC 路径，保留当前 E 盘默认。仓库测试必须显式用 torch128
  Python；曾误用 Anaconda base 的一次 pytest 在收集阶段缺依赖，属于错误运行环境，
  没有计入产品失败或通过。
- 实际验证命令与结果：
  - `E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py`：成功导出 36 份
    运行时同源 Schema。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p004_contracts.py tests\unit\test_settings.py tests\unit\test_model_provider.py -q -p no:cacheprovider`：
    `65 passed, 1 warning`。
  - P0-13 专项 `tests\unit\test_p013_pevr.py`：`10 passed`；覆盖一次计划修复和四类
    Validator/route/simulation 失败反例。
  - P0-14 三个专项文件：`24 passed`；其中真实 PostgreSQL 强杀窗口用例单独执行通过。
  - 最终 `.\scripts\run_smoke.ps1`：退出码 `0`；Python `178 passed, 1 warning in
    10.57s`，CMake/Ninja 构建成功，CTest `34/34 passed`，PostgreSQL 8 张核心表缺失 0，
    Qdrant collection 健康。唯一 warning 仍是 jieba 依赖的 `pkg_resources` 弃用提示。
  - `git diff --check`：退出码 `0`；仅 Git 提示工作区 LF 将来按配置转换为 CRLF，无空白错误。
- 真实在线模型验证：先确认 8080 空闲，再用隐藏窗口运行
  `E:\Llama.cpp\start-qwen3.6-agent.cmd`。`check_model_gateway.py --profile fast` 成功且
  served alias=`qwen3.6-fast`；`--profile smart` 在 Fast 仍在线时预期退出 1，返回
  `MODEL_PROFILE_DISABLED`，证明没有访问服务 alias。
- 三次真实 PEVR：分别使用 `p014-fast-online-1-20260821-1037`、
  `p014-fast-online-2-20260821-1038`、`p014-fast-online-3-20260821-1039`，均
  `completed`；每次 8/8 阶段、5/5 工具、4 次模型调用、4 个计划任务、Validator error=0、
  仿真 completed、ORDER-001 完成、结束时间 120。工具总耗时分别为 13215/7642/7512 ms。
- 服务终态：验收前 8080 确认为空；验收后只停止本次启动且路径/PID 均核对过的
  `E:\Llama.cpp\llama-server.exe` 与其 cmd 父进程，8080/8000 均再次确认无监听。
  Smart 从未启动。`amr-postgres`、`amr-qdrant` 当前 running，5432/6333-6334 正常映射。
- 未验证/限制：按用户指示没有重新启动 Smart 做生成行为测试；其最近一次历史结果仍是
  P0-05 2/5，不能视为通过。没有验证 P0-15、自动补偿、真实 ROS/底盘或未来其他外部
  副作用工具的 reconciler，这些均不在本次授权范围。正常 P0-13 合同仍为
  `max_replans=0`；P0-14 只交付可调用的安全局部 Replanner 边界。
- 当前建议：不要进入 P0-15，先让用户审阅/接受本次修复并处理现有未提交工作树。若用户
  日后要求启用 Smart，应先修改受审配置，再依次重跑 alias、五个 P0-05 节点和至少三次
  真实 PEVR；任何一步未通过都应恢复禁用。

### 2026-08-21 · P0-15 故障分类与终止策略

- 范围差异：上方阶段审计记录是在用户本次明确指令之前写入的，曾要求冻结且不推进
  P0-15；本次用户明确授权推进 P0-15，因此本节和文件顶部是当前有效状态，旧记录仅保留
  历史事实。
- 完成：新增 `agent.runtime.faults`，以稳定 `FaultCategory` 覆盖 `low_battery`、
  `amr_offline`、`channel_closed`、`workstation_occupied`、`tool_timeout`、
  `plan_infeasible`、`state_conflict`，未知错误固定 `fatal`；策略动作固定为
  `retry/replan/fallback/human/fatal`，并记录 raw code、阶段、任务/工具、影响实体和证据。
- 完成：`FaultRecoveryController` 先检查总步骤/总时长/输入输出 Token，再检查全局
  retry/replan 额度和幂等/副作用安全属性。默认 `max_replans=2`、`max_retries=2`，
  `ExecutionBudgets.max_replans<=2`、`max_retries<=4`；PEVR 入口默认总预算为
  300 秒、30000 输入 Token、5000 输出 Token、8 工具步。相同终态故障幂等复用；相同
  非终态故障继续消耗有限额度，避免状态循环。
- 完成：`ExecutionBudgets` 新增 `max_retries`；`BudgetUsage/BudgetSnapshot`、节点
  用量推进和 `RunState` 同步记录 retry；`RunState` 新增 `FaultRecord` 列表并拒绝重复
  fault_id、计数超过合同或与任务状态不一致的 Checkpoint。
- 完成：`PEVRExecutionError` 保留原 `stage/code/message`，并统一附加 `FaultSignal`；
  `PEVRGraphRunner.classify_failure()` 和 `FaultRecoveryController` 可直接消费已有
  影响集合。PEVR 仍是固定八节点图，不动态插入模型循环。
- 完成：`apply_replan/apply_model_replan` 复用 P0-14 `LocalReplanner`，只替换受影响
  未完成子图；已完成任务及 `effect_id` 保留，计划版本严格加一并重新通过完整 Validator。
  `save_replan_checkpoint()` 使用同一 Runtime Store；不删除、复制或重置旧 Effect Ledger，
  副作用 timeout 未明确外部 `not_found` 时直接人工处理。
- 新增/变化公共接口：`FaultCategory`、`RecoveryAction`、`FaultPolicy`、`FaultSignal`、
  `FaultDecision`、`FaultRecoveryController`、`RecoveryUsage`、`FaultRecord`；
  `ExecutionBudgets.max_retries`；`BudgetUsage.retries`；`RunState.retry_count` /
  `fault_history`；`PEVRGraphRunner.classify_failure()`；`PEVRExecutionError.fault`。
  无数据库字段、Alembic revision、ToolName 或 P0-14 Effect Ledger 键变化。
- 设计边界：`workstation_occupied` 为 retry→replan→human；普通 timeout 为有限 retry→
  fallback，副作用状态未知为 human；低电量/离线/封路/计划不可行是最多两次局部 replan→
  human；状态冲突 human；未知 fatal。没有注册任意补偿工具，`compensation_required`
  仍必须人工和独立审批。
- 实际验证：P0-15 单元专项 `tests/unit/test_p015_faults.py` 为 `21 passed`；P0-15
  集成专项 `tests/integration/test_p015_fault_recovery.py` 为 `8 passed`，两专项合计
  `29 passed`；P0-04～P0-15 组合回归（含 P0-12 工具、P0-13 PEVR、P0-14
  Checkpoint/Replanner）为 `163 passed`；
  每条 pytest 命令各有 1 条既有 jieba `pkg_resources` 弃用 warning。
- 实际验证：`tests/integration/test_p014_postgres.py` 连接真实 PostgreSQL 为 `3 passed`；
  `scripts/export_schemas.py` 成功导出 36 份同源 Schema，随后
  `test_checked_in_schemas_are_current` 为 `1 passed`；没有新增数据库字段或 Alembic revision。
- 实际验证：`.\scripts\run_smoke.ps1` 退出码 `0`；Python 全量 `207 passed, 1 warning in
  11.19s`，CMake/Ninja 无需重编，CTest `34/34 passed`，PostgreSQL 8 张核心表缺失数为
  0，Qdrant collection 健康；`python -m compileall -q agent/context agent/planning
  agent/runtime tests/unit/test_p015_faults.py tests/integration/test_p015_fault_recovery.py`
  退出码 `0`，`git diff --check` 退出码 `0`（仅有 Git 的 LF→CRLF 提示，无空白错误）。
- 服务状态/限制：本步没有启动 Fast 或 Smart；Smart 仍按配置硬禁用。当前实测 Docker
  `amr-postgres`/`amr-qdrant` 均为 Up，5432/6333/6334 正在监听，8000/8080 无监听；
  smoke 实测 PostgreSQL/Qdrant 健康；P0-15 集成使用
  InMemoryRuntimeStore 模拟 Checkpoint 重启；P0-14 的真实 PostgreSQL 集成仍是既有回归门禁，
  真实底盘/ROS 和未来其他副作用工具的 reconciler 不在本步范围。
- 直接复用：后续异常入口先调用 `PEVRExecutionError.fault`/`classify_failure()`，再由
  `FaultRecoveryController` 记录 RunState；局部计划必须继续使用 P0-14 的确定性影响分析、
  完整 Validator、Effect Ledger 和外部状态核对，不得把错误 message 当作人工批准或 not_found。

### 2026-08-21 · P0-16 RBAC、HITL 与安全边界（当前有效）

- 范围：完成 viewer/operator 两级 RBAC、JWT 身份真实性、文档检索 ACL、工具级权限、
  Prompt Injection 隔离、禁止任意代码/SQL/Shell/外部 HTTP/未注册工具，以及高优先级覆盖、
  人工接管和高风险写操作的 HITL interrupt/Checkpoint 恢复。
- 身份与权限：`agent.security.Principal` 是唯一授权主体；`JWTAuthenticator` 固定 HS256、
  issuer、audience、iat、exp、sub、role，并拒绝篡改、错误算法、缺 claim 和过期令牌。
  viewer 只能读 ACL 明确允许的文档和只读 ToolSpec；operator 才能访问写工具、上传文档、
  创建运行和决定审批。API 不接受 body/query/Prompt/RAG 自声明角色。
- 检索安全：`assert_retrieval_scope()` 拒绝 viewer 请求 operator 范围；Qdrant/BM25 候选、
  retrieve_knowledge 输出和 DocumentService 读取均执行 ACL，未授权文档对外统一为 404，
  避免文档存在性泄漏。检索文本是数据，不能改变系统权限、Schema、Validator、HITL 或工具白名单。
- 工具边界：安全 `ToolRegistry` 要求已验证 Principal，按 ToolSpec.allowed_roles 授权，
  输入先过固定 Schema，再拒绝 command、SQL、Shell、script、code、URL/HTTP 等执行选择器；
  只有九个固定 ToolName 有 handler，ToolResult 记录 principal_subject。旧的 P0-12 直接构造
  registry 仍保留 legacy role 兼容模式，但不作为 API/安全 PEVR 生产入口。
- HITL 数据流：安全 PEVR 在 Schema 和确定性 Validator 通过后创建 pending `HITLRequest`，
  将已完成工具证据、当前任务、`HITLInterrupt` 和审批定位写入 waiting Checkpoint，再抛出
  `PEVRInterrupt`。operator 通过 API/Store 批准后获得 HMAC `ApprovalGrant`；恢复时重新
  校验 Checkpoint、审批状态、签名、主体、期限、run/task/plan_version、计划摘要和 Validator
  摘要，任何不一致都在 handler 前拒绝。Effect Ledger 继续保证恢复不重复派发。
- 持久化与接口：`PostgresHITLStore` 复用 P0-06 approvals 表的 request_snapshot，不新增
  Alembic revision；`PEVRRequest` 增加 principal/approval_grant，`PEVRGraphState` 增加
  hitl_interrupt/approval_grant，身份和 HITL 四个模型加入 `scripts/export_schemas.py`。
  API 提供 `/agent/runs/{run_id}/hitl/{approval_id}/approve` 和 `/reject`，先核对
  approval_id 所属 run，再以签名 operator 调用 HITL Store。
- 实际验证：
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p016_security.py -q -p no:cacheprovider`：专项 8 passed。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider`：全量 215 passed，1 warning（既有 jieba/pkg_resources 弃用提示）。
  - `E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py`：成功导出 40 份公共 Schema；checked-in schema 一致性回归包含在全量测试。
  - `.\scripts\run_smoke.ps1`：退出码 0；Python 215 passed、1 warning，CTest 34/34 passed，
    PostgreSQL 8 张核心表无缺失，Qdrant collection 健康。
  - `E:\Anaconda\envs\torch128\python.exe -m compileall -q agent apps services tests`：退出码 0。
  - `git diff --check`：退出码 0；仅有 Git 的 LF→CRLF 转换提示，无空白错误。
- 服务状态与限制：本步未启动 Fast/Smart 模型；Smart 仍硬禁用。Smoke 实测 PostgreSQL/Qdrant
  健康，8080/8000 无模型服务监听。没有新增数据库表；没有真实 ROS/底盘、在线模型或未来
  外部副作用 reconciler 验收。HITLController 已覆盖四类暂停原因；实际安全 PEVR 测试覆盖
  高风险写操作的 pending→approved→resume 路径。
- 下一工作包：外部入口继续只从验签 Principal 取得角色；任何新工具必须先登记 ToolSpec、
  Schema、确定性 Validator、ACL/审计/幂等和失败反例，再决定是否接入 HITL，不得增加任意执行面。

### 2026-08-21 · P0-17 Trace、受控验证与证据报告（当前有效）

- 完成：新增 agent.runtime.trace。TraceEvent schema_version=1.0，字段包括
  trace_id、run_id、sequence、event_type、status、node、task_id、tool_name、
  tool_version、model_version、prompt_id、prompt_version、input_tokens、
  output_tokens、total_tokens、started_at、finished_at、latency_ms、
  parameters_digest、input_digest、output_digest、error、evidence_refs 和 metadata。
  task/model 是读取别名；失败、timeout、denied 必须有 TraceError，延迟和 Token 汇总可重算。
- 完成：PEVRRequest 可接收可选 trace_id；PEVRRunResult、PEVRRunReport 和 PEVRGraphState
  暴露 trace_id/trace_events。固定八节点包装器、四类 P0-05 模型节点、retrieve/execute
  工具调用和节点异常都会追加 Trace；失败事件在异常重新抛出前写入。旧 Checkpoint 缺少
  Trace 时按 run_id 稳定摘要补齐，已有 Trace 不允许换 run 或跳号。
- 完成：InMemoryRuntimeStore 按 run 保存 Trace；PostgresRuntimeStore 复用 events 表，
  事件类型为 trace.node/trace.model/trace.tool/trace.verification，使用确定性 event_id
  幂等插入。understand 模型事件早于 runs 创建时暂存，ensure_run 后补写；没有新增表、
  字段或 Alembic revision。
- 完成：run_verification_suite 只接受 p0_12/p0-12、p0_python、p0_cpp、p0_smoke、
  p0_simulation/p0_sim 固定 suite；case 选择是有限枚举，argv、cwd、shell=False 和
  timeout 都由代码固定。仿真入口为 services.validation.simulation_entry，无参数构造
  固定 plan/seed/ID，非 completed 以非零退出码退出。
- 完成：VerificationLogParser 只读固定子进程 stdout/stderr、退出码和超时，结构化
  status、failure_type、task_id、tool_name、parameters_digest、stdout/stderr digest、
  evidence_locations 和 evidence_refs。VerificationReportGenerator 从真实逐 case 结果
  重算状态/计数/report_digest，生成共享身份的 JSON/Markdown；契约拒绝非零退出码的伪造 passed。
- 公共接口变化：RunVerificationSuiteInput.trace_id；VerificationCaseOutput 的
  failure_type/task_id/tool_name/parameters_digest/evidence_locations/summary；VerificationSuiteOutput
  的 trace_id/report_id/report_digest/report_json/report_markdown/evidence_refs；新增
  ParsedVerificationCase、VerificationFailureType、VerificationEvidenceLocation、
  VerificationReport、TraceEvent、TraceError。固定入口不接受 executable、script、shell、
  pytest 表达式或任意命令字段。
- 实际验证：
  - tests/unit/test_p017_trace.py：4 passed。
  - tests/unit/test_p017_validation.py：6 passed，1 条既有 jieba/pkg_resources 弃用 warning。
  - P0-13/P0-14/Trace/Validation 组合：31 passed，1 warning；随后全量回归覆盖了
    P0-16 HITL waiting→resume 的 Trace 合并修复。
  - 真实 services.validation.simulation_entry：退出码 0，SimulationResult.status=completed，
    validation_result.status=valid，error_count=0。
  - 真实 FixedVerificationRunner p0_simulation：status=passed、case_count=1、failure_type=none，
    生成 report_id 和 report_digest；真实 ToolRegistry run_verification_suite 同样返回 success，
    trace_id=trace-p017-registry。
  - 真实 FixedVerificationRunner p0_python：status=passed、case_count=1、failure_type=none，
    生成 report_id=verification-05a1eaac5d745e04e5bb6a58；pytest stdout 通过同一日志解析/报告链。
  - 真实 FixedVerificationRunner p0_cpp：status=passed、case_count=1、failure_type=none，
    生成 report_id=verification-94c10417117aa6f4c1e7af17；固定 ctest argv 实际完成 34/34。
  - scripts/export_schemas.py：成功导出 45 份公共 Schema；checked-in schema 一致性包含在
    全量测试中。
  - E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider：225 passed，
    1 warning。
  - E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration\test_p014_postgres.py
    -q -p no:cacheprovider：4 passed，1 warning。
  - .\scripts\run_smoke.ps1：退出码 0；Python 225 passed、1 warning，CTest 34/34 passed，
    PostgreSQL 8 张核心表缺失 0，Qdrant collection 健康。
  - E:\Anaconda\envs\torch128\python.exe -m compileall -q agent apps services tests：退出码 0；
    git diff --check：退出码 0，仅有 Git 的 LF→CRLF 转换提示。
- 服务状态/限制：本步未启动 Fast/Smart 模型；Smart 仍硬禁用。仿真和受控 runner 使用本地
  Python；CTest 入口要求 build/cpp 已准备，入口缺失返回 unavailable，不会改跑任意命令。
  Trace 保存 digest/引用而非完整 Prompt/日志；真实 ROS/底盘和未来外部副作用 reconciler
  不在 P0-17 范围。
- 直接复用：后续工作包从 TraceEvent/VerificationReport 读取证据，不自行新增审计字段；
  新验证入口必须先注册有限 suite/case、固定 argv、失败类型和证据引用，并补充失败反例。

### 2026-08-21 · P0-18 60 例自动评测（当前有效）

- 完成：新增 `evals/p018/` 统一 `EvalHarness`、严格 Pydantic 数据/报告契约、固定
  `dataset.json`、`config.json`、复现指纹、JSON/Markdown 渲染器和 `python -m
  evals.p018.run_eval` CLI；Windows 一键入口为 `scripts/run_p018_eval.ps1`。
- 固定数据集：ID `amr-p018-60`、版本 `p0-18.v1`、`purpose=evaluation_only`、
  `is_training_data=false`，严格配额为正常订单/充电 25、RAG/权限/审批 10、异常/局部
  重规划 10、CTest/pytest/仿真验证 5、Prompt Injection/越权/审批绕过 10。每例有唯一
  `case_id`、seed、预期终态/原因码、固定地图/订单/AMR 引用、Prompt/ToolSpec 版本和 oracle。
- 固定输入与执行模式：环境 `warehouse_v1@seed-v1`；地图、AMR、订单使用
  `warehouse_v1.json`/`amrs_v1.json`/`orders_seed_v1.json`；Fast 记录为
  `qwen3.6-fast`、Qwen3.6、GGUF、context 8192、temperature 0、reasoning off；Smart
  `qwen3.8-smart` 仍 `enabled=false`。配置固定 `offline_deterministic_oracle`，不启动在线
  模型，报告必须记录 `model_call_count=0` 和输入/Prompt/配置/工具/Git 指纹。P0-07 原有
  20 例 PostgreSQL/Qdrant RAG 评测仍独立保留，不与离线 fixture 混称。
- 执行边界：正常/充电路径做四邻域路径、顶点/交换边、禁行区/禁行边和电量安全审计；
  RAG 使用固定 evidence fixture 并走实际 ACL 判定；异常场景复用 P0-15 FaultClassifier/
  FaultRecoveryController；安全场景复用 P0-16 Principal/RBAC/PEVR/HITL/ToolRegistry；
  5 例验证调用 P0-17 `FixedVerificationRunner` 的固定 CTest、pytest、仿真及本地 Trace/
  报告完整性探针。数据集不含命令、脚本或可执行选择器。
- 公共接口：新增 `EvalCategory`、`EvalOutcome`、`EvalCase`、`EvalDataset`、
  `EvalReportCase`、`EvalAggregateMetrics`、`ZeroToleranceMetrics`、`EvalReport`、
  `EvalHarness`、`run_harness`；新增 `docs/schemas/EvalCase.schema.json`、
  `EvalDataset.schema.json`、`EvalReport.schema.json`。无数据库字段、Alembic revision、
  ToolName 或既有运行时公共字段变化。
- 指标与门槛：报告汇总 Agent/RAG/AMR/security/recovery/verification 六域，保留 60 个
  逐例结果、完整 Trace、证据和失败原因；`observed_negative_cases` 不删除正确拒绝。
  七项零容忍 `vertex_collision_count`、`edge_collision_count`、
  `forbidden_zone_entry_count`、`low_battery_violation_count`、`role_leak_count`、
  `duplicate_side_effect_count`、`approval_bypass_count` 必须全为 0。
- 已实际运行：
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p018_eval.py -q -p no:cacheprovider`：5 passed，1 条既有 `jieba/pkg_resources` 弃用 warning。
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p004_contracts.py::test_export_schemas_writes_exact_utf8_json tests\unit\test_p004_contracts.py::test_checked_in_schemas_are_current tests\unit\test_p018_eval.py -q -p no:cacheprovider`：7 passed，1 条既有 warning。
  - `E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py`：成功生成 P0-18 三份 Schema。
- `E:\Anaconda\envs\torch128\python.exe -m evals.p018.run_eval --output-dir tmp\p018_eval_final`（由 `scripts/run_p018_eval.ps1 -OutputDir tmp\p018_eval_final` 调用）：退出码 0，60/60 符合预期，负向观察 16 例，顶层 failures 为空，当前源报告 digest 为 `6e52da4252d83a147d48ce27db4932ab5288d72045db27c0a287b416a56fa3d8`，七项零容忍全 0；JSON/Markdown 已生成。
  - `E:\Anaconda\envs\torch128\python.exe -m compileall -q agent apps services evals tests`：退出码 0。
  - `git diff --check`：退出码 0；仅有 Git 的 LF→CRLF 转换提示，无空白错误。
  - `.\scripts\run_smoke.ps1`：退出码 0；Python 全量 `231 passed, 1 warning`（既有 `jieba/pkg_resources` 弃用警告），CTest `34/34 passed`，PostgreSQL 8 张核心表缺失 0，Qdrant `amr_warehouse_knowledge` 健康。
- 最终 P0-18 聚合事实：类别计数严格为 `normal_order_charging=25`、`rag_permission_approval=10`、
  `exception_local_replan=10`、`verification=5`、`prompt_injection_security=10`；六域通过率均为
  `1.0`。Agent `expected_outcome_accuracy=1.0`、`trace_completeness_rate=1.0`、`model_call_count=0`；
  RAG `Recall@K=1.0`、`MRR=1.0`、citation/answerability=1.0、ACL leak=0；AMR 正常/充电完成率
  `1.0`，顶点/边/禁行区/低电量均 0；安全注入阻断率和越权工具阻断率 `1.0`，handler blocked=0；
  恢复预期终止率/局部重规划成功率 `1.0`，最大 replan/retry 为 2；验证 5/5、通过率和失败定位率
  `1.0`。正确拒绝/阻塞的 16 例仍在报告中保留。
- 服务状态/限制：本步未启动 Fast/Smart 模型，Smart 仍硬禁用；最终 smoke 实测 PostgreSQL/Qdrant
  健康，8000/8080 未作为 P0-18 前置服务启动。P0-18 默认是离线确定性 oracle，不声称在线
  LLM 质量；报告输出 `tmp/p018_eval_final/` 是自动生成物，不登记为源码交付。
- 已知限制：默认结果是可复现离线 oracle，不是在线模型生成质量验收；没有启动 Fast/Smart
  模型，Smart 仍硬禁用。正常路径是固定地图 fixture 的安全审计，RAG 是固定 evidence
  fixture + 实际 ACL 边界，不替代 P0-07 的实时索引/检索评测。真实 CTest/pytest/仿真
  只通过 P0-17 固定 runner；缺少 build/或入口时应报告 unavailable，不得切换为任意命令。
- 直接复用：后续评测扩展先在 `EvalCategory`/配额契约、dataset/config 指纹和失败反例中
  冻结口径；不要把负向安全例从结果中删除，也不要把离线 oracle 标为在线模型通过。报告
  写入 `tmp/`，该目录是自动生成物；源码职责以 `docs/FILE_PURPOSES.md` 为唯一登记。

### 2026-08-21 · P0-18 Fast 模型真实调用补充验证（当前有效）

- 本次只启动 `E:\Llama.cpp\start-qwen3.6-agent.cmd`，服务实际 alias 为
  `qwen3.6-fast`，命令行上下文为 `--ctx-size 8192`；Smart 脚本没有启动。模型测试结束后
  已停止精确的 `cmd.exe`/`llama-server.exe` 进程，8080 当前无监听。
- 已通过真实模型的网关预检：
  `E:\Anaconda\envs\torch128\python.exe scripts\check_model_gateway.py --profile fast`，
  status=ok，configured/served alias 都是 `qwen3.6-fast`。
- 已通过真实模型结构化冒烟：
  `E:\Anaconda\envs\torch128\python.exe scripts\smoke_llm_structured.py`，20/20 PASS，
  每例 attempts=1。
- 已通过真实模型 P0-05 五节点：
  `E:\Anaconda\envs\torch128\python.exe scripts\smoke_p005_prompts.py --profile fast`，
  `understand_goal`、`plan_tasks`、`verify_observation`、`replan`、`compose_report` 共 5/5
  PASS，均 attempts=1、repaired=false。
- P0-13 真实在线闭环未通过：
  `E:\Anaconda\envs\torch128\python.exe scripts\run_p013_e2e.py --approve-dispatch --output tmp\p013_e2e_model_test.json`
  在 `plan_tasks` 的第一次修复请求返回 HTTP 400，模型实际报告
  `request (9086 tokens) exceeds the available context size (8192 tokens)`；进程退出码 1，
  `tmp/p013_e2e_model_test.json` 未生成。该结果必须记录为真实失败，不能沿用历史 P0-13
  成功记录，也不能把 P0-18 离线 oracle 结果当作在线闭环通过。
- 当前判断：模型服务、alias、单节点结构化输出和五个 P0-05 节点本身可用；阻断点是
  P0-13 `plan_tasks` 的输入上下文超过 Fast 固定窗口。未在本步擅自修改模型上下文、Prompt
  压缩策略或把 `--ctx-size` 调大；若要修复，应先评估减少计划节点上下文/证据摘要，保持
  `context_window=8192` 契约，再重跑该在线闭环。
- 本次无新增公共接口、Schema、数据库字段、Alembic revision 或源码修改；只新增真实
  运行事实和限制记录。模型启动日志/评测输出属于 `tmp/` 或外部进程，不登记为源码交付。

### 2026-08-21 · Fast 模型 16K 上下文复测（当前有效）

- 用户已将外部 `E:\Llama.cpp\start-qwen3.6-agent.cmd` 的服务上下文调整为
  `--ctx-size 16384`。本次实际启动的 `llama-server.exe` 命令行确认了 16384，served alias
  仍为 `qwen3.6-fast`；Smart 未启动，测试结束后 Fast 精确进程已停止，8080 无监听。
- 回归结果：
  - `E:\Anaconda\envs\torch128\python.exe scripts\check_model_gateway.py --profile fast`：status=ok。
  - `E:\Anaconda\envs\torch128\python.exe scripts\smoke_llm_structured.py`：20/20 PASS，每例 attempts=1。
  - `E:\Anaconda\envs\torch128\python.exe scripts\smoke_p005_prompts.py --profile fast`：P0-05 五节点 5/5 PASS，均 attempts=1、repaired=false。
- P0-13 真实在线闭环重测：
  `E:\Anaconda\envs\torch128\python.exe scripts\run_p013_e2e.py --approve-dispatch --output tmp\p013_e2e_model_test_16k.json`
  退出码 0，报告 `final_status=completed`，run_id=`p013-e2e-b22ad4723f9e51bb9034`；8/8
  阶段、4 次模型调用、5/5 工具成功、Validator error=0、5 条 RAG 结果、仿真 completed、
  `ORDER-001` 完成、Trace 18 条、simulation_end_time=120。
- 结论：16K 解决了先前 `9086>8192` 的 `plan_tasks` 上下文超限，真实 Fast P0-13 全链路
  已通过。`evals/p018/config.json` 的离线 oracle 仍按其固定 `context_window=8192` 记录；
  本次是外部 Fast 服务的在线补充复测，两者不可混写成同一配置结果。

- 为排除前一次固定 `run_id` 恢复了 8K 失败 Checkpoint 的影响，又以全新
  `--run-id p013-e2e-fast-16k-fresh-20260821` 从头运行：
  `E:\Anaconda\envs\torch128\python.exe scripts\run_p013_e2e.py --run-id p013-e2e-fast-16k-fresh-20260821 --approve-dispatch --output tmp\p013_e2e_model_test_16k_fresh.json`
  退出码 0，`final_status=completed`，8/8 阶段、4 次模型调用、5/5 工具成功、Validator
  error=0、5 条 RAG 结果、仿真 completed、`ORDER-001` 完成、Trace 17 条；模型服务随后
  已停止，8080 无监听。该 fresh run 才是 16K 全链路通过的主要证据。

### 2026-08-21 · P0-19 策略对照实验（当前有效）

- 已完成 `evals/p019/` 的固定源报告回放、严格契约、三策略汇总和原始轨迹输出。执行模式为
  `offline_trace_replay`：固定 Workflow、ReAct、PEVR 都消费同一份 P0-18 `amr-p018-60`
  的 60 个 case、源 Trace、Prompt/ToolSpec/配置和 `qwen3.6-fast` 身份；ReAct 只生成评测层
  `think -> act -> observe` 投影，`react_production_path_touched=false`，没有修改生产主链或工具能力。
- 当前源文件是 `tmp/p018_eval_final/p018_eval.json`，P0-18 报告为
  `p018-6e52da4252d83a14`，报告 digest 为
  `6e52da4252d83a147d48ce27db4932ab5288d72045db27c0a287b416a56fa3d8`，原始文件 SHA-256 为
  `0df9ea4ab21a3df5a912b6c77221623cb5dbfc1948532cbb23501312541126f7`。P0-19 报告为
  `p019-baf5fb7ee1177042`，digest 为
  `baf5fb7ee117704238f4ecc56a952aab127fad78e63d4de62afbb5b78f67849c`，产物为
  `tmp/p019_strategy_compare/p019_strategy_comparison.json`、同名 `.md` 和
  `p019_raw_trajectories.jsonl`。
- 公平性证据固定为数据集 `amr-p018-60/p0-18.v1`、60 例唯一 case digest
  `4c810340d8a9757a9bcf1343adbb357f479d755c34869491f536979f46194ecc`、P0-18 config SHA-256
  `93f3602f4c7c2bff944b7f7166f678ca1d56232e4977798719d3de272e6dfc61`、P0-19 config SHA-256
  `9d23d3af71718399af48e7f551f2285249c1dcd1b719336cde1897e6e7c32b31`；dataset/tools/prompts/config/model 五项门禁均为 true。
- 三策略均为 60/60 预期符合、44/44 正向任务完成、33/33 计划合法、10/10 异常终止正确、
  8/8 成功重规划、工具/验证错误 15 且意外错误 0。固定 Workflow 与 PEVR 为 349 源事件，
  步数均值/P50/P95 为 5.816667/6/14；ReAct 派生步数为 729，均值/P50/P95 为 12.15/13/28。
  三者 Trace 延迟 P50/P95/最大均为 30/70/70 ms；这是源 Trace 时间而非墙钟。
- P0-18 源 Trace 没有模型调用、Token usage 或 CPU/RSS/GPU 采样；P0-19 以
  `observed=false` 记录 Token 和资源，不把缺失值当作 0。该结果因此是可复核的同源离线对照，
  不是三次在线 Fast 模型质量/资源实验；在线三策略需另立适配器和报告模式。
- Smart 的 15 例对照状态固定为 `deferred`、`started=false`、`completed=false`：因当前速度问题
  本步没有启动、没有测试，也没有阻塞 P0；原双模型对照已登记 Backlog `P0-19-SMART-COMPARISON`，
  必须称为延期而非完成。
- 本步新增/变化的公共契约为 `P019ExecutionMode`、`P019Strategy`、`ResourceObservation`、
  `LatencySummary`、`TokenSummary`、`StrategyCaseResult`、`StrategySummary`、`SmartDeferral`、
  `FairnessEvidence`、`P019Report`，版本为 `p0-19.v1`；没有新增数据库字段、迁移、ToolName 或
  生产 PEVR 接口。对应 Schema 为 `P019Report.schema.json`、`P019StrategyCase.schema.json`、
  `P019StrategySummary.schema.json`。
- 已实际运行：
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p019_compare.py -q -p no:cacheprovider`：4 passed，1 条既有 warning；
  - `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p018_eval.py -q -p no:cacheprovider`：5 passed，1 条既有 warning；
  - P0-04 Schema 回归两项：2 passed，1 条既有 warning；`scripts\export_schemas.py` 成功；P0-19 `compileall` 成功；
  - `scripts\run_p018_eval.ps1 -OutputDir tmp\p018_eval_final`：退出码 0，60/60；
  - `E:\Anaconda\envs\torch128\python.exe -m evals.p019.run_compare --source-report tmp\p018_eval_final\p018_eval.json --output-dir tmp\p019_strategy_compare`：退出码 0，180 条逐例结果；
  - `git diff --check`：退出码 0；`.\scripts\run_smoke.ps1`：Python 235 passed、1 条既有 warning，CTest 34/34 passed，PostgreSQL/Qdrant 健康。
- 服务状态与限制：P0-19 未启动 Fast 或 Smart；最终端口检查仅有 PostgreSQL 5432、Qdrant 6333，
  8000/8080 无监听。Smart 绝不能因本步报告生成而被误认为已测；在线 Fast 16K P0-13 fresh run
  是既有独立证据，不得与本次 P0-18 离线 8192 配置混写。
- 下一工作包直接复用：保留本报告和源 artifact，不覆盖其 `report_digest`/文件 SHA；若新增在线 Fast
  对照，必须保持同一 60 例、工具、Prompt、配置并补齐真实模型调用、Token、墙钟 P95、CPU/RSS/GPU
  采样和失败路径；Smart 双模型 15 例仍等待 Backlog 决策。

### 2026-08-21 · P0-20 部署、文档与演示收口（当前有效）

#### 完成内容与公共边界

- 根目录 `compose.yaml` 现在编排 `postgres`、`qdrant`、`api` 三个服务；PostgreSQL/Qdrant 使用
  命名卷，两个依赖先通过 healthcheck，API 再执行幂等 Alembic upgrade 并启动 Uvicorn。
- `api` 镜像由 `infra/Dockerfile.api` 构建，使用 `infra/requirements.api.lock` 和
  `psycopg[binary]`；镜像不复制 GGUF、Embedding 或权重，也没有模型服务容器。完整 P0 Python
  环境仍由根目录 `requirements.lock` 管理。
- Compose API 环境变量固定 `LLM_PROFILE=fast`、`LLM_MODEL=qwen3.6-fast`、
  `OPENAI_BASE_URL=http://host.docker.internal:8080/v1`；`MODEL_GATEWAY_VALIDATE_ON_STARTUP`
  默认 false，只解耦容器健康与 Windows 宿主 Fast 生命周期。`/health` 是轻量进程检查，
  `/health/model` 才主动访问网关；本次 Fast 在场时后者实测通过，Fast 停止并重启 API 后实测
  HTTP 503 `MODEL_CONNECTION_FAILED`。
- `scripts/start_local.ps1` 是 Windows 最简入口：默认启动 Compose API/PostgreSQL/Qdrant；
  `-StartFast` 只调用现有 `E:\Llama.cpp\start-qwen3.6-agent.cmd` 并运行 Fast alias 门禁，
  绝不启动 Smart。新增 `tests/unit/test_p020_deployment.py` 只做静态部署契约检查。
- 新增/更新交付文档：`docs/ARCHITECTURE.md`、`docs/API.md`、`docs/SERVICES_STARTUP.md`、
  `docs/TEST_REPORT.md`、`docs/DEMO_SCRIPT.md`、`docs/RESUME_FACTS.md`、`README.md` 和
  `infra/README.md`。本步没有新增 Pydantic Schema、ToolName、数据库字段、迁移、HTTP 自然语言
  规划路由或 P1/P2 功能；API `/evals/runs` 仍只登记请求，不执行 Eval。

#### 实际验证命令与结果

- `docker compose -f .\compose.yaml config`：通过。
- `docker compose -f .\compose.yaml build api`：最终通过；首次全量依赖构建未作为交付构建，因
  会引入不必要的 PyTorch/CUDA；改用最小 API 锁后构建通过。
- `docker compose -f .\compose.yaml up -d --build`：通过；三服务最终 healthy，API 迁移日志成功。
- `.\scripts\start_local.ps1`：通过；Qdrant `/readyz`、API `/health`、PostgreSQL `(1,)`、
  Qdrant client collection 检查通过。
- `.\scripts\start_local.ps1 -StartFast`：通过；Fast `/health`、`qwen3.6-fast` alias 网关门禁通过。
  `scripts/smoke_llm_structured.py` 为 20/20，`scripts/smoke_p005_prompts.py --profile fast`
  为 5/5。
- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p020_deployment.py -q -p no:cacheprovider`：3 passed。
- `.\scripts\run_smoke.ps1`：依赖/迁移/数据库/Qdrant/工具链通过；pytest 238 passed、1 个既有
  `jieba/pkg_resources` DeprecationWarning；CTest 34/34 passed。
- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration -q -p no:cacheprovider`：18 passed、
  1 个同源弃用警告。
- `.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p018_eval_p020_final`：退出码 0，60/60；
  report_id=`p018-415f0b3f59574772`，digest=`415f0b3f59574772ad71df36591628f756a79dd26224d356d5f7fb1afb0f4585`，
  model_call_count=0，observed_negative_count=16，zero tolerance 七项均为 0。
- `.\scripts\run_p019_compare.ps1 -SourceReport .\tmp\p018_eval_p020_final\p018_eval.json -OutputDir .\tmp\p019_strategy_compare_p020_final`：退出码 0，180 条；
  report_id=`p019-26947b55d9054d8d`，digest=`26947b55d9054d8de3f5e204b203b24fb54c3e4b57b519df3c265afd0eace8e6`，
  fixed Workflow/ReAct/PEVR 均 60/60，Smart deferred。

#### 连续闭环证据与失败记录

- 正式连续成功的真实 Fast P0-13 证据为 PostgreSQL 中三个独立、非恢复复用的 run：
  `p014-fast-online-1-20260821-1037`、`p014-fast-online-2-20260821-1038`、
  `p014-fast-online-3-20260821-1039`。三次均 `completed`、8/8 阶段、4 次模型调用、5/5 工具、
  Validator error=0、仿真 completed、`ORDER-001` 完成；本次 P0-20 不修改 P0-13 工具/DAG/恢复逻辑，
  这组三次作为正式连续验收证据保留在 `docs/TEST_REPORT.md` 和 `docs/DEMO_SCRIPT.md`。
- 为验证新部署后 fresh run，本次追加的 `p020-demo-1-20260821` 和
  `p020-demo-success-1-20260821` 在 `plan_tasks` 长上下文请求达到 P0-13 固定 300 秒累计预算，
  退出码非 0；`p020-demo-success-2-20260821` 在同一诊断期间被中止，均未计入成功。模型 alias、
  短结构化与 P0-05 节点仍通过；不能把这些失败 fresh 尝试写成闭环通过，也不临时放宽预算。
- Fast 实际外部命令行含 `--ctx-size 16384`；P0-18 离线 config 仍是 `context_window=8192`，两者
  不可混写。Smart 从未启动，`P0-19-SMART-COMPARISON` 仍 deferred。

#### 当前服务状态、限制与下一步

- 当前 Compose 栈保持运行：API `127.0.0.1:8000`、PostgreSQL `localhost:5432`、Qdrant
  `localhost:6333`/dashboard；Fast 8080 已释放，Smart 未运行。停止时执行
  `docker compose stop api postgres qdrant`，不要 `down -v`。
- 本地 Fast 长上下文吞吐会随 GPU/CPU/MoE 负载变化；固定预算耗尽时 fail closed。若要重新获得
  fresh 在线 P0-13 三连，必须先记录新的机器状态和外部脚本命令行、使用新 run_id，再独立复测，
  不能复用旧 Checkpoint 或用离线 Eval 代替。
- 下一步不自动扩大 Scope；若用户后续要求在线三策略或 Smart 对照，需先获得明确范围/启用指示，
  新建独立在线执行模式、采样器和验收报告，并继续保持当前安全边界。

### 2026-08-21 · P0-00～P0-20 发布前全仓审查（当前有效）

#### 本步完成与未完成

- 本步只做发布前审查、真实验证和 Todo 固化，没有修改 Python/C++ 业务代码、测试、配置、
  Schema、数据库迁移或既有报告结论。审查正文和逐项证据见 `docs/P0_AUDIT_TODO.md`。
- 审查结论为 **P0 Release Verdict: FAIL**。共登记 4 个 P0-Critical、7 个 P1-High、
  3 个 P2-Medium；没有为了凑数量列无证据的 P3 项。
- 已确认可工作的部分包括：全量 pytest/CTest、公共 Schema 同源导出、真实 RAG 20 例基线、
  Hungarian/Validator、Python 仿真安全停机、C++/Python 37 个路径时间戳一致、在线 Fast 正常
  8 阶段闭环和 Trace/报告独立重算。
- 未完成的是修复和发布复验。按用户要求，本轮没有顺手修改任何缺陷，也没有把审查发现写成
  已修复；历史 P0-20“已完成”记录仅作为实现历史保留。

#### 阻断事实与设计边界

- `scripts/run_p013_e2e.py --approve-dispatch` 仍以 `principal=null`、`approval_grant=null`、
  legacy `approval_granted=true` 放行 dispatch。三个历史正式 run 和本轮 audit run 均 Effect=1、
  Approval=0，因此 P0-16/HITL 发布验收未通过。
- `dispatch_simulation` 的外部 ID 只由相同计划/seed 摘要生成。当前 PostgreSQL 有 6 个 run 共享
  `simulation-b7551b825b817593d1e700fe`；`PostgresRuntimeStore.get` 因多条 Effect 抛
  `PersistenceConflictError`，同一完成 run 的真实重跑也无法恢复。
- `FaultRecoveryController` 在非测试代码中的唯一调用位于 P0-18 离线 runner；生产 PEVR 图是固定
  八阶段线性边，timeout/infeasible/blocked 直接抛 `PEVRExecutionError`。三个 P0-20 超时 run 的
  Trace 已 failed，但数据库状态仍为 `planning`。
- Compose 默认 JWT secret/数据库密码已公开，API/PostgreSQL/Qdrant 端口绑定全部 IPv4/IPv6
  接口。用默认 secret 伪造 operator JWT 请求真实 run 返回 HTTP 200；匿名 Qdrant scroll 可读取
  70 个 chunk，其中 25 个是 operator-only。
- P0-18 runner 不消费 `case.oracle`，修改 oracle 为必须失败/重复 999 后仍通过；注入样例没有消费
  `input_data.text` 也被记为 blocked。P0-19 仅投影同一源 Trace，不运行三个独立策略。旧 60/60、
  恢复/安全 1.0 和三策略 60/60 不能继续作为发布验收指标。
- 真实 RAG 20 例本轮为 Recall@K=1、MRR=0.970588、citation=1、answerability=1、ACL=0；强制两个
  阈值为 1 后 citation=0、answerability=0.15，CLI 仍退出 0。简历中的“RAG 20 例 MRR=1.0”与
  真实独立评测不一致。
- Fast 外部脚本实际使用 16K、temperature 0.1、IQ4_NL；P0-18 config 仍记录 8K、temperature 0、
  泛化 GGUF。实际 GGUF SHA-256 为
  `B228C988C624DFFE0B57235A395FA79562D4362FED545820F9B7D78908F337E6`，当前运行版本契约未保存它。
- A* 在 release_time 前只允许 wait。本轮空地图可行样例（t=0..5 到 pickup、t=6 到 dropoff）被
  返回 `infeasible/no_safe_path_to_pickup`；Planner 与 Validator 的 release 语义不一致。
- Smart 按用户明确要求继续禁用，不启动、不测试、不作为 P0 bug，也不纳入最小修复集合。

#### 本轮实际验证摘要

- `.\scripts\run_smoke.ps1`：退出码 0，pytest 238 passed、1 warning，CTest 34/34 passed。
- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration -q -p no:cacheprovider`：
  18 passed、1 warning；P0-13 失败路径 + P0-15 组件专项为 10 passed。
- `.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p0_audit_p018`：退出码 0、60/60，但因 oracle
  假通过证据不计发布通过。
- `.\scripts\run_p019_compare.ps1 ... -OutputDir .\tmp\p0_audit_p019`：退出码 0、180 条，仅
  `offline_trace_replay`，不计真实策略对照。
- 真实 Fast audit run `p0-audit-online-normal-20260821-1659`：退出码 0、8/8 stage、5/5 tool、
  4 次模型调用、Validator 0、订单/仿真完成；同 run 第二次执行退出码 1，恢复核对失败。
- `.\scripts\start_local.ps1 -TimeoutSeconds 30`、`docker compose config --quiet`：通过；
  Compose 三服务保持 healthy。宿主和 API 容器访问 Fast 均通过，随后 Fast 已停止，8080 无监听。
- 完整命令、失败命令、异常轨迹和未运行原因均记录在 `docs/P0_AUDIT_TODO.md` 第 8～9 节；
  不得只引用本摘要推断未执行项目通过。

#### 环境、生成物与下一步

- 当前 Compose API/PostgreSQL/Qdrant 仍保持运行且 healthy；实际发布端口映射是 `0.0.0.0`/`[::]`
  的 8000、5432、6333/6334，而不仅是文档中的 localhost。Fast 和 Smart 均未运行，8080 已释放。
- `tmp/p0_audit_*`、`tmp/p0_audit_schemas` 是审查生成物，不是源码/公共契约；可在证据归档后删除。
- 工作树在审查开始前已有 P0-20 未提交修改/新文件。本步只新增
  `docs/P0_AUDIT_TODO.md`，并按仓库规则更新本文件、`docs/FILE_PURPOSES.md` 和
  `docs/LESSONS_LEARNED.md`，没有覆盖或回退用户既有修改。
- 下一任务必须先读 `docs/P0_AUDIT_TODO.md`，按 Critical 顺序修复并采用“实现→失败路径测试→
  回归→真实在线复验→文档事实校正”闭环；未经复验不得把顶部状态改回完成。

### 2026-08-21 · 按 P0_AUDIT_TODO 继续修复（前一 Agent 崩溃后续作）

#### 本步完成与未完成

- 完成（代码已落地；发布复验未全部关闭）：
  - C01：`run_p013_e2e.py` 删除 `--approve-dispatch`，强制 JWT + Postgres HITL + `security_required=True`。
  - C02：`make_external_execution_id(idempotency_key, input_digest)`；`PostgresRuntimeStore.put` 用该 lookup_id；遗留 collision 用无 lookup_id 的 JSONB 种子验证迁移。
  - C03：生产图 `_invoke_graph_with_recovery`；**VALIDATE 对 v2+ 走 `validate_replanned_pevr_plan`**，七类故障 InMemory 生产轨迹可跑到 `replan, replan, human` / `retry, retry, replan, replan, human`。
  - C04：Compose 必填 secrets；`load_settings()` 读取 `.env`，集成测试不再用 `AppSettings()` 默认 `123456`。
  - H01–H04、H07、M01–M03：保持前序实现。
  - H05：A* 允许 release_time 前移动；CTest 4 个 release_time 用例通过。
- 未完成：
  - H06 演示视频仍为 0 个媒体文件。
  - 未启动 Fast，因此没有真实 HITL 三连在线 run、没有 RAG holdout 在线 CLI、没有整段 `run_smoke.ps1`。
  - 七类异常的“真实 C++/仿真器”注入仍只在 FakeRegistry 生产图测试中证明，不能写成现场设备/在线模型 E2E。
  - 不得把 Release Verdict 改成 PASS。

#### 公共接口变化

- `PEVRGraphRunner._validate_node`：`plan_version>1` 或 `completed_task_ids` 非空时调用 `validate_replanned_pevr_plan`。
- `FaultClassifier`：`plan_validation_failed` → `PLAN_INFEASIBLE`。
- `load_settings(..., load_dotenv_file=None)`：默认在 `environ is None` 时读取项目 `.env` 白名单键；测试传入 `environ={}` 时不读磁盘。

#### 设计决策

- 重规划后必须复用 LocalReplanner 的门禁（允许 completed 锚点带证据、替换任务 pending 且 version≠1），不能把首轮 `expected_plan_version=1` 套到 v2。
- `.env` 只作为 `load_settings` 的一层，进程环境变量仍可覆盖；`AppSettings()` 仍是纯 Python 默认值，集成测试必须走 `load_settings()`。
- P0-18 仍保持离线 oracle，与“真实 PEVR 在线 60 例”分开命名。

#### 验证命令

- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration\test_p015_fault_recovery.py tests\unit\test_p015_faults.py tests\unit\test_p013_pevr.py -q -p no:cacheprovider` → **49 passed**。
- `E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider` → **272 passed, 2 warnings**（含 PostgreSQL/Qdrant 集成、进程强杀恢复、七类生产图故障）。
- VsDevCmd 后 `cmake --build build\cpp`；`ctest --test-dir build\cpp -R release_time --output-on-failure` → **4/4**；全量 CTest → **38/38**。
- `python scripts\check_postgres.py` → `status=ok`；`python scripts\check_qdrant.py` → `status=ok`，collection `amr_warehouse_knowledge`。
- 未执行：整段 `.\scripts\run_smoke.ps1`、Fast 在线 HITL CLI、RAG `--minimum-hybrid-score 1` 在线评测。不得推断为通过。

#### 下一工作包直接复用

- 在线复验必须使用新 HITL CLI，禁止 `--approve-dispatch`。
- 独立 P0-19 报告 status=passed 只表示 PEVR 离线 60/60 且公平性通过，不表示 Workflow 也是 60/60。
- RAG CLI 把 `--minimum-hybrid-score 1 --minimum-vector-score 1` 必须非零。
- 生产 VALIDATE 已按计划版本分流；不要再改回只调用 `validate_normal_pevr_plan`。

### 2026-08-21 · 收口发布：启动 Fast/数据库并跑完整验收

#### 本步完成与未完成

- 完成（均有本步实际命令）：
  - Fast 安全启动：UTF-8 BOM + `pwsh`，避免 Windows PowerShell 5.1 把中文脚本解析坏；健康检查超时改为 600s。
  - `.\scripts\run_smoke.ps1`：pytest **272 passed**、CTest **38/38**。
  - HITL 三连：`p020-release-hitl-{1,2,3}-20260821-2028` 均先退出码 3（`waiting_approval`），再 `--approve-and-resume` 退出码 0、`completed`、8/8 阶段、5/5 工具、Validator 0、`ORDER-001`。PostgreSQL 每 run `approval_count=1`、`effect_count=1`。`--approve-dispatch` 被 argparse 拒绝（退出码 2）。
  - RAG holdout：Recall@K=1、MRR=1、citation=1（58/58）、answerability=1、ACL=0；坏阈值命令退出码 **2**。
  - P0-18：60/60，`report_id=p018-85eaad378d39c29d`。P0-19 independent：Workflow 52/60、ReAct 53/60、PEVR 60/60，`report_id=p019-cf6986ed9cc65f8e`。
  - Schema 51 份与导出 **0 diffs**。匿名 Qdrant 401、无/坏 JWT GET run 401、Fast 无 key 401。
- 未完成：
  - **H06 演示视频仍为 0**。本环境不能代替操作者录屏，故停在这里请艺诚补。
  - 未在本步 HITL dispatch 窗口做真实 OS 强杀；跨进程恢复仍以 pytest P0-14 为准。
  - 七类异常未再注入真实 C++/仿真器主链，仍以 FakeRegistry 生产图测试为准。

#### 公共接口变化

- 无新 Pydantic/DB 字段。`scripts/start_local.ps1 -StartFast` 改为优先 `pwsh.exe`，Fast 健康等待 600s；`config/fast_model_manifest.json` 的 launcher size/sha256 随 UTF-8 BOM 更新。

#### 设计决策

- 发布 Fast 入口继续是 `scripts/start_fast_secure.ps1`（18080 后端 + 8080 代理），不是开放 CORS 的旧 `start-qwen3.6-agent.cmd`。
- HITL HTTP 单测必须用墙钟 `datetime.now(timezone.utc)` 构造请求；冻结的 `2026-08-21 12:00Z` 会在 15 分钟 TTL 后对 `store.approve()` 过期。
- 收口复验通过不等于 Release PASS：H06 仍是 P0-20 明确交付物。

#### 验证命令

- `.\scripts\start_local.ps1 -StartFast`：首次因 `powershell.exe` 解析无 BOM UTF-8 失败（300s 空等）；修复后 `pwsh` 启动，`/health` 200。
- `python scripts\check_model_gateway.py --profile fast`：`status=ok`，artifact_id=`qwen3.6-35b-a3b-hauhaucs-iq4_nl-b228c988`。
- `.\scripts\run_smoke.ps1`：退出码 0；中间曾因 HITL HTTP 夹具过期 **1 failed / 271 passed**，把 `now` 改为墙钟后复测 **272 passed, 2 warnings**，CTest **38/38**。
- HITL CLI 三连见上；DB 查询三 run 均为 completed / approval=1 / effect=1。
- `python -m evals.rag.run_eval --output tmp\p020_release_rag_eval.json`：退出码 0，holdout 指标全 1、ACL=0。
- 同一 CLI `--minimum-hybrid-score 1 --minimum-vector-score 1`：退出码 **2**，`citation_total=0`。
- `.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p020_release_p018`：退出码 0，60/60，零容忍全 0。
- `.\scripts\run_p019_compare.ps1 -Mode independent -OutputDir .\tmp\p020_release_p019`：退出码 0，180 条，PEVR 60、Workflow 52、ReAct 53。
- 未运行：演示视频播放核验、Fast dispatch 窗口真实强杀、带真实 C++ 的七类故障在线注入。不得推断为通过。

#### 环境与服务状态

- Fast 仍在：llama-server PID 监听 18080，Python 代理监听 8080。停止前不要再开旧 `start-qwen3.6-agent.cmd`。
- Compose 三服务保持 healthy。JWT 写在 gitignore 的 `tmp/operator.jwt`，不要提交。

#### 下一工作包直接复用

- 录视频时复用本步三个 HITL `run_id` 的 JSON（`tmp/p020_release_hitl_*_done.json`）或再开新 run_id；不要用 Approval=0 的历史 `p014-fast-online-*`。
- 若需停止 Fast：只杀本步的 8080 代理与 18080 `llama-server`，不要按进程名误杀其他模型。

### 2026-08-21 · 启动脚本跳过 Fast SHA-256

- 完成：`config/fast_model_manifest.json` 增加 `verify_sha256=false`；`start_local.ps1` / `start_fast_secure.ps1` 与 `load_and_verify_fast_artifact` 启动时不再扫描 19GB GGUF。仍检查文件存在和 `size_bytes`。启动器记录更新为 size=7289、sha256=`CDBC7D8E…ADBDCA`。
- 未完成：H06 视频仍缺。关闭哈希后，运行报告里的 model_sha256 只是 manifest 记录，不能写成启动时重新核验。
- 验证：`tests/unit/test_model_artifacts.py` 本步会实际运行。

### 2026-08-21 · 演示 UI 扩展：/demo 页面 + 真实 C++ 链路仿真可视化

**与 Scope 的差异（必须先知）**：`docs/scope.md` 把「完整前端」划在 P0 外；本步是用户明确指令要求的演示 UI 扩展，用户指令优先。本步**不是** P0 Release PASS，也**不替代** H06 正式演示视频；`docs/TEST_REPORT.md` 发布口径未因此改动。

#### 本步完成与未完成

- 完成（均有实测命令，见下）：
  - 新 API：`GET /demo`（匿名静态页）、`GET /demo/warehouse`（viewer+，返回 `warehouse_v1@seed-v1` 规范化地图：30×20、障碍、临时封路、窄通道、单向/禁行边、P1–P6、S1–S6、C1/C2、4 台 AMR 初始位姿与可选订单）、`POST /demo/simulate`（operator，服务端串行跑真实 C++ Hungarian → A* → Validator，合法后才跑 Python `AMRSimulator`，返回 map/routes/result/path_steps/summary）、`POST /demo/launcher/start` 与 `GET /demo/launcher/status`（受控一键启动，白名单仅 `scripts/start_local.ps1`，唯一参数 `start_fast` 布尔，Smart 无入口）。
  - 前端 `apps/api/static/demo.html`：Canvas 30×20（y 向上翻转、原点标注 (0,0) 左下）、全图例、AMR 编号/朝向箭头/电量、当前订单 pickup/dropoff 高亮、播放/暂停/拖动 tick/显示最终轨迹、状态栏（simulation status、完成订单、Validator error_count）、失败时展示 C++ 证据表且不画任何轨迹。轨迹只渲染后端 `path_steps`（move 折线、turn 橙色、wait 灰色格内标记），无插值。
  - 权限矩阵实测：匿名 simulate 401、viewer simulate 403（`OPERATOR_REQUIRED`）、viewer 可读地图与启动状态；未知字段 422；未知订单 404（`demo_order_not_found` + 可选清单）；坏计划 422 `fleet_plan_invalid` 带 C++ 错误证据且无轨迹。
  - 生成物 `tmp/demo_trajectory_order-001.json`（37 个 `amr.path_step` / 47 个事件，`demo-9ff095e7e610c2f1`，gitignore，不登记为源码）。
  - 顺手修复存量问题：`tests/unit/test_p020_deployment.py` 的 `WindowStyle Hidden` 断言随上一提交脚本改为 `Minimized` 而过期（HEAD 即红），已改为断言 Minimized 并禁止 Hidden 回归；`run_smoke.ps1` 及本仓库中文 `.ps1` 在 Windows PowerShell 5.1 下解析失败，统一改用 `pwsh.exe` 执行。
- 未完成/有意不做：
  - 不恢复 `--approve-dispatch`；演示链路不写 Effect Ledger、不作 HITL 发布证据。
  - 不做自然语言下单 UI（页面保留「暂不支持」按钮）、不把 FaultInjection 暴露给前端、不改 Smart 为 enabled。
  - 浏览器端 Token 仍需人工粘贴（localStorage 保存、永不回显）；未做登录接口。
  - H06 演示视频仍为 0。

#### 公共接口变化

- 新 Pydantic 契约（全部 `extra=forbid`）：`services/demo/contracts.py` 的 `DemoWarehouseMap`、`DemoSimulateRequest`、`DemoSimulateResponse`、`DemoPathStep`、`DemoRouteInfo`、`DemoSimulationOutcome`、`DemoSimulationSummary`、`DemoLauncherRequest`、`DemoLauncherStatus`；`scripts/export_schemas.py` 同源导出 5 份主 Schema 到 `docs/schemas/Demo*.schema.json`（P0-04 一致性测试覆盖）。
- 新路由全部挂在 `apps/api/main.py`（`demo_router`）；`docs/API.md` 已登记 5 个端点、权限矩阵与业务错误码（`demo_order_not_found` 404、`demo_cpp_request_rejected`/`allocation_infeasible`/`route_infeasible`/`fleet_plan_invalid` 422、`demo_cpp_output_schema_violation` 502、`cpp_executable_unavailable`/`cpp_timeout`/`demo_launcher_*` 503）。
- `apps/api/dependencies.py` 新增 `get_demo_service`/`get_demo_launcher`（惰性构造、缓存 app.state）；`pyproject.toml` package-data 增加 `apps.api = ["static/*.html"]`。
- 无 DB 字段/表变化，无 C++ 源码改动；现有 `/runs`、`/documents` 等接口零改动。

#### 设计决策

- **visualization-only**：演示服务直连 `FixedCppJsonClient` + `AMRSimulator`，刻意绕过 `ToolRegistry.dispatch_simulation`（它会写 Effect Ledger 且 `requires_approval=true`）。演示轨迹只能当可视化证据。
- **无状态确定性**：每次请求重读固定 seed 快照；`simulation_id` 由计划内容哈希派生（同订单重跑同 ID）；前端不可改环境/路径/载荷。
- **envelope 镜像生产**：发给三个 C++ exe 的请求逐字段镜像 `agent/tools/registry.py` 与 `agent/runtime/graph.py`（allocator 不带 `environment_ref`——C++ 严格 codec 会拒），改那两处必须同步检查 `services/demo/service.py`。
- **启动器白名单**：argv 完全由服务端构造，请求体只有 `start_fast` 布尔；运行中重复点击幂等返回同一进程；Shell 优先 `pwsh.exe` 回退 `powershell.exe`（5.1 解析不了无 BOM 中文脚本）；非 Windows 返回 `unavailable`。
- **修复 P0-20 过期断言而非回退脚本**：`Minimized` 是 LESSONS_LEARNED 记录的既定修复（Hidden 导致启动器空转），故改测试。

#### 验证命令（全部实测）

- `python -m pytest tests\unit\test_demo_api.py -q` → **13 passed**（仿真用例真实调用 `build/cpp` 三个 exe；缺产物时仅这些用例跳过）。
- `E:\PowerShell7\7\pwsh.exe -NoProfile -File .\scripts\run_smoke.ps1` → 退出码 0：Python **286 passed, 2 warnings**（272 基线 + 13 演示 + 1 修复的 P0-20）、CTest **38/38**、环境/迁移/Qdrant 门禁全过。注意：`powershell.exe`（5.1）会因无 BOM 中文注释解析失败，smoke 必须用 pwsh。
- `python scripts\export_demo_trajectory.py` → 生成 `tmp/demo_trajectory_order-001.json`。
- 浏览器实测（宿主机 uvicorn 8010）：匿名打开 `/demo` → 粘贴 operator JWT 加载地图 → 运行 ORDER-001 看到逐格轨迹（t=0..36，AMR-01 (1,2)→P1→S3 (27,9)，pickup/dropoff 脉冲高亮）→ 暂停/拖动 tick/显示最终轨迹正常 → ORDER-002 显示 422 + C++ 证据表（`pickup_before_release`/`pickup_time_mismatch`）且无轨迹 → 8000 容器路径（无 C++ exe）显示 503 `cpp_executable_unavailable`。
- 未执行：Fast 在线联动（演示不依赖模型）、容器内 `/demo/simulate`（容器无 Windows C++ exe，预期 503）。不得推断为通过。

#### 环境与服务状态

- 宿主机 uvicorn 仍在跑：`http://127.0.0.1:8010`（`model_validated=false`，无 Fast）；演示入口 `http://127.0.0.1:8010/demo`。
- `compose` 的 `amr-api` 容器仍占 `127.0.0.1:8000`（旧镜像，无 `/demo` 路由，对 `/demo` 返回 404）。要容器提供演示页需先 `docker compose build api`，但容器是 Linux、没有 `build/cpp` 产物，容器内 `/demo/simulate` 会如实 503——仿真演示只能用宿主机 uvicorn。
- PostgreSQL 5432 / Qdrant 6333 容器 healthy（smoke 门禁实测）。Fast/Smart 均未运行，8080 无监听。
- 演示用 JWT 在 gitignore 的 `tmp/demo_operator.jwt` / `tmp/demo_viewer.jwt`，启动器日志 `tmp/demo_launcher.log`；均不得提交。

#### 已知限制与待决策

- **Planner/Validator `release_time` 语义冲突（新发现，未裁决）**：A* 允许 release_time 前预定位，代价最优时会在 pickup 格上提前原地转身等待；Validator 把「首次到达 pickup 格」认定为 pickup 时刻 → ORDER-002（release_time=10）稳定被判 invalid（`pickup_before_release` + `pickup_time_mismatch`）。演示如实展示该拒绝。P0-18 数据集 normal-002/003 期望 completed，评测前必须裁决语义（Validator 放宽或 Planner 禁止 pickup 格提前等待）；详见 `docs/LESSONS_LEARNED.md` 同日条目。
- ORDER-003 依赖未完成的 ORDER-001，C++ 分配器直接拒绝（422 `demo_cpp_request_rejected`）——这是正确业务行为，不是缺陷。
- 前端 Token 手工粘贴；多标签页各自播放、无服务端推送；窄通道/单向边只做展示，A* 不建模窄通道减速（与生产 envelope 一致）。
- 本步偏离 `docs/scope.md` 的「完整前端 P0 外」排除项（用户指令优先）；如 P0 审计复核，需要说明这是演示扩展而非 Scope 变更。

#### 下一工作包直接复用

- 演示入口：`http://127.0.0.1:8010/demo`（宿主机）；离线导出：`python scripts\export_demo_trajectory.py --order ORDER-001`。
- 录 H06 视频可复用本页展示地图/轨迹；自然语言闭环仍走 `scripts/run_p013_e2e.py`。
- 修改 `registry.py`/`graph.py` 的 C++ envelope 时，必须同步核对 `services/demo/service.py`（两处镜像关系已写在各自 docstring）。
- 运行仓库内任何含中文注释的 `.ps1` 一律用 `pwsh.exe`；新增中文脚本要么带 BOM 要么走 pwsh。

### 2026-08-22 · 演示令牌与脚本编码修复

- 演示 JWT：给运行中服务签令牌必须用 `load_settings()`（读 `.env` 的 `AMR_JWT_SECRET`）；`AppSettings()` 只拿开发默认值，签出的令牌会被线上 401。`tmp/demo_operator.jwt` / `tmp/demo_viewer.jwt` 已按正确方式重签（12h），并对 8010 实测 200。详见 LESSONS_LEARNED 同日条目。
- 脚本编码：`scripts/` 下 6 个含中文的 `.ps1` 已统一补 UTF-8 BOM 并通过 PS 5.1 解析验证；`start_local.ps1`/`start_fast_secure.ps1` 与 HEAD 恢复一致。此后双击或 `powershell.exe` 直接调用不会再报 `MissingCatchOrFinally`。
- 服务状态：宿主机 uvicorn 8010（含演示路由）与 compose 8000 均在跑；Fast/Smart 未启动。

### 2026-08-22 · 自然语言下单闭环接入演示页（PEVR × 演示 UI）

#### 背景与完成内容

用户确认「自然语言下单是核心功能」，要求把 P0-13 完整闭环接入演示页（此前演示页只有占位按钮，系当时指令明确排除）。本步完成后演示页具备两条互不混用的链路：

1. **可视化-only**：`POST /demo/simulate`（选种子订单 → C++ 链 → 仿真轨迹；不写 Ledger、不需审批、不能当发布证据）。
2. **自然语言完整闭环**：`/demo/nl/*`（自然语言 → Fast LLM 理解 → RAG → C++ 分配/寻路/校验 → **人工审批** → 仿真 → 报告与轨迹；写 Effect Ledger、HITL 门禁、可作发布证据）。

#### 关键设计决策（下游不要再推翻）

- **受控子进程包装已实测 CLI，不在 API 进程内重建图**：`services/demo/nl_runner.py` 的 `ControlledNLRunner` 以固定 argv 拉起 `scripts/run_p013_e2e.py`（python 取 `sys.executable`；自然语言仅为 `--request` 独立 argv 元素，无 Shell；`--run-id demo-nl-<uuid12>` 由服务端生成）。这是 P0-20 实测的 HITL 三连同一路径，避免在 API 进程复制图装配带来的并发与漂移风险。
- **审批决定不在演示后端发生**：waiting 后前端用**浏览器本人 JWT** 调既有受保护 API `POST /agent/runs/{run_id}/hitl/{approval_id}/approve`（或 `/reject`）签发 grant，再调 `POST /demo/nl/resume` 由后端以 `--resume-approved` 恢复。演示后端**永不使用 `--approve-and-resume`**（该开关会由 CLI 代批，绕过本人留痕）。
- **每 run 现铸令牌**：runner 每次拉起用 `app.state.authenticator.issue_token(subject="demo-nl-runner", role=OPERATOR, ttl=3600)` 写 `tmp/demo_nl_<run_id>.jwt`；浏览器与长期令牌文件不参与。
- **单并发槽位**：本地只有一个 Fast 实例；running/waiting_approval 时新运行 409 `demo_nl_busy`；dismiss 清槽（running 先 terminate），不改写 PostgreSQL 事实。
- **状态可从产物重建**：waiting/完成事实以 CLI 落盘的 `tmp/demo_nl_<run_id>.json` 为准，meta 边车保存原始请求文本；API 重启后 status/resume 仍可用。
- **结果复用渲染器**：`GET /demo/nl/result/{run_id}` 从 PEVRRunResult.tool_results 定位 dispatch_simulation 的完整 SimulationResult，path_steps 提取与 `/demo/simulate` 同一函数，前端复用同一轨迹渲染器。

#### 新增公共接口

6 条 `/demo/nl/*`（详见 `docs/API.md` 2.2 节与 `docs/schemas/DemoNL*.schema.json`）：`POST /demo/nl/run`（operator）、`GET /demo/nl/active`（viewer+）、`GET /demo/nl/status/{run_id}`（viewer+）、`POST /demo/nl/resume`（operator）、`POST /demo/nl/dismiss`（operator）、`GET /demo/nl/result/{run_id}`（viewer+）。契约：`DemoNLRunRequest`（request 1–500 字符、纯空白 422）、`DemoNLResumeRequest`/`DemoNLDismissRequest`、`DemoNLRunStatus`、`DemoNLReportSummary`、`DemoNLResultResponse`。

#### 实测验证

- `pytest tests\unit\test_demo_nl.py`：7 passed（假进程；覆盖权限矩阵、argv 白名单证据、完整状态机、单并发 409、非 waiting 恢复 409、未完成取结果 409、未知 run 404、失败日志尾部、API 重启产物恢复、dismiss terminate）。
- `pytest tests\unit\test_demo_api.py tests\unit\test_demo_nl.py tests\unit\test_p004_contracts.py`：72 passed（含新 Schema 一致性）。
- 全量 `.\scripts\run_smoke.ps1`：293 passed（Python，含新增 7 条）+ 38/38 CTest passed，exit 0（2026-08-22）。
- **真实 Fast 在线实测通过（2026-08-22）**：
  - API 级：`POST /demo/nl/run`（"请把 MAT-001 从 P1 运到 S3，并在截止时间前完成。"）→ waiting_approval → 浏览器身份 operator JWT 调 `POST /agent/runs/{run_id}/hitl/{approval_id}/approve` 签发 grant → `POST /demo/nl/resume` → completed。结果：ORDER-001 completed、simulation completed、end_time=120、model=qwen3.6-fast、**37 个 path_step 与 `/demo/simulate` 确定性链轨迹逐步一致**（run `demo-nl-1956318a1e38`）。
  - 浏览器级（`http://127.0.0.1:8010/demo` 全 UI 驱动）：提交 → 审批卡片正确透出 approval_id/high_risk_write/过期时间 → 点「批准并继续执行」→ completed → 轨迹渲染 + 报告摘要（run `demo-nl-3b0e1c8f5f9d`）。页面刷新后 `/demo/nl/active` 恢复进行中/已完成运行也已实测。
- **重要平台发现（已于同日晚根治，见下文「固定事实字段强制覆盖」条目）**：Fast（temperature=0.1）的 plan 产出对固定字段不稳定——6 次演示运行中 4 次把 `$ref` 数据流引用语法当成字面值照抄（`fixed:order_ids`、`task:TASK-ROUTE-002/input/...` 等伪引用），确定性校验正确拒绝（environment_ref/seed/blocked_cells/max_time/ruleset 多项 mismatch），图内一次重规划反馈后仍未收敛。对照实验（同日同时刻同模型同请求文本，仅 run_id 不同）一次通过。P0-20 HITL 三连 3/3 通过属幸运样本。根治方案：plan 规范化层把已知固定字段（order_ids/environment_ref/seed/latest_deadline/blocked_cells/ruleset_version）从 LLM 输出强制覆盖为请求/快照真值，详见 LESSONS_LEARNED 同日条目。

#### 环境与服务状态

- 宿主机 uvicorn 8010 **已重启并加载 `/demo/nl/*` 路由**（实测 `/demo/nl/active` 200）；compose 8000 容器仍是旧镜像（无演示路由）。
- 自然语言闭环需要：Fast 在线（8080）+ PostgreSQL/Qdrant 容器（compose）+ 宿主机 API（8010）。CLI 子进程自己连 PostgreSQL 写 Checkpoint/HITL/Effect Ledger。
- 实测时 Fast 由 `pwsh -File .\scripts\start_local.ps1 -StartFast` 启动（编码修复后 PS 脚本正常）；本条目完成时 Fast 仍在运行。
- 生成物 `tmp/demo_nl_*.json/.meta.json/.jwt/.log` 均在 gitignore 的 `tmp/` 下。

#### 已知限制

- ~~Fast 计划产出不稳定~~（**2026-08-22 晚已根治**，见下文「固定事实字段强制覆盖」条目：规范化层把固定字段覆盖为真值后，真实 Fast 连续 6/6 到达 waiting_approval）。
- LLM 延迟不可控：run 阶段通常 60–90 秒到数分钟，前端 3s 轮询；无服务端推送。
- 审批有过期时间（HITLRequest.expires_at，实测 15 分钟）；过期后 approve 返回 409，页面如实展示，需 dismiss 后重新下单。
- `--resume-approved` 在 grant 不存在（未批准/已拒绝）时 CLI 退出码 1，状态呈 failed + 日志尾部——这是如实呈现，不是缺陷。
- 同一时刻只能跑一个自然语言运行；多用户场景不在演示范围。

### 2026-08-22 · 固定事实字段强制覆盖（Fast 计划稳定性根治）

#### 背景与完成内容

艺诚在演示页提交自然语言订单时再次命中上方「重要平台发现」的校验失败（`environment_ref_mismatch`/`simulation_seed_invalid` 等 7 项）。本步把根治方案落地：`agent/planning/validator.py` 的 `canonicalize_normal_pevr_plan` 在传入 contract+expected_seed 时，把 `environment_ref`、`order_ids`、`blocked_cells`、`ruleset_version`、`seed` **一律覆盖**为合同/请求真值并记录 `{task}.{field}:fixed_fact_override` note；`max_time` 是唯一例外（Validator 接受 ≥ 最晚 deadline，只在缺失/非整数/不足时拉回真值，保留合法更大 horizon）。

#### 关键设计决策（下游不要再推翻）

- **覆盖不放宽任何约束**：这些字段的正确值由合同/请求唯一确定，LLM 本就没有合法选择权（Validator 对任何偏差都拒绝）；覆盖只能让计划更贴近合同。Validator 门禁对覆盖后的计划逐项生效，`assignments`/`plan` 数据流引用、工具基数、DAG 拓扑**不在**豁免范围，语义修复重问路径（`_SequencePlanProvider` 相关测试）继续用数据流错误夹具验证。
- **审计可见**：每次覆盖都写 normalization note 并随 checkpoint/trace 持久化，LLM 的错误不会无声消失。
- 首轮 plan 与图内语义修复重问共用同一 canonicalize（graph.py 两处调用点），修复对两条路径同时生效。

#### 实测验证

- `pytest tests\unit\test_p013_pevr.py`：14 passed（新增 4 条：fixed:* 伪引用覆盖、自引用 task:<自身>/input/* 覆盖、合法大 max_time 保留、正确计划零 note；2 条语义修复测试夹具从 seed 改为 assignments 数据流错误）。
- PEVR 相关回归：`test_p013_pevr / test_p014_checkpoint / test_p014_replanner / test_p015_faults / test_p016_security / test_p017_validation / test_p017_trace / test_p018_eval / test_p019_compare / test_demo_api / test_demo_nl` 共 108 passed。
- 全量 `.\scripts\run_smoke.ps1`：297 passed（Python，含新增 4 条）+ 38/38 CTest passed，exit 0（2026-08-22 晚）。
- **真实 Fast 连续成功率实测（修复后，2026-08-22 晚）**：同一请求文本经 `POST /demo/nl/run` 连续 6 次运行，**6/6 一次到达 `waiting_approval`**（run `demo-nl-e16d747eae52`/`b382ee6c8ba0`/`0a8a71ccf280`/`abb08109dcdd`/`2666945596f6`/`957ddd13b6eb`；修复前同文本 2/6）。
- **修复后完整 E2E（2026-08-22 晚）**：`demo-nl-b6c97bb986ba` 提交 → waiting_approval（approval_df0c9a290f37…）→ operator JWT 调受保护 API 批准（200）→ `POST /demo/nl/resume` → completed（exit 0）。结果：ORDER-001 completed、simulation completed、end_time=120、**37 个 path_step** 与确定性链一致。该完成的运行留在演示槽位中，打开演示页即可看到轨迹与报告。
- 备注：宿主机 uvicorn（8010）未因本修复重启——canonicalize 由 CLI 子进程从磁盘加载，修复对 `/demo/nl/*` 即时生效；API 进程本身不执行计划校验。

### 2026-08-22（晚）· 自然语言任意下单轻量链 + 演示页极简重写

#### 背景与完成内容

艺诚明确要求：演示页的效果是「自然语言**任意**下单」——左边只有一个自然语言下单框和一个历史轨迹选择框，每成功规划一次加一条历史轨迹，且历史**不持久化**。此前页面上的种子订单仿真、受控启动、完整 PEVR 闭环面板都不是他要的效果。本步落地：

- **新端点 `POST /demo/order`（匿名）**：任意自然语言 → Fast `ModelProvider.generate_structured` 抽取四要素（`DemoOrderExtraction`：material_id/pickup/dropoff/deadline，未提截止默认 120 秒）→ 服务端按 warehouse_v1 地点白名单（P1–P6/S1–S6）重建动态订单（ID 由服务端生成 `NL-xxxxxxxx`，LLM 无权命名）→ 与 `/demo/simulate` **完全相同**的 C++ Hungarian → A* → Validator → Python 仿真链 → 同一 `DemoSimulateResponse`。
- **`GET /demo/warehouse` 改匿名**（用户明确选择本机演示免 Token；地图是只读视图）。
- **演示页重写**：左栏为 NL 输入框 +「服务启动」卡 + 历史轨迹选择器；历史是纯浏览器内存数组（刷新即清空）；Token 输入、种子订单仿真、PEVR 审批卡从页面撤下（后端端点全部保留，安全姿态不变）；图例与播放控制移到画布下方；订单高亮由后端 `summary.order` 真值驱动（`DemoSimulationSummary` 新增 `order: TransportOrder` 字段）。同日稍晚按用户指令把受控启动加回页面：左栏「服务启动」卡（启动本地服务 / 启动服务+Fast），`/demo/launcher/start` 与 `/demo/launcher/status` 同步改匿名，浏览器实测点击 → 脚本退出码 0、状态与日志尾部正确回显。

#### 关键设计决策（下游不要再推翻）

- **轻量链 vs PEVR 闭环的边界**：`/demo/order` 不写 Effect Ledger、不需 HITL、不持久化、不作发布证据；`/demo/nl/*` 完整闭环（审批 + Ledger + 发布证据）后端保留、仅从页面撤下。两条链互不混用；要发布证据仍走 `/demo/nl/*` 或 `scripts/run_p013_e2e.py`。
- **LLM 只抽四要素，订单真值在服务端重建**：沿用「固定事实字段强制覆盖」的教训——交给 LLM 的字段越少，幻觉失败面越小。地点白名单、订单 ID、deadline 下限都由服务端对快照校验/生成；LLM 输出永远只是线索。
- **匿名范围仅限无痕迹演示链 + 受控启动器**：`/demo/order` 与 `/demo/warehouse` 不写任何持久存储、不触发副作用（除 C++ 子进程计算）；`/demo/launcher/*` 同日稍晚按用户明确决策也放开匿名（白名单脚本 + 单布尔开关约束不变；API 若绑定非回环地址应恢复 operator 门禁）。凡写库/审批的端点（`/demo/nl/*`、`/agent/*`）仍全部走 JWT。
- **种子链路行为不变**：`_plan_routes` 的 `orders=None` 默认仍传快照全量订单表（与生产 registry envelope 逐字段一致）；只有 NL 链显式传单元素动态订单列表。

#### 实测验证

- `pytest tests\unit\test_demo_order.py`（新建 6 条）：真实 C++ 匿名 happy path（假 Provider 抽取 P3→S3 → 200，validator_valid=true、simulation completed、轨迹按 (time,amr_id) 排序、取货步坐标=P3、终点=S3）、空白/未知字段 422（且不消耗模型调用）、unknown_location 422（附合法 P/S 清单）、nl_extract_failed 422、fast_model_unavailable 503、超载坏计划被真实 Validator 拒绝 422 无轨迹。
- `pytest tests\unit\test_demo_api.py tests\unit\test_demo_nl.py`：20 passed（地图测试改匿名 200 断言；页面静态测试改断言 `/demo/order`）。
- 全量 `pytest tests\unit`：272 passed（含 Schema 一致性；`DemoNLOrderRequest.schema.json` 已导出，`DemoSimulateResponse.schema.json` 因 summary.order 重导）。
- 全量 `.\scripts\run_smoke.ps1`：303 passed（Python，含新增 6 条）+ 38/38 CTest passed，exit 0（2026-08-22 晚）。
- **启动器加回页面（2026-08-22 晚，同日追加）**：`/demo/launcher/start`、`/demo/launcher/status` 去 JWT 改匿名（`test_demo_launcher_auth_and_whitelist` 更名 `test_demo_launcher_anonymous_and_whitelist`，断言匿名 200 + 白名单 argv 不变）；演示三件套复测 26 passed；浏览器实测点「启动本地服务」→ 脚本退出码 0、状态行与日志尾部正确回显。
- **真实 Fast 在线实测（2026-08-22 晚，宿主机 API 8010 已重启加载新路由）**：
  - API 级：此前被 PEVR 闭环按设计拒绝的「请把 MAT-001 从 P3 运到 S3，并在截止时间前完成。」经 `POST /demo/order` 匿名提交 → 200：LLM 正确抽取 MAT-001/P3→S3/deadline=120，Validator valid（0 错误），仿真 completed，31 个 path_step（订单 `NL-FDE24429`）。
  - 浏览器级（`http://127.0.0.1:8010/demo` 全 UI 驱动，无 Token）：提交「MAT-002 从 P5 运到 S2，90 秒内送达。」→ 正确抽取 deadline=90、completed、历史条目 #1；再提交「把 MAT-003 从 P2 运到 S6。」→ 历史条目 #2；点击 #1 正确切回 P5→S2 轨迹与状态栏。历史刷新即清空（内存数组，用户明确要求）。

#### 环境与服务状态

- 演示页 `http://127.0.0.1:8010/demo` 开箱即用：无需粘贴 JWT；Fast 离线时下单返回 503 `fast_model_unavailable`（页面如实展示，不画假轨迹）。
- Fast 启动方式不变：`pwsh -File .\scripts\start_local.ps1 -StartFast`；页面「服务启动」卡（同日稍晚按用户指令加回，匿名）点「启动服务 + Fast」等价于该命令，「启动本地服务」等价于不带 `-StartFast`。

#### 已知限制

- 单次下单串行：LLM 抽取（数秒）+ C++ 链（<1s）+ 仿真（<1s），全程同步阻塞该请求；未做并发排队（演示范围外）。
- LLM 抽取存在温度方差：四要素极小 + Schema 约束 + 一次修复，实测未见失败；若失败返回 422 如实呈现，用户可重试。
- 历史轨迹刷新即清空是**用户明确要求**，不要改成持久化。
- `deadline` 语义：LLM 未提及默认 120 秒；用户说「60 秒内送达」会被抽成 60。过紧的 deadline 会被 C++ Validator/分配器如实拒绝（422），这是正确行为。
- 启动器日志尾部里脚本自身的中文输出有乱码（子进程控制台编码问题，不影响状态与退出码，既有外观问题，未在本步处理）。

### 2026-08-22 · 演示页任意下单走完整 PEVR 闭环（匿名审批）

#### 用户最高优先级决策（原文级摘要）

**2026-08-22 用户明确指令：演示闭环完全不考虑安全，审批匿名、页面仅闭环链。**

1. HITL 审批接口对演示链匿名开放：`POST /agent/runs/{run_id}/hitl/{approval_id}/approve` 与 `/reject`，以及 `/demo/nl/*`，页面无需 token 即可批准或拒绝。用户已知悉并接受「任何能访问 API 的进程都能放行副作用」。
2. 页面只保留完整闭环链；`POST /demo/order` 接口本身保留供既有测试与快速验证，从演示页撤下。
3. 不得以安全姿态、发布证据可信度等理由劝阻、保留或变相恢复门禁。

#### 本步完成与未完成

- 完成：
  - `DynamicOrderSnapshotProvider`：包装 seed 快照，用服务端重建的动态订单替换 `orders`（不改 seed 文件；地点仍走 DefaultWarehouseSnapshotProvider 的固定路径）。
  - understand：有 `injected_orders` 时把合同订单/环境约束强制对齐快照并清零 `missing_information`，然后仍做逐字段相等校验。
  - `POST /demo/nl/run` 先 `prepare_dynamic_order`（四要素 + 地点白名单），再写 `tmp/demo_nl_order_<run_id>.json`，CLI `--order-json` 只接受该文件名模式。
  - HITL approve/reject 与 `/demo/nl/*` 匿名；grant 的 `approved_by=demo-anonymous-approver`。
  - 演示页：唯一提交走闭环；阶段进度；HITL 卡匿名批准后 resume；完成后轨迹入内存历史。
- 未完成：本步结束时**尚未**做浏览器级真实 Fast 在线实测（见下方验证命令）；H06 视频仍为 0。

#### 公共接口变化

- CLI：`scripts/run_p013_e2e.py --order-json`（可选）。
- HTTP：HITL approve/reject 匿名；`/demo/nl/*` 匿名；`DemoNLResultResponse.order` 为服务端重建的 `TransportOrder`。
- 无新 DB revision。Effect Ledger 幂等不变。

#### 设计决策（下游不要再推翻，除非用户再次改口）

- 不放松 `order_snapshot_mismatch`：改快照内容 + 合同规范化，而不是允许 LLM 编造订单字段。
- 快照 `orders` **替换**而非追加种子订单，避免 Hungarian 同时分配 ORDER-001。
- 抽取仍只抽四要素；订单 ID 仍由服务端生成 `NL-xxxxxxxx`。
- CLI 子进程仍现铸 JWT 作为 PEVR Principal（图内 security_required）；浏览器不持有该令牌。这不是页面门禁。
- `--order-json` 路径约束是防路径穿越，不是被豁免的认证门禁。

#### 验证命令

- `python -m pytest tests\unit\test_demo_nl_closed_loop.py tests\unit\test_demo_nl.py tests\unit\test_p016_security.py -q` → **24 passed**。
- 全量 `E:\PowerShell7\7\pwsh.exe -NoProfile -File .\scripts\run_smoke.ps1` → 退出码 0：Python **312 passed, 2 warnings** + CTest **38/38**（字段补丁后再跑一次，同样 312/38）。
- **真实 Fast 在线实测（2026-08-22）**：重启宿主机 uvicorn `127.0.0.1:8010` 加载本步代码；Fast 8080 在线。匿名 `POST /demo/nl/run`「请把 MAT-001 从 P3 运到 S3，并在截止时间前完成。」→ `demo-nl-698c553e9840` → `waiting_approval` → 匿名 HITL approve（`approved_by=demo-anonymous-approver`）→ resume → `completed`。订单 `NL-9C98B2B1` P3→S3、仿真 completed、end_time=120、**31 个 path_step**、`qwen3.6-fast`、产物含 RAG `retrieve_knowledge` 引用与 C++/仿真工具证据。未用浏览器 MCP 点按钮；页面已切到闭环入口。
- Fast 与 8010 在本步结束时仍在运行。

#### 两条链边界变化

| 链 | 之前 | 现在 |
|---|---|---|
| `POST /demo/order` | 页面提交入口；无 Ledger/HITL | **接口保留**，页面撤下 |
| `/demo/nl/*` | 仅种子订单 + JWT 审批；页面已撤下 | 动态订单 + 完整 PEVR + **匿名 HITL**；**页面唯一入口** |

#### 已知后果

匿名审批意味着本机回环上任何能打到 API 的进程都可以放行 `dispatch_simulation` 副作用。这是用户接受的演示姿态，不是生产安全模型。

#### 下一工作包直接复用

- 在线验收：Fast 8080 + 宿主机 API（通常 8010）+ compose PostgreSQL/Qdrant；打开 `/demo`，提交任意 P/S 组合，等 waiting_approval，点批准。
- 改 envelope 时仍须同步 `services/demo/service.py` 与 registry/graph。



