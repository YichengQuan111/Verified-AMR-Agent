# Verified AMR Agent

一个面向 4 台仓储 AMR 的本地、受控、可验证 Agent 项目。项目目标不是让大模型直接控制机器人，而是完成下面这条可审计闭环：

```text
自然语言目标
  → 受限上下文与结构化任务合同
  → RAG 证据
  → 任务 DAG
  → 确定性分配/路径/验证
  → 离散事件仿真
  → 观测验证与局部重规划/审批
  → 带引用的证据报告
```

当前代码范围已完成 **P0-00～P0-19**。P0-16 已把签名身份、两级 RBAC、文档 ACL、
工具白名单和 HITL/Checkpoint 审批接入安全边界；Smart Profile 因真实 P0-05 在线验收仅通过 2/5，现为硬禁用，
只有收到用户日后的明确指示并重新完成在线验收后才能启用。

## 1. 固定范围

- 地图：30 × 20 二维栅格，每格 1 m。
- 机器人：4 台同构差速驱动 AMR。
- 固定资源：6 个取货点、6 个交付点、2 个充电站。
- 订单主链：`pickup → transport → dropoff`。
- LLM 只负责目标理解、结构化规划、受控工具选择和异常处置建议。
- Python/C++ 确定性代码负责约束、分配、路径、冲突处理、仿真与验证。
- P0 不包含 ROS 2、Gazebo、真实底盘、CBS/ECBS、MILP、多 Agent、Redis、Celery、Kubernetes 或任意代码执行 Sandbox。

完整边界以 [docs/scope.md](docs/scope.md) 和 [P0 技术路线](docs/AMR_Agent_P0技术路线与实施ToDo.docx) 为准。

## 2. 当前完成状态

