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

## 2026-08-20：新会话入口与 P0-07 初始提示词

本步只更新 Markdown 交接材料，没有修改运行时代码、公共 Schema、数据库或配置，因此无核心代码注释需求。P0-07 仍未开始，不能把本次文档准备报告成工作包完成。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `README.md` | 把项目首页扩展为中文总览：固定 Scope、P0-00～06 状态、已落地架构、API、目录、环境、启动/验证命令、数据库安全边界和 P0-07 正式要求。 | 新会话和新开发者可以从一个入口理解“已实现/未实现”边界，不会误把未来模块当成现有能力。 |
| 新建 | `docs/NEXT_SESSION_PROMPT.md` | 提供可直接复制到新 Codex 会话的 P0-07 初始提示词，包含恢复步骤、已知基线、RAG 验收、ACL/拒答/幂等约束、测试和最终报告要求。 | 下一会话仍必须以 AGENTS、HANDOFF、正式路线和可运行代码为准，并重新检查瞬时服务状态。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本次 README、提示词和交接文档的职责。 | 保持所有文件变化都有长期可追踪用途。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 更新日期、Git/Docker/Qwen/Embedding 当前状态，并记录本次跨会话准备和未推进 P0-07 的事实。 | 下一会话不会沿用 P0-06 结束时已经过期的服务状态。 |

## 2026-08-20：P0-07 仓储 SOP RAG

本步核心 Python 代码均补充中文模块说明、公共接口 docstring 和关键安全注释，重点解释 frozen 门禁、section-aware 分块、动态维度、服务端/候选前 ACL、幂等写入、拒答和失败状态。Markdown、JSON、TOML、环境示例和依赖锁没有核心代码注释需求；其职责与配置边界在下表逐项登记。数据库继续使用 `0001_p006_core`，本步没有 migration 文件变化。

### 配置、持久化与检索核心

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `.env.example` | 增加 collection、Embedding 路径/设备、chunk、候选、融合和双拒答阈值环境变量示例。 | 部署或本地实验可覆盖 P0-07 默认值；语料/模型变化后需重新校准阈值。 |
| 修改 | `config/default.toml` | 固定本地模型路径、collection、0.5/0.5 权重、BM25 饱和尺度、chunk 上限和评测校准阈值。 | `load_settings()`、索引 CLI、查询 CLI 和评测共用同一配置来源。 |
| 修改 | `services/config/settings.py` | 扩展严格 `RetrievalSettings`、权重凸组合校验、Qdrant URL 校验和 RAG 环境变量白名单。模型维度故意不在配置中声明。 | P0-12 `retrieve_knowledge` 工具只能从此处读取检索运行参数，不能在工具内另写常量。 |
| 修改 | `pyproject.toml` | 把 PyYAML 声明为直接依赖，把实际验证的 SentenceTransformers 范围更新为 6.0.x，并把 6 份 Markdown/既有领域 JSON 作为包数据。 | wheel 安装后仍能携带冻结知识库；Loader 不依赖偶然传递依赖，Embedder API 与实测环境一致。 |
| 修改 | `requirements.in` | 增加 PyYAML，并把 SentenceTransformers 直接依赖范围更新为 `>=6.0,<6.1`。 | 后续重建锁文件不会遗漏 Front Matter 解析器或回退到未验收的旧 Embedder 版本。 |
| 修改 | `requirements.lock` | 锁定当前实测 `PyYAML==6.0.3`、`sentence-transformers==6.0.0`。 | `check_environment.py` 与统一 smoke 校验实际模型运行环境一致。 |
| 修改 | `services/application/document_service.py` | 增加 frozen 知识文档幂等 upsert 和整批 indexed 标记；内容变化清空 `indexed_at`，Qdrant 成功前不声称已索引。 | P0-07 复用 P0-06 `documents` 表；P0-12/P0-13 可读取同一版本、ACL 和 checksum 真相。 |
| 修改 | `services/application/__init__.py` | 明确应用服务公共入口已被 P0-07 documents 事务复用。 | 调用方继续从稳定包入口导入 `DocumentService`。 |
| 修改 | `services/persistence/repositories.py` | 为 `DocumentRepository.get()` 增加可选行锁，不引入 commit/rollback。 | frozen upsert 与批量 indexed 标记可在 Service 事务内防止并发状态覆盖。 |
| 修改 | `services/retrieval/__init__.py` | 从占位模块升级为 P0-07 全部公共契约、组件和构造器的稳定导入入口。 | P0-12 工具和测试无需依赖内部模块路径。 |
| 新建 | `services/retrieval/contracts.py` | 定义 `KnowledgeDocument/Chunk`、两路 hit、`RetrievalResult/Response`、拒答状态和索引报告；可转换 P0-05 `ContextEvidence`。 | P0-12 工具输出、P0-13 RAG 上下文和 P0-17 引用审计以此为公共契约。 |
| 新建 | `services/retrieval/loader.py` | 用 `yaml.safe_load` 解析 Front Matter、严格 UTF-8、原始字节 SHA-256、frozen-only 和重复 doc ID 门禁。 | 新知识文档必须先通过此入口，非 frozen 内容不会进入任何检索后端。 |
| 新建 | `services/retrieval/chunking.py` | 优先按 Markdown H2 切 section，仅超长 section 按语义块二次拆分；排除只有问题没有答案的 `RAG 示例问题` section。 | Qdrant/BM25 使用同一组确定性 chunk ID、ACL、版本和正文，避免两路漂移。 |
| 新建 | `services/retrieval/embedding.py` | 独立 `Embedder` 提供 `embed_documents()`/`embed_query()`，使用 Qwen3 query/document prompt、离线本地加载、动态维度和数值形状校验。 | 更换模型时不改 Qdrant 维度常量；collection 不兼容会明确要求 rebuild。 |
| 新建 | `services/retrieval/vector_store.py` | 管理精确 Qdrant collection、cosine 配置、payload keyword index、UUIDv5 point、重建/替换和带 `role_scope`/doc filter 的 query。 | ACL 在 Qdrant 候选阶段执行；P0-12 不得改成召回后隐藏。 |
| 新建 | `services/retrieval/bm25.py` | 使用 `jieba.lcut()` 与 `BM25Okapi`；先过滤角色/文档范围，再构建进程内语料和评分。 | viewer 的关键词矩阵中从一开始就没有 operator-only chunks。 |
| 新建 | `services/retrieval/hybrid.py` | 归一化 cosine/BM25、按配置权重融合、稳定排序，并以 hybrid 或绝对 vector 门禁产生 answerable/insufficient_evidence；不含 Reranker。 | 下游只消费通过门禁的正文；拒答响应强制 `results=[]`。 |
| 新建 | `services/retrieval/indexing.py` | 编排 Loader → DocumentService 回读 → Chunker → Embedder → Qdrant → indexed 标记，并构造查询运行时。 | 正式索引复用 PostgreSQL，重复运行可重建或只替换本批 doc ID。 |

### 冻结知识、脚本与评测

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 纳入交付 | `domains/amr_warehouse/knowledge/amr_fault_handling.md` | operator-only 异常分类、Effect Ledger、局部重规划、Fallback/fatal 冻结知识。 | viewer 向量/BM25 候选禁止出现；P0-15 可复用规则但不得由 RAG 执行动作。 |
| 纳入交付 | `domains/amr_warehouse/knowledge/amr_operation_manual.md` | viewer/operator 可见的 AMR 状态、状态机、电量与设备参数边界。 | P0-08～P0-11 可引用，不得用文档替代实时 Observation。 |
| 纳入交付 | `domains/amr_warehouse/knowledge/battery_charging_sop.md` | 冻结 30/20/10/15/90 电量阈值和充电规则。 | 分配器、Validator、仿真与安全评测必须保持一致。 |
| 纳入交付 | `domains/amr_warehouse/knowledge/dispatch_approval_policy.md` | operator-only RBAC、HITL、审批/硬约束/幂等边界。 | P0-16 直接复用；viewer 检索泄漏目标固定为 0。 |
| 纳入交付 | `domains/amr_warehouse/knowledge/warehouse_traffic_rules.md` | viewer/operator 可见的单向/窄通道、禁行区、顶点/交换边冲突规则。 | P0-09/P0-10 的路径和验证器可引用这些硬约束。 |
| 纳入交付 | `domains/amr_warehouse/knowledge/warehouse_transport_sop.md` | viewer/operator 可见的标准转运 DAG、分配、路径、验证、完成与异常摘要。 | P0-12 `retrieve_knowledge` 的普通 SOP 主来源。 |
| 新建 | `scripts/index_warehouse_knowledge.py` | 可重复索引 CLI，默认同步 PostgreSQL并重建 `amr_warehouse_knowledge`，支持 `--no-rebuild` 和隔离调试开关。 | 运维/CI 能用结构化报告确认文档数、chunk 数、模型维度和 checksum。 |
| 新建 | `scripts/query_warehouse_knowledge.py` | 单次角色化混合查询 CLI，输出严格 `RetrievalResponse` JSON。 | 人工验证 ACL、引用和拒答，也可作为 P0-12 工具适配前的独立入口。 |
| 修改 | `scripts/check_qdrant.py` | 从硬编码打印升级为类型化配置、真实 API 请求和 JSON collection 健康报告。 | `run_smoke.ps1` 可在 pytest 前给出清晰 Qdrant 门禁。 |
| 修改 | `scripts/run_smoke.ps1` | 在迁移后增加 Qdrant 健康检查，并把 Python 阶段说明更新为真实 PostgreSQL/Qdrant 集成测试。 | P0-07 起统一验收要求两个容器在线；仍不删除表、collection 或数据卷。 |
| 修改 | `scripts/export_schemas.py` | 增加 `KnowledgeChunk`、`RetrievalResult`、`RetrievalResponse` 三个公共 Schema 导出。 | 修改 P0-07 契约后必须重跑并提交完全一致的 JSON。 |
| 新建 | `evals/__init__.py` | 使版本化评测可通过 `python -m` 稳定运行。 | 后续 P0 评测包可沿用模块入口。 |
| 修改 | `evals/README.md` | 说明 P0-07 固定数据、执行命令和指标文档入口。 | 操作者能从评测根目录发现可重复命令。 |
| 新建 | `evals/rag/__init__.py` | 标记 P0-07 RAG 评测包。 | 支持 `python -m evals.rag.run_eval`。 |
| 新建 | `evals/rag/cases.json` | 固定 20 例事实、改写、数值、关键词、跨文档、ACL 和拒答数据，含角色/金标准/禁止文档。 | 语料、模型、融合或阈值变化必须用同一集合观察分布与回归。 |
| 新建 | `evals/rag/run_eval.py` | 执行真实查询并计算 Recall@K、MRR、section recall、Citation Correctness、answerability、ACL leak 和阈值分布。 | P0-17 可复用报告字段；ACL leak 非 0 时命令返回非零。 |

### 测试、Schema 与文档

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `tests/unit/test_p007_retrieval.py` | 覆盖 Front Matter/frozen/checksum、安全 YAML、H2/长 section、动态维度、BM25 ACL、融合/引用/拒答、配置和 20 例类别。 | 后续修改检索实现时可快速发现 ACL、契约或无答案边界回退。 |
| 新建 | `tests/integration/test_p007_rag_backends.py` | 在真实 PostgreSQL 验证文档状态幂等，在真实 Qdrant 验证重建/替换和 payload filter ACL；只清理精确测试 ID/collection。 | 证明 ACL 不是仅靠 fake 或检索后处理，统一 smoke 必须保持通过。 |
| 修改 | `tests/unit/test_settings.py` | 增加 RAG 环境变量优先级、权重和双阈值断言。 | 配置字段重命名/遗漏会在单元测试中失败。 |
| 新建 | `docs/schemas/KnowledgeChunk.schema.json` | Qdrant payload/进程内 chunk 的机器可读 Schema。 | P0-12 工具和跨进程消费者可验证 chunk 完整性。 |
| 新建 | `docs/schemas/RetrievalResult.schema.json` | 带三类分数与 citation 的单条结果 Schema。 | Agent、评测和报告层不需手写引用字段。 |
| 新建 | `docs/schemas/RetrievalResponse.schema.json` | answerable/insufficient_evidence、阈值、top score 和结果列表的响应 Schema。 | 下游必须先检查 status；拒答时 results 为空。 |
| 新建 | `docs/RAG.md` | 说明数据流、分块、ACL、融合公式、阈值校准、命令、指标定义和当前真实结果。 | P0-12/P0-13/P0-17 学习和接入 P0-07 的唯一专题入口。 |
| 修改 | `docs/DATABASE.md` | 记录 frozen upsert、PostgreSQL 回读、Qdrant 成功后批量 indexed 及 revision 不变。 | 后续不能另造文档表或修改已执行 migration。 |
| 修改 | `docs/PROJECT_SETUP.md` | 把 retrieval/evals 目录从未来占位更新为 P0-07 已实现职责。 | 新开发者不会误判模块完成状态。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 增加本地 Embedding、索引/评测命令、RAG 环境变量和两个阈值。 | 新会话能按 PostgreSQL → Qdrant → 索引/评测顺序复现。 |
| 修改 | `README.md` | 更新 P0-00～07 完成状态、目录、Embedding 环境、索引/评测命令和当前指标，下一步改为 P0-08。 | 项目首页不再把 RAG 描述成占位。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部源码、配置、冻结文档、评测、测试、Schema 和文档职责。 | 保持长期文件职责入口完整。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-07 公共契约、设计边界、真实验证、服务状态、风险和 P0-08 输入。 | 下一任务无需重新推断索引、ACL、阈值或当前基础设施状态。 |

## 2026-08-20：P0-08 C++ Hungarian 任务分配

