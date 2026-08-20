# 项目文件职责登记表

本文件是后续所有项目步骤登记“创建/修改了哪些文件、分别有什么用”的固定入口。每次推进工作包都必须更新。P0-01/P0-03 的完整基线清单见 [`P001_P003_FILE_GUIDE.md`](P001_P003_FILE_GUIDE.md)。

## 登记规则

- 每一批推进按工作包或治理任务新增一节。
- 必须覆盖新建、修改、移动和删除的文件。
- “作用”应说明职责和调用关系；“下游影响”应说明后续步骤如何复用。
- 自动生成目录单独说明，不计入源码文件。

## 2026-08-19：P0-01 / P0-03 基线

P0-01/P0-03 涉及的全部文件、用途、学习顺序、修改前后差异和生成物说明，统一记录在：

- [`docs/P001_P003_FILE_GUIDE.md`](P001_P003_FILE_GUIDE.md)

该基线包括工程骨架、配置、日志、模型网关、FastAPI、C++、脚本、测试和文档。

## 2026-08-19：长期协作与交接规则

本步没有新增业务代码，因此没有核心业务代码注释需求；新增内容均为中文治理和交接文档。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `AGENTS.md` | 定义覆盖整个仓库的永久协作规则：推进步骤时写中文注释、登记文件作用、更新交接上下文并运行验证。 | 后续所有 P0/P1/P2 工作包开始前都必须读取并遵守。 |
| 新建 | `docs/FILE_PURPOSES.md` | 作为持续登记文件职责的唯一入口，并链接已有 P0-01/P0-03 详细基线。 | 后续每一步都要追加或修订相应记录。 |
| 新建 | `docs/HANDOFF_CONTEXT.md` | 汇总当前完成状态、关键接口、验证证据、环境、已知限制以及 P0-04 和后续步骤所需信息。 | 新任务或新 Agent 先读此文件即可恢复上下文，完成步骤后必须更新。 |
| 修改 | `README.md` | 增加长期文件职责表和交接上下文入口。 | 项目首页可直接找到治理与交接文档。 |

## 2026-08-19：P0-04 领域数据契约