| 工作包 | 状态 | 已实现能力 |
|---|---|---|
| P0-00 | 已完成 | 冻结 Scope、地图/AMR/订单种子和 Backlog。 |
| P0-01 | 已完成 | Python/C++17 工程骨架、分层配置、结构化日志、依赖锁和统一冒烟。 |
| P0-02 | 已完成（Smart 暂停） | Fast/Smart 两套本地 Qwen 文本 Profile 和启动手册；Smart 当前配置为硬禁用。 |
| P0-03 | 已完成 | OpenAI 兼容模型网关、alias 门禁、超时、版本记录和最多一次 Schema 修复。 |
| P0-04 | 已完成 | 8 个严格 Pydantic 核心契约、validator、DAG 校验和 JSON Schema 导出。 |
| P0-05 | 已完成 | 5 个独立 2-shot Prompt、有限状态摘要、来源/版本/时间标记和确定性预算门禁。 |
| P0-06 | 已完成 | FastAPI、Router/Service/Repository、8 张 PostgreSQL 核心表、事务回滚和接口集成测试。 |
| P0-07 | 已完成 | 6 份冻结文档、章节切块、本地 Embedding、Qdrant + BM25 混合检索、检索期 ACL、引用、拒答和 20 例评测。 |
| P0-08 | 已完成 | 独立 C++17 `task_allocator` 库、Hungarian/最近空闲 baseline、严格 JSON stdin/stdout 和 7 个 CTest 场景。 |
| P0-09 | 已完成 | 独立 C++17 `route_planner`、A*、时空 `(cell,t)/(edge,t)` 预约、Dijkstra baseline、严格 JSON CLI 和 12 个路由 CTest 场景。 |
| P0-10 | 已完成 | 独立 C++17 `fleet_plan_validator`、稳定错误字典/证据、严格 JSON CLI 和 14 个正反例 CTest 场景。 |
| P0-11 | 已完成 | Python 固定时间步仿真、P0-10 前置门禁、P0-09 时间戳路径执行、Observation/事件日志、充电和 Eval 故障注入。 |
| P0-12 | 已完成 | 九个统一白名单工具、Pydantic 输入/输出 Schema、角色/超时/错误/审计/幂等门禁、固定 C++ JSON 适配器、状态/审批/验证封装。 |
| P0-13 | 已完成 | 固定 `guard→understand→retrieve→plan→validate→execute→verify→finish` 主图、真实 Fast LLM/RAG/C++/仿真/Observation 成功闭环、带引用和工具证据的报告。 |
| P0-14 | 已完成 | PostgreSQL Checkpoint、由 `run_id + plan_version + task_id` 规范三元组摘要生成的 Effect Ledger 幂等键、真实状态核对后的中断恢复，以及只替换受影响未完成 DAG 子图的 Local Replanner。 |
| P0-15 | 已完成 | 覆盖低电量、AMR 离线、通道封闭、工位占用、工具超时、计划不可行和状态冲突的稳定故障分类；有限 retry/replan/fallback/human/fatal 策略；默认最多两次局部重规划、全局步骤/时长/Token/重试门禁，以及与 Checkpoint/Effect Ledger 一致的恢复测试。 |
| P0-16 | 已完成 | JWT 验签、viewer/operator 两级 RBAC、工具级权限、检索期文档 ACL、Prompt Injection 不可信输入边界、禁止任意代码/SQL/Shell/外部 HTTP、PostgreSQL/内存 HITL 审批和 waiting Checkpoint 恢复；越权、注入、审批绕过和 ACL 泄漏反例全部阻断。 |
| P0-17 | 已完成 | 严格 TraceEvent（run/trace/node/task、模型/Prompt/工具版本、Token、延迟、错误、摘要和证据引用）、只允许预注册 CTest/pytest/smoke/仿真入口的受控验证、日志失败结构化解析，以及由真实退出码重算的 JSON/Markdown 验证报告。 |
| P0-18 | 已完成 | 统一 Eval Harness、固定 25/10/10/5/10 共 60 例、地图/订单/seed/模型/Prompt/工具指纹、Agent/RAG/AMR/安全/恢复/验证指标、负向轨迹和 JSON/Markdown 报告；七项零容忍指标必须为 0。 |
| P0-19 | 已完成（离线 Trace Replay） | 在同一 P0-18 60 例、工具、Prompt/配置和 `qwen3.6-fast` 身份下对照固定 Workflow、ReAct、PEVR；输出 180 条原始策略轨迹、完成/合法/恢复/工具错误/步数/Trace P95 汇总和公平性指纹。ReAct 不进入生产主链；Smart 对照延期而非完成。 |

明确尚未实现：真实 ROS/底盘接入；P0-15 不注册任意自动补偿工具，副作用未知
状态仍必须人工核对。单独构造 `ToolRegistry` 时状态/审批仍默认使用进程内适配器；
真实 PEVR CLI 会把 Checkpoint、Effect Ledger 和 dispatch 外部状态共同注入 PostgreSQL，
能够跨进程恢复。

## 3. 已落地架构

### 3.1 模型与上下文边界

- 业务代码只依赖 `ModelProviderProtocol`，不直接依赖 GGUF、llama.cpp 参数或 OpenAI SDK 响应对象。
- Fast 模型 alias 为 `qwen3.6-fast`。Smart 的保留 alias 为 `qwen3.8-smart`，但当前
  `enabled=false`，Provider 会在访问 `/v1/models` 前返回 `MODEL_PROFILE_DISABLED`。
- 五个 Prompt 分别负责 `understand_goal`、`plan_tasks`、`verify_observation`、`replan`、`compose_report`。
- 每次模型调用只接收 system Prompt 和当前有限上下文，不传整个聊天/运行历史。
- RAG/工具证据必须携带来源、版本、时间和引用；Token、工具步数、总时间和重规划次数由模型外代码确定性限制。

### 3.2 API 与持久化边界

```text
FastAPI Router
    → Application Service（业务状态与事务）
        → Repository（无 commit/rollback）
            → SQLAlchemy ORM
                → PostgreSQL 17
```

P0-06 的 8 张核心表必须保留：