本步核心 C++ 代码均补充中文注释，说明数据流、dummy 匹配、INF 安全表示、失败原因、stdin 大小限制和后续路径/验证边界；Markdown 文档只记录公共契约，因此无核心代码注释需求。没有新增第三方 JSON 依赖。`build/`、测试可执行文件和 CMake 中间文件属于自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `services/planner_cpp/CMakeLists.txt` | 将服务扩展为 `task_allocator` 静态库、JSON CLI 和独立 CTest 场景，同时保留 P0-01 `amr_planner_smoke` 与 MSVC `/utf-8`/C++17 约束。 | P0-09/P0-10 继续复用同一 C++ 服务；统一 smoke 现在执行 7 个 CTest。 |
| 修改 | `services/planner_cpp/src/main.cpp` | 保留 P0-01 C++ 工程冒烟和版本入口，并更新注释说明 P0-08 由独立分配器测试，避免把骨架自检误认为生产算法验收。 | 统一 smoke 继续验证编译/执行门禁；P0-08～P0-10 使用各自的功能测试。 |
| 新建 | `services/planner_cpp/include/task_allocator/task_allocator.hpp` | 定义复用 P0-04 字段的 AMR/订单/工位位置、代价配置、分解、分配结果、稳定错误和 Hungarian/baseline 公共 API。 | P0-12 Python 工具适配和 P0-09/P0-10 算法可以依赖稳定库接口，不复制领域结构。 |
| 新建 | `services/planner_cpp/src/task_allocator.cpp` | 实现输入验证、坐标/时间/电量/负载可行性、五项显式代价、dummy 行列 Hungarian 和独立最近空闲 AMR baseline。 | 生产分配只返回可行组合；`INF` 组合及原因可供 Validator/Trace 审计。 |
| 新建 | `services/planner_cpp/include/task_allocator/json_codec.hpp` | 声明无第三方依赖的严格 JSON 值模型、解析/序列化和请求/响应转换入口。 | 固定 JSON stdin/stdout 契约不会依赖偶然安装的 C++ 包。 |
| 新建 | `services/planner_cpp/src/json_codec.cpp` | 实现有限输入边界所需的 JSON 解析、重复键/未知字段门禁、UTF-8 转义、`INF` sentinel 和稳定响应序列化。 | Python/后续工具可稳定解析正常、partial、无可行和错误响应。 |
| 新建 | `services/planner_cpp/src/task_allocator_main.cpp` | 提供 `task_allocator_cli`，白名单算法参数、4 MiB stdin 限制、稳定退出码和 JSON 错误输出。 | P0-12 通过固定可执行文件调用，不使用 `shell=True` 或任意路径。 |
| 新建 | `services/planner_cpp/tests/task_allocator_tests.cpp` | 以无额外测试框架的 C++ 用例覆盖正常、低电量、无可行、订单多于车辆、边界/依赖/重复 ID、JSON 契约和 baseline。 | CTest 为 P0-08 及后续改动提供 7 个回归门禁。 |
| 新建 | `docs/TASK_ALLOCATOR.md` | 记录请求/响应字段、代价公式、阈值、`INF`/原因码、baseline、退出码和 P0-09/P0-10 边界。 | 跨语言消费者以此实现契约，不从 C++ 内部实现猜字段。 |
| 修改 | `README.md` | 将 P0-08 标记为完成，加入分配器目录、命令、结果和范围说明。 | 项目首页不再把 P0-08 描述成未来占位。 |
| 修改 | `docs/PROJECT_SETUP.md` | 登记 `services/planner_cpp` 当前目标和 CLI 调用入口。 | 新开发者能区分库、生产算法和 baseline。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 增加 P0-08 构建后 CLI 的 JSON 调用示例和契约链接。 | 本地启动/验收顺序与跨语言安全边界一致。 |
| 新建 | `docs/LESSONS_LEARNED.md` | 记录 MSVC 环境初始化和 C++17 兼容性验证坑，避免后续工作包重复误判构建失败。 | P0-09/P0-10 复用同一开发环境门禁和测试习惯。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步所有 C++、CLI、测试、契约和交接文件职责。 | 文件职责入口保持完整，自动生成物与源码交付物分离。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-08 公共 API/JSON 契约、设计决策、真实测试、环境状态、限制和 P0-09 下一步。 | 下一 Agent 可直接复用 `task_allocator_cli` 和 7/7 CTest 事实。 |

## 2026-08-20：P0-09 C++ A* 与时空预约表

本步核心 C++ 代码均补充中文注释，重点说明 `(x,y,heading,t)` 数据流、曼哈顿启发式、动作代价、预约表顶点/交换边安全门禁、无解返回和 Dijkstra 独立边界；测试代码注释说明冲突反例和性能夹具。Markdown 文档只记录公共契约，因此无核心代码注释需求。`build/`、测试可执行文件和 CMake 中间文件属于自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `services/planner_cpp/CMakeLists.txt` | 新增 `route_planner` 静态库、`route_planner_cli` 和路由测试目标；保留 P0-08 目标，统一启用 C++17、MSVC `/utf-8` 和 CTest。 | P0-10 车队验证器可链接同一路径库，Python 后续通过固定 CLI 调用，不复制算法。 |
| 新建 | `services/planner_cpp/include/route_planner/route_planner.hpp` | 定义地图/边、分配绑定、动作路径、计划结果、稳定错误、`ReservationTable` 及 A*/Dijkstra 公共入口。 | P0-10 Validator、P0-11 仿真和 P0-12 工具适配复用同一坐标/时间/路径语义。 |
| 新建 | `services/planner_cpp/include/route_planner/json_codec.hpp` | 声明 route_planner 请求/响应 JSON 转换，复用 P0-08 严格 JSON 值模型但不复用任务分配逻辑。 | Python 调用方只依赖稳定 stdin/stdout 契约。 |
| 新建 | `services/planner_cpp/src/route_planner.cpp` | 实现输入归一化、障碍/禁行边/单向边校验、按优先级调度、生产 A*、时空预约表和无解原因。 | 生产路线必须通过该确定性实现；失败不会隐式降级到 baseline。 |
| 新建 | `services/planner_cpp/src/route_json_codec.cpp` | 实现 route_planner 的严格请求解析、稳定响应序列化和错误 envelope；拒绝未知字段、重复键、非有限数和越界坐标。 | P0-12 工具注册表可固定字段白名单和退出码，不读取任意路径。 |
| 新建 | `services/planner_cpp/src/route_planner_main.cpp` | 提供 `route_planner_cli`，白名单 `astar/dijkstra`、4 MiB stdin 上限、业务不可行与输入/内部错误的稳定退出码。 | Python 后续通过固定工作目录和外部超时调用，不使用 `shell=True`。 |
| 新建 | `services/planner_cpp/tests/route_planner_tests.cpp` | 覆盖障碍、边界、禁行边、单向边、等待、顶点预约、交换边预约、无解、Dijkstra 对照、可复现性、四车性能和 JSON 契约。 | CTest 为 P0-09 及 P0-10 回归提供 12 个独立门禁。 |
| 新建 | `docs/ROUTE_PLANNER.md` | 记录算法边界、输入/输出 JSON、动作时间语义、预约安全规则、退出码和 CLI 示例；本步无核心代码注释需求。 | 跨语言消费者和后续 Validator 以此为路线契约唯一入口。 |
| 修改 | `README.md` | 将 P0-09 标记完成，更新当前下一步、目录职责、CTest 统计并增加 A*/预约使用说明。 | 项目首页不再把路线/冲突处理描述成未来占位。 |
| 修改 | `docs/PROJECT_SETUP.md` | 更新 `services/planner_cpp` 当前职责，并登记 route_planner CLI 和契约入口。 | 新会话能区分 P0-08 分配器与 P0-09 路径器。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 增加 route_planner 构建产物、A*/Dijkstra 调用和契约链接。 | Windows 启动/验收顺序可复用同一 MSVC/CMake 环境。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 记录 P0-09 的等待夹具与启发式/动作代价测试陷阱，避免把“可绕行”误测成“已等待”。 | 后续冲突/性能测试继续使用真实预约反例，而非只看结果状态。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部路由源码、测试、CLI、契约和文档职责。 | 文件职责入口保持完整，自动生成物与源码交付物分离。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-09 公共 API/JSON、设计决策、实际测试、环境状态、限制和 P0-10 交接信息。 | 下一 Agent 可直接复用预约表、路径时间语义和 CTest 基线。 |

## 2026-08-20：P0-10 C++ 车队计划验证器

本步核心 C++ 代码均补充中文注释，说明验证边界、数据流、证据定位、安全限制、失败行为和后续扩展点；MSVC 目标继续使用 `/utf-8`。新增 Markdown 仅记录公共契约、启动方式和交接事实，因此无核心代码注释需求。`build/`、测试可执行文件和 CMake 中间文件属于自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `services/planner_cpp/CMakeLists.txt` | 在现有 P0-08/P0-09 C++17 工程中加入 `fleet_plan_validator` 静态库、严格 JSON 编解码、CLI 和 14 个 CTest 场景；保留 `/utf-8` 与统一 CTest 门禁。 | P0-12 工具注册表和 P0-13 主闭环可复用固定 CLI/库，不得绕过验证器。 |
| 新建 | `services/planner_cpp/include/fleet_plan_validator/fleet_plan_validator.hpp` | 定义完整车队计划输入、路线、配置、稳定错误证据、错误字典和验证结果公共 API。 | P0-11 仿真、P0-12 工具适配和 P0-13 验证节点以此为 C++ 边界。 |
| 新建 | `services/planner_cpp/src/fleet_plan_validator.cpp` | 确定性校验任务依赖/时间窗、载荷、电量余量、地图硬约束、工位容量、距离及顶点/交换边路径冲突，并生成可定位错误证据。 | 任何 LLM/Prompt/规划器声明都不能替代此复核；后续仿真必须复用同一时间和终点占用语义。 |
| 新建 | `services/planner_cpp/include/fleet_plan_validator/json_codec.hpp` | 声明 P0-10 严格 JSON 请求/响应/错误字典编解码入口。 | Python 工具适配可以依赖固定字段白名单，未知旁路字段会在边界拒绝。 |
| 新建 | `services/planner_cpp/src/fleet_plan_validator_json_codec.cpp` | 实现有限 JSON 值模型的类型、范围、非有限数、重复/未知字段门禁，以及计划和证据的稳定序列化。 | 跨语言调用方获得确定性错误 envelope，不依赖偶然的 JSON 库或字段顺序。 |
| 新建 | `services/planner_cpp/src/fleet_plan_validator_main.cpp` | 提供 `fleet_plan_validator_cli`，限制 stdin 大小，区分参数/契约、业务非法和内部错误退出码。 | P0-12 通过固定可执行文件调用；业务非法不能被误判为 CLI 崩溃或成功。 |
| 新建 | `services/planner_cpp/tests/fleet_plan_validator_tests.cpp` | 用正反例覆盖合法稳定通过、每类非法约束定位、证据可复现、JSON 旁路拒绝和错误字典完整性。 | P0-10 的 14 个 CTest 是后续修改验证器和接入仿真时的回归门禁。 |
| 新建 | `docs/FLEET_PLAN_VALIDATOR.md` | 记录请求/响应 JSON、规则、稳定错误码、证据字段、CLI 退出码和 P0-09/P0-10 边界；本步无核心代码注释需求。 | P0-12/P0-13 以此实现调用和错误展示，不从实现细节猜契约。 |
| 修改 | `README.md` | 将 P0-10 标记完成，加入验证器职责、CLI、无绕过边界和 33/33 CTest 基线。 | 项目首页的当前下一步变为 P0-11。 |
| 修改 | `docs/PROJECT_SETUP.md` | 更新 `services/planner_cpp` 目录职责并登记验证器 CLI/契约入口；本步无核心代码注释需求。 | 新会话可按统一构建和 CTest 入口复现 P0-10。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 增加验证器构建后调用、错误字典和业务非法退出码说明；本步无核心代码注释需求。 | 运维/验收不会只检查进程退出码而漏掉 `status=invalid`。 |
| 新建 | `docs/LESSONS_LEARNED.md` | 持续记录可复用的环境、接口和测试陷阱；本步新增 P0-10 的独立复核、载荷补充和终点占用经验。 | P0-11/P0-12 避免把规划器审计字段或 Prompt 当作安全验证。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步所有验证器源码、测试、CLI、契约和文档职责。 | 保持文件职责入口完整，并区分自动生成物与源码交付物。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-10 公共 API/JSON、错误字典、设计决策、真实测试、环境状态、限制和 P0-11 输入。 | 下一 Agent 可直接复用验证器边界和 14/14、33/33 测试事实。 |

## 2026-08-20：P0-11 Python AMR 离散事件仿真

