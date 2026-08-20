# P0-01 / P0-03 文件导览与学习顺序

本文记录 P0-01（工程骨架）和 P0-03（统一模型网关）期间新建、修改及自动生成的文件。建议先按“推荐阅读顺序”理解主链，再把测试文件作为可运行示例阅读。

## 1. 推荐阅读顺序

1. `config/default.toml`：先看系统有哪些可配置项，以及 Fast/Smart 的差异。
2. `services/config/settings.py`：理解配置如何按优先级合并并由 Pydantic 校验。
3. `services/model_gateway/contracts.py`：理解业务层与模型网关之间传递什么数据。
4. `services/model_gateway/exceptions.py`：理解网关如何把第三方错误归一化。
5. `services/model_gateway/protocols.py`：理解业务层为什么不直接依赖 llama.cpp/OpenAI SDK。
6. `services/model_gateway/provider.py`：阅读启动门禁、模型调用、Schema 校验和一次修复主流程。
7. `apps/api/main.py`：理解 FastAPI 如何在生命周期启动阶段执行模型门禁。
8. `tests/unit/test_model_provider.py`：通过可运行用例观察正常、错误 alias 和 Schema 修复行为。
9. `scripts/run_smoke.ps1`：理解 Python 与 C++ 验收如何由一条命令串联。
10. `CMakeLists.txt`、`services/planner_cpp/`：理解 C++17 空工程和 CTest 入口。

## 2. 两条核心执行链

### 2.1 API 启动链

```text
Uvicorn 导入 apps.api.main:app
  → create_app()
  → load_settings()
  → 创建 ModelProvider
  → FastAPI lifespan 启动
  → provider.startup()
  → 请求 llama.cpp /v1/models
  → 检查“只暴露一个模型 + alias 精确匹配”
  → 保存 ModelVersionRecord
  → API 开始接受请求
```

服务不可达、同时暴露多个模型或 alias 错误时，异常会从 lifespan 继续向上抛出，Uvicorn 因而拒绝启动。

### 2.2 结构化生成链

```text
generate_structured(messages, PydanticModel)
  → 把消息转换为严格 ChatMessage
  → 从 PydanticModel 导出 JSON Schema
  → 组装白名单 OpenAI 请求
  → llama.cpp 生成文本
  → Pydantic model_validate_json 校验
  → 成功：返回 StructuredGeneration
  → 失败：附加错误与 Schema，最多修复一次
  → 再失败：抛出 StructuredOutputError，禁止无限循环
```

## 3. 新建文件

### 3.1 项目根目录与配置

| 文件 | 用途 |
|---|---|
| `.gitignore` | 忽略 Python 缓存、测试缓存、构建目录、日志、临时输出和本机 `.env`。 |
| `.env.example` | 列出可用环境变量及本机开发示例；不存放真实密钥。 |
| `README.md` | 项目入口、当前阶段状态和最常用检查命令。 |
| `pyproject.toml` | Python 项目元数据、Python 3.12 约束、依赖范围、pytest 与 setuptools 配置。 |
| `requirements.in` | 人工维护的直接运行依赖范围。 |
| `requirements.lock` | 从当前 `torch128` 环境记录的精确运行依赖版本。 |
| `requirements-dev.lock` | 精确测试依赖版本。 |
| `config/default.toml` | 应用、日志、模型 Profile、PostgreSQL 和 Qdrant 的默认配置。 |
| `CMakeLists.txt` | C++ 顶层工程，固定 C++17、启用 CTest，并加载规划器子目录。 |
| `CMakePresets.json` | Windows + Ninja + Release 的标准 CMake 配置、构建和测试预设。 |

### 3.2 Python 包骨架

| 文件 | 用途 |
|---|---|
| `apps/__init__.py` | 声明应用入口包。 |
| `apps/api/__init__.py` | 声明 FastAPI 子包。 |
| `agent/__init__.py` | 声明受控 Agent 主包。 |
| `agent/runtime/__init__.py` | 为后续 LangGraph 状态图和 Checkpoint 预留包。 |
| `agent/planning/__init__.py` | 为后续 TaskContract、Planner、Validator、Replanner 预留包。 |
| `agent/tools/__init__.py` | 为后续白名单工具注册表和路由预留包。 |
| `domains/__init__.py` | 声明领域模型主包。 |
| `domains/amr_warehouse/__init__.py` | 声明 AMR 仓储领域包。 |
| `services/__init__.py` | 声明基础服务主包。 |
| `services/retrieval/__init__.py` | 为 P0-07 混合检索和 ACL 预留包。 |
| `services/amr_simulator/__init__.py` | 为 P0-11 离散事件仿真预留包。 |
| `services/validation/__init__.py` | 为受控测试、日志解析和证据报告预留包。 |

这些 `__init__.py` 大多没有业务逻辑，主要作用是形成稳定的 Python 导入路径。学习时快速浏览即可。

### 3.3 配置与日志

| 文件 | 用途 |
|---|---|
| `services/config/__init__.py` | 对外导出 `AppSettings` 和 `load_settings`。 |
| `services/config/settings.py` | Pydantic 配置模型、五层配置合并、环境变量映射、别名一致性和安全字段校验。 |
| `services/observability/__init__.py` | 对外导出日志配置和 logger 工厂。 |
| `services/observability/logging.py` | 统一 structlog/标准 logging，输出 JSON 或开发控制台格式。 |

### 3.4 模型网关