`runs`、`plans`、`tasks`、`tool_calls`、`effects`、`approvals`、`events`、`documents`

唯一辅助表是 Alembic 标准表 `alembic_version`。数据采用“高频查询字段关系化 + 完整 Pydantic 快照 JSONB”，文档原始字节使用 BYTEA。迁移只向前，不提供自动删表 downgrade。

运行创建的关键顺序是：

```text
BEGIN → INSERT runs → flush → INSERT events → flush → COMMIT
```

真实 PostgreSQL 故障注入已经验证：第二个 INSERT 失败时，第一个已经发出的 INSERT 也会回滚。

详细设计见 [docs/DATABASE.md](docs/DATABASE.md)。

## 4. 当前 HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 进程健康状态。 |
| GET | `/health/model` | 主动检查模型网关和 alias。 |
| POST | `/agent/runs` | 创建持久化运行。 |
| GET | `/agent/runs/{run_id}` | 查询/恢复运行。 |
| GET | `/agent/runs/{run_id}/plan` | 查询最新或指定计划版本。 |
| GET | `/agent/runs/{run_id}/events` | 返回已持久化事件的有限 SSE 快照。 |
| POST | `/agent/runs/{run_id}/approve` | 保存人工批准或拒绝。 |
| POST | `/documents` | 上传不超过 10 MiB 的文档。 |
| GET | `/documents/{document_id}` | 查询文档元数据。 |
| POST | `/evals/runs` | 创建评测类型运行，不执行评测套件。 |

## 5. 主要目录

| 路径 | 作用 |
|---|---|
| `agent/planning/` | TaskContract、PlanTask、风险/预算和 DAG 校验。 |
| `agent/context/` | 5 个 Prompt、上下文契约、摘要器、预算门禁和独立节点。 |
| `agent/runtime/` | Observation、RunState、Checkpoint/Effect Ledger、Trace 契约、恢复协调和 PEVR 状态图。 |
| `agent/tools/` | 9 个白名单工具名、ToolSpec/ToolResult、输入/输出 Schema、统一执行器、固定 C++/状态/审批/验证适配器。 |
| `apps/api/` | FastAPI 应用工厂、请求 Schema、依赖和 Router。 |
| `services/model_gateway/` | 本地模型统一访问边界。 |
| `services/application/` | 运行、计划、审批、文档以及 PostgreSQL Checkpoint/Effect Ledger 事务 Service。 |
| `services/persistence/` | SQLAlchemy ORM、Session 工厂和 Repository。 |
| `services/retrieval/` | P0-07 文档加载、分块、Embedding、Qdrant/BM25、混合融合、ACL、拒答与引用。 |
| `services/planner_cpp/` | P0-08 C++17 Hungarian/最近空闲分配、P0-09 A*/Dijkstra 路径与时空预约、P0-10 计划验证器、严格 JSON 编解码和 CTest。 |
| `services/amr_simulator/` | P0-11 Python 固定 tick 仿真、P0-10 Validator 适配、状态/订单/工位/充电快照、Observation/事件日志和 Eval 专用故障注入。 |
| `docs/P012_TOOLS.md` | 九工具清单、Schema、角色/超时/副作用/幂等、固定 C++ 边界和错误/重复调用语义。 |
| `docs/P017_TRACE_VERIFICATION.md` | Trace 字段、受控验证白名单、日志解析、证据定位和 JSON/Markdown 报告契约。 |
| `evals/rag/` | 20 例固定 RAG 数据与 Recall/MRR/Citation/ACL 执行器。 |
| `evals/p018/` | P0-18 固定 60 例数据、严格契约、离线确定性 Harness、复现指纹、JSON/Markdown 报告和 CLI。 |
| `evals/p019/` | P0-19 固定策略配置、P0-18 源报告 digest 门禁、三策略 Trace Replay、Token/延迟/资源可观测性标记、原始 JSONL 和汇总报告 CLI。 |
| `docs/P019_STRATEGY_COMPARISON.md` | P0-19 公平性口径、指标定义、实测汇总、限制、Smart 延期和复核入口。 |
| `domains/amr_warehouse/` | 仓储领域契约和种子数据。 |
| `migrations/` | Alembic 前向迁移。 |
| `tests/` | 单元、契约、真实 PostgreSQL 集成和 C++ 冒烟测试。 |
| `docs/` | Scope、技术路线、Schema、数据库说明、文件职责和交接上下文。 |