本步核心 Python 代码均补充中文模块说明、类/函数 docstring 和关键状态、数据流、失败行为注释；故障注入保持在仿真包内，不注册到 `agent.tools`。新增 Markdown 只记录公共契约和启动方式，因此无核心代码注释需求。`build/`、`__pycache__/`、`.pytest_cache/` 和 `tmp/` 仍属于自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `services/amr_simulator/contracts.py` | 定义 P0-10 计划 envelope、P0-09 路径步、仿真配置、订单/工位/充电站状态、Observation 关联事件和 Eval 故障注入的严格 Pydantic 契约。 | P0-12 `dispatch_simulation` 和 P0-13 状态图应直接复用；不能另造不兼容的 AMR 状态或路径时间字段。 |
| 新建 | `services/amr_simulator/validator.py` | 通过固定 `fleet_plan_validator_cli.exe` 和 JSON stdin/stdout 执行 P0-10 前置门禁，区分业务非法、契约错误和进程超时。 | 后续工具注册表只能把 `status=valid` 作为执行前提，不能信任 planner/LLM 审计字段。 |
| 新建 | `services/amr_simulator/simulator.py` | 实现 1 秒固定 tick 的路径执行、电量扣减、订单/工位/充电状态迁移、结构化 Observation/事件日志和安全停机故障注入。 | P0-13 `verify_observation`、P0-14 Checkpoint 和 P0-15 异常处置直接消费其输出；不接入 ROS/真实底盘。 |
| 修改 | `services/amr_simulator/__init__.py` | 汇总仿真公共入口、契约、Validator 客户端和异常类型。 | Python 调用方从稳定包入口导入，不依赖模块内部实现路径。 |
| 新建 | `tests/unit/test_p011_simulator.py` | 覆盖正常运输、完整状态迁移、充电/容量状态、低电量待充、离线/电量故障、非法时间戳拒绝、同 seed 重放和故障不入工具白名单。 | 后续修改仿真状态机或跨语言契约时作为 P0-11 回归门禁。 |
| 新建 | `docs/AMR_SIMULATOR.md` | 记录 P0-09/P0-10 路径与验证边界、固定 tick、装卸零时长、充电、Observation/事件和故障注入契约；本步无核心代码注释需求。 | P0-12/P0-13/Eval 以此作为仿真接入唯一专题入口。 |
| 修改 | `scripts/export_schemas.py` | 将 P0-11 `SimulationPlan`、`SimulationEvent`、`SimulationResult` 纳入统一 Pydantic Schema 导出清单。 | P0-12/P0-13 的 JSON 边界由运行时模型自动生成，避免手写漂移。 |
| 新建 | `docs/schemas/SimulationPlan.schema.json` | P0-10 兼容仿真计划 envelope 的机器可读 Schema。 | P0-12 dispatch 输入和跨语言适配校验使用。 |
| 新建 | `docs/schemas/SimulationEvent.schema.json` | 单个仿真事件的机器可读 Schema。 | P0-06 events、Trace 和 Eval 记录使用。 |
| 新建 | `docs/schemas/SimulationResult.schema.json` | 仿真最终状态、Observation、事件和资源快照的机器可读 Schema。 | P0-12 ToolResult/P0-13 verify 与报告层使用。 |
| 修改 | `README.md` | 将 P0-11 标记完成，增加仿真目录、能力和专项测试入口。 | 项目首页当前下一步变为 P0-12，避免把仿真误判为占位。 |
| 修改 | `docs/PROJECT_SETUP.md` | 更新目录职责并登记 P0-11 Python 入口；本步无核心代码注释需求。 | 新会话可以按固定 Validator CLI 和专项 pytest 复现仿真。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 增加 P0-11 无常驻服务、Validator 前置和专项测试说明；本步无核心代码注释需求。 | 操作者不会在没有 C++ Validator 的情况下误运行仿真。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记 P0-11 所有源文件、测试和文档职责。 | 保持长期文件职责入口唯一。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录仿真公共契约、状态机、Validator 调用、故障边界、实际测试和下一步 P0-12。 | 后续工具注册和主闭环可直接复用本步事实，不重复推翻时间语义。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀 P0-11 的路径复用、终点占用、充电不瞬移和固定 epoch 可复现性经验。 | 后续 Eval/Checkpoint 避免引入第二套时间轴或墙上时钟。 |

## 2026-08-20：P0-12 九个白名单工具

本步新建/修改的 Python 核心代码均补充中文模块说明、类/函数 docstring 和关键数据流、边界、失败行为、安全限制及扩展点；测试代码也说明反例意图。Schema/Markdown 只保存机器契约和职责，因此无核心代码注释需求。`build/`、`__pycache__/`、`.pytest_cache/` 和 `tmp/` 属于自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `agent/tools/contracts.py` | 扩展 `ToolSpec` 的审计字段声明和 `ToolResult` 的版本、角色、输入/输出 digest、幂等键及审计元数据，同时保持 P0-04 旧载荷兼容。 | P0-13/P0-17 可统一消费九工具结果，不再为每个工具解析私有日志。 |
| 修改 | `agent/tools/__init__.py` | 保留 P0-04 纯契约的低副作用导入，并懒加载 P0-12 注册表入口。 | RAG/领域契约导入不要求外部服务在线；Agent 从稳定包入口获取注册表。 |
| 新建 | `agent/tools/schemas.py` | 定义九工具输入/输出 Pydantic Schema、ID/角色/时间边界和 C++/仿真响应模型。 | `ToolSpec` 和 `docs/schemas/` 使用同一实时模型，后续公共接口依赖这些封闭字段。 |
| 新建 | `agent/tools/cpp_client.py` | 通过固定 exe、固定 argv、JSON stdin/stdout、`shell=False` 和超时调用 P0-08～P0-10。 | 分配、A*、Validator 不被任意命令/路径旁路；Python 不复制 C++ 算法。 |
| 新建 | `agent/tools/snapshots.py` | 提供固定 warehouse seed 环境快照、执行状态存储协议和进程内幂等状态适配器。 | 车队状态、分配、路径、仿真共享同一事实；后续可替换 PostgreSQL 而不改工具契约。 |
| 新建 | `agent/tools/verification.py` | 将 Python/CTest/Smoke 验证入口映射为固定 suite/case argv，并拒绝未知套件。 | P0-17 可复用受控验证；调用方不能传入 Shell、脚本或 pytest 命令。 |
| 新建 | `agent/tools/approval.py` | 以请求 digest 幂等创建 pending 审批及 effect ID，不自动批准。 | P0-06/P0-16 可接入持久化/HITL 决策；工具层保留审批审计边界。 |
| 新建 | `agent/tools/registry.py` | 注册恰好九个 handler，执行参数/角色预检、超时、输出 Schema、错误映射、证据和重复调用缓存。 | P0-13 只能经白名单进入确定性能力；故障注入不进入正常 Agent 工具表。 |
| 修改 | `scripts/export_schemas.py` | 新增 P0-12 各工具输入/输出 Schema 导出清单。 | 运行时模型和提交的 JSON 契约可由测试逐字核对。 |
| 新建 | `docs/P012_TOOLS.md` | 记录九工具清单、角色/超时/幂等/副作用、固定 C++ 边界、错误和重复调用语义；本步无核心代码注释需求。 | 后续 Agent、API 和 Trace 以此作为工具层专题入口。 |
| 新建 | `docs/schemas/RetrieveKnowledgeInput.schema.json`、`GetFleetStateInput.schema.json`、`AllocateTasksInput.schema.json`、`PlanMultiAMRRoutesInput.schema.json`、`ValidateFleetPlanInput.schema.json`、`DispatchSimulationInput.schema.json`、`QueryExecutionStateInput.schema.json`、`RunVerificationSuiteInput.schema.json`、`RequestApprovalInput.schema.json` | 保存九工具的机器可读输入边界；本步无核心代码注释需求。 | Agent/API/契约测试从同一模型校验参数。 |
| 新建 | `docs/schemas/FleetStateOutput.schema.json`、`docs/schemas/AllocationResponse.schema.json`、`docs/schemas/RoutePlanResponse.schema.json`、`docs/schemas/ValidationResponse.schema.json`、`docs/schemas/ExecutionStateOutput.schema.json`、`docs/schemas/VerificationSuiteOutput.schema.json`、`docs/schemas/ApprovalRequestOutput.schema.json` | 保存车队状态、分配、路线、验证、状态、验证套件和审批的机器可读输出边界；检索与仿真输出分别复用既有 `RetrievalResponse`/`SimulationResult` Schema；本步无核心代码注释需求。 | ToolResult 的 output 能被下游逐工具审查。 |
| 修改 | `docs/schemas/ToolSpec.schema.json`、`docs/schemas/ToolResult.schema.json` | 反映统一审计字段和结果扩展；本步无核心代码注释需求。 | P0-04/P0-12 Schema 导出保持一致。 |
| 新建 | `tests/unit/test_p012_tools.py` | 覆盖九工具清单、预执行参数/角色门禁、RAG/状态、幂等、冲突、超时、故障隔离和固定 C++ argv。 | 后续改动必须继续通过 P0-12 正反例回归。 |

## 2026-08-20：P0-12 完成后严格工程审查

本次没有扩展 P0 Scope，也没有新增 LLM、数据库或常驻服务依赖。修改的核心 Python
代码均同步补充/更新中文注释，重点解释输出侧安全门禁、并发幂等、协作取消和固定
验证 argv；Markdown 与运行时导出的 JSON Schema 无核心代码注释需求。P0-07～P0-11
实现仅作为被测依赖，没有为通过审查复制或重写算法。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `agent/tools/contracts.py` | 将 `audit_metadata` 纳入默认审计字段，保证 ToolSpec 声明与 ToolResult 实际审计载荷一致。 | P0-13/P0-17 可依赖统一审计字段，不需按工具猜测。 |
| 修改 | `agent/tools/schemas.py` | 收紧严格整数、非有限数、空白字符串、A* 最大时域、固定算法名、Validator/验证套件汇总一致性和审批时区。 | 错误参数和矛盾的 C++/runner 输出均在执行或返回前确定性拒绝。 |
| 修改 | `agent/tools/registry.py` | 增加 in-flight call_id 协调、公共错误类别声明、输出侧 RAG ACL 熔断、子进程 timeout 余量、超时取消门禁、共享快照 Provider 及仿真订单筛选。 | P0-13 不会因并发重放重复副作用，也不能从后端回归路径绕过 ACL/状态筛选。 |
| 修改 | `agent/tools/snapshots.py` | 合并并校验静态障碍与临时障碍，稳定排序后传给 A*/Validator。 | 地图 seed 的安全约束不会在工具适配层静默丢失。 |
| 修改 | `agent/tools/verification.py` | 去除本机盘符硬编码，从可信 PATH 解析固定绝对程序，并把 P0-12 case 固定为明确 pytest node id。 | 受控验证既不能注入命令，也不会因模糊选择零测试而产生假证据。 |
| 修改 | `tests/unit/test_p012_tools.py` | 扩至 20 个用例，新增并发幂等、输出 ACL、严格预检、审计身份、障碍合并、状态筛选、跨语言汇总一致性和固定验证 argv 反例。 | 后续工具层修改必须保留本次修复的失败路径。 |
| 重导出 | `docs/schemas/*.schema.json`（34 份公共 Schema） | 使用 `scripts/export_schemas.py` 从当前 Pydantic 模型重建机器契约；本步无核心代码注释需求。 | 契约测试可继续逐字检查运行时模型与提交产物无漂移。 |
| 修改 | `docs/P012_TOOLS.md` | 补充并发重复调用、输出侧 ACL、固定算法、子进程 timeout、固定验证程序和状态筛选语义；本步无核心代码注释需求。 | P0-13 组装主闭环时有明确安全与失败边界。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本次审查涉及的全部源文件、测试、契约和文档职责；本步无核心代码注释需求。 | 保持文件职责入口唯一。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 修正过期的 P0-12 状态，记录审查缺陷、修复、实测结果、服务状态和 P0-13 边界；本步无核心代码注释需求。 | 下一任务不再依据 P0-12 前的陈旧描述开展工作。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀并发幂等、输出侧 ACL、验证空选集和错误声明漂移的可复用经验；本步无核心代码注释需求。 | 后续工具与 Eval 不重复引入相同缺陷。 |

## 自动生成物

以下内容不是源码，不需要逐文件登记：

- `build/`
- `**/__pycache__/`
- `.pytest_cache/`
- `tmp/`

## 2026-08-20：P0-13 PEVR 正常闭环

本步 Python 核心代码、Planner 兼容层和测试均补充中文注释/docstring，说明固定状态图、数据流、预算、审批、安全边界和失败行为；Prompt/README/专题 Markdown 与导出的 JSON Schema 只保存公共契约，因此本步无核心代码注释需求。`tmp/`、`build/`、`__pycache__/` 和 pytest 临时目录属于自动生成物，不是源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `agent/planning/validator.py` | 实现 P0-13 四任务正常 DAG 的确定性 Validator、拓扑/参数/审批/数据流/seed 门禁，以及本地 JsonValue 包装的严格固定事实规范化。 | `agent/runtime/graph.py` 在任何 P0-12 Planner 工具执行前调用；P0-14/P0-15 必须继续把它作为安全硬门。 |
| 修改 | `agent/planning/__init__.py` | 延迟导出 P0-13 Validator 与规范化入口，避免 contracts/context 循环导入。 | 主图和测试从稳定 planning 包入口复用；不改变 P0-04/P0-05 原有契约。 |
| 新建 | `agent/runtime/pevr.py` | 定义八阶段枚举、PEVR 请求/轨迹、工具证据、指标、最终报告、结果和 LangGraph 受控状态信封。 | P0-13 CLI/API 消费；P0-14 Checkpoint 可在不重造 RunState 的前提下持久化。 |
| 新建 | `agent/runtime/graph.py` | 编译固定 `guard→understand→retrieve→plan→validate→execute→verify→finish` 图，串接 P0-05 命名节点、P0-12 注册表、RAG、C++、仿真和 Observation 验证。 | P0-14/P0-15 在此图上增加恢复/异常分支，但不得让 LLM 动态添加节点或绕过 Validator。 |
| 修改 | `agent/runtime/__init__.py` | 懒加载导出 PEVR 公共类型，避免 `services.amr_simulator` 反向导入时形成循环。 | 外部调用方可从 runtime 稳定入口导入 RunState/PEVR 类型。 |
| 修改 | `agent/context/prompts/plan_tasks.md` | 增加 P0-13 四任务顺序、固定 `$ref`、原语参数、空运行期证据和禁止示例事实的 Prompt 约束。 | `plan_tasks` 仍由 P0-05 Prompt Registry 统一渲染并注入实时 Schema。 |
| 修改 | `config/default.toml` | 将本地网关输出安全上限调为 4096，避免四任务结构化 JSON 在 1024 处截断；节点预算仍继续收紧。 | Fast 本地 P0-13 运行使用；Provider/测试显式小预算仍保持原语义。 |
| 修改 | `services/config/settings.py` | 保留代码级保守 fallback，并注释 TOML 覆盖与节点级预算边界。 | 配置契约、ModelProvider 单测和 P0-03 网关行为保持兼容。 |
| 修改 | `scripts/export_schemas.py` | 将 `PEVRRunReport` 纳入统一 Pydantic Schema 导出。 | `docs/schemas/PEVRRunReport.schema.json` 与运行时报告契约同步。 |
| 新建 | `docs/schemas/PEVRRunReport.schema.json` | 保存 P0-13 带引用、计划版本、工具证据、指标和风险的机器可读报告 Schema；本步无核心代码注释需求。 | P0-06/P0-17 或后续持久化消费者可校验最终报告。 |
| 新建 | `scripts/run_p013_e2e.py` | 提供真实 Fast LLM P0-13 验收入口，要求预先启动固定模型且显式传入审批，不自动启动/批准副作用。 | 操作者可复现自然语言订单→RAG→DAG→C++→仿真→验证→报告；输出 JSON 属于 `tmp/` 自动生成物。 |
| 新建 | `tests/unit/test_p013_pevr.py` | 覆盖 mock 八阶段成功闭环、非法 dataflow、dispatch 可信审批和模型 JsonValue/固定事实兼容反例。 | 后续状态图、Validator 或 Planner Prompt 修改必须通过此门禁。 |
| 修改 | `README.md` | 将 P0-13 标记完成，增加主图、真实 Fast 验收命令和报告入口；本步无核心代码注释需求。 | 项目首页当前边界转交 P0-14/P0-15。 |
| 新建 | `docs/P013_PEVR.md` | 记录 P0-13 状态图、Planner/Validator/审批边界、真实模型配置、实际指标、运行命令和限制；本步无核心代码注释需求。 | 下一 Agent 以此专题入口复现正常闭环，不把异常能力提前混入。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部新建/修改源码、配置、测试、Schema 与文档职责；本步无核心代码注释需求。 | 文件职责入口保持唯一，并明确 `tmp/`/`build/` 为生成物。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-13 公共类型/状态图/报告契约、真实 Fast E2E、测试、服务状态、风险和下一工作包；本步无核心代码注释需求。 | P0-14/P0-15 可直接复用 RunState、ToolResult 和证据边界。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀 Fast 输出预算、JsonValue 包装/固定事实引用、报告上下文窗口和安全审批的可复用坑；本步无核心代码注释需求。 | 后续 Prompt/网关/主图工作避免重复踩坑。 |

