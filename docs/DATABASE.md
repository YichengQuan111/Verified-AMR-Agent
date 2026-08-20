# P0-06 PostgreSQL 与 API 说明

## 1. 分层与事务边界

P0-06 固定调用方向如下：

```text
FastAPI Router → Application Service → Repository → SQLAlchemy ORM → PostgreSQL
```

- Router 只处理 HTTP、multipart 和 SSE，不直接执行 SQL。
- Service 负责业务状态转换和跨表事务。一个 Service 操作可以组合多个 Repository。
- Repository 只构造查询和增删对象，禁止调用 `commit()` 或 `rollback()`。
- ORM 描述表、索引、外键和约束；Alembic 负责显式建表。
- API 启动不会自动建表，部署或首次运行前应显式执行迁移。

## 2. 八张核心表

所有外键都采用 `ON DELETE RESTRICT`，避免运行、计划或审计证据被级联静默删除。

| 表 | 高频关系化字段示例 | JSONB / 大字段 | 主要职责 |
|---|---|---|---|
| `runs` | `run_id`、`run_kind`、`status`、`contract_id`、`environment_ref`、`plan_version`、Prompt 版本字段 | `task_contract_snapshot`、`run_state_snapshot` | 一次 Agent/评测运行的索引状态与完整合同/运行态快照。 |
| `plans` | `run_id`、`plan_version`、`status`、`trigger_observation_id` | `plan_snapshot` | 保存不可混淆的版本化计划 DAG。 |
| `tasks` | `task_id`、`run_id`、`plan_version`、`status`、`tool_name`、`target_amr`、风险与审批字段 | `tool_arguments`、`task_snapshot` | 保存计划任务的查询字段和完整 `PlanTask`。 |
| `tool_calls` | `run_id`、`task_id`、`plan_version`、`tool_name`、`status`、`attempt`、耗时/错误字段 | `tool_arguments`、`result_snapshot` | 预留一次真实工具尝试的输入、结果和错误证据。 |
| `effects` | `run_id`、`task_id`、`plan_version`、`status`、`idempotency_key` | `payload_snapshot` | 副作用幂等账本，防止恢复或重试时重复执行。 |
| `approvals` | `run_id`、`task_id`、`plan_version`、`status`、角色和决定人 | `request_snapshot` | 保存审批请求、决定和时间证据。 |
| `events` | `run_id`、`sequence_no`、`event_type`、`node_name`、`task_id` | `payload` | 按 run 内严格序号保存审计/SSE 事件。 |
| `documents` | `status`、`version`、`role_scope`、`source`、`checksum`、`size_bytes` | `metadata_snapshot`、正文 `BYTEA` | 保存文档 ACL/版本索引、完整元数据和原始字节。 |

没有把所有内容塞进一个 JSONB。状态、运行 ID、计划版本和工具名等查询/评测字段都是独立列；复杂 Pydantic 对象同时保留 UTF-8 JSON 兼容快照。

唯一辅助表是 Alembic 自动维护的 `alembic_version`。它只保存当前迁移版本，不承载业务数据，也不替代上述八张表。

## 3. 迁移与检查

在项目根目录运行：

```powershell
E:\Anaconda\envs\torch128\python.exe scripts\migrate_database.py upgrade
E:\Anaconda\envs\torch128\python.exe scripts\migrate_database.py check
E:\Anaconda\envs\torch128\python.exe scripts\migrate_database.py current
```

`upgrade` 可重复执行并只向前迁移。脚本故意不提供 downgrade；首个迁移的 `downgrade()` 也会明确抛错，防止误删八张核心表。后续字段变化应新增前向迁移，不能修改已执行迁移或用自动降级删表。

## 4. 关键回滚保证

`RunService.create_run()` 使用一个 `session.begin()` 包住运行和首事件：

```text
BEGIN
  INSERT runs
  flush（确认 SQL 已发给 PostgreSQL）
  INSERT events
  flush（故障注入点）
COMMIT
```

只要第二次 INSERT 因主键、外键或其他约束失败，事务上下文就执行 ROLLBACK，第一个已经发出的 INSERT 也不会留在 `runs`。

`tests/integration/test_p006_postgres.py` 在真实 PostgreSQL 中预置冲突的 `event_id`，监听并确认 SQL 顺序确实为 `runs → events`，捕获第二步 `IntegrityError` 后再查询新 `run_id` 不存在。测试结束只按自己生成的精确 ID 删除测试行，从不删除表。

## 5. P0-06 HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/agent/runs` | 使用严格 `TaskContract` 创建运行。 |
| GET | `/agent/runs/{run_id}` | 从 PostgreSQL 查询运行。 |
| GET | `/agent/runs/{run_id}/plan` | 查询最新或指定 `plan_version`。 |
| GET | `/agent/runs/{run_id}/events` | 以有限 SSE 快照输出已持久化事件。 |
| POST | `/agent/runs/{run_id}/approve` | 原子保存批准/拒绝、运行状态和事件。 |
| POST | `/documents` | multipart 上传不超过 10 MiB 的文档。 |
| GET | `/documents/{document_id}` | 查询不含正文的文档元数据。 |
| POST | `/evals/runs` | 创建 `run_kind=eval` 的持久化运行。 |

API 错误只返回稳定应用错误码，不泄漏 SQL、数据库密码或约束内部细节。文档原始字节可由 `DocumentService.get_document()` 提供给 P0-07 受控索引器，当前公共 GET 只返回元数据。

## 6. 当前边界

- P0-06 只实现持久化、接口和事务，不执行工具、检索、C++ 规划、仿真、LangGraph 主闭环或评测套件。
- `tool_calls` 与 `effects` 已有 ORM/Repository/约束，但要等 P0-12/P0-13 的真实工具执行路径写入。
- `documents.status` 当前为 `stored`，`indexed_at` 为 `null`；分块、向量化和混合检索属于 P0-07。
- SSE 当前返回请求时已经持久化的有限事件快照，不实现长时间轮询或消息代理。
