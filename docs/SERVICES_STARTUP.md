# AMR Agent P0 服务启动手册

本文档固化 P0 开发阶段所需服务、固定路径、端口、健康检查、启动顺序与故障排查。所有命令默认在 Windows PowerShell 中执行；特别标注为 `cmd.exe` 的命令除外。

基准日期：2026-08-21
项目根目录：仓库根目录（本机绝对路径见 [`LOCAL_ENV.md`](LOCAL_ENV.md)）

## 1. 固定环境与端口

本机 Python、MSVC/CMake/Ninja、llama.cpp 与 Embedding 的绝对路径见 [`LOCAL_ENV.md`](LOCAL_ENV.md)。下表只列端口与仓库内入口。

| 组件 | 位置或地址 | 用途 |
|---|---|---|
| Python | `python`（或 `$env:AMR_PYTHON_EXE`） | API、Agent、数据库检查与评测 |
| C++ 构建 | `scripts/run_smoke.ps1` 初始化 MSVC 后调用 CMake/Ninja | MSVC、CMake、Ninja、CTest |
| Fast 模型 | `.\scripts\start_local.ps1 -StartFast` | 默认开发与主评测模型 |
| Smart 模型 | 暂时禁用 | 保留 alias，不得启动 |
| LLM API | `http://127.0.0.1:8080/v1` | OpenAI 兼容接口 |
| PostgreSQL | `localhost:5432` | 运行状态、Checkpoint、Effect Ledger |
| Qdrant | `http://localhost:6333` | SOP 和设备文档向量检索 |
| Embedding | `$env:RAG_EMBEDDING_MODEL_PATH` | Qwen3-Embedding-0.6B；不占用 8080 |
| AMR Agent API | `http://127.0.0.1:8000` | 当前 FastAPI 服务入口 |

模型别名：

- Fast：`qwen3.6-fast`
- Smart：`qwen3.8-smart`（保留标识，当前 `enabled=false`）

Fast 与 Smart 的 llama.cpp 入口都绑定 `127.0.0.1:8080`。当前只允许启动 Fast；不要启动 Smart，也不要仅靠
`LLM_PROFILE=smart` 尝试绕过配置。日后收到用户明确指示并恢复 Smart 时，仍必须保证
一次只常驻一个模型，切换前先确认 8080 已释放。

## 2. P0-20 最简启动

P0-20 将 API、PostgreSQL 和 Qdrant 收进同一份 Compose 配置；Qwen3.6 Fast 仍只由现有 Windows 脚本在宿主机启动，不复制模型文件进镜像，也不依赖远程模型。推荐从仓库根目录执行：

```powershell
# 在仓库根目录执行
.\scripts\start_local.ps1
```

需要进行真实 Fast 闭环时，在同一命令中额外启动宿主机 Fast：

```powershell
.\scripts\start_local.ps1 -StartFast
```

脚本会依次检查 Docker Engine，启动 `postgres`、`qdrant`、`api`，等待 `/readyz` 和 `/health`，运行 PostgreSQL/Qdrant 检查；`-StartFast` 会调用仓库内 `scripts/start_fast_secure.ps1`（校验 GGUF 哈希后启动 `127.0.0.1:18080` llama-server，再在 `127.0.0.1:8080` 提供强制 Bearer 代理），并运行模型网关预检。不要把开放 CORS、无 API key 的旧 llama.cpp 启动脚本当作发布入口。Compose API 默认将 `MODEL_GATEWAY_VALIDATE_ON_STARTUP=false`，因为容器中的 `host.docker.internal:8080` 与宿主机回环地址不是可靠的启动期依赖；真正需要模型的链路必须在宿主机执行 Fast 网关预检。若要测试 API 的严格模型门禁，应显式设置 `MODEL_GATEWAY_VALIDATE_ON_STARTUP=true` 并先让 Fast 健康。

一键脚本不启动 Smart；Smart 的 `qwen3.8-smart` 仍处于硬禁用状态。

## 3. 推荐启动顺序

每次完整开发建议按以下顺序启动：

1. 进入项目根目录。
2. 确认 Docker Engine 可用。
3. 用 `scripts\start_local.ps1` 启动并检查 PostgreSQL、Qdrant 和 Compose API。
4. 需要真实模型闭环时，在独立终端启动 Fast 模型；Smart 当前不得启动。
5. 在宿主机运行 Fast 网关预检，再运行 P0-13 演示入口或其他模型依赖测试。
6. 需要编译规划器时，再打开已初始化的 C++ 开发终端。

先打开 PowerShell 并进入项目目录：

```powershell
# 在仓库根目录执行
```

## 4. PostgreSQL、Qdrant 与 Compose API