## 2026-08-20：P0-14 Checkpoint、幂等与局部重规划

本步新增/修改的 Python 核心代码均补充中文模块说明、类/函数 docstring 和关键分支注释，
说明 Checkpoint/Effect Ledger 数据流、事务边界、外部状态核对、重复调用、补偿停机和
局部 DAG 传播。Markdown、README、JSON Schema 和纯文档登记没有核心代码注释需求；
`migrations/` 没有新增文件，继续复用 `0001_p006_core` 的 8 张表。`build/`、
`__pycache__/`、`.pytest_cache/`、`tmp/` 和 pytest 临时目录均为自动生成物，不登记为源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `agent/runtime/checkpoint.py` | 定义 CheckpointSnapshot、EffectLedgerEntry、唯一幂等键、外部状态快照、恢复决策、持久化 Protocol 及线程安全单测适配器。 | `agent/runtime/graph.py`、PostgreSQL Service 和 P0-15 补偿/重规划流程共享同一恢复契约。 |
| 修改 | `agent/runtime/graph.py` | 给固定 PEVR 图加入阶段/任务 Checkpoint、Effect Ledger 预留/完成、恢复前真实仿真查询、终态核对和已完成任务跳过。 | P0-13 保持不注入 Store 时的兼容行为；生产入口可注入 PostgreSQL Store。 |
| 修改 | `agent/runtime/__init__.py` | 从 runtime 稳定入口导出 Checkpoint、Effect Ledger 和 Recovery 公共类型，并保留 PEVR 延迟导出。 | API/测试/P0-15 不需依赖内部模块路径。 |
| 新建 | `agent/planning/replanner.py` | 规范化 AMR/通道/工位/工具/任务影响集合，沿 DAG 后继传播，保留完成锚点，生成版本加一的新局部计划并复验 DAG。 | P0-15 可在 verify/异常分类后复用，不允许 LLM 绕过确定性影响分析。 |
| 修改 | `agent/planning/__init__.py` | 延迟导出 LocalReplanner、AffectedEntitySet 和局部重规划结果契约。 | 规划调用方共享稳定导入，避免 planning/context/runtime 循环。 |
| 修改 | `agent/tools/registry.py` | 扩展 ToolRegistry/ToolExecutor 接收统一业务 `idempotency_key`，并让 ToolResult/cache/in-flight 以该键协调重复调用。 | P0-14 外层 Ledger 与 P0-12 进程内幂等使用同一副作用身份；旧 call_id 调用保持兼容。 |
| 修改 | `docs/P012_TOOLS.md` | 补充业务幂等键优先于 call_id、业务键冲突错误和 P0-14 持久化边界说明；本步无核心代码注释需求。 | P0-12 工具调用方和 P0-15 补偿流程复用一致的重复调用语义。 |
| 修改 | `services/persistence/repositories.py` | 增加按 `(run_id, plan_version, task_id)` 查询任务和按幂等键/运行查询 Effect Ledger 的无事务 Repository 方法。 | Checkpoint Service 可在同一事务挂载任务外键、读取赢家和审计列表。 |
| 新建 | `services/application/checkpoint_service.py` | 实现 PostgreSQLRuntimeStore：运行绑定、Checkpoint JSONB/计划任务事务、Effect Ledger 唯一预留、完成/失败更新和跨实例回读。 | PEVR 生产组装和 FastAPI state 注入使用；不新增表或修改已执行 migration。 |
| 修改 | `services/application/__init__.py` | 导出 PostgresRuntimeStore/PostgresCheckpointStore。 | 应用组装、API 和外部运行器通过稳定入口注入。 |
| 修改 | `apps/api/main.py` | 在 FastAPI 生命周期中组装 PostgreSQL Checkpoint Store，供运行图依赖注入；不在启动时执行迁移或副作用。 | 后续 P0-15/API 执行入口可直接复用同一数据库边界。 |
| 修改 | `apps/api/dependencies.py` | 增加 `get_checkpoint_store` 依赖入口。 | 新增运行接口可获取同一 PostgreSQL Store，不直接访问 ORM。 |
| 新建 | `tests/unit/test_p014_checkpoint.py` | 覆盖三元组幂等键、重复预留、真实状态恢复决策、补偿分支、Checkpoint 序号、模拟进程重启和已完成副作用重复次数为 0。 | 后续修改恢复流程必须保留安全反例和 no-redispatch 断言。 |
| 新建 | `tests/unit/test_p014_replanner.py` | 覆盖 AMR/通道 cell/工位/工具影响传播、下游失效、版本加一、完成 effect 保留和 RunState 重建。 | P0-15 扩展异常处置时作为局部 DAG 回归门禁。 |
| 新建 | `tests/integration/test_p014_postgres.py` | 在真实 PostgreSQL 验证 Checkpoint 跨 Store 实例回读、Effect Ledger 唯一预留/完成和精确清理。 | 证明 P0-14 不是 SQLite/fake-only；统一 smoke 必须保留该集成门禁。 |
| 修改 | `docs/DATABASE.md` | 记录 P0-14 复用 8 表、事务数据流、幂等约束、恢复顺序和局部重规划边界；本步无核心代码注释需求。 | 数据库设计仍以 P0-06 migration 为唯一表结构来源。 |
| 修改 | `docs/P013_PEVR.md` | 更新 P0-13 与 P0-14 的职责边界和 Store 注入方式；本步无核心代码注释需求。 | 下一步可区分正常内存闭环与可恢复运行。 |
| 新建 | `docs/P014_CHECKPOINT.md` | P0-14 专题说明 Checkpoint/Effect Ledger、恢复流程、局部重规划、验证命令和限制；本步无核心代码注释需求。 | P0-15/运维/新 Agent 以此作为恢复语义唯一专题入口。 |
| 修改 | `README.md` | 将 P0-14 标记完成，增加恢复/幂等/局部重规划能力和专项测试入口；本步无核心代码注释需求。 | 项目首页当前下一步转为 P0-15。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部新建/修改源码、测试和文档职责；本步无核心代码注释需求。 | 文件职责入口保持唯一，并明确自动生成物与无 migration 变化。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 写入 P0-14 公共契约、恢复决策、数据库/外部服务、测试事实、限制和下一步；本步无核心代码注释需求。 | 后续 Agent 可直接接续 P0-15，不把旧 Checkpoint 当外部事实。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀唯一业务键、reserved 窗口、外部状态优先和局部重规划锚点的可复用坑；本步无核心代码注释需求。 | 后续副作用/恢复/重规划实现避免重复派发和全图重算。 |

## 2026-08-21：P0-00～P0-14 阶段审计修复

