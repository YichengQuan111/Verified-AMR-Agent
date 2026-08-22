# AMR Agent API 文档

本文档描述当前已经实现并通过测试的 FastAPI HTTP 契约。默认地址为 `http://127.0.0.1:8000`；启动后可通过 Swagger UI `/docs` 和 OpenAPI JSON `/openapi.json` 查看运行时生成的精确 Schema。

## 1. 运行边界

- `/health` 与 `/health/model` 匿名可访问。
- **2026-08-22 用户指令**：演示闭环链路匿名开放——`/demo/nl/*` 与
  `POST /agent/runs/{run_id}/hitl/{approval_id}/approve|reject` 不要求 JWT。
  其余业务路由仍须 Bearer JWT。
- JWT 由外部受信身份系统签发，当前项目不提供面向浏览器的 token minting；角色只取自验签后的 `Principal`。
- `viewer` 只能读取允许的运行/事件/文档；`operator` 才能创建运行、上传文档、评测登记和处理 **P0-06 合同级** `/agent/runs/{run_id}/approve`。HITL 演示审批按用户指令匿名。
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
| POST | `/agent/runs/{run_id}/hitl/{approval_id}/approve` | 匿名 | 签发绑定 run/task/plan 的 HMAC ApprovalGrant（2026-08-22 用户指令豁免 JWT） | 200 / 404 / 409 |
| POST | `/agent/runs/{run_id}/hitl/{approval_id}/reject` | 匿名 | 拒绝 pending HITL 请求（同上豁免） | 200 / 404 / 409 |
| POST | `/documents` | operator | 上传不超过 10 MiB 的文档元数据与正文 | 201 / 422 |
| GET | `/documents/{document_id}` | viewer/operator | 查询 ACL 允许的文档元数据，不返回正文 | 200 / 404 |
| POST | `/evals/runs` | operator | 登记一个评测运行请求 | 201 |
| GET | `/demo` | 匿名 | 托管演示页（纯静态 HTML，不含数据） | 200 |
| GET | `/demo/warehouse` | 匿名 | 固定 seed 的规范化仓库地图快照（2026-08-22 晚起免 Token） | 200 |
| POST | `/demo/order` | 匿名 | 任意自然语言下单（轻量演示链）：LLM 抽取 → 动态订单 → C++ 链 → 仿真 | 200 / 422 / 503 |
| POST | `/demo/simulate` | operator | C++ 计划 → Validator 门禁 → Python 仿真的演示链路 | 200 / 404 / 422 / 503 |
| POST | `/demo/launcher/start` | 匿名 | 受控启动白名单脚本 `scripts/start_local.ps1`（2026-08-22 晚起免 Token） | 200 / 503 |
| GET | `/demo/launcher/status` | 匿名 | 启动器状态与日志尾部 | 200 |
| POST | `/demo/nl/run` | 匿名 | 抽取四要素、重建动态订单并拉起 PEVR 闭环（单并发槽位） | 200 / 409 / 422 / 503 |
| GET | `/demo/nl/active` | 匿名 | 当前自然语言运行槽位（无则 null） | 200 |
| GET | `/demo/nl/status/{run_id}` | 匿名 | 运行状态轮询（running/waiting_approval/completed/failed） | 200 / 404 |
| POST | `/demo/nl/resume` | 匿名 | HITL grant 已签发后恢复运行 | 200 / 404 / 409 |
| POST | `/demo/nl/dismiss` | 匿名 | 清理演示槽位（不改写运行/审批事实） | 200 / 404 |
| GET | `/demo/nl/result/{run_id}` | 匿名 | PEVR 证据摘要 + 动态订单真值 + path_step 轨迹子集 | 200 / 404 / 409 |

所有未列出的路径都不是 P0 公共接口。未知 JSON 字段在请求模型层拒绝。

## 2.1 演示 UI 扩展端点（用户指令优先于 scope.md 的 P0 前端排除项）

`/demo/*` 是为演示页提供的可视化-only 链路，**不写 Effect Ledger、不触发 HITL 审批、不能当发布证据**；正式闭环仍走 `scripts/run_p013_e2e.py`。

