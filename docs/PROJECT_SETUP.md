# P0-01 工程骨架与环境约定

## 目录职责

| 目录 | 当前职责 |
|---|---|
| `apps/api` | FastAPI 入口、生命周期、健康检查与 P0-06 业务 Router |
| `agent/runtime` | 后续 LangGraph 状态图、Checkpoint |
| `agent/planning` | 后续领域契约、Planner、Validator、Replanner |
| `agent/tools` | 后续白名单工具契约和路由 |
| `domains/amr_warehouse` | 地图、订单、AMR 状态和领域约束 |
| `services/model_gateway` | P0-03 本地模型统一访问边界 |
| `services/application` | P0-06 运行、计划、审批和文档事务 Service |
| `services/persistence` | P0-06 SQLAlchemy ORM、会话工厂与无事务 Repository |
| `services/retrieval` | 后续混合检索与 ACL |
| `services/planner_cpp` | C++17 确定性规划服务 |
| `services/amr_simulator` | 后续离散事件仿真 |
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

项目继续固定使用：

```text
E:\Anaconda\envs\torch128\python.exe
```

安装锁定的直接依赖：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pip install `
  -r .\requirements.lock `
  -r .\requirements-dev.lock
```

一条命令输出 Python、锁定包、CMake、Ninja 和 MSVC 路径及匹配状态：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\check_environment.py
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