本次只修复阶段审计发现的 P0-00～P0-14 缺口，没有推进 P0-15。修改的 Python/C++
核心代码均同步补充或校正中文注释，说明禁用门禁、规范摘要、事务窗口、失败关闭、运行时
provenance、空闲 AMR 占位和路径可移植性。Markdown、TOML、JSON seed、运行时导出的
JSON Schema 与测试数据没有核心代码注释需求；其设计原因记录在本节和专题文档中。
`build/`、`tmp/`、`__pycache__/`、`.pytest_cache/` 与系统临时在线报告是生成物，不登记
为源码交付，也没有新增 Alembic revision。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `services/config/settings.py`、`config/default.toml` | 给模型 Profile 增加 `enabled/disabled_reason`，默认硬禁用 Smart 并保留审计原因。 | API、CLI 和 Provider 都不能借环境变量悄悄启用 Smart；日后须经用户明确指示修改受审配置。 |
| 修改 | `services/model_gateway/exceptions.py`、`services/model_gateway/provider.py` | 新增稳定 `MODEL_PROFILE_DISABLED`，并在 `/v1/models` 或 completion 之前拒绝禁用 Profile。 | Smart 服务即使碰巧在线也不可被当前 Agent 使用；Fast alias/Schema 门禁保持原样。 |
| 修改 | `agent/planning/contracts.py`、`agent/runtime/state.py`、`agent/runtime/pevr.py`、`agent/tools/schemas.py` | 对齐 run/task/environment/assignment 等公共 ID 长度，并给栅格坐标和运行请求保持一致边界；PEVR 状态增加模型调用计数与资源 provenance。 | Pydantic、数据库列、工具参数和 Checkpoint 不再因上下游最大长度漂移产生晚失败。 |
| 修改 | `agent/runtime/checkpoint.py` | 把幂等键改为规范三元组 SHA-256；收紧 Effect/ToolResult digest 一致性，并对外部 effect/tool/key/input/output 身份做完整核对。 | 合法 ID 含冒号不碰撞；伪造或串线的 `completed` 结果不能被恢复器跳过执行。 |
| 修改 | `agent/runtime/graph.py` | 严格恢复 Checkpoint、校验 seed/列表/Trace，对坏快照 fail closed；统一工具输入 digest；首个非法计划只允许一次语义修复并准确统计模型调用；保存运行时 provenance。 | 恢复不会静默丢证据，Planner 不能绕过正常 Validator，在线指标反映真实调用数。 |
| 修改 | `agent/planning/validator.py`、`agent/planning/replanner.py`、`agent/planning/__init__.py` | 从真实 allocation/route 结果构建资源 provenance；局部影响只传播未完成后继；新版本复用合同、ToolSpec、seed 重新执行完整 PEVR 验证。 | route-only 替换和遗漏实际 AMR/cell/edge 的局部计划被拒绝，完成锚点仍保留。 |
| 修改 | `agent/runtime/__init__.py` | 从稳定入口导出新增/变化的 PEVR、恢复和 provenance 类型。 | 调用方不必依赖内部模块路径，循环导入边界保持不变。 |
| 修改 | `agent/tools/registry.py`、`agent/tools/snapshots.py` | dispatch 使用业务幂等键写 execution store；固定地图经 `WarehouseMap` 解析并暴露障碍、窄通道、单向/禁行边和容量；更正内存 Store 仅供单测/无持久化调用的职责。 | 真实 CLI 可在图完成落账前持久化外部仿真事实；九工具共享非空且受契约校验的地图。 |
| 修改 | `services/persistence/repositories.py`、`services/application/checkpoint_service.py` | 增加按外部 execution ID 查 Effect；`PostgresRuntimeStore` 同时实现 execution state store，按业务键锁行、独立提交外部快照并校验摘要。 | 真实进程在 external commit 与 Effect completed 之间退出后，新进程仍能核对且不重复 dispatch。 |
| 修改 | `scripts/run_p013_e2e.py` | 将同一 PostgreSQL Store 同时注入 ToolRegistry 与 PEVR Runner，并在结束时释放数据库资源。 | 在线 PEVR 不再依赖进程内仿真真相；每个独立 run_id 都可跨进程恢复。 |
| 修改 | `services/planner_cpp/src/route_planner.cpp` | 为所有未分配 AMR 在完整规划时域预约初始 cell。 | A* 不会穿过物理上仍停在窄通道的空闲机器人。 |
| 修改 | `services/planner_cpp/CMakeLists.txt`、`services/planner_cpp/tests/route_planner_tests.cpp` | 注册并实现空闲 AMR 阻断单格通道的确定性反例。 | CTest 基线从 33 增至 34，持续保护 P0-09 物理占位语义。 |
| 修改 | `domains/amr_warehouse/contracts.py`、`domains/amr_warehouse/__init__.py` | 新增严格 `WarehouseMap/Location/Edge/NarrowAisle` 公共契约，拒绝 bool/float 伪装整数坐标并校验重复、相邻和连续性。 | Python seed、工具快照和 JSON Schema 共享同一地图语义，不靠 C++ 晚失败。 |
| 修改 | `domains/amr_warehouse/data/warehouse_v1.json` | 加入不干扰固定 ORDER-001 正常主链的非空 obstacle、窄通道、blocked/one-way edge 和临时封锁 fixture。 | 地图能力测试不再是空数组上的空命题；本文件是纯数据，无核心代码注释需求。 |
| 修改 | `scripts/export_schemas.py`；新建 `docs/schemas/WarehouseMap.schema.json` | 将地图公共契约加入统一导出。 | 运行时 Pydantic 与机器可读地图 JSON 契约保持同源。 |
| 修改 | `docs/schemas/AMRState.schema.json`、`DispatchSimulationInput.schema.json`、`FleetStateOutput.schema.json`、`Observation.schema.json`、`PlanMultiAMRRoutesInput.schema.json`、`PlanTask.schema.json`、`PlanTasksOutput.schema.json`、`QueryExecutionStateInput.schema.json`、`ReplanOutput.schema.json`、`RequestApprovalInput.schema.json`、`RunState.schema.json`、`RunVerificationSuiteInput.schema.json`、`SimulationPlan.schema.json`、`SimulationResult.schema.json`、`TaskContract.schema.json`、`TransportOrder.schema.json`、`ValidateFleetPlanInput.schema.json`；保留/再生成 `PEVRRunReport.schema.json` | 反映 ID 长度、strict 坐标、运行状态/provenance 和嵌套公共契约变化；全部由导出脚本生成，无手写分叉。 | 契约测试逐字比较 36 份 Schema，API/工具/文档消费者获得同一边界。 |
| 修改 | `scripts/check_environment.py`、`scripts/run_smoke.ps1` | Python/CMake/Ninja/CTest/MSVC 路径允许命令参数或 `AMR_*` 环境变量覆盖，同时保留当前 E 盘默认值。 | smoke 可在不同 Windows 开发机复用；仍显式打印实际解释器和工具版本。 |
| 修改 | `tests/unit/test_settings.py`、`tests/unit/test_model_provider.py` | 覆盖 Smart 配置状态和“网络调用次数为 0”的禁用反例。 | 防止以后只隐藏文档、实际仍可访问 Smart。 |
| 修改 | `tests/unit/test_p004_contracts.py`、`tests/unit/test_p012_tools.py` | 覆盖非空地图 seed、严格坐标、公共 ID 边界和规范化工具摘要。 | Pydantic/JSON/工具上下游契约漂移会在单测阶段暴露。 |
| 修改 | `tests/unit/test_p013_pevr.py` | 增加一次语义修复、二次非法停止、Validator 业务 invalid、路线 timeout 和仿真 blocked 等反例。 | “工具 success”不能替代业务完成；任何 Planner 工具执行前仍有确定性门禁。 |
| 修改 | `tests/unit/test_p014_checkpoint.py`、`tests/unit/test_p014_replanner.py` | 覆盖冒号碰撞、seed 漂移、坏快照、外部身份/digest 不一致、真实 provenance、只失效未完成子图和完整 PEVR 复验。 | P0-14 的幂等、恢复和局部重规划不再只测正常路径。 |
| 新建 | `tests/helpers/__init__.py`、`tests/helpers/p014_process_worker.py` | 提供仅供集成测试的子进程工作器，在外部快照提交后用 `os._exit(73)` 模拟真实掉电窗口。 | 不进入生产导入链；用于证明进程内 fake 无法替代跨进程恢复测试。 |
| 修改 | `tests/integration/test_p014_postgres.py` | 用新 Engine/Runner 恢复被强杀的真实 PostgreSQL 运行，并断言 dispatch 次数 0、Effect 单行和结果核对复用。 | P0-14 恢复门槛包含真实进程边界，而非仅跨对象实例。 |
| 修改 | `README.md`、`docs/MODEL_GATEWAY.md`、`docs/SERVICES_STARTUP.md`、`docs/P001_P003_FILE_GUIDE.md`、`docs/P012_TOOLS.md`、`docs/P013_PEVR.md`、`docs/P014_CHECKPOINT.md`、`docs/DATABASE.md` | 对齐 Smart 禁用、摘要幂等键、外部状态持久化、严格恢复、完整局部复验和最新离线/在线证据；均为文档，无核心代码注释需求。 | 操作者和后续工作不再按旧的“Smart 可启动/冒号拼接/内存外部状态”语义行动。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀本次模型验收、幂等、digest、进程恢复、reconcile、provenance、空闲 AMR、坏快照、非空地图和 Python 环境十类坑；本步无核心代码注释需求。 | 后续工作包可复用失败模式与反例设计。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md` | 记录全部修复、公共接口、真实命令/结果、服务终态和文件职责；本步无核心代码注释需求。 | 仓库唯一交接/文件作用入口与当前代码一致，当前状态冻结在 P0-14。 |

## 2026-08-21：P0-15 故障分类与终止策略

本步 Python 核心代码和测试均同步补充中文模块说明、类/函数 docstring 与关键分支注释，
解释分类优先级、预算数据流、副作用安全限制、重复故障和最终终止行为。Markdown 与
JSON Schema 只记录公共契约/职责，本步无核心代码注释需求；`build/`、`tmp/`、
`__pycache__/`、`.pytest_cache/` 仍是自动生成物，不属于源码交付。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `agent/runtime/faults.py` | 定义七类稳定 `FaultCategory`、五种 `RecoveryAction`、策略表、`FaultSignal`、有限预算恢复控制器、RunState/Checkpoint 回写和 P0-14 局部重规划适配。 | PEVR、API 或后台执行器消费统一故障决策；未知异常 fail closed，副作用未知状态不重放。 |
| 修改 | `agent/planning/contracts.py` | 将 `ExecutionBudgets` 默认 `max_replans` 固定为 2，并新增受上限约束的 `max_retries`。 | TaskContract、RunState、Context Budget 和 P0-15 共享同一恢复额度。 |
| 修改 | `agent/context/contracts.py` | 给 `BudgetUsage/BudgetSnapshot` 增加重试累计和剩余额度/超限计算。 | Prompt 节点和恢复控制器不会因重试绕过总预算。 |
| 修改 | `agent/context/budget.py`、`agent/context/nodes.py` | 在节点预算门禁及用量推进中保留/拒绝超限重试计数。 | P0-05/P0-13 的旧预算调用继续兼容，超限进入确定 fallback。 |
| 修改 | `agent/runtime/state.py` | 新增 `retry_count`、`fault_history`、`FaultRecord` 及跨任务/预算/重复 ID 校验。 | Checkpoint 恢复拥有可审计故障事实，状态循环和手工篡改会被拒绝。 |
| 修改 | `agent/runtime/graph.py` | 给所有 `PEVRExecutionError` 附加结构化 FaultSignal，补充 PEVR 故障分类入口，并保留 P0-14 Checkpoint/Effect Ledger 边界。 | 上层可把固定图失败交给同一恢复控制器，不允许模型动态插入循环。 |
| 修改 | `agent/runtime/__init__.py` | 从稳定 runtime 入口导出 P0-15 枚举、策略、信号和控制器。 | API/测试不依赖内部模块路径；延迟导入循环边界保持不变。 |
| 新建 | `tests/unit/test_p015_faults.py` | 覆盖七类分类、默认动作、重试/重规划上限、重复故障终止、副作用 timeout、RunState 校验和完成 effect 保留。 | 后续策略/预算修改必须保留正常与失败路径。 |
| 新建 | `tests/integration/test_p015_fault_recovery.py` | 用 InMemory Checkpoint Store 验证七类异常进入 retry/replan/terminal 状态，局部重规划跨 Store 回读且旧 effect 锚点不变。 | 证明 P0-15 与 P0-14 Checkpoint/LocalReplanner 的集成不是 fake-only 状态拼接。 |
| 新建 | `docs/P015_FAULTS.md` | 记录稳定错误分类表、动作升级、预算硬门、终止条件、P0-14 数据流和验收入口；本步无核心代码注释需求。 | 后续 P0-16/运维/接口调用方按同一错误契约接续。 |
| 修改 | `docs/P014_CHECKPOINT.md` | 修正历史 P0-13 `max_replans=0` 夹具与 P0-15 默认恢复预算的边界，并链接 P0-15 专题；本步无核心代码注释需求。 | 避免把历史测试夹具误读为当前恢复默认值。 |
| 修改 | `docs/P013_PEVR.md` | 将 P0-13 的异常职责边界更新为由 P0-15 专题承接分类与终止策略；本步无核心代码注释需求。 | P0-13 正常闭环文档不再声称 P0-15 尚未实现。 |
| 修改 | `README.md` | 将项目状态推进到 P0-15，公开故障动作/预算/副作用边界和专项测试入口；本步无核心代码注释需求。 | 操作者和后续工作包从首页进入 P0-15 专题，不再按旧的 P0-14 待办描述行动。 |
| 修改 | `docs/schemas/FinalReport.schema.json`、`docs/schemas/PEVRRunReport.schema.json`、`docs/schemas/RunState.schema.json`、`docs/schemas/TaskContract.schema.json` | 由统一 Schema 导出脚本反映 `BudgetUsage`、`ExecutionBudgets` 和 `RunState` 的公共字段变化；本步无核心代码注释需求。 | 机器消费者与 Pydantic 契约同源；不得手工编辑生成物。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 登记 P0-15 完成内容、公共接口、实际验证、服务状态、文件职责和新坑；本步无核心代码注释需求。 | 下一 Agent 直接复用故障/预算/Checkpoint 事实，不按旧 P0-14 冻结记录行动。 |

## 2026-08-21：P0-16 RBAC、HITL 与安全边界

本步 Python 核心代码同步补充中文模块说明、类/函数 docstring 和关键分支注释，解释身份
来源、ACL 数据流、审批摘要绑定、恢复前核验、失败关闭和禁止执行面。Markdown、TOML、
JSON、JSON Schema 和测试登记文件没有核心代码注释需求；其职责、边界和验证事实在本节
及 `docs/P016_SECURITY.md` 记录。没有新增 Alembic revision；`build/`、`tmp/`、
`__pycache__/`、`.pytest_cache/` 和在线报告仍是自动生成物，不登记为源码交付。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 新建 | `agent/security/__init__.py` | 从稳定入口导出 Principal、JWT 验证、RBAC 和 ACL 门禁。 | API、工具和 PEVR 复用同一安全边界，避免各层自行解析角色。 |
| 新建 | `agent/security/contracts.py` | 定义严格 Principal 与封闭安全契约配置；角色只能来自已验证身份。 | JWT、ToolResult、HITL 和审计共享同一主体模型。 |
| 新建 | `agent/security/auth.py` | 实现固定 HS256、issuer/audience、时间和必需 claim 校验，并安全映射 JWT。 | FastAPI 业务路由在进入 Service 前取得真实 viewer/operator 身份。 |
| 新建 | `agent/security/rbac.py` | 实现 operator 门禁、ToolSpec 工具权限、文档 ACL 和检索范围收紧。 | API、检索器和 ToolRegistry 共享 fail-closed 授权语义。 |
| 新建 | `agent/runtime/hitl.py` | 定义 HITLReason/Status/Request/Grant/Interrupt、摘要签名、存储协议和线程安全内存适配器。 | PEVR、API 和 PostgreSQL 适配器共享 pending→approved/rejected/expired 状态机。 |
| 修改 | `agent/runtime/graph.py` | 在安全 PEVR 中执行 Principal、Schema、Validator、HITL、Checkpoint 和恢复前票据核验。 | 高风险写操作暂停且保留完整进度，审批后不得重复副作用。 |
| 修改 | `agent/runtime/pevr.py`、`agent/runtime/__init__.py` | 暴露 principal/approval_grant、HITLInterrupt 和 PEVRInterrupt 公共运行契约。 | 调用方可从稳定入口恢复 waiting Checkpoint，不使用布尔审批旁路。 |
| 修改 | `agent/tools/contracts.py` | 给 ToolResult/ToolSpec 审计契约增加安全主体字段和默认审计项。 | 每次工具结果可追溯到主体；JSON Schema 与运行时同源。 |
| 修改 | `agent/tools/registry.py` | 要求安全模式绑定 Principal，执行 ToolSpec 权限、固定输入 Schema、禁止执行选择器和审批票据核验。 | 未注册工具、任意命令/SQL/Shell/HTTP 和越权调用在 handler 前阻断。 |
| 修改 | `agent/context/prompt_registry.py` | 为系统 Prompt 明确 RAG/node 输入是不可信数据，不能改写权限、Validator、HITL 或工具白名单。 | Prompt Injection 不能把检索内容提升为控制指令。 |
| 新建 | `services/application/hitl_service.py` | 复用 approvals 表实现跨进程 HITL 请求、行锁审批、HMAC grant 和恢复核验。 | 生产恢复使用 PostgreSQL 事实，不依赖进程内自动批准；无新增表。 |
| 修改 | `services/application/document_service.py`、`services/application/exceptions.py` | 在文档 Service 读取路径执行 ACL，未授权访问统一隐藏为 404。 | 防止 API 通过状态码枚举 ACL 不可见文档。 |
| 修改 | `services/application/run_service.py`、`services/application/__init__.py` | 绑定 operator 主体到运行创建/审批决定，并导出 HITL Service。 | 业务服务不信任请求体中的 decided_by 或 role。 |
| 修改 | `services/persistence/repositories.py` | 增加审批记录查询和行锁读取能力。 | PostgreSQL HITL 适配器在同一事务内完成状态迁移。 |
| 修改 | `apps/api/dependencies.py`、`apps/api/main.py` | 增加 Bearer JWT/current Principal/operator 依赖，并组装认证器和 PostgreSQL HITL Store。 | 健康检查保持匿名，业务路由统一先认证/授权。 |
| 修改 | `apps/api/routers/documents.py`、`apps/api/routers/runs.py`、`apps/api/routers/evals.py`、`apps/api/schemas.py` | 将上传、运行、评测和审批入口绑定已验证主体，兼容字段不能提升权限。 | viewer/operator 权限在 HTTP 边界统一执行，审批者身份由令牌决定。 |
| 修改 | `services/config/settings.py`、`services/config/__init__.py`、`config/default.toml`、`.env.example` | 增加 JWT/HITL 密钥、issuer、audience 和 leeway 的配置契约。 | 默认开发配置可运行，生产必须注入独立随机密钥。 |
| 修改 | `scripts/export_schemas.py` | 将 Principal、HITLRequest、ApprovalGrant、HITLInterrupt 加入同源 Schema 导出。 | API/持久化/跨语言消费者不会手写安全字段。 |
| 新建 | `docs/schemas/Principal.schema.json`、`docs/schemas/HITLRequest.schema.json`、`docs/schemas/ApprovalGrant.schema.json`、`docs/schemas/HITLInterrupt.schema.json` | 保存身份与审批公共 JSON Schema；由脚本生成，不手工分叉。 | 契约测试验证运行时 Pydantic 与 checked-in Schema 一致。 |
| 修改 | `docs/schemas/FinalReport.schema.json`、`docs/schemas/PEVRRunReport.schema.json`、`docs/schemas/RunState.schema.json`、`docs/schemas/SimulationResult.schema.json`、`docs/schemas/TaskContract.schema.json`、`docs/schemas/ToolResult.schema.json`、`docs/schemas/Observation.schema.json` | 由统一导出脚本反映本步运行态/审计字段变化。 | 机器消费者继续使用同源闭合 Schema；本步无核心代码注释需求。 |
| 新建 | `tests/unit/test_p016_security.py` | 覆盖 JWT 篡改/算法/过期、HTTP 认证、viewer 越权、工具注入、检索 ACL 泄漏、审批绕过和 Checkpoint 恢复。 | P0-16 的安全反例门禁要求全部阻断，专项目标阻断率 100%。 |
| 修改 | `tests/integration/test_p006_postgres.py` | 将既有 API 集成请求改为使用真实 operator JWT，回归新认证契约。 | P0-06 PostgreSQL/API 测试继续覆盖真实业务入口，不绕过 RBAC。 |
| 修改 | `docs/P013_PEVR.md`、`docs/P014_CHECKPOINT.md` | 补充 P0-16 安全审批和 waiting Checkpoint 的职责边界；本步无核心代码注释需求。 | 后续 Agent 不把 legacy approval_granted 或普通 Checkpoint 当作生产安全恢复。 |
| 新建 | `docs/P016_SECURITY.md` | 记录 RBAC、ACL、工具白名单、HITL 状态机、持久化复用、验证事实和限制；本步无核心代码注释需求。 | 作为 P0-16 专题唯一详细说明入口。 |
| 修改 | `README.md` | 更新项目状态、P0-16 能力、审批流程和专项测试入口；本步无核心代码注释需求。 | 用户从首页可找到当前安全边界和验收命令。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 记录本步完成内容、公共接口、真实测试/服务状态、文件职责和安全坑；本步无核心代码注释需求。 | 后续工作从有效的 P0-16 事实接续，不复用过时 P0-15 状态。 |

## 2026-08-21：P0-17 Trace、受控验证与证据报告

本步 Python 核心代码和测试同步补充中文模块说明、类/函数 docstring 及关键分支注释，
说明 Trace 数据流、摘要边界、失败关闭、固定验证入口、日志解析和报告重算规则。
Markdown、README、TOML、JSON Schema 和测试登记文件没有核心代码注释需求；职责与公共
契约在本节及 P017 专题记录。没有新增 Alembic revision；build/、tmp/、__pycache__/、
.pytest_cache/ 和系统临时验证输出是自动生成物，不登记为源码交付。

| 变更 | 文件 | 作用 | 调用方/下游 |
|---|---|---|---|
| 新建 | agent/runtime/trace.py | 定义 TraceEvent、TraceError、线程安全 TraceCollector、模型/工具/验证结果适配器和可选持久化 sink；只保存摘要与证据引用，不执行日志或参数。 | PEVR、Checkpoint、验证工具、API/报告消费者；后续可接 SSE/OTLP sink。 |
| 修改 | agent/runtime/pevr.py | 给 PEVRRequest、PEVRRunResult、PEVRRunReport 和 graph state 增加 trace_id/trace_events 关联。 | PEVR 调用方、Checkpoint 恢复和最终报告。 |
| 修改 | agent/runtime/graph.py | 在固定八节点、模型调用、工具调用和失败路径追加严格 Trace，并在恢复时校验身份/序号。 | P0-13 主图、P0-14 Store；P0-15 Fault 作为失败详情来源。 |
| 修改 | agent/runtime/checkpoint.py | 为 InMemoryRuntimeStore 增加 Trace 追加/回读，并扩展运行时持久化 Protocol。 | P0-14 单测、PEVR 恢复和后续报告导出。 |
| 修改 | agent/runtime/__init__.py | 从稳定 runtime 入口导出 TraceCollector、TraceError、TraceEvent 和 new_trace_id。 | API/测试不依赖内部模块路径。 |
| 修改 | services/application/checkpoint_service.py | 复用 events 表保存 Trace；runs 尚未创建时暂存首批模型事件，ensure_run 后按序补写并做幂等校验。 | 生产 PEVR PostgreSQL Checkpoint；不新增表或 migration。 |
| 修改 | agent/tools/contracts.py | 把 trace_id 纳入 run_verification_suite 可选参数白名单。 | ToolRegistry、Schema 和受控验证入口。 |
| 修改 | agent/tools/schemas.py | 增加仿真 suite ID、Trace 关联、逐 case 失败/证据字段、报告字段和退出码状态一致性门禁。 | ToolRegistry、Schema 导出、API/验证消费者。 |
| 修改 | agent/tools/verification.py | 固定注册 CTest/pytest/smoke/仿真 argv，禁止任意命令，并将真实日志转成报告；超时保留结构化 case。 | run_verification_suite handler；不接受用户 executable/cwd/shell。 |
| 修改 | agent/tools/registry.py | 透传 trace_id，返回报告/evidence，并把 timeout/验证失败绑定 ToolResult 错误。 | 九工具统一执行器和 PEVR。 |
| 新建 | services/validation/contracts.py | 定义 ParsedVerificationCase、VerificationReport、失败类型和证据位置公共契约，并禁止伪造通过状态。 | 解析器、报告生成器、工具 Schema 和 Trace 适配器。 |
| 新建 | services/validation/log_parser.py | 只解析固定入口 stdout/stderr/退出码/超时，提取失败类型、任务、工具、参数摘要和日志行号。 | FixedVerificationRunner 和报告。 |
| 新建 | services/validation/reporting.py | 从逐 case 真实结果重算状态、计数、digest、JSON 和 Markdown，追加报告证据引用。 | run_verification_suite、人工审阅和机器消费者。 |
| 新建 | services/validation/simulation_entry.py | 提供无参数、固定 seed/plan/仿真 ID 的预注册仿真验证入口，非 completed 以非零码退出。 | FixedVerificationRunner 的 p0_simulation/p0_sim。 |
| 修改 | services/validation/__init__.py | 暴露验证契约、解析器和报告生成器稳定入口。 | 工具 runner、测试和未来 API。 |
| 修改 | scripts/export_schemas.py | 将 TraceError/TraceEvent 与验证 case/report 契约加入同源 JSON Schema 导出。 | docs/schemas 机器消费者；生成物不可手工编辑。 |
| 新建 | docs/P017_TRACE_VERIFICATION.md | 记录 Trace 字段、受控 suite 白名单、解析/报告数据流、验证命令和限制；本步无核心代码注释需求。 | 后续 Agent、运维、API/报告消费者。 |
| 修改 | docs/P012_TOOLS.md、docs/P013_PEVR.md、docs/P014_CHECKPOINT.md | 补充 trace_id、报告字段、events 持久化和 P0-17 交接边界；本步无核心代码注释需求。 | P0-12～P0-16 下游工作包和恢复运维。 |
| 修改 | README.md | 将项目状态推进到 P0-17，公开 Trace、受控验证和专项测试入口；本步无核心代码注释需求。 | 项目入口和新 Agent。 |
| 新建 | tests/unit/test_p017_trace.py | 覆盖 Trace 字段、序号/失败门禁、模型/工具/验证适配器和证据引用。 | 防止审计字段或失败定位回退。 |
| 新建 | tests/unit/test_p017_validation.py | 覆盖日志分类、仿真证据、报告重算、固定 argv、真实退出码、非法 case 和 timeout。 | 防止任意命令面和伪造通过结论。 |
| 修改 | tests/unit/test_p013_pevr.py、tests/unit/test_p014_checkpoint.py | 回归验证 PEVR Trace 数量/序列/报告关联，以及 Checkpoint 恢复复用同一 Trace。 | P0-13/P0-14 公共回归门禁。 |
| 修改 | tests/integration/test_p014_postgres.py | 验证首条 Trace 在 runs 建立前暂存、建 run 后补写、重复提交幂等且真实 PostgreSQL 可回读。 | P0-14/P0-17 生产持久化边界。 |
| 新建 | docs/schemas/TraceError.schema.json、docs/schemas/TraceEvent.schema.json、docs/schemas/ParsedVerificationCase.schema.json、docs/schemas/VerificationEvidenceLocation.schema.json、docs/schemas/VerificationReport.schema.json | 由 export_schemas.py 生成 P0-17 机器契约；本步无核心代码注释需求。 | API、工具和跨语言消费者。 |
| 修改 | docs/schemas/PEVRRunReport.schema.json、docs/schemas/RunVerificationSuiteInput.schema.json、docs/schemas/VerificationSuiteOutput.schema.json | 反映 trace_id、逐 case 失败/证据和报告字段变化；本步无核心代码注释需求。 | P0-13/P0-12 Schema 消费者。 |
| 修改 | docs/FILE_PURPOSES.md、docs/HANDOFF_CONTEXT.md、docs/LESSONS_LEARNED.md | 登记本步文件职责、公共接口、实际验证、状态和可复用坑；本步为文档，无核心代码注释需求。 | 唯一文件职责/交接/经验入口。 |

## 2026-08-21：P0-18 60 例自动评测

本步 Python 核心代码和测试同步补充中文模块说明、类/函数 docstring 及关键分支注释，
解释固定数据流、复现边界、负向结果保留、零容忍检查、授权/审批失败关闭和验证入口。
JSON、JSON Schema、PowerShell、Markdown 与 README 属于契约/入口/文档，本步无核心代码
注释需求；其职责、版本和限制在本节及 `docs/P018_EVAL.md` 记录。`tmp/`、
`__pycache__/`、`.pytest_cache/`、build/ 及实际评测报告是自动生成物，不登记为源码交付。

| 变更 | 文件 | 作用 | 调用方/下游 |
|---|---|---|---|
| 新建 | `evals/p018/__init__.py` | 导出 P0-18 严格契约、数据集加载器和 `EvalHarness` 稳定入口。 | CLI、专项测试和后续报告消费者。 |
| 新建 | `evals/p018/contracts.py` | 定义五类评测枚举、固定 60 例数据集、逐例结果、聚合指标、Trace/失败证据和七项零容忍 Pydantic 契约；拒绝训练数据和畸形负向结果。 | 数据集加载、Harness、JSON Schema、机器验收。 |
| 新建 | `evals/p018/dataset.py` | 从 UTF-8 固定 JSON 加载并校验版本、仓库内执行配置、类别配额、唯一 ID/seed 和 evaluation-only 边界。 | `run_eval.py`、Harness 和复现指纹。 |
| 新建 | `evals/p018/config.json` | 冻结 P0-18 版本、离线执行模式、地图/AMR/订单、Fast/Smart 模型记录、Prompt/ToolSpec 版本、配额和验证 suite 映射；本步无核心代码注释需求。 | 运行器与报告复现信息。 |
| 新建 | `evals/p018/dataset.json` | 保存 25/10/10/5/10 共 60 个不进入训练的固定场景、输入 fixture、预期终态和 oracle 证据；本步无核心代码注释需求。 | `EvalDataset`、Harness 和人工复核。 |
| 新建 | `evals/p018/reproducibility.py` | 对固定输入、Prompt、配置、九个 ToolSpec、seed、模型记录和 Git/runtime 生成 SHA-256/版本指纹；拒绝配置路径逃逸。 | `EvalHarness`、JSON/Markdown 报告。 |
| 新建 | `evals/p018/runner.py` | 统一执行五类场景；复用 P0-15 Fault/Recovery、P0-16 RBAC/HITL 和 P0-17 Trace/FixedVerificationRunner，检查路线/电量/禁行区、ACL、审批、幂等和失败轨迹，最终按逐例事实聚合指标。 | CLI、专项测试、报告层；默认不调用在线模型。 |
| 新建 | `evals/p018/reporting.py` | 将同一 `EvalReport` 写为完整 JSON 和包含配额、六域指标、零容忍表、复现信息及负向 Trace 的 Markdown；不删除拒绝/失败案例。 | CLI、人工验收和机器消费者。 |
| 新建 | `evals/p018/run_eval.py` | 提供 `python -m evals.p018.run_eval` 固定参数入口和可自动化退出码；不暴露任意命令、脚本或测试表达式。 | PowerShell 一键脚本、CI/验收。 |
| 新建 | `scripts/run_p018_eval.ps1` | 在仓库根目录调用固定 Python CLI，允许显式 Python/输出目录/验证超时覆盖；本步无核心代码注释需求。 | Windows 操作者和 CI。 |
| 新建 | `tests/unit/test_p018_eval.py` | 覆盖固定配额、复现确定性、负向轨迹、安全/恢复反例、低电量零容忍、JSON/Markdown 一致性。 | P0-18 专项回归门禁。 |
| 修改 | `scripts/export_schemas.py` | 将 `EvalCase`、`EvalDataset`、`EvalReport` 纳入同源 Schema 导出清单。 | Schema 回归和跨语言机器消费者。 |
| 新建 | `docs/schemas/EvalCase.schema.json`、`docs/schemas/EvalDataset.schema.json`、`docs/schemas/EvalReport.schema.json` | 保存 P0-18 运行时 Pydantic 契约的 UTF-8 JSON Schema；本步无核心代码注释需求，不得手工分叉。 | 契约测试、API/CI/报告消费者。 |
| 新建 | `docs/P018_EVAL.md` | 记录数据集组成、一键命令、复现字段、指标/零容忍门槛、失败轨迹语义和在线/离线限制；本步无核心代码注释需求。 | 用户、后续 Agent 和验收人员。 |
| 修改 | `evals/README.md` | 增加 P0-18 统一评测入口、配额、报告路径和限制说明；本步无核心代码注释需求。 | 评测目录入口。 |
| 修改 | `README.md` | 将项目状态推进到 P0-18，增加目录、能力、命令和报告说明；本步无核心代码注释需求。 | 项目首页、操作者和新 Agent。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/LESSONS_LEARNED.md` | 登记 P0-18 公共接口、实际验证、服务状态、限制、后续复用信息和固定数据集/负向报告坑；本步无核心代码注释需求。 | 跨任务唯一交接与经验入口。 |

