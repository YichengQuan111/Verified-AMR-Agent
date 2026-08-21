# AMR Agent API 文档

本文档描述当前已经实现并通过测试的 FastAPI HTTP 契约。默认地址为 `http://127.0.0.1:8000`；启动后可通过 Swagger UI `/docs` 和 OpenAPI JSON `/openapi.json` 查看运行时生成的精确 Schema。

## 1. 运行边界

- `/health` 与 `/health/model` 匿名可访问；业务路由必须携带 Bearer JWT。
- JWT 由外部受信身份系统签发，当前项目不提供 token minting；角色只取自验签后的 `Principal`。
- `viewer` 只能读取允许的运行/事件/文档；`operator` 才能创建运行、上传文档、评测登记和处理审批。
- 请求体、Prompt、RAG 正文和工具参数不能提升角色或绕过 Validator/HITL。
- `/evals/runs` 只把评测请求持久化为 `run_kind=eval`，不在 API 进程内执行 P0-18/P0-19 Harness。
- 自然语言 → RAG → DAG → C++ → 仿真的完整真实演示入口仍是 Windows 主机脚本 `scripts/run_p013_e2e.py`，不是一个未实现的 `/plan` HTTP 路由。

## 2. 端点总览

| 方法 | 路径 | 身份 | 作用 | 成功 |
|---|---|---|---|---:|
| GET | `/health` | 匿名 | API 进程健康和模型门禁状态摘要 | 200 |
| GET | `/health/model` | 匿名 | 主动请求本地模型 `/v1/models` 并校验 Fast alias | 200 / 503 |
| POST | `/agent/runs` | operator | 原子创建 run 与首个事件，可创建合同级审批 | 201 |
| GET | `/agent/runs/{run_id}` | viewer/operator | 从 PostgreSQL 查询完整运行快照 | 200 / 404 |
| GET | `/agent/runs/{run_id}/plan` | viewer/operator | 查询最新或指定计划版本 | 200 / 404 |
| GET | `/agent/runs/{run_id}/events` | viewer/operator | 以 SSE 返回已持久化事件快照 | 200 / 404 |
| POST | `/agent/runs/{run_id}/approve` | operator | 处理 P0-06 合同/计划审批 | 200 / 404 / 409 |
| POST | `/agent/runs/{run_id}/hitl/{approval_id}/approve` | operator | 签发绑定 run/task/plan 的 HMAC ApprovalGrant | 200 / 404 / 409 |
| POST | `/agent/runs/{run_id}/hitl/{approval_id}/reject` | operator | 拒绝 pending HITL 请求 | 200 / 404 / 409 |
| POST | `/documents` | operator | 上传不超过 10 MiB 的文档元数据与正文 | 201 / 422 |
| GET | `/documents/{document_id}` | viewer/operator | 查询 ACL 允许的文档元数据，不返回正文 | 200 / 404 |
| POST | `/evals/runs` | operator | 登记一个评测运行请求 | 201 |

所有未列出的路径都不是 P0 公共接口。未知 JSON 字段在请求模型层拒绝。

## 3. 健康检查

### `GET /health`

该接口不联网请求模型，适合作为容器 liveness/readiness 检查。正常响应示例：

```json
{
  "status": "ok",
  "service": "amr-agent-api",
  "version": "0.1.0",
  "environment": "compose",
  "model_validated": false,
  "model_alias": null
}
```

Compose 默认将 `MODEL_GATEWAY_VALIDATE_ON_STARTUP=false`，原因是 Fast 继续在 Windows 主机的 `127.0.0.1:8080` 运行；这不代表模型可用。需要真实模型时，先运行主机脚本并执行 `scripts/check_model_gateway.py --profile fast`。若部署网络确认容器可以访问本地 Fast，可把该变量显式设为 `true`，让 API 启动期也执行 alias 门禁。

### `GET /health/model`

该接口主动调用配置的模型网关。Fast 通过时返回 `status=ok` 和 `served_alias=qwen3.6-fast`；连接失败、alias 不符或 Profile 禁用时返回 HTTP 503 及稳定错误码。Smart 当前必须返回 `MODEL_PROFILE_DISABLED`，不应启动或以单条响应冒充验收通过。

## 4. 认证调用示例

以下命令中的 `<signed-jwt>` 必须是由配置的 issuer/audience、HS256 密钥和角色签发的真实令牌；项目不会接受 body 中自行填写的 `role`。

```powershell
$headers = @{ Authorization = 'Bearer <signed-jwt>' }
Invoke-RestMethod 'http://127.0.0.1:8000/agent/runs/<run_id>' -Headers $headers
```

## 5. 创建运行

`POST /agent/runs` 的 `task_contract` 必须先通过 P0-04 的严格 `TaskContract` Schema；`prompt_id` 与 `prompt_version` 要么同时省略，要么同时出现。运行服务在一个事务中执行：

```text
BEGIN → INSERT runs → flush → INSERT events → flush → COMMIT
```

调用方应先使用 `TaskContract.model_dump(mode="json")` 生成合同 JSON，再按以下方式提交；仓库不提供虚构的示例文件，也不接受未通过严格 Schema 的任意正文：

```powershell
$headers = @{
  Authorization = 'Bearer <operator-jwt>'
  'Content-Type' = 'application/json'
}
# $body 必须是由 TaskContract.model_dump(mode="json") 产生的完整 JSON 字符串。
$body = Get-Content '<validated-task-contract.json>' -Raw
Invoke-RestMethod 'http://127.0.0.1:8000/agent/runs' -Method Post -Headers $headers -Body $body
```

字段定义见 [TaskContract Schema](schemas/TaskContract.schema.json)。

## 6. 事件与审批

```powershell
$headers = @{ Authorization = 'Bearer <viewer-or-operator-jwt>' }
curl.exe -N -H "Authorization: Bearer <signed-jwt>" `
  'http://127.0.0.1:8000/agent/runs/<run_id>/events'
```

事件只返回已持久化的有限快照，`after_sequence` 可用于从指定序号继续读取。合同/计划审批请求体只允许 `approved` 或 `rejected`，服务端使用签名主体作为 `decided_by`，拒绝 body 冒充其他审批人。

## 7. 文档与评测登记

`POST /documents` 使用 multipart 字段 `file/version/role_scope/source/metadata_json`，上传上限为 10 MiB；服务端先做 metadata Schema 校验和角色门禁，再由 `DocumentService` 计算 SHA-256 并持久化。文档正文不会通过 `GET /documents/{document_id}` 返回。

`POST /evals/runs` 接收 `task_contract/suite_id/case_ids`；`requested_by` 仅保留兼容字段，真实请求主体来自 JWT。P0-18/P0-19 的一键执行仍使用：

```powershell
.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p018_eval_final
.\scripts\run_p019_compare.ps1 -SourceReport .\tmp\p018_eval_final\p018_eval.json
```

## 8. 错误处理

业务异常使用统一结构：

```json
{
  "detail": {
    "code": "AUTH_REQUIRED",
    "message": "..."
  }
}
```

客户端应同时检查 HTTP 状态码和 `detail.code`；不要把自然语言 `message` 当作恢复或审批信号。数据库内部 DSN、SQL 和堆栈不会作为响应泄漏。
