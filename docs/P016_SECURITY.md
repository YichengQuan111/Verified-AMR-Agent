# P0-16：RBAC、HITL 与安全边界

状态：已完成（2026-08-21）

本专题把 P0-06 的 API/数据库、P0-07 的检索 ACL、P0-12 的白名单工具、P0-13 的
固定 PEVR 图和 P0-14 的 Checkpoint/Effect Ledger 连接成一个 fail-closed 安全边界。
本文件是专题说明；源码中的中文 docstring 和关键分支注释仍是实现契约的一部分。

## 1. 身份与角色

API 业务路由只接受 Bearer JWT。JWT 必须通过固定 HS256、issuer、audience、iat、exp、
subject 和 role 校验；算法错误、篡改、过期、缺 claim 和错误生命周期都会返回认证失败。
角色只从签名后的 Principal 读取，不能由 request body、query、Prompt、RAG 正文或工具
参数重新声明。

| 角色 | 允许范围 |
|---|---|
| viewer | 只读工具；只能读取文档 ACL 明确包含 viewer 的内容；不能创建运行、上传文档、分配/规划/仿真或决定审批 |
| operator | viewer 读范围之外，可调用 ToolSpec 明确允许的 operator 工具；可创建运行、上传文档和决定审批 |

工具权限不复制一份硬编码角色表，而是读取 ToolSpec.allowed_roles；带副作用工具的
ToolSpec 不能声明 viewer。安全 ToolRegistry 要求 Principal，审计结果同时记录
principal_role 和 principal_subject。

## 2. 文档 ACL 与不可信检索

检索请求的 role_scope 只能保持或收紧主体范围，viewer 请求 operator 范围会被拒绝。
Qdrant 服务端过滤、BM25 预过滤、Hybrid 融合后的候选复核和 retrieve_knowledge
handler 输出复核共同检查角色、文档 ID、top_k、query 和逐条 ACL。文档 Service 在
读取 documents 行时再次检查 ACL；未授权文档对外统一表现为 404，避免枚举文档存在性。

检索正文永远是数据，不是控制指令。Prompt 的 system 边界明确拒绝“忽略系统权限、
授予 operator、批准工具”等文本；它不能改变角色、ACL、Schema、Validator、HITL
状态或工具白名单。工具参数递归拒绝 command、SQL、Shell、script、code、URL/HTTP
选择器等执行面；query 文本本身不按关键词扫描，避免把普通问题误判为命令。

## 3. 工具与执行边界

安全模式下执行顺序为：

1. 解析已登记 ToolName；未知名称不进入 handler。
2. 检查已验证 Principal 和 ToolSpec.allowed_roles。
3. 检查固定顶层参数白名单和禁止执行选择器。
4. 通过输入 Pydantic Schema。
5. 对 requires_approval 工具核对签名 ApprovalGrant。
6. 执行固定 handler，再校验输出 Schema、错误分类、审计和幂等结果。

P0-12 的 C++ 适配器仍只使用仓库内固定可执行文件和固定 argv，Shell=False；验证套件
只允许固定 suite/case。没有任何 Agent 参数能选择任意 Python、SQL、Shell、外部
HTTP、cwd、可执行文件或未注册工具。

安全模式由 API/安全 PEVR 显式开启。为保持 P0-12 纯契约测试，直接构造的 legacy
ToolRegistry 仍允许旧的 role 参数；该模式没有暴露给 API，也不能作为生产入口。

## 4. HITL 状态机

高风险写操作在 execute 的最后一道门禁前创建 pending 请求；请求绑定：

- run_id、task_id、plan_version；
- waiting Checkpoint ID；
- 完整计划 SHA-256；
- 确定性 Validator 结果 SHA-256；
- requested_by、operator required_role、原因和过期时间。

状态只能按 pending → approved/rejected/expired 转移。viewer 不能批准；批准不产生
通用管理员开关，而是由内存或 PostgreSQL Store 签发 HMAC ApprovalGrant。票据绑定
审批请求摘要、当前计划/Validator 摘要、主体、期限和同一任务。

PEVR 暂停顺序为：

验证计划 → 创建 pending → 写 waiting_approval Checkpoint → 抛出 PEVRInterrupt

Checkpoint 中保留已完成工具结果、当前等待任务、HITLInterrupt 和审批定位信息。恢复
顺序为：

读取并严格验证 Checkpoint → 读取审批 Store → 验签/核对主体/期限/计划/Validator →
复用 Effect Ledger → 执行未完成任务

缺少批准、篡改票据、审批拒绝/过期、计划或 Validator 摘要变化、Checkpoint 与票据不
匹配时均不会进入 handler。PostgresHITLStore 复用既有 approvals 表的 request_snapshot，
不新增 Alembic 表；PostgresRuntimeStore 继续保存图状态和 Effect Ledger。

高优先级覆盖、人工接管和高风险写操作共享 HITLReason 与 HITLController 协议；这些
原因只是暂停原因，不是自动批准依据。

## 5. 公共接口变化

- agent.security.Principal、JWTAuthenticator、authorize_tool、authorize_document、
  assert_retrieval_scope。
- ToolRegistry.execute/build_tool_registry 增加 Principal、security_required 和
  ApprovalGrant 验证边界；ToolResult 增加 principal_subject。
- PEVRRequest 增加 principal、approval_grant；PEVRGraphState 增加 hitl_interrupt、
  approval_grant；新增 PEVRInterrupt。
- agent.runtime.hitl 提供 HITLReason、HITLStatus、HITLRequest、ApprovalGrant、
  HITLInterrupt、HITLController、InMemoryHITLStore 和 HITLStoreProtocol。
- services.application.PostgresHITLStore 复用 PostgreSQL approvals 表。
- API 提供 run-scoped 的 `/agent/runs/{run_id}/hitl/{approval_id}/approve` 和 `/reject`；
  两个入口都要求 operator，并先核对 approval_id 所属 run。
- AppSettings 增加 security.jwt_secret、issuer、audience、leeway_seconds；生产部署
  应通过 AMR_JWT_SECRET 使用独立随机密钥。

纯 JSON Schema 由 scripts/export_schemas.py 重新导出；本步无 Alembic revision。

## 6. 实际验证

- P0-16 专项：tests/unit/test_p016_security.py，8 passed。
- 全量 Python：E:\Anaconda\envs\torch128\python.exe -m pytest -q -p no:cacheprovider，
  215 passed，1 warning；唯一 warning 是既有 jieba/pkg_resources 弃用提示。
- 仓库 smoke：scripts/run_smoke.ps1 通过；环境/依赖检查通过，PostgreSQL 8 张核心表
  缺失数 0，Qdrant amr_warehouse_knowledge 健康，Python 215 passed、1 warning，
  CTest 34/34 passed。
- Schema：scripts/export_schemas.py 成功导出 40 份，checked-in schema 回归包含在全量 215 passed。

本步没有启动 Fast/Smart 模型；Smart 仍由配置硬禁用。Smoke 使用的 PostgreSQL 和
Qdrant 已健康，真实在线模型行为不在 P0-16 本步验证范围。