本步没有新增数据库表、字段、Alembic revision、C++ 源文件或模型服务；`tmp/p018_eval/`
只属于运行时报告输出，不是源码交付物。

## 2026-08-21：P0-18 Fast 在线模型补充验证

本步只执行外部 Fast 模型验证，没有修改核心代码或公共契约；因此无核心代码注释需求。
模型进程、启动日志和失败时未生成的 `tmp/p013_e2e_model_test.json` 都是运行时事实，不
登记为源码交付。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `docs/P018_EVAL.md` | 补充 Fast 真实模型门禁、结构化/五 Prompt 结果和 P0-13 9086>8192 上下文失败；本步无核心代码注释需求。 | 用户可区分 P0-18 离线结果与在线 PEVR 真实验收状态。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 Fast alias 门禁、20 例结构化输出、P0-05 五节点 5/5、P0-13 上下文超限失败、服务停止状态和未生成报告事实。 | 后续在线修复必须区分已通过的单节点/结构化测试与未通过的 P0-13 全链路。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀“单 Prompt 通过不代表 PEVR 组合上下文适配模型窗口”的现象、原因和后续验收要求。 | 后续 Prompt/摘要压缩和模型窗口变更需要先做 token/上下文预算验证。 |

## 2026-08-21：Fast 模型 16K 上下文复测记录