## 6. 固定环境

| 组件 | 路径或地址 |
|---|---|
| 项目 Python | `E:\Anaconda\envs\torch128\python.exe`（Python 3.12） |
| PostgreSQL | `localhost:5432` / database `amr_agent` |
| Qdrant | `http://localhost:6333` |
| Embedding | `E:\Llama.cpp\Embedding`（Qwen3-Embedding-0.6B，维度动态读取） |
| FastAPI | `http://127.0.0.1:8000` |
| 模型 API | `http://127.0.0.1:8080/v1` |
| Fast 模型脚本 | `E:\Llama.cpp\start-qwen3.6-agent.cmd` |
| Smart 模型脚本 | `E:\Llama.cpp\start-qwen3.8-agent.cmd`（暂时禁用，不得启动） |

不要假设 Docker、数据库、Qdrant 或 Qwen 已经运行；每次新会话都应重新检查。

## 7. 快速开始（Windows PowerShell）

### 7.1 安装锁定依赖

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
& 'E:\Anaconda\envs\torch128\python.exe' -m pip install `
  -r .\requirements.lock `
  -r .\requirements-dev.lock
```

### 7.2 启动基础设施并迁移

先启动 Docker Desktop，确认 Engine 可用，再执行：

```powershell
docker compose up -d postgres qdrant
docker compose ps
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\migrate_database.py upgrade
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\migrate_database.py check
```

`check` 必须报告 8 张核心表全部存在；不要执行手工删表或 `docker compose down -v`。

### 7.3 统一回归

```powershell
.\scripts\run_smoke.ps1
```

该脚本会检查环境、执行幂等前向迁移、运行全部 pytest，并构建/运行 CTest。仅跳过 C++ 时使用：

```powershell
.\scripts\run_smoke.ps1 -SkipCpp
```

最近一次完整验证基线（2026-08-21，P0-16 完成后）：

- pytest：215/215 通过，唯一警告为既有 `jieba/pkg_resources` 弃用警告。
- CTest：34/34 通过；新增反例确认未分配/空闲 AMR 的初始位置仍会阻断冲突路线。
- Alembic：`0001_p006_core (head)`，8 张核心表缺失数 0。
- Qdrant：健康检查通过，`amr_warehouse_knowledge` 保留 70 个正式 points。
- Fast Qwen：alias 门禁通过；三个独立 `run_id` 的真实 PEVR 均 `completed`，每次都是
  8/8 阶段、5/5 工具、Validator error=0、仿真完成。验收后 8080 已释放。
- Smart Qwen：未启动；禁用门禁实测返回 `MODEL_PROFILE_DISABLED`。历史在线 P0-05
  节点仅 2/5，因此未把 Smart 记为通过。

### 7.4 模型与 API

需要真实模型时，只启动 Fast，再运行：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\check_model_gateway.py
& 'E:\Anaconda\envs\torch128\python.exe' -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

隔离测试可以关闭启动期模型门禁；生产/真实联调不得用这个开关掩盖模型未启动或 alias 错误。
不要启动或选择 Smart；环境变量不能绕过其 `enabled=false` 门禁。

完整启动顺序见 [docs/SERVICES_STARTUP.md](docs/SERVICES_STARTUP.md)。

## 8. P0-07 仓储 SOP RAG

P0-07 已实现以下闭环：

- 文档按章节切块，并保留 `doc_id`、`section`、`version`、`role_scope`、`source`、`checksum`。
- 使用本地 Embedding + Qdrant 做向量检索，使用进程内 BM25 做关键词检索。
- 对两路分数归一化融合，返回带引用的 Top-K。
- 证据不足时确定性拒答；P0 不实现 Reranker。
- 交付索引器、查询 CLI、混合检索器、ACL 过滤器和 20 个 RAG 评测样例。
- 能计算 Recall@K、MRR、引用正确率；跨角色泄漏必须为 0。

索引器复用 P0-06 `documents` 表和 `DocumentService`，没有新增数据库 revision。检索结果可转换为 P0-05 `ContextEvidence(source_type="rag")`。详细设计、阈值校准、运行命令和当前 20 例结果见 [docs/RAG.md](docs/RAG.md)。

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\index_warehouse_knowledge.py
& 'E:\Anaconda\envs\torch128\python.exe' -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

当前 20 例实测：Recall@K `1.0`、MRR `0.970588`、Citation Correctness `1.0`、Answerability Accuracy `1.0`、ACL leak `0`。P0-08 直接复用 P0-04 的 `TransportOrder`、AMR 状态和嵌套坐标，没有修改本次 RAG 公共契约。

## 9. P0-08 C++ Hungarian 任务分配

分配器计算 `distance + lateness_risk + priority_bonus + battery_risk + load_penalty` 的显式加权代价；预计完成后低于 15% 安全余量、电量不高于 20% 的普通新任务、非空闲/非健康/离线车辆、未满足依赖和缺失工位都返回 `INF` 与稳定原因码。矩形 AMR—订单矩阵通过 dummy 行/列表示未匹配，不会把 INF 组合返回为真实分配。

详细请求/响应字段、原因码、阈值、退出码和范围边界见 [docs/TASK_ALLOCATOR.md](docs/TASK_ALLOCATOR.md)。编译后可直接执行：

```powershell
Get-Content .\request.json -Raw | .\build\cpp\services\planner_cpp\task_allocator_cli.exe --algorithm hungarian
```

P0-08 只用 Manhattan 距离做分配代价，不处理障碍、单向边、时空冲突或实际路线；这些由 P0-09/P0-10 负责。

## 10. P0-09 C++ A* 与时空预约

`route_planner` 使用 `(x, y, heading, t)` 时间扩展状态，前进、转向和等待都有明确代价；生产 A* 使用曼哈顿启发式，独立 Dijkstra 只作为正确性基线。多 AMR 按订单优先级、发布时间和稳定 ID 规划，路径写入 `(cell,t)` 与 `(edge,t)` 预约表，禁止顶点冲突和交换边冲突。障碍、禁行边、单向边、边界和有限 `max_time` 都是硬约束，无解时返回 `status=infeasible` 且失败路线不携带路径。

请求/响应字段、动作时间语义、退出码和调用示例见 [docs/ROUTE_PLANNER.md](docs/ROUTE_PLANNER.md)。编译后可直接执行：

```powershell
Get-Content .\route_request.json -Raw | .\build\cpp\services\planner_cpp\route_planner_cli.exe --algorithm astar
```

## 11. P0-10 C++ 车队计划验证

`fleet_plan_validator` 对地图、P0-04 AMR/订单字段、工位位置、载荷和 P0-09 路径做独立确定性复核，检查任务依赖、时间窗、载荷、电量安全余量、禁行区、工位容量、最小安全距离、顶点冲突和交换边冲突。路线规划器返回的 `status`、LLM 提供的“已验证”字段或 Prompt 文本都只是外部输入，不能替代这一步；未知的 `llm_valid`、`skip_validation` 等旁路字段会在 JSON 边界被拒绝。

每个非法计划返回 `status=invalid` 和按约束分组的稳定错误码；证据包含相关任务/订单、AMR、坐标、时间、观测值/上限和路径索引，便于定位与审计。错误码全集由 [docs/FLEET_PLAN_VALIDATOR.md](docs/FLEET_PLAN_VALIDATOR.md) 和 `--error-dictionary` 输出共同定义。

请求/响应字段、离散时间语义、退出码和边界见 [docs/FLEET_PLAN_VALIDATOR.md](docs/FLEET_PLAN_VALIDATOR.md)。编译后可直接执行：

```powershell
Get-Content .\fleet_plan_request.json -Raw | .\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --validate
Get-Content .\fleet_plan_request.json -Raw | .\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --error-dictionary
```

业务计划非法时 CLI 仍返回结构化结果并使用退出码 `0`；只有 JSON/参数/内部错误使用非零退出码，调用方必须检查响应中的 `status`/`valid`。

## 12. P0-11 Python 离散事件仿真

`AMRSimulator` 只执行已经通过 P0-10 的完整计划，并直接按 P0-09 `path[*].time`
推进位置、朝向和电量。固定 1 秒 tick 记录 `Observation` 与结构化事件；到达
工位时产生零时长 `LOADING/UNLOADING` 事件，路线终点保持占用到时间上限。仿真
侧还维护订单、工位、充电站状态，并提供不进入正常 Agent 工具白名单的
`FaultInjection`（`offline`、`battery_drain`、`stuck`）供后续 Eval 使用。

详细字段和边界见 [docs/AMR_SIMULATOR.md](docs/AMR_SIMULATOR.md)。专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p011_simulator.py -q
```

