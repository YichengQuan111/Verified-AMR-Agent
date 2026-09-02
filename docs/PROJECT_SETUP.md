# P0-01 工程骨架与环境约定

## 目录职责

| 目录 | 当前职责 |
|---|---|
| `apps/api` | FastAPI 入口、生命周期、健康检查与 P0-06 业务 Router |
| `agent/runtime` | 后续 LangGraph 状态图、Checkpoint |
| `agent/planning` | 后续领域契约、Planner、Validator、Replanner |
| `agent/tools` | P0-12 九个白名单工具的 ToolSpec/ToolResult、输入/输出 Schema、统一执行器、固定 C++/状态/审批/验证适配器 |
| `domains/amr_warehouse` | 地图、订单、AMR 状态和领域约束 |
| `services/model_gateway` | P0-03 本地模型统一访问边界 |
| `services/application` | P0-06 运行、计划、审批和文档事务 Service |
| `services/persistence` | P0-06 SQLAlchemy ORM、会话工厂与无事务 Repository |
| `services/retrieval` | P0-07 Loader、section chunk、Embedding、Qdrant/BM25、ACL、融合、引用与拒答 |
| `evals/rag` | P0-07 固定 20 例数据及 Recall/MRR/Precision/nDCG/Citation/ACL 执行器 |
| `services/planner_cpp` | P0-08 `task_allocator`、P0-09 `route_planner` 与 P0-10 `fleet_plan_validator` C++17 库、独立 baseline、JSON CLI 和 CTest |
| `services/amr_simulator` | P0-11 Python 固定 tick 离散事件仿真、P0-10 Validator 适配、结构化 Observation/事件日志和 Eval 故障注入 |
| `services/validation` | 后续受控测试和证据报告 |
| `evals` | 版本化评测集与 Harness |
| `infra` | Compose、模型启动元数据和部署辅助文件 |
| `tests` | 单元、契约、集成和端到端测试 |

## 配置优先级

配置按以下顺序覆盖，最后一项优先级最高：

1. Python 类型化默认值；
2. `config/default.toml`；
3. `config/<AMR_ENV>.toml`（文件存在时）；
4. `AMR_CONFIG_FILE` 或代码显式传入的 TOML；
5. 环境变量。

可用环境变量示例见根目录 `.env.example`。API key 和 PostgreSQL DSN 使用 `SecretStr` 保存，配置对象序列化时不会输出明文。

## 依赖与环境检查

解释器由 `AMR_PYTHON_EXE` 指定，本机值见 [LOCAL_ENV.md](LOCAL_ENV.md)。公开命令写 `python`；若 PATH 不是项目环境，先设置该变量。

```powershell
python -m pip install `
  -r .\requirements.lock `
  -r .\requirements-dev.lock
```

一条命令输出 Python、锁定包、CMake、Ninja 和 MSVC 路径及匹配状态：

```powershell
python .\scripts\check_environment.py
```

## Python 与 C++ 冒烟测试

统一入口会先检查环境、执行幂等数据库迁移并确认 8 张核心表，再运行包含真实 PostgreSQL 集成测试的 pytest，随后初始化 MSVC、配置/编译 C++17 工程并运行 CTest：

```powershell
.\scripts\run_smoke.ps1
```

跳过 C++、但仍验证 PostgreSQL 与 Python 时：

```powershell
.\scripts\run_smoke.ps1 -SkipCpp
```

构建产物固定写入 `build/cpp`，不与源码混放。

P0-08 的 CLI 目标为 `task_allocator_cli`，生产算法使用 `--algorithm hungarian`，独立基线使用 `--algorithm nearest_idle`；请求/响应契约见 [TASK_ALLOCATOR.md](TASK_ALLOCATOR.md)。

P0-09 的 CLI 目标为 `route_planner_cli`，生产算法使用 `--algorithm astar`，独立基线使用 `--algorithm dijkstra`；请求/响应契约见 [ROUTE_PLANNER.md](ROUTE_PLANNER.md)。

P0-10 的 CLI 目标为 `fleet_plan_validator_cli`，使用 `--validate`（默认动作）校验完整车队计划，使用 `--error-dictionary` 输出稳定错误字典；请求/响应契约见 [FLEET_PLAN_VALIDATOR.md](FLEET_PLAN_VALIDATOR.md)。业务非法计划通过 JSON `status=invalid` 报告，不能只看进程退出码。

P0-11 的 Python 入口为 `services.amr_simulator.AMRSimulator`；它在执行前固定调用
`build/cpp/services/planner_cpp/fleet_plan_validator_cli.exe --validate`，然后直接
按 P0-09 的路径时间戳推进。仿真契约、充电站配置、Observation/事件字段和 Eval
故障注入边界见 [AMR_SIMULATOR.md](AMR_SIMULATOR.md)。

P0-12 的统一入口为 `agent.tools.build_tool_registry()`；九个工具的角色、超时、
输入/输出 Schema、错误分类、审计和重复调用语义见 [P012_TOOLS.md](P012_TOOLS.md)。
Python 调用 C++ 时只使用固定 exe + JSON stdin/stdout，生产工具不接受 Shell、路径、
命令或 P0-11 `FaultInjection`。