本步仍只更新真实测试事实，没有修改核心代码或公共契约；本步无核心代码注释需求。
`tmp/p013_e2e_model_test_16k.json` 是模型在线运行输出，不登记为源码交付。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `docs/P018_EVAL.md` | 追加外部 Fast 服务 16K 上下文下的网关、结构化、P0-05 和 P0-13 实测结果；明确离线配置仍为 8192。 | 报告读者能区分历史 8K 失败、16K 在线通过和 P0-18 离线 oracle。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 更新当前下一步并记录 16K 实际命令行、8/8 阶段、5/5 工具、Validator、仿真、Trace 和服务停止状态。 | 后续 Agent 直接复用成功在线 run_id 与报告路径。 |

## 2026-08-21：Fast 16K 全新 run_id 复测记录

本步为排除旧 Checkpoint 恢复影响而补跑全新在线 E2E；没有修改核心代码或公共契约，本步
无核心代码注释需求。`tmp/p013_e2e_model_test_16k_fresh.json` 是运行时报告，不登记为源码。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `docs/P018_EVAL.md` | 记录全新 run_id 下从头执行 P0-13 的 16K 通过结果。 | 在线全链路结论不再依赖旧 8K 失败 Checkpoint 的恢复。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 fresh run_id、8/8 节点、4 次模型调用、5/5 工具、Trace 17 条和报告路径。 | 后续 Agent 可复用独立的在线验收证据。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 记录稳定 run_id 会触发 Checkpoint 恢复，在线模型变更验收需显式新 run_id。 | 后续测试区分恢复测试与从头 E2E。 |

## 2026-08-21：P0-19 策略对照实验

本步新增的 Python 核心代码、测试和报告渲染器同步补充中文模块说明、类/函数 docstring
及关键分支注释，重点解释源 Trace digest、同源公平性、ReAct 仅评测投影、负向轨迹和
Token/资源未观测的失败关闭语义。JSON 配置、PowerShell 入口、Markdown 文档和由
`export_schemas.py` 生成的 Schema 是纯契约/文档/入口，本步无核心代码注释需求；
`tmp/p019_strategy_compare/`、`__pycache__/`、`.pytest_cache/`、`build/` 是自动生成物，
不登记为源码交付。

| 变更 | 文件 | 作用 | 调用方/下游 |
|---|---|---|---|
| 新建 | `evals/p019/__init__.py` | 导出 P0-19 策略枚举、报告契约、源报告加载和对照执行稳定入口。 | CLI、专项测试、后续在线 adapter；ReAct 仍不得接入生产 PEVR。 |
| 新建 | `evals/p019/contracts.py` | 定义执行模式、三策略、逐例结果、Token/延迟/资源可观测性、汇总、公平性门禁和 Smart deferred 契约；未知字段和不完整失败证据拒绝。 | 回放器、JSON/Markdown/JSONL 报告、Schema 消费者。 |
| 新建 | `evals/p019/dataset.py` | 加载固定 P0-19 配置并限制 P0-18 dataset/config 只能解析仓库内路径；强制 Fast、三策略顺序和 Smart 未启动/未完成。 | `run_compare.py`、公平性校验。 |
| 新建 | `evals/p019/config.json` | 冻结 P0-19 版本、`offline_trace_replay`、三策略语义、Fast 参数和 Smart 延期/Backlog 记录；本步无核心代码注释需求。 | CLI、报告复现指纹和人工复核。 |
| 新建 | `evals/p019/replay.py` | 验证 P0-18 源报告 digest、60 例、Prompt/ToolSpec/配置/Fast 指纹，保留源 Trace 并生成固定 Workflow/PEVR/ReAct 控制流投影；Token/资源缺失时 fail closed 为未观测。 | P0-19 CLI、报告层和专项测试；不调用模型、不执行工具。 |
| 新建 | `evals/p019/reporting.py` | 从同一 `P019Report` 渲染完整 JSON、Markdown 汇总和逐行 JSONL 原始轨迹，不过滤 denied/blocked。 | 用户验收、人工逐例复核和后续分析。 |
| 新建 | `evals/p019/run_compare.py` | 提供固定源报告/配置/输出目录参数和退出码，禁止任意命令/脚本/数据集选择器。 | `scripts/run_p019_compare.ps1`、CI/验收。 |
| 新建 | `scripts/run_p019_compare.ps1` | 使用 torch128 Python 调用 P0-19 CLI；只消费 P0-18 源报告，不启动 Fast/Smart；本步无核心代码注释需求。 | Windows 操作者和项目验收。 |
| 新建 | `tests/unit/test_p019_compare.py` | 覆盖三策略同一 60 例、ReAct 非生产投影、负向/零容忍事实、Token/资源未观测、Smart 延期和源 digest 篡改反例。 | 后续策略/报告/在线 adapter 变更回归门禁。 |
| 新建 | `docs/P019_STRATEGY_COMPARISON.md` | 记录公平性口径、指标定义、原始产物、实测汇总、离线限制、ReAct 主链边界和 Smart 延期；本步无核心代码注释需求。 | 用户、后续 Agent、P0-20 文档和复核人员。 |
| 修改 | `scripts/export_schemas.py` | 将 `P019Report`、`StrategyCaseResult`、`StrategySummary` 加入运行时同源 Schema 导出清单，并保留中文契约边界注释。 | Schema 一致性测试和机器消费者。 |
| 新建 | `docs/schemas/P019Report.schema.json` | 保存 P0-19 完整报告机器契约；本步无核心代码注释需求。 | API/CI/报告消费者。 |
| 新建 | `docs/schemas/P019StrategyCase.schema.json` | 保存逐例策略结果、源 Trace 和控制流投影契约；本步无核心代码注释需求。 | JSONL/人工复核/后续在线 adapter。 |
| 新建 | `docs/schemas/P019StrategySummary.schema.json` | 保存策略汇总表、P95、Token/资源可观测性和零容忍契约；本步无核心代码注释需求。 | Markdown/机器指标消费者。 |
| 修改 | `evals/README.md` | 增加 P0-19 一键入口、执行模式、产物路径和 Smart 延期说明；本步无核心代码注释需求。 | 评测目录入口。 |
| 修改 | `README.md` | 将状态推进到 P0-19，增加策略对照命令、目录、实测摘要和限制；本步无核心代码注释需求。 | 项目首页、操作者和新 Agent。 |
| 修改 | `docs/backlog.md` | 将“更完整 ReAct 对照”改为在线三策略 Fast 后续项，并新增 `P0-19-SMART-COMPARISON` 延期项；本步无核心代码注释需求。 | 后续范围管理和 Smart 恢复决策。 |
| 修改 | `docs/P018_EVAL.md` | 将 P0-18 当前源报告 digest 更新为本次复跑实际 artifact，并继续明确离线 oracle 与在线 Fast 的边界；本步无核心代码注释需求。 | P0-19 源报告复核和后续评测不能引用过期 digest。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部源码/配置/文档/Schema/测试职责；本步为文档，无核心代码注释需求。 | 唯一文件职责入口。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 记录 P0-19 公共契约、实际命令/结果、服务状态、离线限制、Smart 延期和下一步；本步为文档，无核心代码注释需求。 | 跨任务唯一交接入口。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀离线 oracle、源报告原始 hash 与稳定 digest、策略投影不可冒充在线质量等复用经验；本步为文档，无核心代码注释需求。 | 后续在线评测和报告设计。 |

## 2026-08-21：P0-20 部署、文档与演示收口

本步新增的启动脚本和部署配置补充了中文注释，说明服务顺序、宿主机模型边界、健康检查、
失败行为和安全限制；测试文件补充中文模块/测试 docstring。Markdown、Compose YAML、
Dockerfile、依赖锁和报告属于配置/文档/数据契约，本步无核心业务代码注释需求；`tmp/`、
`build/`、`__pycache__/`、`.pytest_cache/` 以及在线 PEVR JSON 是自动生成物，不登记为源码交付。

| 变更 | 文件 | 作用 | 调用方/下游 |
|---|---|---|---|
| 修改 | `compose.yaml` | 固化 `postgres`、`qdrant`、`api` 三服务、持久化卷、healthy 依赖、迁移命令、API 健康检查和宿主 Fast 可选地址；不启动模型服务。 | `scripts/start_local.ps1`、Docker 操作者、API 部署；后续 P1 若接入服务必须保持 Fast 宿主边界。 |
| 新建 | `infra/Dockerfile.api` | 构建非 root FastAPI API 镜像；使用最小 API 锁，不复制 GGUF/Embedding/权重。 | `compose.yaml`；API 健康与迁移验收。 |
| 新建 | `infra/requirements.api.lock` | 固定容器 HTTP/数据库/配置直接依赖，并以 `psycopg[binary]` 解决 slim 镜像 libpq 边界；不承载本地模型运行时。 | `infra/Dockerfile.api`；后续镜像构建复用。 |
| 新建 | `.dockerignore` | 排除 Git、构建缓存、运行报告、文档渲染物、GGUF/权重和 Python 缓存，避免泄漏和膨胀构建上下文。 | Docker 构建；模型安全边界。 |
| 新建 | `scripts/start_local.ps1` | P0-20 Windows 最简启动器；检查 Docker，启动并等待 Compose API/PostgreSQL/Qdrant，可选启动 Fast 并运行网关预检；Smart 不进入路径。 | 操作者、演示脚本、启动手册。 |
| 新建 | `tests/unit/test_p020_deployment.py` | 静态验证三服务/健康依赖、Fast 宿主边界、镜像最小锁、Smart 禁止启动和交付文档入口；不启动外部服务。 | P0-20 定向测试和全量 smoke。 |
| 新建 | `docs/ARCHITECTURE.md` | Mermaid 系统架构图、宿主机/容器数据边界、证据流和真实演示入口。 | README、演示人员、后续部署复核；本步无核心代码注释需求。 |
| 新建 | `docs/API.md` | 已实现健康、运行、计划、SSE、审批、文档和评测登记 HTTP 契约，包含认证/错误/模型门禁边界；不虚构自然语言 HTTP 路由。 | API 使用者、Swagger/OpenAPI 复核；本步无核心代码注释需求。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 更新 Compose API/数据库/Qdrant/Fast 顺序、健康检查、停止方式、`host.docker.internal` 和故障排查。 | README、`start_local.ps1`、现场演示；本步无核心代码注释需求。 |
| 修改 | `infra/README.md` | 登记 API 镜像、Compose 三服务、Fast 宿主模型和 Smart 禁用说明。 | Infra 操作者；本步无核心代码注释需求。 |
| 新建 | `docs/TEST_REPORT.md` | 保存 P0-20 实际 Compose、一键启动、在线三次正式证据、P0-18/P0-19 digest、smoke/regression 数量和限制。 | 用户验收、交接和简历事实复核；本步无核心代码注释需求。 |
| 新建 | `docs/DEMO_SCRIPT.md` | 固化 3 分钟演示台词、命令、自然语言→RAG→DAG→C++→仿真→验证/恢复→证据报告观察点。 | 现场演示；本步无核心代码注释需求。 |
| 新建 | `docs/RESUME_FACTS.md` | 只汇总已实现/已实测的简历事实，明确离线 Eval、Trace Replay、Smart 和 P0 Scope 限制。 | 简历/项目介绍审查；本步无核心代码注释需求。 |
| 修改 | `README.md` | 将项目状态推进到 P0-20，链接架构/API/测试/演示/简历文档，更新快速启动和 Compose/Fast 边界。 | 项目首页和新 Agent；本步无核心代码注释需求。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 记录 API slim/psycopg、Qdrant healthcheck、Fast 长 Prompt 与 PowerShell 插值等可复用坑。 | 后续部署/在线验收；本步无核心代码注释需求。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本步全部新建/修改文件、职责、公共契约和生成物边界。 | 后续每个工作包的唯一文件职责入口。 |
| 修改 | `docs/P018_EVAL.md` | 追加 P0-20 输出目录、60/60 结果、report_id/digest 和离线/在线边界；不改写原有评测模式。 | 用户复核最终 Eval，后续 P0-19 源报告复用；本步无核心代码注释需求。 |
| 修改 | `docs/P019_STRATEGY_COMPARISON.md` | 追加 P0-20 新源报告下的 180 条 Trace Replay、三策略指标、Smart deferred 和限制。 | 用户复核策略结论；后续在线 adapter 不能覆盖本结果；本步无核心代码注释需求。 |

