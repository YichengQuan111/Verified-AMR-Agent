# AMR Agent P0 服务启动手册

本文档固化 P0 开发阶段所需服务、固定路径、端口、启动顺序、健康检查与停止方式。所有命令默认在 Windows PowerShell 中执行；特别标注为 `cmd.exe` 的命令除外。

基准日期：2026-08-21
项目根目录：`C:\Users\QYC\Documents\AMR_Agent`

## 1. 固定环境与端口

| 组件 | 固定位置或地址 | 用途 |
|---|---|---|
| Python | `E:\Anaconda\envs\torch128\python.exe` | API、Agent、数据库检查与评测 |
| C++ Build Tools | `E:\BuildingTools` | MSVC、CMake、Ninja、CTest |
| Fast 模型 | `E:\Llama.cpp\start-qwen3.6-agent.cmd` | 默认开发与主评测模型 |
| Smart 模型 | `E:\Llama.cpp\start-qwen3.8-agent.cmd` | 暂时禁用；保留路径只供日后重新验收 |
| LLM API | `http://127.0.0.1:8080/v1` | OpenAI 兼容接口 |
| PostgreSQL | `localhost:5432` | 运行状态、Checkpoint、Effect Ledger |
| Qdrant | `http://localhost:6333` | SOP 和设备文档向量检索 |
| Embedding | `E:\Llama.cpp\Embedding` | Qwen3-Embedding-0.6B；不占用 8080 |
| AMR Agent API | `http://127.0.0.1:8000` | 当前 FastAPI 服务入口 |

模型别名：

- Fast：`qwen3.6-fast`
- Smart：`qwen3.8-smart`（保留标识，当前 `enabled=false`）

两个脚本都绑定 `127.0.0.1:8080`。当前只允许启动 Fast；不要启动 Smart，也不要仅靠
`LLM_PROFILE=smart` 尝试绕过配置。日后收到用户明确指示并恢复 Smart 时，仍必须保证
一次只常驻一个模型，切换前先确认 8080 已释放。

## 2. 推荐启动顺序

每次完整开发建议按以下顺序启动：

1. 进入项目根目录。
2. 确认 Docker Engine 可用。
3. 启动 PostgreSQL 和 Qdrant。
4. 在独立终端启动 Fast 模型；Smart 当前不得启动。
5. 在独立终端启动 AMR Agent API。
6. 需要编译规划器时，再打开已初始化的 C++ 开发终端。

先打开 PowerShell 并进入项目目录：

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
```

## 3. PostgreSQL 与 Qdrant

数据库服务由项目根目录的 `compose.yaml` 管理：

- Compose 服务名：`postgres`、`qdrant`
- 容器名：`amr-postgres`、`amr-qdrant`

### 3.1 启动

确认 Docker Desktop 已启动后执行：

```powershell
docker version
docker compose -f .\compose.yaml up -d postgres qdrant
docker compose -f .\compose.yaml ps
```

如果 `docker version` 无法连接 Server，先启动 Docker Desktop，等待 Engine 就绪后重试。

### 3.2 PostgreSQL 连接信息

| 字段 | 值 |
|---|---|
| Host | `localhost` |
| Port | `5432` |
| Database | `amr_agent` |
| User | `amr` |
| Password | `123456` |

当前密码只适用于本机开发。项目共享、远程部署或提交公开仓库前，应改为从 `.env` 或密钥服务读取。

执行只读连通性检查：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\check_postgres.py'
```

正常输出应包含：

```text
(1,)
```

首次启动或拉取到新 migration 后，执行只向前迁移和表检查：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\migrate_database.py' upgrade
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\migrate_database.py' check
```

成功报告必须包含 8 张核心表、`missing_core_tables: []`，辅助表只能包含 Alembic 的 `alembic_version`。脚本不提供 downgrade；不要通过手工删表代替前向修复 migration。表结构和事务说明见 [`DATABASE.md`](DATABASE.md)。

### 3.3 Qdrant 检查

```powershell
Invoke-WebRequest -UseBasicParsing 'http://localhost:6333/readyz'
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\check_qdrant.py'
```

Qdrant Dashboard：`http://localhost:6333/dashboard`