数据库服务由项目根目录的 `compose.yaml` 管理：

- Compose 服务名：`postgres`、`qdrant`、`api`
- 容器名：`amr-postgres`、`amr-qdrant`、`amr-api`

### 4.1 启动

确认 Docker Desktop 已启动后执行：

```powershell
docker version
docker compose -f .\compose.yaml up -d --build postgres qdrant api
docker compose -f .\compose.yaml ps
```

如果 `docker version` 无法连接 Server，先启动 Docker Desktop，等待 Engine 就绪后重试。`api` 只有在 PostgreSQL 和 Qdrant 健康后才会启动，并在容器内执行前向数据库迁移。

### 4.2 PostgreSQL 连接信息

| 字段 | 值 |
|---|---|
| Host | `localhost` |
| Port | `5432` |
| Database | `amr_agent` |
| User | `amr` |
| Password | 从仓库根目录 gitignore 的 `.env` 读取 `POSTGRES_PASSWORD`；禁止继续使用已公开的 `123456` |

本地连通性以 `scripts/check_postgres.py` 为准，该脚本通过 `load_settings()` 读取 `.env` 与 `POSTGRES_DSN`，不会在输出中打印密码。

执行只读连通性检查：

```powershell
python '.\scripts\check_postgres.py'
```

正常输出应包含：

```text
(1,)
```

首次启动或拉取到新 migration 后，执行只向前迁移和表检查：

```powershell
python '.\scripts\migrate_database.py' upgrade
python '.\scripts\migrate_database.py' check
```

成功报告必须包含 8 张核心表、`missing_core_tables: []`，辅助表只能包含 Alembic 的 `alembic_version`。脚本不提供 downgrade；不要通过手工删表代替前向修复 migration。表结构和事务说明见 [`DATABASE.md`](DATABASE.md)。

### 4.3 Qdrant 检查

```powershell
Invoke-WebRequest -UseBasicParsing 'http://localhost:6333/readyz'
python '.\scripts\check_qdrant.py'
```

Qdrant Dashboard：`http://localhost:6333/dashboard`

### 4.4 P0-07 知识索引与评测

Embedding 直接从本地目录加载，不需要启动 Fast/Smart 文本模型。PostgreSQL 与 Qdrant 健康后执行：

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
python '.\scripts\index_warehouse_knowledge.py'
python -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

索引成功应报告 6 份文档、70 个当前证据 chunk 和动态读取的 1024 维；评测的 `acl_leak_count` 必须为 0。语料或 chunk 规则变化后数量可能变化，应以索引报告与 `docs/RAG.md` 为准，不能把 70/1024 写进运行时代码。

### 4.5 日志与停止

查看最近日志：

```powershell
docker compose -f .\compose.yaml logs --tail 100 api postgres qdrant
```

日常停止，保留容器和数据卷：

```powershell
docker compose -f .\compose.yaml stop api postgres qdrant
```

需要移除容器和 Compose 网络时：

```powershell
docker compose -f .\compose.yaml down
```

不要执行 `docker compose down -v`，该命令会删除 PostgreSQL 与 Qdrant 数据卷。

## 5. 本地模型服务

### 5.1 Fast：Qwen3.6

Fast 是 P0 默认模型，使用别名 `qwen3.6-fast`：

```powershell
.\scripts\start_local.ps1 -StartFast
```

脚本会在当前窗口拉起鉴权代理与 llama-server。保持窗口开启；停止时在该窗口按 `Ctrl+C`，或按启动器说明精确结束 PID。
 

### 5.2 Smart：Qwen3.8（暂时禁用）

Smart 的脚本和别名 `qwen3.8-smart` 仅为日后重新验收保留。最近一次真实 P0-05
五节点在线验收只有 2/5，因此仓库配置明确设为 `enabled=false`。在用户日后给出明确
启用指示之前：

- 不启动 Smart 模型脚本（路径见 [`LOCAL_ENV.md`](LOCAL_ENV.md)）；
- 不修改 `config/default.toml` 的 Smart `enabled=false`；
- 不把 alias 预检或单条结构化响应记作 Smart 在线验收通过。

`check_model_gateway.py --profile smart` 当前预期返回非零退出码和
`MODEL_PROFILE_DISABLED`，而且门禁发生在 `/v1/models` 之前。

### 5.3 模型健康检查