本步核心 Python 代码均加入了中文模块说明、类说明、validator 说明和关键算法行内注释，便于按“领域 → 工具 → 规划 → 运行态 → Schema”顺序学习。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `domains/amr_warehouse/contracts.py` | 定义 `GridPosition`、`AMRState`、`TransportOrder` 及 AMR 状态枚举；校验 30 × 20 坐标、时间窗、路线与订单直接依赖。 | P0-08～P0-11 的 C++ JSON 边界和仿真状态必须复用这些字段与单位。 |
| 修改 | `domains/amr_warehouse/__init__.py` | 从领域包顶层导出仓储契约，给调用方提供稳定导入入口。 | 后续业务代码无需依赖模块内部路径。 |
| 修改 | `domains/amr_warehouse/data/amrs_v1.json` | 把原来的顶层 `x/y` 迁移为统一的 `position: {x, y}`。 | 种子数据现在可直接通过 `AMRState` 校验；后续不得再维护第二种坐标格式。 |
| 新建 | `agent/tools/contracts.py` | 定义 `ToolSpec`、`ToolResult`、稳定错误对象、角色/状态枚举、九个工具名及顶层参数白名单。只定义契约，不实现工具。 | P0-12 工具注册表必须使用相同名称、Schema、角色、超时和错误分类。 |
| 修改 | `agent/tools/__init__.py` | 从工具包顶层导出公共工具契约和参数校验入口。 | 规划器与未来注册表共享同一白名单，避免规则复制。 |
| 新建 | `agent/planning/dag.py` | 用确定性 Kahn 算法校验未知依赖、重复依赖、自依赖和循环，并返回稳定拓扑顺序。 | P0-05 规划输出、P0-10 验证器和 P0-13 主闭环应复用此校验。 |
| 新建 | `agent/planning/contracts.py` | 定义 `TaskContract`、`PlanTask`、约束、审批、预算及风险/回退/任务状态枚举。 | P0-05 Prompt、P0-06 API/表结构和 P0-13 状态图以此为唯一字段来源。 |
| 修改 | `agent/planning/__init__.py` | 从规划包顶层导出合同、计划任务和 DAG 校验入口。 | 稳定后续模块的导入路径。 |
| 新建 | `agent/runtime/state.py` | 定义 `Observation`、`RunState`、约束违规及运行枚举；跨对象校验任务/订单/AMR/观测归属、终态和重规划预算。 | P0-06 Checkpoint/API、P0-11 仿真观测和 P0-13 LangGraph State 必须直接复用。 |
| 修改 | `agent/runtime/__init__.py` | 从运行时包顶层导出观测和聚合状态契约；没有实现执行节点。 | 给后续持久化与状态图提供稳定导入入口。 |
| 新建 | `scripts/export_schemas.py` | 逐一调用 `model.model_json_schema()`，以 UTF-8、2 空格缩进、非 ASCII 不转义和固定 LF 导出八个 Schema。 | 修改任一核心契约后必须重跑该脚本并提交生成物。 |
| 新建 | `docs/schemas/TaskContract.schema.json` | `TaskContract` 的机器可读 JSON Schema。 | P0-05 目标理解/合同 Prompt 和 P0-06 API 输入校验使用。 |
| 新建 | `docs/schemas/AMRState.schema.json` | `AMRState` 的机器可读 JSON Schema。 | P0-08～P0-11 跨语言与仿真状态使用。 |
| 新建 | `docs/schemas/TransportOrder.schema.json` | `TransportOrder` 的机器可读 JSON Schema。 | P0-08 分配器和 P0-10 计划验证器使用。 |
| 新建 | `docs/schemas/PlanTask.schema.json` | `PlanTask` 的机器可读 JSON Schema。 | P0-05 规划 Prompt 和 P0-13 DAG 执行使用。 |
| 新建 | `docs/schemas/ToolSpec.schema.json` | `ToolSpec` 的机器可读 JSON Schema。 | P0-12 工具注册表使用。 |
| 新建 | `docs/schemas/ToolResult.schema.json` | `ToolResult` 的机器可读 JSON Schema。 | P0-12 工具适配器、P0-13 验证节点和审计记录使用。 |
| 新建 | `docs/schemas/Observation.schema.json` | `Observation` 的机器可读 JSON Schema。 | P0-11 仿真事件和 P0-13 观察/重规划决策使用。 |
| 新建 | `docs/schemas/RunState.schema.json` | `RunState` 的机器可读 JSON Schema。 | P0-06 持久化和 P0-13 LangGraph Checkpoint 使用。 |
| 新建 | `tests/unit/test_p004_contracts.py` | 提供八个核心契约的合法样例及 46 个正反例，覆盖额外/缺失字段、边界、DAG、白名单、状态一致性和 Schema 导出。 | 后续修改契约时可立即发现不兼容变更或生成物漂移。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记 P0-04 创建/修改文件及其职责。 | 保持用户要求的长期文件职责索引。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-04 公共契约、单位、枚举、工具白名单、验证证据、限制与 P0-05 输入。 | 新任务可从这里无损接续 P0-05，不必重新推断 P0-04 决策。 |

## 2026-08-19：P0-05 Context Engineering

