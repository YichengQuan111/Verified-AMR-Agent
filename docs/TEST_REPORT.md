# P0-20 最终测试与验收报告

基准日期：2026-08-21  
项目根目录：`C:\Users\QYC\Documents\AMR_Agent`

本报告只记录实际执行过的命令和观察结果。自动生成的 JSON/Markdown/JSONL 放在 `tmp/`，不登记为源码职责。

## 1. 部署验收

| 验收项 | 实际命令/结果 |
|---|---|
| Compose 静态展开 | `docker compose -f .\compose.yaml config`：通过；服务为 `postgres`、`qdrant`、`api`。 |
| API 镜像 | `docker compose -f .\compose.yaml build api`：通过；使用 `infra/requirements.api.lock`，不安装模型/Embedding 依赖。 |
| Compose 启动 | `docker compose -f .\compose.yaml up -d --build`：通过；API 等待 PostgreSQL/Qdrant healthy 后启动并执行迁移。 |
| 一键启动 | `.\scripts\start_local.ps1`：通过；Qdrant `/readyz`、API `/health`、PostgreSQL `(1,)`、Qdrant collections 检查通过。 |
| API 健康 | `/health` 返回 `status=ok`、`environment=compose`、`model_validated=false`；这是设计边界，不是模型验收。 |
| 数据库 | `migrate_database.py check`：8 张核心表存在，`missing_core_tables=[]`，仅 `alembic_version` 为辅助表。 |

## 2. 在线 Fast 闭环证据

正式 HITL 连续成功证据来自已实际写入 PostgreSQL 的三个独立 run（2026-08-21 收口）：

| run_id | 结果 |
|---|---|
| `p020-release-hitl-1-20260821-2028` | 先 `waiting_approval`，再批准恢复 `completed`；8/8 阶段，5/5 工具，Validator error=0，仿真 completed，ORDER-001，4 次模型调用，Approval=1，Effect=1。 |
| `p020-release-hitl-2-20260821-2028` | 同上。 |
| `p020-release-hitl-3-20260821-2028` | 同上。 |

模型服务为宿主机 `scripts/start_fast_secure.ps1`：llama-server `127.0.0.1:18080`，鉴权代理 `127.0.0.1:8080`，served alias `qwen3.6-fast`，IQ4_NL，ctx=16384。Smart 未启动。历史 `p014-fast-online-*` 为 Approval=0，不再列入发布证据。

## 3. P0-18/P0-19 指标口径（审计修复后）

历史 `tmp/p018_eval_p020_final` 的 60/60 与 P0-19 三策略 60/60 来自未消费 oracle 的离线 runner 和同源 Trace Replay，**废止为发布验收**。2026-08-21 审计修复后：

- P0-18：独立 `evaluate_oracle()`，未知键 fail closed。本步 `.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p020_release_p018` 为 60/60、`report_id=p018-85eaad378d39c29d`。
- P0-19：`offline_independent_oracle`。本步 Workflow 52/60、ReAct 53/60、PEVR 60/60，`report_id=p019-cf6986ed9cc65f8e`。
- 真实 RAG holdout：Recall@K=1、MRR=1、Precision@K=0.236364、nDCG@K=1、citation=1、answerability=1、ACL=0；坏阈值退出码 2。Precision/nDCG 使用唯一文档+章节二元 oracle，不可答例不参与排序指标。
- 演示视频：仓库内可播放媒体文件仍为 0。

## 4. 代码/契约回归

实际命令与结果：

- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p020_deployment.py -q -p no:cacheprovider`：3 passed。
- `.\scripts\run_smoke.ps1`：依赖/工具链/迁移/数据库/Qdrant 检查通过；pytest **272 passed**、2 warnings；CTest **38/38**。
- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration -q -p no:cacheprovider`：18 passed、1 个同源弃用警告。
- `docker compose -f .\compose.yaml config`：通过；最终三服务均为 healthy。
- `Invoke-RestMethod http://127.0.0.1:8000/openapi.json`：通过；运行时标题 `AMR Agent API`，公开 12 个已实现路径，与 [`API.md`](API.md) 一致。
- `E:\Anaconda\envs\torch128\python.exe .\scripts\smoke_llm_structured.py`：20/20；`smoke_p005_prompts.py --profile fast`：5/5。两者是在线节点/结构化冒烟，不替代 P0-13 全链路。

## 5. 仍存在的限制

- Compose API 默认关闭启动期模型门禁，原因是宿主 Fast 绑定 Windows `127.0.0.1:8080`；真实模型链路必须运行宿主机 `check_model_gateway.py --profile fast`。
- 宿主 Fast 由 `start_fast_secure.ps1` 提供；长上下文吞吐仍受本机 GPU 影响，预算耗尽会 fail closed。
- P0-18/P0-19 的离线口径、Smart 禁用、P0 范围边界必须原样保留。
- 正式演示视频尚未交付；未在本步 HITL dispatch 窗口做真实 OS 强杀；七类异常未再注入真实 C++ 主链。因此 Release Verdict 仍不能写成 PASS。