- `GET /demo/warehouse`：返回 `DemoWarehouseMap`（[Schema](schemas/DemoWarehouseMap.schema.json)）：30×20 地图、障碍/临时封路/窄通道/禁行与单向边、P1–P6/S1–S6/C1/C2、4 台 AMR 初始位姿和可演示订单清单，全部来自 `warehouse_v1@seed-v1`，前端不得自行猜测。**2026-08-22 晚起匿名可读**（用户明确决策：本机演示免 Token；地图是 warehouse_v1 的只读视图，不含密钥）。
- `POST /demo/order`（**任意自然语言下单，轻量演示链，匿名**）：请求体 `DemoNLOrderRequest`（[Schema](schemas/DemoNLOrderRequest.schema.json)，只有 `request`，1–500 字符，纯空白拒绝）。服务端用 Fast 经 `ModelProvider.generate_structured` 把文本抽成四要素（`DemoOrderExtraction`：material_id/pickup/dropoff/deadline，未提截止时间默认 120 秒），再按 warehouse_v1 地点白名单重建动态订单（订单 ID 由服务端生成 `NL-xxxxxxxx`，LLM 无权命名），随后走与 `/demo/simulate` 完全相同的 C++ Hungarian → A* → Validator → Python 仿真链。响应复用 `DemoSimulateResponse`；`summary.order` 携带实际执行的完整订单真值，前端历史清单只能以它为准。失败语义：Fast 离线 503 `fast_model_unavailable`；抽取两次不过 Schema 422 `nl_extract_failed`；地点不在地图内 422 `unknown_location`（附合法 P/S 清单）；Validator 拒绝 422 `fleet_plan_invalid` 且不带轨迹。**本端点不写 Effect Ledger、不需 HITL、不持久化历史、不作发布证据**——这是与 `/demo/nl/*` 闭环的本质区别。
- `POST /demo/simulate`：请求体 `DemoSimulateRequest`（只有 `order_id`，默认 `ORDER-001`，必须是种子订单）。服务端依次调用真实 C++ Hungarian → A* → Validator，仅在 `valid=true` 后运行 Python `AMRSimulator`。响应 `DemoSimulateResponse`（[Schema](schemas/DemoSimulateResponse.schema.json)）包含内嵌地图、对照用 `routes`、`result`（SimulationResult 稳定子集：status/events/最终快照）、按 `(time, amr_id)` 排序的 `path_steps` 轨迹子集和 `summary`（order/order_id、validator_valid、error_count、completed_order_ids、simulation_status）。
- Validator 拒绝时返回 HTTP 422 且 `detail.code=fleet_plan_invalid`，`detail.errors` 是 C++ 原始错误证据，响应不含任何轨迹字段；订单不存在返回 404 `demo_order_not_found`；C++ 进程不可用/超时返回 503。
- 启动器只接受 `{"start_fast": bool}` 一个开关：脚本路径固定为仓库内 `scripts\start_local.ps1`，`-StartFast` 仅当显式传 `true`；Smart 没有入口。启动器只在 Windows 主机可用，日志尾部写入 gitignore 的 `tmp/demo_launcher.log`。**2026-08-22 晚起匿名可用**（用户明确决策，与演示页免 Token 一致）；白名单约束不变——若 API 绑定非回环地址，应恢复 operator 门禁。
- 演示页（`GET /demo`）唯一提交入口是完整 PEVR 闭环：左栏自然语言框提交 `POST /demo/nl/run`，轮询进度，到达 `waiting_approval` 后匿名批准/拒绝 HITL，完成后渲染轨迹并写入内存历史。轻量链 `POST /demo/order` **接口保留**（既有测试与快速验证），已从页面撤下。

### 2.2 自然语言下单闭环（PEVR 接入演示页）

`/demo/nl/*` 把 P0-13 正式闭环接到演示页（2026-08-22 用户指令：演示闭环完全不考虑安全）。浏览器匿名提交自然语言 → 服务端 LLM 只抽四要素并按地点白名单重建动态订单 → 写入 `tmp/demo_nl_order_<run_id>.json` → 受控子进程运行 `scripts/run_p013_e2e.py --order-json` → 在 dispatch 前停于 `waiting_approval` → **页面匿名调** `POST /agent/runs/{run_id}/hitl/{approval_id}/approve`（或 `/reject`）签发 grant → 再调 `/demo/nl/resume` 用 `--resume-approved` 恢复 → 完成后 `/demo/nl/result/{run_id}` 返回报告、订单真值与轨迹。这条链**写 Effect Ledger**；副作用仍只执行一次（幂等是正确性机制，不是门禁）。

- `POST /demo/nl/run`：请求体 `DemoNLRunRequest`。先 `prepare_dynamic_order`（地点非法 422 `unknown_location`，抽取失败 422 `nl_extract_failed`，Fast 离线 503），再拉起 CLI。动态订单经 `--order-json` 独立 argv 传入（无 Shell）；文件名必须是 `tmp/demo_nl_order_*.json`。单并发槽位：已有 running/waiting_approval 时 409 `demo_nl_busy`。
- `GET /demo/nl/status/{run_id}`：返回 `DemoNLRunStatus`；waiting 时携带 `approval_id`/`approval_reason_code`/`approval_expires_at`。
- `POST /demo/nl/resume`：仅 `waiting_approval` 可恢复（否则 409 `demo_nl_not_waiting`）。本端点不签发审批。
- `POST /demo/nl/dismiss`：清理演示槽位；running 先 terminate。
- `GET /demo/nl/result/{run_id}`：返回 `DemoNLResultResponse`：`order`（服务端重建的 TransportOrder）、`report`、`path_steps`。未完成 409 `demo_nl_not_completed`。


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