### 3.4 P0-07 知识索引与评测

Embedding 直接从本地目录加载，不需要启动 Fast/Smart 文本模型。PostgreSQL 与 Qdrant 健康后执行：

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\index_warehouse_knowledge.py'
& 'E:\Anaconda\envs\torch128\python.exe' -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

索引成功应报告 6 份文档、70 个当前证据 chunk 和动态读取的 1024 维；评测的 `acl_leak_count` 必须为 0。语料或 chunk 规则变化后数量可能变化，应以索引报告与 `docs/RAG.md` 为准，不能把 70/1024 写进运行时代码。

### 3.5 日志与停止

查看最近日志：

```powershell
docker compose -f .\compose.yaml logs --tail 100 postgres qdrant
```

日常停止，保留容器和数据卷：

```powershell
docker compose -f .\compose.yaml stop postgres qdrant
```

需要移除容器和 Compose 网络时：

```powershell
docker compose -f .\compose.yaml down
```

不要执行 `docker compose down -v`，该命令会删除 PostgreSQL 与 Qdrant 数据卷。

## 4. 本地模型服务

### 4.1 Fast：Qwen3.6

Fast 是 P0 默认模型，使用别名 `qwen3.6-fast`：

```powershell
& 'E:\Llama.cpp\start-qwen3.6-agent.cmd'
```

脚本会在当前窗口运行模型。保持窗口开启；停止时在该窗口按 `Ctrl+C`。

 

### 4.2 Smart：Qwen3.8（暂时禁用）

Smart 的脚本和别名 `qwen3.8-smart` 仅为日后重新验收保留。最近一次真实 P0-05
五节点在线验收只有 2/5，因此仓库配置明确设为 `enabled=false`。在用户日后给出明确
启用指示之前：

- 不启动 `E:\Llama.cpp\start-qwen3.8-agent.cmd`；
- 不修改 `config/default.toml` 的 Smart `enabled=false`；
- 不把 alias 预检或单条结构化响应记作 Smart 在线验收通过。

`check_model_gateway.py --profile smart` 当前预期返回非零退出码和
`MODEL_PROFILE_DISABLED`，而且门禁发生在 `/v1/models` 之前。

### 4.3 模型健康检查

模型加载可能需要一段时间。加载完成后，在另一个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/health'
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id
```

随后执行项目统一模型网关预检。该检查会校验当前 Profile、服务实际 alias 和版本记录；alias 不一致时返回非零退出码：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\check_model_gateway.py'
```

必须确认返回的模型别名与本次配置一致：

- Fast 运行时必须看到 `qwen3.6-fast`。
- Smart 当前不能进入在线 alias 检查；选择它必须得到 `MODEL_PROFILE_DISABLED`。

Fast 模型可执行现有 20 次结构化输出冒烟测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\smoke_llm_structured.py'
```

当前冒烟脚本固定使用 `qwen3.6-fast`，不要在 Smart 模型运行时直接执行。

P0-05 五个 2-shot Prompt 的真实节点测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\smoke_p005_prompts.py' --profile fast
```

该命令会再次执行模型别名门禁，然后按顺序验证五个独立节点。返回 `Result: 5/5`
且进程退出码为 0 才算通过；单个节点失败时会继续测试其余节点，以便一次收集完整诊断。

统一离线验收脚本会把 pytest 临时目录放到项目 `tmp/`，并在运行期间临时设置
`TEMP/TMP`，以兼容禁止写用户临时目录的受管环境；脚本结束时会恢复调用者原值。

## 5. AMR Agent API

当前 API 入口为 `apps.api.main:app`，默认使用 8000 端口。

API 默认在启动生命周期中执行同一模型 alias 门禁。因此必须先启动并通过模型网关预检；模型不可达或 alias 错误时，API 会拒绝启动。

开发模式启动：

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
& 'E:\Anaconda\envs\torch128\python.exe' -m uvicorn apps.api.main:app `
  --host 127.0.0.1 `
  --port 8000 `
  --reload
```