本步所有核心 Python 模块均提供中文模块、类、函数和关键分支注释。五个 Prompt 文件也分别写明职责、禁止事项和输出要求，便于脱离运行框架逐份学习。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `agent/context/contracts.py` | 定义有限上下文、来源证据、动态状态、预算快照、状态摘要、五节点输出及 `success/fallback/human` 路由契约；递归拒绝完整历史、RunState、state_delta 和原始 ToolResult 旁路。 | P0-06 可据此持久化 Prompt/预算元数据；P0-13 直接复用节点输入输出，不另造状态形状。 |
| 新建 | `agent/context/summarizer.py` | 将完整 `RunState` 压缩为带版本和时间的 `StateSummary`，最多保留三条观测结论，不复制完整工具载荷或状态差量。 | P0-13 每次调用模型前必须先摘要，不能把 Checkpoint 全量塞进 Prompt。 |
| 新建 | `agent/context/builder.py` | 从任务预算、累计用量、可选 RunState、动态快照及带来源证据构建 `NodeContext`。 | P0-07 的 RAG 结果和 P0-12 的工具结果必须通过此入口标明来源/版本/时间。 |
| 新建 | `agent/context/prompt_registry.py` | 注册五个 Prompt 的 ID、版本、模板和输出 Pydantic 模型；运行时把 `model_json_schema()` 注入模板，并只构造 system/user 两条当前消息。 | P0-13 通过注册表选择节点；P0-17 Trace 应记录 prompt_id、prompt_version 和 context digest。 |
| 新建 | `agent/context/budget.py` | 提供确定性 Token 估算和调用前预算门禁；输入/输出/tool/time 不足转 fallback，重规划次数耗尽转 human。 | P0-13/P0-15 复用同一门禁，不能仅依靠 Prompt 自觉遵守预算。 |
| 新建 | `agent/context/nodes.py` | 实现五个不依赖 LangGraph 的同步节点入口和调用后真实 usage/耗时复核；只通过 `ModelProviderProtocol` 调模型。 | P0-13 只需把这些入口接入状态图，不应复制其 Prompt 或预算逻辑。 |
| 新建 | `agent/context/__init__.py` | 汇总导出 P0-05 的公共契约、构造器、摘要器、Prompt 定义及五个具名节点。 | 为测试、未来 API 和 LangGraph 提供稳定导入路径。 |
| 新建 | `agent/context/prompts/understand_goal.md` | 独立目标理解 Prompt；输出 `TaskContract`，禁止规划、执行、虚构来源或绕过审批。 | P0-13 `understand_goal` 节点使用。 |
| 新建 | `agent/context/prompts/plan_tasks.md` | 独立规划 Prompt；输出 `PlanTasksOutput`，要求白名单工具、合法 DAG、预算与回退策略。 | P0-13 `plan_tasks` 节点使用。 |
| 新建 | `agent/context/prompts/verify_observation.md` | 独立观察验证 Prompt；输出 `ObservationVerification`，禁止只凭工具 success 宣布业务完成。 | P0-13 verify 分支和 P0-15 故障分类使用。 |
| 新建 | `agent/context/prompts/replan.md` | 独立局部重规划 Prompt；输出 `ReplanOutput`，只替换受影响未完成子图并保留副作用证据。 | P0-14 局部重规划器使用。 |
| 新建 | `agent/context/prompts/compose_report.md` | 独立报告 Prompt；输出 `FinalReport`，严格区分已验证事实、未完成事项和风险。 | P0-13 完成节点和 P0-17 证据报告使用。 |
| 修改 | `services/model_gateway/protocols.py` | 为结构化生成增加只能收紧的调用级 `max_output_tokens`、`timeout_seconds` 参数。 | 独立节点可以把剩余 Token/时间变成真实请求上限。 |
| 修改 | `services/model_gateway/provider.py` | 实施调用级 Token/时间上限；首次生成和一次 Schema 修复共享总额度，并累计两次调用 usage。 | 防止修复请求重复获得完整预算；P0-17 可记录准确总用量。 |
| 修改 | `services/model_gateway/contracts.py` | 给 `StructuredGeneration` 增加 `total_usage`，保留最终 call 的同时返回所有尝试累计 Token。 | 节点预算记账不再漏掉 Schema 修复调用。 |
| 修改 | `docs/MODEL_GATEWAY.md` | 补充调用级 Token/时间只能收紧、修复共享总预算和 `total_usage` 语义。 | P0-03 网关使用说明与 P0-05 新公共接口保持一致。 |
| 修改 | `pyproject.toml` | 把 `agent/context/prompts/*.md` 登记为 Python 包数据。 | 安装 wheel 后 Prompt 文件仍可由注册表读取。 |
| 修改 | `scripts/export_schemas.py` | 在 P0-04 八个 Schema 之外增加四个 P0-05 输出 Schema；`understand_goal` 继续复用已有 `TaskContract`。 | 一条命令可重新生成当前全部 12 个公共 Schema。 |
| 新建 | `docs/schemas/PlanTasksOutput.schema.json` | `plan_tasks` 的输出 Schema。 | Prompt 注入、P0-13 规划节点和接口校验使用。 |
| 新建 | `docs/schemas/ObservationVerification.schema.json` | `verify_observation` 的输出 Schema。 | 观察验证与确定性路由使用。 |
| 新建 | `docs/schemas/ReplanOutput.schema.json` | `replan` 的输出 Schema。 | P0-14 局部重规划版本及子图校验使用。 |
| 新建 | `docs/schemas/FinalReport.schema.json` | `compose_report` 的输出 Schema。 | P0-13/P0-17 报告生成和持久化使用。 |
| 新建 | `tests/unit/test_p005_context_engineering.py` | 25 个专项用例覆盖五份 Prompt、实时 Schema、摘要裁剪、来源/版本/时间、完整历史阻断、五节点独立执行及预算路由。 | 后续接入 LangGraph 时可确认没有改变 P0-05 的独立行为。 |
| 修改 | `tests/unit/test_model_provider.py` | 增加调用级预算只能收紧、不能放宽，以及 Schema 修复累计 usage/共享输出额度测试。 | 防止未来网关修改绕过 P0-05 预算。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记 P0-05 全部文件职责。 | 保持长期文件职责索引完整。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 Prompt ID/版本、输出模型、上下文边界、预算路由、验证证据、限制和 P0-06/P0-13 接入信息。 | 后续任务无需重新推断 P0-05 设计。 |