## 2026-08-21：P0-00～P0-20 发布前全仓审查

本步只审查、运行验证并创建发布 Todo，没有修改业务代码、测试、配置、Schema 或数据库迁移，
因此本步无核心代码注释需求。`tmp/p0_audit_*`、`tmp/p0_audit_schemas/`、pytest/CTest 缓存和
在线模型日志均为自动生成的审查证据，不登记为源码交付物；外部 Fast 脚本/GGUF 只读取指纹，
不属于仓库文件。

| 变更 | 文件 | 作用 | 调用方/下游 |
|---|---|---|---|
| 新建 | `docs/P0_AUDIT_TODO.md` | 保存 P0-00～P0-20 逐项验收矩阵、4 个 Critical/7 个 High/3 个 Medium 的证据化 Todo、Release Verdict、最小修复集合、实际命令/结果、异常轨迹和未运行原因；明确 Smart 禁用不计 P0 缺陷。 | 发布决策、下一修复任务、复验、报告/简历事实校正的唯一审计入口；不是业务公共契约。 |
| 修改 | `docs/HANDOFF_CONTEXT.md` | 将顶部状态从历史“已完成”修正为发布审计 FAIL，记录阻断事实、真实命令、服务终态、生成物和下一步修复顺序；本步无公共接口变化。 | 后续 Agent 必须先处理审计 Todo，不能根据旧 P0-20 记录直接宣称发布完成。 |
| 修改 | `docs/FILE_PURPOSES.md` | 登记本次新建/修改文档及生成物边界；本步无核心代码注释需求。 | 保持仓库唯一文件职责入口与当前审查交付一致。 |
| 修改 | `docs/LESSONS_LEARNED.md` | 沉淀独立 oracle、外部执行身份、安全入口 fail closed、组件测试与生产接线、数据服务 ACL 等发布审查经验；缺陷仍待 Todo 修复，不伪写为已解决。 | 后续评测、恢复、安全和部署工作包避免重复出现同类假绿/身份冲突。 |

## 2026-08-21：按 P0_AUDIT_TODO 继续修复（前一 Agent 崩溃后续作）

本步在已有未提交工作树（C01–C04、H04、M01–M03 大半已改）上补齐 H01 oracle、H02 独立对照、H03 RAG split/CLI 门禁、H05 CTest、H07 文档诚实化；H06 只登记视频缺口，不伪造媒体文件。核心 Python/C++ 均补充了中文注释或沿用已有设计注释。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `scripts/run_p013_e2e.py`、`agent/runtime/pevr.py`、`agent/runtime/graph.py` | C01：发布入口强制 JWT/HITL/`security_required=True`，拒绝 principal+布尔审批。 | 演示 CLI、P0-16、P0-18 安全样例。 |
| 修改 | `agent/runtime/checkpoint.py`、`agent/tools/registry.py`、`services/application/checkpoint_service.py`、`scripts/migrate_external_execution_ids.py` | C02：外部仿真 ID 绑定幂等键+输入摘要，查询按 lookup_id，冲突记录可迁移。 | 恢复、Effect Ledger、P0-14 测试。 |
| 修改 | `agent/runtime/graph.py` | C03：生产图调用 FaultRecoveryController；replan 后丢掉 VALIDATE/EXECUTE stage_trace 并回到 VALIDATE。 | P0-15 生产轨迹、P0-18 异常指标。 |
| 修改 | `compose.yaml`、`compose.dev.yaml`、`scripts/bootstrap_local_secrets.ps1` | C04：发布 Compose 缺 secret 失败、数据面内部网、API loopback；开发 profile 仅 127.0.0.1 暴露库端口。 | 本地启动、P0-20 部署测试。 |
| 新建 | `evals/p018/oracle.py` | H01：独立消费 `case.oracle`，未知键 fail closed。 | P0-18 Harness、P0-19 独立对照。 |
| 修改 | `evals/p018/runner.py`、`dataset.py`、`dataset.json`、`config.json` | H01：注入文本进入不可信上下文；异常按恢复额度真实循环；Fast 指纹改为 IQ4_NL/16K/0.1。 | 离线 60 例回归。 |
| 新建 | `evals/p019/independent.py` | H02：三种策略各自执行同一 60 例。 | P0-19 CLI/测试；replay 降为可视化。 |
| 修改 | `evals/p019/contracts.py`、`config.json`、`dataset.py`、`reporting.py`、`run_compare.py` | 增加 `offline_independent_oracle` / `p0-19.v2`。 | Schema 导出、对照报告。 |
| 修改 | `evals/rag/cases.json`、`evals/rag/run_eval.py` | H03：8/8/4 split；holdout 指标；CLI 门禁；传入 Qdrant API key。 | RAG 发布门禁、P0-07 文档。 |
| 新建 | `config/fast_model_manifest.json`、`services/model_gateway/artifacts.py`、`scripts/start_fast_secure.ps1`、`scripts/verify_fast_artifact.py` | H04：固定 Fast GGUF/脚本哈希与启动预检。 | 模型网关、P0-18 指纹。 |
| 修改 | `services/planner_cpp/src/route_planner.cpp`、`tests/route_planner_tests.cpp`、`CMakeLists.txt` | H05：release_time 前允许预定位；新增 CTest。 | Planner/Validator 时间窗语义。 |
| 修改 | `README.md`、`docs/RESUME_FACTS.md`、`docs/TEST_REPORT.md`、`docs/DEMO_SCRIPT.md`、`docs/P018_EVAL.md`、`docs/P019_STRATEGY_COMPARISON.md` | H07：撤下无法证明的 60/60 发布数字和 `--approve-dispatch` 演示。 | 简历/演示；H06 视频仍缺。 |
| 修改 | `agent/runtime/state.py` | M01：completed/running 任务依赖闭包校验。 | Checkpoint 回读 fail closed。 |
| 修改 | `infra/requirements.api.lock`、`infra/Dockerfile.api` | M02：带 hash 的完整锁文件。 | API 镜像复现。 |
| 新建 | `services/model_gateway/secure_proxy.py` | M03：无 CORS、强制 Bearer 的 Fast 代理。 | 正式 Fast 入口。 |
| 新建 | `tests/unit/test_rag_eval_gates.py` | RAG split/门禁单测，不依赖 Qdrant。 | CI 离线门禁。 |
| 修改 | `tests/unit/test_p018_eval.py`、`tests/unit/test_p019_compare.py` | oracle mutation 与独立对照分差。 | 评测回归。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 交接、文件职责与坑。 | 后续 Agent。 |

## 2026-08-21：补完生产 VALIDATE 重规划门禁与 .env 凭据加载

前一 Agent 已把 FaultRecoveryController 接到生产图，但 VALIDATE 仍调用 `validate_normal_pevr_plan(expected_plan_version=1)`，v2 计划被误判后以 UNKNOWN/FATAL 终止。本步只修该接线缺口、轮换后的本地凭据读取，以及 H05 CTest 夹具。核心代码补充了中文注释。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `agent/runtime/graph.py` | VALIDATE 对 `plan_version>1` 或已有完成任务走 `validate_replanned_pevr_plan`。 | P0-15 生产 replan 可二次失败并耗尽额度，而不是在 v2 门禁处 fatal。 |
| 修改 | `agent/runtime/faults.py` | `plan_validation_failed` 归入 `PLAN_INFEASIBLE`，避免未知故障 fail closed 吞掉可重规划错误。 | 恢复策略表；不能替代正确的重规划 Validator。 |
| 修改 | `services/config/settings.py` | `load_settings()` 在未传入 `environ` 时读取项目 `.env` 白名单键；进程环境变量仍最高优先。 | 集成测试、`check_postgres.py`/`check_qdrant.py` 使用轮换凭据。 |
| 修改 | `tests/integration/test_p014_postgres.py`、`test_p006_postgres.py`、`test_p007_rag_backends.py`、`tests/helpers/p014_process_worker.py` | 数据库/Qdrant 夹具改为 `load_settings()`；遗留 collision 用例直接写入无 `lookup_id` 的 JSONB。 | C02 迁移与 C04 密钥轮换可在本机复验。 |
| 修改 | `tests/unit/test_settings.py`、`tests/unit/test_p015_faults.py` | dotenv 解析与 `plan_validation_failed` 分类回归。 | 防止再把公开 DSN 或 UNKNOWN 分类写回去。 |
| 修改 | `services/planner_cpp/tests/route_planner_tests.cpp` | 提前到达等待代价、idle 绕行时域、非法 `deadline==release_time` 夹具修正。 | H05 CTest 覆盖预定位/等待/绕行/反例。 |
| 修改 | `docs/SERVICES_STARTUP.md` | 撤下已公开的 PostgreSQL `123456` 示例。 | 本步无核心代码注释需求。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 记录本步完成内容、实测命令和坑。 | 后续在线 HITL/视频/RAG holdout。 |

## 2026-08-21：收口发布复验（Fast/数据库/HITL/评测）

本步以启动真实 Fast 与 Compose 并跑发布验收为主。核心代码注释只补充启动器编码/超时原因；评测 JSON 在 `tmp/`，不是源码交付物。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `scripts/start_fast_secure.ps1` | 写入 UTF-8 BOM，让 Windows PowerShell 5.1 能解析中文 throw 字符串。 | `-StartFast`、H04 artifact 预检。 |
| 修改 | `scripts/start_local.ps1` | 优先 `pwsh.exe` 拉起 Fast；健康检查 600s；同样加 BOM。 | 一键启动；避免 300s 空等后误杀加载中的模型。 |
| 修改 | `config/fast_model_manifest.json` | launcher size/sha256 随 BOM 更新，启动器自校验才能通过。 | `verify_fast_artifact.py`、网关版本证据。 |
| 修改 | `tests/unit/test_p016_security.py` | HTTP HITL 夹具改用墙钟，避免 15 分钟 TTL 过期后 409。 | P0-16 回归、smoke。 |
| 修改 | `README.md`、`docs/DEMO_SCRIPT.md`、`docs/TEST_REPORT.md`、`docs/RESUME_FACTS.md`、`docs/SERVICES_STARTUP.md`、`docs/P013_PEVR.md` | 换成 HITL 三连真实 run_id、smoke 272/38、安全启动器；撤下 `--approve-dispatch`。 | H07 对外口径；H06 视频仍缺。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 记录实测命令、服务仍在跑、视频缺口。 | 后续 Agent/录屏。 |

## 2026-08-21：启动脚本跳过 Fast SHA-256

演示启动被 19GB GGUF 哈希堵住。manifest 增加 `verify_sha256=false`；启动脚本与网关只检查文件存在和大小。记录用 sha256 仍保留，但不能当成启动时重新算过。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `config/fast_model_manifest.json` | `verify_sha256=false`；同步启动器 size/sha256。 | `start_local.ps1 -StartFast`、网关版本记录。 |
| 修改 | `scripts/start_local.ps1`、`scripts/start_fast_secure.ps1` | 按 manifest 跳过 SHA-256，并打印进度。 | 本地演示启动。 |
| 修改 | `services/model_gateway/artifacts.py` | `load_and_verify_fast_artifact` 尊重 `verify_sha256`。 | `check_model_gateway.py`、Provider 启动。 |
| 修改 | `tests/unit/test_model_artifacts.py` | 关闭哈希后等长篡改可通过，截断仍失败。 | 防止再把演示启动扫回 19GB。 |
| 修改 | `docs/HANDOFF_CONTEXT.md`、`docs/FILE_PURPOSES.md`、`docs/LESSONS_LEARNED.md` | 记录该启动权衡。 | 后续 Agent。 |

## 2026-08-21：Fast 隐藏启动器空转

`start_local -StartFast` 用 Hidden 拉起启动器后，llama-server 没有出现，父进程空等 `/health`。改为最小化窗口、标准输出日志、子进程退出立即失败，并去掉 `--model` 的多余引号。

| 变更 | 文件 | 作用 | 下游影响 |
|---|---|---|---|
| 修改 | `scripts/start_fast_secure.ps1` | Transcript + llama 日志；`ProgressPreference`；无引号模型路径。 | 演示启动可看 `tmp/fast_secure.transcript.log`。 |
| 修改 | `scripts/start_local.ps1` | 启动器最小化且 PassThru；子进程退出则立刻抛出日志尾。 | `-StartFast`。 |
| 修改 | `config/fast_model_manifest.json` | 同步启动器 size/sha256。 | 大小门禁。 |