## 13. P0-12 九个白名单工具

从 `agent.tools` 取得封闭注册表：

```python
from agent.tools import ToolName, UserRole, build_tool_registry

registry = build_tool_registry()
result = registry.execute(
    ToolName.GET_FLEET_STATE,
    {"environment_ref": "warehouse_v1@state-001"},
    role=UserRole.VIEWER,
    call_id="state-read-001",
)
```

九个工具为 `retrieve_knowledge`、`get_fleet_state`、`allocate_tasks`、
`plan_multi_amr_routes`、`validate_fleet_plan`、`dispatch_simulation`、
`query_execution_state`、`run_verification_suite` 和 `request_approval`。每次调用
统一返回 `ToolResult`，执行前拒绝未知参数/角色/Schema 错误；结果带工具版本、
角色、输入/输出 digest、证据引用、effect ID、超时/错误分类和幂等语义。

分配、A* 和 Validator 只通过固定 exe 与 JSON stdin/stdout 调用；不会接受 Shell、
任意路径、任意命令或 `faults` 字段。故障注入仍仅供 P0-11 Eval 接口使用。完整
契约和固定 suite/case 列表见 [docs/P012_TOOLS.md](docs/P012_TOOLS.md)。专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p012_tools.py -q
```

## 14. P0-13 PEVR 正常闭环

P0-13 的主图固定为 `guard → understand → retrieve → plan → validate → execute → verify → finish`。
理解和四个 P0-05 命名 Prompt 节点使用本地模型网关；Planner DAG 先经过确定性
Validator，再由 Executor 通过 P0-12 九工具注册表调用 Hungarian、A*、P0-10
Validator 和 Python 仿真。`dispatch_simulation` 的 `requires_approval=true` 不会
被 Planner 自己满足，CLI 必须显式提供 operator 的可信审批上下文。

真实 Fast 验收入口（先启动 `E:\Llama.cpp\start-qwen3.6-agent.cmd`）：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' scripts\run_p013_e2e.py --approve-dispatch
```

