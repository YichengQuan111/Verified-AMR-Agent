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

正式连续成功证据来自已实际写入 PostgreSQL 的三个独立 run：

| run_id | 结果 |
|---|---|
| `p014-fast-online-1-20260821-1037` | `completed`，8/8 阶段，5/5 工具，Validator error=0，仿真 completed，ORDER-001 完成，4 次模型调用。 |
| `p014-fast-online-2-20260821-1038` | 同上。 |
| `p014-fast-online-3-20260821-1039` | 同上。 |

模型服务为宿主机 `E:\Llama.cpp\start-qwen3.6-agent.cmd`，实际 served alias 为 `qwen3.6-fast`，外部脚本 `--ctx-size 16384`。Smart 未启动。

Smart 负向门禁实际命令 `check_model_gateway.py --profile smart` 返回退出码 1、`MODEL_PROFILE_DISABLED`；该非零结果是预期安全门禁，不是 Smart 对照通过。

P0-20 当日为检查新部署边界追加了新的 `p020-*` fresh run 尝试；其中冷启动/长上下文在 `plan_tasks` 节点达到 300 秒累计预算，退出码非 0，未计入三次成功，也没有用它覆盖上表正式证据。该限制和失败轨迹会持续记录，不能写成通过。

## 3. P0-18/P0-19 最终指标

### P0-18

实际命令：

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p018_eval_p020_final
```

结果：退出码 0；`tmp/p018_eval_p020_final/p018_eval.json`；`report_id=p018-415f0b3f59574772`；`report_digest=415f0b3f59574772ad71df36591628f756a79dd26224d356d5f7fb1afb0f4585`；60/60 通过。五类配额为 25/10/10/5/10，五类 `category_pass_rates` 均为 1.0；`observed_negative_count=16`；`model_call_count=0`；RAG `Recall@5=1.0`、`MRR=1.0`、citation correctness=1.0、ACL leak=0；Agent normal completion/trace completeness=1.0；AMR normal order/charging completion=1.0；security prompt-injection block/unauthorized-tool block=1.0；recovery expected termination/replan success=1.0；verification pass/failure locator=1.0；七项 zero tolerance 全为 0。

### P0-19

实际命令：

```powershell
.\scripts\run_p019_compare.ps1 `
  -SourceReport .\tmp\p018_eval_p020_final\p018_eval.json `
  -OutputDir .\tmp\p019_strategy_compare_p020_final
```

结果：退出码 0；`raw_result_count=180`；`report_id=p019-26947b55d9054d8d`；`report_digest=26947b55d9054d8de3f5e204b203b24fb54c3e4b57b519df3c265afd0eace8e6`；fixed Workflow、ReAct、PEVR 均 60/60 预期符合，正向任务 44/44，计划合法 33/33，异常终止 10/10，成功重规划 8/8，工具错误 15、意外工具错误 0。fixed Workflow/PEVR 步数均值/P95 为 5.816667/14，ReAct 为 12.15/28；Trace 延迟 P95=70ms 但 `wall_clock=false`；Token/CPU/RSS/GPU `observed=false`。Smart `started=false/completed=false/status=deferred`。

## 4. 代码/契约回归

实际命令与结果：

- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p020_deployment.py -q -p no:cacheprovider`：3 passed。
- `.\scripts\run_smoke.ps1`：依赖/工具链/迁移/数据库/Qdrant 检查通过；pytest 238 passed、1 个既有 `jieba/pkg_resources` DeprecationWarning；CTest 34/34 passed。
- `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration -q -p no:cacheprovider`：18 passed、1 个同源弃用警告。
- `docker compose -f .\compose.yaml config`：通过；最终三服务均为 healthy。
- `Invoke-RestMethod http://127.0.0.1:8000/openapi.json`：通过；运行时标题 `AMR Agent API`，公开 12 个已实现路径，与 [`API.md`](API.md) 一致。
- `E:\Anaconda\envs\torch128\python.exe .\scripts\smoke_llm_structured.py`：20/20；`smoke_p005_prompts.py --profile fast`：5/5。两者是在线节点/结构化冒烟，不替代 P0-13 全链路。

## 5. 仍存在的限制

- Compose API 默认关闭启动期模型门禁，原因是宿主 Fast 绑定 Windows `127.0.0.1:8080`；真实模型链路必须运行宿主机 `check_model_gateway.py --profile fast`。
- 宿主 Fast 的长上下文吞吐受本机 GPU/CPU/MoE 配置影响；P0-13 的固定累计时间预算耗尽时会 fail closed，不自动放宽预算。本次 P0-20 追加的 fresh `p020-*` 尝试在 `plan_tasks` 达到该预算，未计入三次成功；正式三次成功证据仍是第 2 节列出的独立 run。
- P0-18/P0-19 的离线口径、Smart 禁用、P0 范围边界必须原样保留。