## 2026-08-19：P0-05 2-shot 提示词升级

本次没有新增业务模型或后续工作包功能。核心加载逻辑增加了中文 docstring 和关键校验注释；五份 Markdown 示例本身使用中文解释其职责、边界和禁止复制规则。12 份 JSON Schema 已重新导出验证，但业务模型未变化，因此生成物不逐文件重复登记。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `agent/context/prompt_registry.py` | 把五个 Prompt 版本升至 `1.1.0`；解析恰好两组带固定标记的 JSON 示例，并在渲染前用对应 Pydantic 输出模型逐组校验。 | 后续修改示例时，数量、JSON 格式或契约漂移会在调用模型前被确定性拒绝。 |
| 修改 | `agent/context/prompts/understand_goal.md` | 增加“完整低风险合同”和“高风险且待审批合同”两组输入/输出示例。 | 目标理解节点获得审批、缺失信息和预算字段的 2-shot 参照。 |
| 修改 | `agent/context/prompts/plan_tasks.md` | 增加“读取状态后分配”的合法 DAG 与“高风险先审批”两组示例。 | 规划节点获得依赖、工具白名单、风险、审批和回退字段的 2-shot 参照。 |
| 修改 | `agent/context/prompts/verify_observation.md` | 增加“证据满足条件后继续”和“AMR 离线后重规划”两组示例。 | 验证节点获得 verified、decision、证据和受影响实体之间关系的 2-shot 参照。 |
| 修改 | `agent/context/prompts/replan.md` | 增加“仅替换受影响子图”和“无安全可行替代时转人工”两组示例。 | P0-14 可继续复用版本加一、保留/失效集合和局部 DAG 边界；预算耗尽仍由模型外门禁处理。 |
| 修改 | `agent/context/prompts/compose_report.md` | 增加“全部完成”和“审批超时需人工处理”两组示例。 | 报告节点获得事实、未完成订单、风险、证据和预算用量的 2-shot 参照。 |
| 修改 | `tests/unit/test_p005_context_engineering.py` | 在原有 25 个专项用例中逐一检查版本、2-shot 章节、恰好两组不同示例及示例输出模型类型。 | 防止任一 Prompt 退回 zero-shot、增加第三组示例或留下失效答案。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本次全部源码、Prompt、测试和文档职责变化。 | 保持长期文件职责索引完整。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 Prompt `1.1.0`、2-shot 校验机制、验证证据、Token 影响及在线评测限制。 | P0-06/P0-13/P0-17 能使用正确版本和审计边界。 |

## 2026-08-19：P0-05 Fast Qwen 在线验收

