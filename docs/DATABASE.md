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
python scripts\migrate_database.py upgrade
python scripts\migrate_database.py check
python scripts\migrate_database.py current
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

API 错误只返回稳定应用错误码，不泄漏 SQL、数据库密码或约束内部细节。文档原始字节由 `DocumentService.get_document()` 提供给 P0-07 受控索引器，当前公共 GET 仍只返回元数据。

## 6. P0-07 对 documents 表的复用

P0-07 没有新增或修改数据库 revision，当前仍为 `0001_p006_core (head)`。索引器使用 Front Matter `doc_id` 作为 6 份冻结知识文档的稳定 `documents.document_id`，并调用：

- `DocumentService.upsert_frozen_knowledge_document()`：幂等同步正文、版本、ACL、source、checksum 和受管元数据；内容变化时把状态恢复为 `frozen` 并清空 `indexed_at`。
- `DocumentService.get_document()`：从 PostgreSQL 重新读取原始字节，再次解析 Front Matter 和校验 checksum；索引器不旁路 Service 直接读取 ORM。
- `DocumentService.mark_documents_indexed()`：只有 Qdrant 全批写入成功后，才在单事务中把所有本批文档改为 `status=indexed` 并设置相同 `indexed_at`。

重复同步相同 checksum 时保留既有 `indexed_at`；Qdrant 写入失败不会产生“数据库声称已索引”的部分状态。普通 `/documents` 上传仍使用随机 document ID 和初始 `status=stored`，不会被知识库 upsert 入口覆盖。

## 7. 当前边界

- P0-06 只实现持久化、接口和事务，不执行工具、检索、C++ 规划、仿真、LangGraph 主闭环或评测套件。
- `tool_calls` 与 `effects` 由 P0-14 `PostgresRuntimeStore` 在真实执行路径中写入；普通
  P0-12 工具单独调用仍可只使用进程内 ToolExecutor 缓存。
- 普通上传文档仍是 `status=stored`、`indexed_at=null`；6 份 P0-07 冻结知识文档在当前成功索引后为 `status=indexed` 并带时间戳。
- SSE 当前返回请求时已经持久化的有限事件快照，不实现长时间轮询或消息代理。

## 8. P0-14 Checkpoint 与 Effect Ledger

P0-14 复用本节已存在的八张表，没有新增数据库 revision 或同义状态表：

```text
ensure_run
  → runs + run.created event
阶段/任务完成
  → runs.run_state_snapshot + plans/tasks（同一事务）+ checkpoint.saved event
副作用任务
  → INSERT effects(status=reserved, unique key)
  → 调用外部仿真/工具
  → UPDATE effects(status=completed/reconciled/compensation_required)
```

### 8.1 幂等约束

- 业务副作用身份固定为 `run_id + plan_version + task_id` 三元组；具体
  `idempotency_key` 是该规范 JSON 数组的 SHA-256（`p014:<digest>`）。不能直接用冒号
  拼接，因为 ID 本身允许包含冒号；`attempt`、`call_id` 和随机 UUID 也不能替代它。
- `effects` 同时受 `(run_id, plan_version, task_id)` 和 `idempotency_key` 两个唯一约束
  保护。并发插入发生唯一键冲突时，失败事务只重新读取赢家，不重新调用 handler。
- `payload_snapshot` 保存规范化输入 digest、原始 call ID、参数、ToolResult、外部效果 ID
  和恢复说明。`dispatch_simulation` 还在 handler 返回前独立保存
  `external_execution={simulation_id,snapshot,snapshot_digest}`；重复写必须逐摘要相同，
  不允许覆盖另一份外部事实。旧版本记录不会被局部重规划删除。

### 8.2 恢复顺序

1. 从 `runs.run_state_snapshot` 读取并重新通过 `CheckpointSnapshot`/`PEVRGraphState`
   契约校验；请求、环境或 seed 不一致直接拒绝恢复，畸形列表项和未知状态字段不做
   “尽量恢复”。
2. 对每条 `reserved/completed/reconciled` 账本记录先调用只读真实状态核对器。外部
   `completed` 只有在 effect ID、工具名、业务键、输入/输出 digest 均与账本一致时才能
   写为 `reconciled` 并复用；明确 `not_found` 的 reserved 行才可用同一业务键继续。
3. 外部仍 `in_progress`、状态未知、账本完成但外部查不到时，禁止重放并转重规划；外部
   失败时先标记 `compensation_required`，由后续 P0-15/人工补偿流程处理。
4. 图恢复时已完成任务必须从 Checkpoint 或 Effect Ledger 取得 ToolResult；缺证据直接
   停止，不通过重复执行补齐。

### 8.3 局部重规划

`LocalReplanner` 从 allocation/route ToolResult 构建真实 AMR、通道 cell/edge、工位、
工具和任务 provenance，再沿 DAG 依赖向后继传播影响。它只删除未完成受影响节点，保留
已完成节点及 `effect_id`，生成新任务 ID，使计划版本恰好加一，并重新执行完整 PEVR
验证（不仅是 DAG 校验）。新版本副作用自然使用新的三元组摘要键，旧版本账本仍作为
审计和恢复依据。