| 文件 | 用途 |
|---|---|
| `services/model_gateway/__init__.py` | 汇总并导出网关最常用的公共类型。 |
| `services/model_gateway/contracts.py` | 定义消息、Token 用量、版本记录、健康检查和结构化生成结果。 |
| `services/model_gateway/exceptions.py` | 定义稳定错误码和异常层次，供上层确定 retry/fallback/fatal。 |
| `services/model_gateway/protocols.py` | 定义业务层依赖的 `ModelProviderProtocol`，隔离底层 SDK。 |
| `services/model_gateway/provider.py` | P0-03 核心实现：OpenAI 客户端、启动 alias 门禁、超时、固定推理参数、版本记录、普通/结构化生成和一次修复。 |

### 3.5 C++ 骨架

| 文件 | 用途 |
|---|---|
| `services/planner_cpp/CMakeLists.txt` | 定义 C++17 冒烟程序、编译警告、版本宏和 CTest 用例。 |
| `services/planner_cpp/src/main.cpp` | 提供 `--version` 和 `--self-test` 两个白名单入口，证明 C++ 工程可编译、可执行、可测试。 |

这里还不是 Hungarian/A*/车队验证器；这些算法属于后续 P0-08～P0-10。

### 3.6 检查与运行脚本

| 文件 | 用途 |
|---|---|
| `scripts/check_environment.py` | 一条命令输出 Python、锁定依赖、CMake、Ninja 和 MSVC 的版本匹配报告。 |
| `scripts/check_model_gateway.py` | 对 Fast 或 Smart 执行真实 `/v1/models`、单模型和 alias 门禁。 |
| `scripts/run_smoke.ps1` | 串联环境检查、全部 pytest、MSVC 初始化、CMake/Ninja 构建和 CTest。 |

### 3.7 测试

| 文件 | 用途 |
|---|---|
| `tests/__init__.py` | 声明测试包。 |
| `tests/unit/__init__.py` | 声明单元/契约测试包。 |
| `tests/unit/fakes.py` | 模拟 OpenAI SDK 的 models 和 chat.completions 响应，不需要真实模型。 |
| `tests/unit/test_settings.py` | 验证配置优先级、Smart Profile、alias 冲突和最多一次修复的硬限制。 |
| `tests/unit/test_logging.py` | 验证日志确实输出可解析 JSON 及必要上下文字段。 |
| `tests/unit/test_model_provider.py` | 验证 alias、单模型、超时参数、安全请求面、结构化输出和一次修复。 |
| `tests/unit/test_api.py` | 验证 API 健康检查、错误 alias 拒绝启动和模型健康接口。 |
| `tests/smoke/__init__.py` | 声明 Python 冒烟测试包。 |
| `tests/smoke/test_python_smoke.py` | 验证主要包可导入，并能在隔离模式创建 FastAPI 应用。 |

### 3.8 文档与占位目录

| 文件 | 用途 |
|---|---|
| `docs/PROJECT_SETUP.md` | P0-01 目录、配置、依赖和 Python/C++ 检查说明。 |
| `docs/MODEL_GATEWAY.md` | P0-03 边界、门禁、超时、推理参数和结构化输出说明。 |
| `docs/P001_P003_FILE_GUIDE.md` | 本文件：完整变更导览和推荐学习顺序。 |
| `evals/README.md` | 说明评测目录后续保存哪些数据和安全约束。 |
| `infra/README.md` | 说明基础设施目录与当前 Compose/Windows 模型服务的边界。 |

## 4. 修改的原有文件

| 文件 | 原来 | 现在 |
|---|---|---|
| `apps/api/main.py` | 只有最小 `/health`。 | 改为应用工厂、类型化配置、依赖注入、FastAPI lifespan 模型门禁、基础健康和模型健康接口。 |
| `scripts/smoke_llm_structured.py` | 直接创建 OpenAI 客户端并手工解析 JSON。 | 改为通过统一 ModelProvider、Pydantic Schema 和 alias 门禁运行 Fast/Smart 重复测试。 |
| `docs/SERVICES_STARTUP.md` | 记录 P0-02 服务启动方式。 | 增加模型网关预检命令，并说明 API 默认在启动时执行 alias 门禁。 |

## 5. 原有但未修改的文件

以下文件仍属于此前手动完成的 P0-00 / P0-02 或数据库准备，本次没有改动其内容：

- `compose.yaml`
- `Start_End_Database.txt`
- `docs/AMR_Agent_P0技术路线与实施ToDo.docx`
- `docs/scope.md`
- `docs/scope_changes.md`
- `docs/backlog.md`
- `domains/amr_warehouse/data/warehouse_v1.json`
- `domains/amr_warehouse/data/amrs_v1.json`
- `domains/amr_warehouse/data/orders_seed_v1.json`
- `scripts/check_postgres.py`
- `scripts/check_qdrant.py`

## 6. 自动生成物，不需要作为源码学习

| 路径 | 来源 | 是否应提交 |
|---|---|---|
| `build/cpp/` | CMake/Ninja 构建结果。 | 否，已被 `.gitignore` 忽略。 |
| `**/__pycache__/`、`*.pyc` | Python 导入和测试缓存。 | 否。 |
| `.pytest_cache/` | pytest 缓存。 | 否。 |
| `tmp/` | 文档检查及临时文件。 | 否。 |
| `output/` | 本地运行输出。 | 默认不提交，后续正式评测报告应按版本管理策略另行决定。 |

## 7. 本机 Python 环境中补装的包

这些是环境变化，不是项目文件：

- `rank-bm25==0.2.2`
- `jieba==0.42.1`
- `PyJWT==2.13.0`
- `structlog==26.1.0`
- `sse-starlette==3.4.8`
- `pytest-asyncio==1.4.0`

精确版本已经写入 `requirements.lock` 或 `requirements-dev.lock`。