本步新增的测试脚本包含中文模块说明、函数 docstring 和关键边界注释。模型运行日志保存在 `tmp/`，属于可删除的运行时生成物，不是源码交付物，也不逐文件登记。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `scripts/smoke_p005_prompts.py` | 启动门禁通过后，用真实 Provider 分别运行五个 2-shot 节点；检查 Pydantic 输出、示例污染、工具白名单、观测结论、重规划版本和报告事实，并输出尝试次数与 Token 证据。 | 后续修改 Prompt 或模型 Profile 后可重复执行同一套在线验收，不再依赖一次性命令。 |
| 修改 | `scripts/run_smoke.ps1` | 为 pytest 分配唯一的项目内临时目录、禁用非必要持久缓存，并在结束时恢复 `TEMP/TMP`，解决受管环境无权写用户 Temp 和既有缓存的问题。 | 推荐的一键验收命令可在本地终端和受管 Agent 环境保持一致，不改变测试本身。 |
| 修改 | `docs/MODEL_GATEWAY.md` | 增加五节点真实 Prompt 冒烟命令和语义检查范围。 | 模型网关文档同时覆盖最小 Schema 稳定性和 P0-05 实际节点验证。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 在 Fast Qwen 启动流程中加入五节点脚本、通过标准和失败收集行为。 | 后续操作者能按固定顺序完成健康检查、基础冒烟和 Prompt 冒烟。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本次脚本与文档职责，并区分 `tmp/` 运行日志。 | 保持长期文件职责索引完整。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录真实模型版本、25/25 在线结果、五节点 Token、服务关闭状态、安全提示及未覆盖范围。 | P0-06/P0-13/P0-17 可复用本次真实模型基线，并了解其统计边界。 |

## 2026-08-19：P0-06 FastAPI 与 PostgreSQL 持久化