模型加载可能需要一段时间。加载完成后，在另一个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/health'
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id
```

随后执行项目统一模型网关预检。该检查会校验当前 Profile、服务实际 alias 和版本记录；alias 不一致时返回非零退出码：

```powershell
python '.\scripts\check_model_gateway.py'
```

必须确认返回的模型别名与本次配置一致：

- Fast 运行时必须看到 `qwen3.6-fast`。
- Smart 当前不能进入在线 alias 检查；选择它必须得到 `MODEL_PROFILE_DISABLED`。

Fast 模型可执行现有 20 次结构化输出冒烟测试：

```powershell
python '.\scripts\smoke_llm_structured.py'
```

当前冒烟脚本固定使用 `qwen3.6-fast`，不要在 Smart 模型运行时直接执行。

P0-05 五个 2-shot Prompt 的真实节点测试：

```powershell
python '.\scripts\smoke_p005_prompts.py' --profile fast
```

该命令会再次执行模型别名门禁，然后按顺序验证五个独立节点。返回 `Result: 5/5`
且进程退出码为 0 才算通过；单个节点失败时会继续测试其余节点，以便一次收集完整诊断。

统一离线验收脚本会把 pytest 临时目录放到项目 `tmp/`，并在运行期间临时设置
`TEMP/TMP`，以兼容禁止写用户临时目录的受管环境；脚本结束时会恢复调用者原值。

## 6. AMR Agent API

当前 API 入口为 `apps.api.main:app`，默认使用 8000 端口。

有两种 API 启动方式：P0-20 推荐的 Compose API 默认关闭启动期模型门禁，让 API、数据库和 Qdrant 可以先独立健康；宿主机开发 Uvicorn 默认保留启动期 alias 门禁，必须先启动并通过 Fast 网关预检。两种方式遇到模型不可达或 alias 错误时，严格门禁都会拒绝启动。

Compose API：

```powershell
docker compose -f .\compose.yaml up -d --build api
Invoke-RestMethod 'http://127.0.0.1:8000/health'
```

Compose API 使用 `host.docker.internal:8080` 作为 Fast 的可选 OpenAI 兼容地址；自然语言 P0-13 演示入口仍在宿主机运行，以便完整复用 Windows 模型、Embedding、C++ CLI 和仿真进程。

开发模式启动：

```powershell
# 在仓库根目录执行
python -m uvicorn apps.api.main:app `
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

## 7. Python 环境约定

所有项目 Python 命令默认写 `python`。本机解释器绝对路径见 [`LOCAL_ENV.md`](LOCAL_ENV.md)；PATH 不是项目环境时先设置 `$env:AMR_PYTHON_EXE`。

```powershell
python --version
```

后续代码统一采用以下服务配置名；当前最小 API 尚未全部读取这些变量：

```powershell
$env:OPENAI_BASE_URL = 'http://127.0.0.1:8080/v1'
$env:OPENAI_API_KEY = 'dummy'  # 发布/Compose 必须换成 .env 中至少 32 字符的独立密钥
$env:LLM_PROFILE = 'fast'  # Smart 当前硬禁用，不得改为 smart
$env:LLM_MODEL = 'qwen3.6-fast'
# POSTGRES_DSN / QDRANT_API_KEY / RAG_EMBEDDING_MODEL_PATH 从 .env 加载
$env:QDRANT_URL = 'http://localhost:6333'
$env:RAG_MINIMUM_HYBRID_SCORE = '0.809'
$env:RAG_MINIMUM_VECTOR_SCORE = '0.499'
```

这些环境变量只对当前 PowerShell 会话生效。完整 RAG 配置和阈值校准见 [`RAG.md`](RAG.md)。

## 8. C++ 开发环境

C++ 工具链不是常驻服务。需要编译或测试规划器时，先按 [`LOCAL_ENV.md`](LOCAL_ENV.md) 初始化 MSVC x64 开发环境，再在仓库根目录使用 PATH 中的 `cmake`/`ninja`/`ctest`：

```powershell
cmake -S . -B build\cpp -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build\cpp
ctest --test-dir build\cpp --output-on-failure
```

日常验收优先走 `.\scripts\run_smoke.ps1`，它会自行导入 MSVC 并构建/运行 CTest。

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

## 8.1 P0-11 Python 仿真

P0-11 不启动常驻服务。调用方在已构建 P0-10 CLI 的项目根目录运行：

```powershell
python -m pytest tests\unit\test_p011_simulator.py -q
```

业务调用使用 `services.amr_simulator.AMRSimulator`。它会先以固定可执行文件和
JSON stdin 调用 P0-10；Validator 不是 `valid` 时仿真立即失败，不会回退到 Python
自判。通过后按 P0-09 `path[*].time` 推进 1 秒 tick，输出 P0-04 Observation、
事件日志、充电站快照和可选 Eval 故障注入。完整边界见 [`AMR_SIMULATOR.md`](AMR_SIMULATOR.md)。

## 8.2 P0-12 白名单工具

P0-12 不启动新的常驻服务。固定 C++ 产物和 Python 依赖准备好后，从项目根目录
构造统一注册表：

```powershell
python -c "from agent.tools import build_tool_registry; print([item.tool_name.value for item in build_tool_registry().specs()])"
```

输出必须恰好包含九个工具：`retrieve_knowledge`、`get_fleet_state`、
`allocate_tasks`、`plan_multi_amr_routes`、`validate_fleet_plan`、
`dispatch_simulation`、`query_execution_state`、`run_verification_suite`、
`request_approval`。分配、A*、Validator 的 Python 适配器只会调用本节前文列出的
三个固定 exe，使用 `shell=False`、JSON stdin/stdout 和工具级超时；没有固定构建产物
时工具返回 `unavailable`，不会改用 PATH 中的同名程序或任意命令。

运行 P0-12 专项契约/安全测试：

```powershell
python -m pytest tests\unit\test_p012_tools.py -q
```

详细 Schema、角色、幂等、副作用和失败映射见 [`P012_TOOLS.md`](P012_TOOLS.md)。
`retrieve_knowledge` 只有实际调用时才连接 Qdrant/Embedding；默认状态/审批存储是
进程内适配器，后续可在组装注册表时替换为 P0-06/P0-16 实现。

## 9. 一次开发会话的最小检查清单

按顺序执行并确认全部通过：

```powershell
# 在仓库根目录执行