健康检查：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
```

正常结果：

```json
{"status":"ok"}
```

API 文档：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

停止 API：在运行 Uvicorn 的窗口按 `Ctrl+C`。

## 6. Python 环境约定

所有项目 Python 命令都显式使用以下解释器，不依赖系统 PATH：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' --version
```

后续代码统一采用以下服务配置名；当前最小 API 尚未全部读取这些变量：

```powershell
$env:OPENAI_BASE_URL = 'http://127.0.0.1:8080/v1'
$env:OPENAI_API_KEY = 'dummy'
$env:LLM_PROFILE = 'fast'  # Smart 当前硬禁用，不得改为 smart
$env:LLM_MODEL = 'qwen3.6-fast'
$env:POSTGRES_DSN = 'postgresql://amr:123456@localhost:5432/amr_agent'
$env:QDRANT_URL = 'http://localhost:6333'
$env:RAG_EMBEDDING_MODEL_PATH = 'E:\Llama.cpp\Embedding'
$env:RAG_MINIMUM_HYBRID_SCORE = '0.809'
$env:RAG_MINIMUM_VECTOR_SCORE = '0.499'
```

这些环境变量只对当前 PowerShell 会话生效。完整 RAG 配置和阈值校准见 [`RAG.md`](RAG.md)。

## 7. C++ 开发环境

C++ 工具链不是常驻服务。需要编译或测试规划器时，单独打开一个 `cmd.exe` 开发终端。

从普通 `cmd.exe` 初始化 MSVC x64 环境：

```bat
call E:\BuildingTools\VC\Auxiliary\Build\vcvars64.bat
cd /d C:\Users\QYC\Documents\AMR_Agent
cl
```

也可以从 PowerShell 直接打开并保持一个已初始化的开发终端：

```powershell
cmd.exe /k ""E:\BuildingTools\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 && cd /d C:\Users\QYC\Documents\AMR_Agent"
```

固定工具位置：

```text
MSVC:  E:\BuildingTools\VC\Tools\MSVC\14.51.36231\bin\Hostx64\x64\cl.exe
CMake: E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe
Ninja: E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe
```

项目根目录出现 `CMakeLists.txt` 后，推荐使用独立构建目录：

```bat
"E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" -S . -B build\cpp -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_MAKE_PROGRAM="E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
"E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" --build build\cpp
ctest --test-dir build\cpp --output-on-failure
```

P0-08 构建后会生成 `build\cpp\services\planner_cpp\task_allocator_cli.exe`。它只通过 JSON stdin/stdout 工作：

```powershell
Get-Content .\request.json -Raw | .\build\cpp\services\planner_cpp\task_allocator_cli.exe --algorithm hungarian
Get-Content .\request.json -Raw | .\build\cpp\services\planner_cpp\task_allocator_cli.exe --algorithm nearest_idle
```

字段、`INF` sentinel、不可行原因码和退出码见 [`TASK_ALLOCATOR.md`](TASK_ALLOCATOR.md)。在 `CMakeLists.txt` 尚未创建时，不执行上述构建命令。

P0-09 构建后会生成 `build\cpp\services\planner_cpp\route_planner_cli.exe`。A* 是生产算法，Dijkstra 只用于正确性基线；两者都只通过 JSON stdin/stdout 工作：

```powershell
Get-Content .\route_request.json -Raw | .\build\cpp\services\planner_cpp\route_planner_cli.exe --algorithm astar
Get-Content .\route_request.json -Raw | .\build\cpp\services\planner_cpp\route_planner_cli.exe --algorithm dijkstra
```

路线字段、预约冲突规则、失败退出码和 `infeasible` 业务结果见 [`ROUTE_PLANNER.md`](ROUTE_PLANNER.md)。

P0-10 构建后会生成 `build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe`。验证器不读取 Prompt，也不信任路线规划器或调用方声明的“已验证”字段；它对完整计划重新执行硬约束检查：

```powershell
Get-Content .\fleet_plan_request.json -Raw | .\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --validate
Get-Content .\fleet_plan_request.json -Raw | .\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --error-dictionary
```