本步所有核心 Python 代码均包含中文模块说明、Service/Repository/Router 职责、事务边界、失败回滚、安全限制和后续扩展点注释。TOML、INI、Mako、锁文件和纯配置没有核心代码注释需求；其用途与安全边界在下表说明。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `pyproject.toml` | 声明 Alembic 与 multipart 表单解析依赖。 | 安装项目后可执行迁移和文档上传接口。 |
| 修改 | `requirements.in` | 把 Alembic、python-multipart 纳入直接依赖源清单。 | 后续重建锁文件时不会丢失 P0-06 依赖。 |
| 修改 | `requirements.lock` | 固定 `alembic==1.18.4`、`python-multipart==0.0.32`。 | 环境检查与部署复用本次验证版本。 |
| 新建 | `alembic.ini` | 配置迁移目录和日志；仅含无效占位 URL，不保存真实 DSN。 | P0-06 及后续前向迁移共用入口。 |
| 新建 | `migrations/env.py` | 从类型化配置安全加载 PostgreSQL DSN，并把 SQLAlchemy `Base.metadata` 交给 Alembic。 | 后续 migration 可继续比较 ORM 类型，不复制连接配置。 |
| 新建 | `migrations/script.py.mako` | Alembic 后续 revision 的标准文件模板。 | P0-07+ 如需改表，用它新增前向迁移。 |
| 新建 | `migrations/versions/0001_p006_core_tables.py` | 前向创建 8 张核心表、索引、唯一/检查/外键约束；`downgrade()` 明确拒绝删表。 | 所有后续持久化能力以此数据库基线为准，不能回写修改已执行 revision。 |
| 新建 | `scripts/migrate_database.py` | 提供 `upgrade/current/check`，报告核心/辅助/缺失表；故意不提供 downgrade。 | 运维和冒烟可确定性检查 8 表，不会删除核心表。 |
| 修改 | `scripts/run_smoke.ps1` | 在全量 pytest 前执行幂等 Alembic upgrade/表检查，随后运行真实 PostgreSQL 集成测试。 | P0-06 起统一验收要求 PostgreSQL 正常运行并已迁移。 |
| 新建 | `services/persistence/models.py` | 用 SQLAlchemy 2.0 映射 `runs/plans/tasks/tool_calls/effects/approvals/events/documents`；高频字段关系化、完整快照 JSONB、正文 BYTEA。 | P0-07/P0-12/P0-13 直接扩展这些表，不能另造同义持久化模型。 |
| 新建 | `services/persistence/database.py` | 规范 psycopg 3 DSN，创建惰性 Engine、同步 Session 工厂并管理连接池释放。 | API 和测试共享相同会话配置；模块不自动建表。 |
| 新建 | `services/persistence/repositories.py` | 为 8 表提供无 commit/rollback 的查询与写入仓储。 | Service 可在一个事务中组合多表，后续不得在 Repository 内部分提交。 |
| 新建 | `services/persistence/__init__.py` | 汇总导出 ORM、Repository、Engine/Session 工厂。 | 给 Service、迁移和测试提供稳定导入路径。 |
| 新建 | `services/application/exceptions.py` | 定义可安全映射为 4xx/5xx 的稳定应用错误，不泄漏 SQL/驱动细节。 | Router 与未来调用方不依赖 SQLAlchemy 异常类型。 |
| 新建 | `services/application/contracts.py` | 定义运行、计划、事件、审批、文档严格 Pydantic 视图及文档上传元数据。 | API 和 P0-07 索引器共享经过验证的 Service 边界。 |
| 新建 | `services/application/run_service.py` | 负责运行创建/恢复、计划保存、事件读取和审批事务；显式 `runs → events` flush，失败整体回滚。 | P0-13 主闭环应调用此 Service，不直接调用 Repository 或 ORM。 |
| 新建 | `services/application/document_service.py` | 校验 10 MiB 上限、规范文件名、计算 SHA-256，并持久化/恢复文档。 | P0-07 可从受控 Service 取得原始字节和 ACL/版本元数据。 |
| 新建 | `services/application/__init__.py` | 汇总导出应用契约、异常和两个 Service。 | Router 与后续模块使用稳定公共入口。 |
| 新建 | `apps/api/schemas.py` | 定义创建普通/评测运行和审批决定的严格 HTTP 请求体。 | 未声明字段不能借 API 绕过 Pydantic 写入 JSONB。 |
| 新建 | `apps/api/dependencies.py` | 从 FastAPI `app.state` 提供 RunService/DocumentService。 | 保持 Router → Service 方向，并支持测试注入 Session 工厂。 |
| 新建 | `apps/api/routers/runs.py` | 实现 run 创建/查询、plan 查询、持久化 SSE 事件和审批接口。 | P0-13 可在相同运行记录上继续写计划/事件。 |
| 新建 | `apps/api/routers/documents.py` | 实现受限 multipart 文档上传和元数据查询。 | P0-07 将在 Service 后接入分块与索引，不改变上传入口。 |
| 新建 | `apps/api/routers/evals.py` | 实现 `run_kind=eval` 的评测运行创建入口。 | P0-16 可复用运行持久化，不在 P0-06 执行评测套件。 |
| 新建 | `apps/api/routers/__init__.py` | 汇总三个 Router。 | 应用工厂只需装配稳定路由入口。 |
| 修改 | `apps/api/main.py` | 创建/注入数据库运行时和 Service、注册稳定异常处理器及 8 个业务接口，同时保留模型启动门禁。 | API 进程关闭时只释放自己拥有的连接池，不删除数据。 |
| 新建 | `tests/unit/test_p006_persistence.py` | 离线检查 8 表完整性、关系化/JSONB 分工、RESTRICT 外键、Repository 事务边界、禁止 downgrade 和 API extra=forbid。 | 防止后续把字段重新塞入大 JSONB 或引入删表路径。 |
| 新建 | `tests/integration/__init__.py` | 标记真实基础设施测试包。 | pytest 可稳定发现 P0-06 集成测试。 |
| 新建 | `tests/integration/test_p006_postgres.py` | 在真实 PostgreSQL 验证两次实际 INSERT 的整体回滚、跨 Service 恢复、审批、计划/任务、SSE、文档和评测接口；只清理精确测试 ID。 | 这是后续事务改动必须保持通过的回归门禁。 |
| 新建 | `docs/DATABASE.md` | 说明分层、8 表字段策略、迁移命令、事务回滚证据、接口和 Scope 边界。 | 学习代码和推进 P0-07/P0-13 时先从此文档进入。 |
| 修改 | `docs/PROJECT_SETUP.md` | 更新应用/持久化目录职责，并说明统一冒烟现已包含迁移和真实 PostgreSQL 测试。 | 新开发者不会再把 P0-06 测试误认为完全离线。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 在 PostgreSQL 启动流程中加入只向前迁移、8 表检查和禁止手工删表说明。 | 本地启动顺序与 P0-06 部署前置条件保持一致。 |
| 修改 | `README.md` | 更新完成状态并增加数据库说明入口。 | 项目首页能发现 P0-04～P0-06 能力。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记 P0-06 全部源码、配置、测试和文档职责。 | 保持文件职责唯一入口完整。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录数据库公共契约、迁移版本、API、真实回滚证据、服务状态、限制和 P0-07 交接信息。 | 后续任务不需要重新推断表结构和事务边界。 |

## 自动生成物

以下内容不是源码，不需要逐文件登记：

- `build/`
- `**/__pycache__/`
- `.pytest_cache/`
- `tmp/`