运行报告写入 `tmp/p013_e2e_result.json`；报告包含 RAG citations、plan version、
工具 call/evidence、阶段和仿真指标、预算用量及未解决风险。详细边界见
[docs/P013_PEVR.md](docs/P013_PEVR.md)。

## 15. P0-14 Checkpoint、幂等与局部重规划

P0-14 在不新增数据库表的前提下复用 P0-06 的 `runs.run_state_snapshot`、`plans`、
`tasks`、`tool_calls`、`effects` 和 `events`。副作用身份严格由
`run_id + plan_version + task_id` 三元组决定，持久化键是其规范 JSON 的 SHA-256 摘要，
避免 ID 含分隔符时碰撞。首次执行先原子写入 `reserved` Effect Ledger；dispatch 的
外部状态在返回图节点前持久化到同一 Effect 行。重启时核对 effect ID、工具名、输入和
输出 digest，再决定继续、复用、待补偿或重规划，不能只信任 `completed` 字符串。

局部重规划从真实分配/路线 `ToolResult` 构建 AMR、cell、edge、通道和工位 provenance，
只失效受影响的未完成后继，保留已完成任务及旧 Effect；新版本必须重新通过完整
`allocate → route → validate → dispatch` PEVR Validator，而不只是 DAG 拓扑校验。