docker compose -f .\compose.yaml ps
python '.\scripts\check_postgres.py'
python '.\scripts\check_qdrant.py'

Invoke-RestMethod 'http://127.0.0.1:6333/readyz'
Invoke-RestMethod 'http://127.0.0.1:8000/health'
```

通过标准：

- `amr-postgres` 和 `amr-qdrant` 状态为运行中。
- `amr-api` 状态为 `healthy`，并已完成前向迁移。
- PostgreSQL 检查返回 `(1,)`。
- Qdrant 可以返回集合列表。
- API `/health` 返回 `{"status":"ok"}`。
- 只有启动 Fast 后，才要求 LLM `/health` 成功且模型别名为 `qwen3.6-fast`；未启动 Fast 时跳过两条 8080 检查。

启动 Fast 后再补充：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/health'
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id
```

## 10. 推荐停止顺序

1. 如果 Fast 已启动，在模型窗口按 `Ctrl+C`，等待模型进程退出。
2. 停止 Compose 服务，保留数据卷：

```powershell
# 在仓库根目录执行
docker compose -f .\compose.yaml stop api postgres qdrant
```

不要执行 `docker compose down -v`；这会删除 PostgreSQL 与 Qdrant 数据卷。

## 11. 常见故障定位

### 11.1 端口被占用

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5432,6333,8080,8000 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

查看进程身份后再决定是否停止，不要仅凭端口直接结束未知进程：

```powershell
Get-Process -Id <OwningProcess>
```

### 11.2 Docker 服务异常

```powershell
docker compose -f .\compose.yaml ps
docker compose -f .\compose.yaml logs --tail 200 api
docker compose -f .\compose.yaml logs --tail 200 postgres
docker compose -f .\compose.yaml logs --tail 200 qdrant
```

若 `api` 反复重启，先看迁移错误和依赖状态：

```powershell
docker compose -f .\compose.yaml logs --tail 200 api
docker compose -f .\compose.yaml ps
python '.\scripts\migrate_database.py' check
```

若 Qdrant 显示 `unhealthy`，先直接检查 `readyz`，再确认容器日志；镜像不保证带 `curl` 或 `wget`，Compose 健康检查使用镜像内置 Bash 的 TCP 请求，不要把缺少 `curl` 当成服务故障。

### 11.3 Compose API 访问宿主 Fast 失败

容器中的 `127.0.0.1` 指向 API 容器自身。Compose 已通过 `extra_hosts` 提供 `host.docker.internal`，但模型依然必须在 Windows 宿主机绑定 `127.0.0.1:8080`，且 API 的启动期模型门禁默认关闭。模型依赖链路请在宿主机先运行：

```powershell
python '.\scripts\check_model_gateway.py' --profile fast
```

### 11.4 模型端口占用或别名错误

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080
(Invoke-RestMethod 'http://127.0.0.1:8080/v1/models').data | Select-Object id
```

如果运行的是错误模型，回到模型窗口按 `Ctrl+C`，确认 8080 已释放，再启动目标脚本。

### 11.5 API 导入失败

确认没有误用系统 Python：

```powershell
python -c "import fastapi, uvicorn, openai, psycopg, qdrant_client; print('imports ok')"
```

不要在未确认解释器路径的情况下直接执行 `python` 或 `pip`。