业务非法计划使用退出码 `0` 并在 JSON 中返回 `status=invalid`、稳定错误码和定位证据；输入契约/参数错误使用退出码 `2`，内部错误使用退出码 `3`。请求字段、错误字典、证据结构和离散时间边界见 [`FLEET_PLAN_VALIDATOR.md`](FLEET_PLAN_VALIDATOR.md)。

## 7.1 P0-11 Python 仿真

P0-11 不启动常驻服务。调用方在已构建 P0-10 CLI 的项目根目录运行：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p011_simulator.py -q
```

业务调用使用 `services.amr_simulator.AMRSimulator`。它会先以固定可执行文件和
JSON stdin 调用 P0-10；Validator 不是 `valid` 时仿真立即失败，不会回退到 Python
自判。通过后按 P0-09 `path[*].time` 推进 1 秒 tick，输出 P0-04 Observation、
事件日志、充电站快照和可选 Eval 故障注入。完整边界见 [`AMR_SIMULATOR.md`](AMR_SIMULATOR.md)。

## 7.2 P0-12 白名单工具

P0-12 不启动新的常驻服务。固定 C++ 产物和 Python 依赖准备好后，从项目根目录
构造统一注册表：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -c "from agent.tools import build_tool_registry; print([item.tool_name.value for item in build_tool_registry().specs()])"
```

输出必须恰好包含九个工具：`retrieve_knowledge`、`get_fleet_state`、
`allocate_tasks`、`plan_multi_amr_routes`、`validate_fleet_plan`、
`dispatch_simulation`、`query_execution_state`、`run_verification_suite`、
`request_approval`。分配、A*、Validator 的 Python 适配器只会调用本节前文列出的
三个固定 exe，使用 `shell=False`、JSON stdin/stdout 和工具级超时；没有固定构建产物
时工具返回 `unavailable`，不会改用 PATH 中的同名程序或任意命令。

运行 P0-12 专项契约/安全测试：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m pytest tests\unit\test_p012_tools.py -q
```

详细 Schema、角色、幂等、副作用和失败映射见 [`P012_TOOLS.md`](P012_TOOLS.md)。
`retrieve_knowledge` 只有实际调用时才连接 Qdrant/Embedding；默认状态/审批存储是
进程内适配器，后续可在组装注册表时替换为 P0-06/P0-16 实现。

## 8. 一次开发会话的最小检查清单

按顺序执行并确认全部通过：

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'

docker compose -f .\compose.yaml ps
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\check_postgres.py'
& 'E:\Anaconda\envs\torch128\python.exe' '.\scripts\check_qdrant.py'

Invoke-RestMethod 'http://127.0.0.1:8080/health'
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id

Invoke-RestMethod 'http://127.0.0.1:8000/health'
```

通过标准：

- `amr-postgres` 和 `amr-qdrant` 状态为运行中。
- PostgreSQL 检查返回 `(1,)`。
- Qdrant 可以返回集合列表。
- LLM `/health` 成功，模型别名正确。
- API `/health` 返回 `{"status":"ok"}`。

## 9. 推荐停止顺序

1. 在 API 窗口按 `Ctrl+C`。
2. 在模型窗口按 `Ctrl+C`，等待模型进程退出。
3. 停止数据库容器：

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
docker compose -f .\compose.yaml stop postgres qdrant
```

## 10. 常见故障定位

### 10.1 端口被占用

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5432,6333,8080,8000 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

查看进程身份后再决定是否停止，不要仅凭端口直接结束未知进程：

```powershell
Get-Process -Id <OwningProcess>
```

### 10.2 Docker 服务异常

```powershell
docker compose -f .\compose.yaml ps
docker compose -f .\compose.yaml logs --tail 200 postgres
docker compose -f .\compose.yaml logs --tail 200 qdrant
```

### 10.3 模型端口占用或别名错误

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id
```

如果运行的是错误模型，回到模型窗口按 `Ctrl+C`，确认 8080 已释放，再启动目标脚本。

### 10.4 API 导入失败

确认没有误用系统 Python：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -c "import fastapi, uvicorn, openai, psycopg, qdrant_client; print('imports ok')"
```

不要在未确认解释器路径的情况下直接执行 `python` 或 `pip`。