专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p014_checkpoint.py tests\unit\test_p014_replanner.py tests\integration\test_p014_postgres.py -q -p no:cacheprovider
```

详细恢复顺序、数据库事务边界和已知限制见 [docs/P014_CHECKPOINT.md](docs/P014_CHECKPOINT.md)。

## 16. P0-15 故障分类与终止策略

`agent.runtime.faults` 将底层错误映射为稳定 `FaultCategory` 和五种恢复动作。低电量、
AMR 离线、封路和计划不可行默认只允许最多两次局部重规划；工位占用按 retry→replan→
human 处理；普通无副作用 timeout 有限 retry 后 fallback；副作用状态未知、Effect/
Checkpoint 冲突直接 human；未知错误 fatal。总工具步数、总时长、输入/输出 Token 和重试
次数均是硬预算，终态决策不能回到执行态。

局部重规划通过 P0-14 `LocalReplanner` 和同一 Checkpoint Store 落账，保留已完成任务及
Effect Ledger，不重复副作用；完整分类表、预算边界、状态字段和验收命令见
[docs/P015_FAULTS.md](docs/P015_FAULTS.md)。

专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p015_faults.py tests\integration\test_p015_fault_recovery.py -q -p no:cacheprovider
```

## 17. P0-16 RBAC、HITL 与安全边界

API 业务路由只接受 Bearer JWT；角色从验签后的 Principal 读取，request body、
Prompt、RAG 正文和工具参数不能声明或提升角色。viewer 只能调用只读工具并只能
读取 ACL 包含 viewer 的文档；operator 才能调用写工具、上传文档、创建运行和决定
审批。工具仍只能从九个固定 ToolName 注册项进入，参数 Schema、确定性 Validator
和 ToolSpec 权限在审批前后都必须通过。

dispatch_simulation 等高风险写操作在安全 PEVR 中创建 pending HITL 请求，把
HITLInterrupt、当前任务状态和已完成工具证据写入 Checkpoint，随后抛出
PEVRInterrupt。operator 审批后，带 HMAC 签名的 ApprovalGrant 绑定同一
run_id/task_id/plan_version、计划摘要和 Validator 摘要；恢复入口重新核对
Checkpoint、数据库/内存审批存储、签名、期限和主体，才允许继续，Effect Ledger
仍保证副作用只执行一次。高优先级覆盖和人工接管使用同一 HITLReason/
HITLController 协议，不能被自然语言自动批准。

生产 API 通过 `/agent/runs/{run_id}/hitl/{approval_id}/approve` 或 `/reject`
由 operator 决定 HITL 请求；approval_id 必须属于同一 run，服务端不接受请求体中的审批人。

检索文档按不可信数据处理；系统 Prompt 明确拒绝“忽略权限/授予 operator/批准工具”
等注入内容，Qdrant/BM25 候选过滤、工具输出 ACL 复核和文档 Service ACL 三层均
fail closed。命令、SQL、Shell、脚本、URL/HTTP 选择器和未注册工具没有可达参数或
handler。

详细契约、审批状态机、数据库复用和限制见 docs/P016_SECURITY.md。

专项测试：E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p016_security.py -q -p no:cacheprovider

## 18. P0-17 Trace、受控验证与证据报告

