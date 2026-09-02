# 本机开发环境路径

本文件记录当前开发机上的绝对路径，供本机脚本、评测和交接使用。公开说明文档（`README.md`、`PROJECT_OVERVIEW.md`、启动手册、专题文档）只写仓库相对路径、端口和环境变量名，不重复这些盘符路径。

复制根目录 `.env.example` 为 `.env` 后，按本表填写；也可只设置 `AMR_PYTHON_EXE`、`FAST_ARTIFACT_ROOT`、`RAG_EMBEDDING_MODEL_PATH`。仓库脚本在未设置环境变量时仍可能回退到下表默认值，那只适配本机，不是跨机器约定。

本步无核心代码注释需求。

## 当前开发机（2026-09-02）

| 项目 | 路径或地址 |
|---|---|
| 项目根目录 | `C:\Users\QYC\Documents\AMR_Agent` |
| Python | `E:\Anaconda\envs\torch128\python.exe`（Python 3.12.13） |
| CMake | `E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe` |
| Ninja | `E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe` |
| MSVC 初始化 | `E:\BuildingTools\Common7\Tools\VsDevCmd.bat` |
| MSVC `cl` | `E:\BuildingTools\VC\Tools\MSVC\14.51.36231\bin\Hostx64\x64\cl.exe` |
| Fast 模型脚本 | `E:\Llama.cpp\start-qwen3.6-agent.cmd` |
| Smart 模型脚本 | `E:\Llama.cpp\start-qwen3.8-agent.cmd`（暂时禁用，不得启动） |
| llama-server | `E:\Llama.cpp\llama-server.exe` |
| Embedding 模型 | `E:\Llama.cpp\Embedding`（Qwen3-Embedding-0.6B） |

发布/演示启动不要直接跑上表 Fast cmd；应使用仓库内 `scripts/start_local.ps1 -StartFast`（内部走 `scripts/start_fast_secure.ps1`，校验 GGUF 哈希后启动 `127.0.0.1:18080` llama-server，再在 `127.0.0.1:8080` 提供强制 Bearer 代理）。

## 不随机器变化的服务地址

| 项目 | 地址 |
|---|---|
| 模型 API | `http://127.0.0.1:8080/v1` |
| FastAPI | `http://127.0.0.1:8000` |
| PostgreSQL | `localhost:5432` / database `amr_agent` |
| Qdrant | `http://localhost:6333` |

## 对应环境变量

| 变量 | 本机值 |
|---|---|
| `AMR_PYTHON_EXE` | `E:\Anaconda\envs\torch128\python.exe` |
| `FAST_ARTIFACT_ROOT` | `E:\Llama.cpp` |
| `RAG_EMBEDDING_MODEL_PATH` | `E:\Llama.cpp\Embedding` |

公开文档中的命令一律写 `python`。若当前 PATH 不是 `torch128`，先执行：

```powershell
$env:AMR_PYTHON_EXE = 'E:\Anaconda\envs\torch128\python.exe'
& $env:AMR_PYTHON_EXE --version
```