PEVRRunResult/PEVRRunReport 通过同一 trace_id 关联严格递增的 TraceEvent。事件覆盖
节点、任务、模型/Prompt/工具版本、Token、延迟、错误、输入/输出 digest 和
evidence_refs；Checkpoint 恢复和 PostgreSQL events 表都保留该链路。验证工具只接受
预注册的 p0_12、p0_python、p0_cpp、p0_smoke、p0_simulation 入口，不能传入任意命令。
固定入口的 stdout/stderr、退出码和超时由 Parser 解析为失败类型、任务/工具、参数摘要
和带行号证据；Report Generator 从真实 case 结果重算 JSON/Markdown 结论。

详细字段、入口和限制见 docs/P017_TRACE_VERIFICATION.md。

专项测试：

    & 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p017_trace.py tests\unit\test_p017_validation.py -q -p no:cacheprovider

## 19. P0-18 60 例自动评测

P0-18 使用固定 `warehouse_v1@seed-v1` 地图/AMR/订单和每例唯一 seed，严格检查
25 例正常订单/充电、10 例 RAG/权限/审批、10 例异常/局部重规划、5 例
CTest/pytest/仿真验证和 10 例 Prompt Injection/越权/审批绕过。默认
`offline_deterministic_oracle` 不启动模型服务，但报告会记录 Fast `qwen3.6-fast`、
GGUF、Prompt、九个 ToolSpec 和输入文件 SHA-256；正确拒绝和意外失败都保留逐例 Trace。

```powershell
.\scripts\run_p018_eval.ps1
```

输出为 `tmp/p018_eval/p018_eval.json` 和 `tmp/p018_eval/p018_eval.md`。详细字段、指标、
限制和验收命令见 [docs/P018_EVAL.md](docs/P018_EVAL.md)。

专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p018_eval.py -q -p no:cacheprovider
```

## 20. P0-19 策略对照实验

P0-19 复用 P0-18 的 60 例和逐例 Trace，在同一 Prompt/ToolSpec/配置及
`qwen3.6-fast` 身份下对照固定 Workflow、ReAct 和 PEVR。当前是
`offline_trace_replay`：ReAct 只作为测评投影，不进入生产主链；Token/资源没有源样本时
显示为未观测，不能把离线结果写成在线模型质量验收。

```powershell
.\scripts\run_p019_compare.ps1
```

产物写入 `tmp/p019_strategy_compare/`，包括完整 JSON、180 条原始 JSONL 轨迹和 Markdown
汇总表。当前实测三策略均 60/60 符合 P0-18 预期，ReAct 派生控制步均值/P95 为
12.15/28，PEVR 为 5.82/14；详细口径和限制见
[docs/P019_STRATEGY_COMPARISON.md](docs/P019_STRATEGY_COMPARISON.md)。Qwen3.8 Smart
对照已延期而非完成：本步未启动、未测试，不阻塞 P0-19，延期项为
`P0-19-SMART-COMPARISON`。

专项测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p019_compare.py -q -p no:cacheprovider
```

## 21. 协作与交接入口

开始任何新任务前，按顺序阅读：

1. [AGENTS.md](AGENTS.md)：全仓库强制规则。
2. [docs/HANDOFF_CONTEXT.md](docs/HANDOFF_CONTEXT.md)：唯一跨会话事实入口。
3. [docs/FILE_PURPOSES.md](docs/FILE_PURPOSES.md)：所有新增/修改文件的长期职责登记。
4. 与当前工作包相关的 Scope、技术路线、接口和测试。

推进任何工作包时，都必须同步：

- 为核心代码编写准确的中文注释或 docstring。
- 更新 `docs/FILE_PURPOSES.md`，逐项说明文件作用和下游依赖。
- 更新 `docs/HANDOFF_CONTEXT.md`，记录公共接口、验证证据、服务状态、限制和下一步。
- 实际运行相关测试，不把未执行的项目写成通过。
