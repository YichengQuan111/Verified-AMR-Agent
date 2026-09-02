# AMR Agent P0-00～P0-20 发布前全仓审查 Todo

审查日期：2026-08-21  
审查范围：仓库当前 P0-00～P0-20 实现、公共契约、Python/C++ 算法、RAG、安全、恢复、评测、报告和部署链路。  
审查原则：以实际代码、实际数据库状态、真实命令退出码和正式 P0 路线为准；本轮只审查与记录，不修改业务代码、不重构。  
模型边界：Qwen3.8 Smart 已按用户要求排除在 P0 缺陷之外；本次在线模型只使用 Qwen3.6 Fast。

## 1. P0 Release Verdict

**FAIL**

当前正常单订单链路可以真实调用 Qwen3.6 Fast、RAG、C++ Hungarian/A*/Validator 和 Python 仿真并生成可重算报告；全量 smoke 也通过。但是发布入口仍可用 legacy 布尔值绕过签名身份/HITL，跨运行复用了同一仿真外部执行 ID 并使恢复失败，P0-15 恢复控制器没有接入生产 PEVR 图，Compose 默认部署又暴露已知 JWT/数据库凭据及无认证 Qdrant。这四项分别落入安全违规、无法恢复和核心异常闭环失败，不能以测试绿灯或历史文档的“已完成”覆盖。

本次确认的 Todo 数量：

- P0-Critical：4
- P1-High：7
- P2-Medium：3
- P3-Low：0（未发现值得在发布审查中单列且有充分证据的低优先级问题）

## 2. 发布前必须修的最小问题集合

以下集合缺一不可；修复后必须重跑第 8 节所列发布验收：

1. `AUDIT-C01`：所有发布/演示执行入口强制启用验签 Principal、HITL Store 和签名 ApprovalGrant，删除 legacy 布尔审批的发布可达路径。
2. `AUDIT-C02`：修复外部仿真执行 ID 的跨运行冲突，完成既有冲突记录的安全迁移，并证明终态/中断恢复不会重放副作用。
3. `AUDIT-C03`：把 P0-15 有界恢复控制器和 LocalReplanner 接到真实 PEVR 执行路径，确保异常进入明确的 retry/replan/fallback/human/fatal 终态而不是直接抛错或永久停在 `planning`。
4. `AUDIT-C04`：移除 Compose 已知默认密钥与公开数据库/Qdrant 端口，启用服务间认证或仅内部网络访问，并轮换当前凭据。
5. `AUDIT-H01`、`AUDIT-H02`、`AUDIT-H03`：修复评测 oracle、RAG 退出门禁和策略对照后，重新生成可证明真实行为的 P0-18/P0-19 报告；否则发布指标不可引用。
6. `AUDIT-H04`：固定并校验实际 Fast 模型/参数/文件哈希，保证发布报告能够证明运行的具体 artifact。
7. `AUDIT-H05`：修复 release_time 前禁止移动导致的可行计划误判，并统一 Planner/Validator/Simulator 时间窗语义。
8. `AUDIT-H06`：**2026-09-02 已关闭**。对外演示资产改为 GIF，不再要求正式演示视频。
9. `AUDIT-H07`：在上述修复和复测后同步纠正 README、测试报告、演示稿和简历事实，撤下无法证明的能力与指标。

## 3. P0-00～P0-20 逐项验收矩阵

这里的“通过”只表示本轮未发现违反该工作包验收门槛的证据，不表示对未来输入的形式化证明。

| 工作包 | 审查结论 | 实际证据与缺口 |
|---|---|---|
| P0-00 Scope/种子/Backlog | PASS | `docs/scope.md` 与 30×20 地图、4 AMR、6+6 工位、2 充电站实际资源一致，未发现越界实现被包装为 P0。 |
| P0-01 工程骨架 | PASS | 全量 smoke、Python 与 C++ 构建/测试入口可运行；生成物与源码边界清楚。精确容器复现缺口归入 P0-20。 |
| P0-02 本地模型 Profile | FAIL | Fast 实际为 16K、temperature 0.1、IQ4_NL，评测配置却记录 8K、temperature 0、泛化 `GGUF`；模型文件哈希未进入运行版本证据，见 `AUDIT-H04`。Smart 禁用不计缺陷。 |
| P0-03 模型网关 | PARTIAL | alias 门禁、20 例结构化输出和真实在线调用通过；`ModelVersionRecord` 不能证明实际 GGUF、量化和采样参数，受 `AUDIT-H04` 影响。 |
| P0-04 公共契约 | PARTIAL | 51 份 Schema 重新导出与仓库零差异，TaskContract/PlanTask/ToolSpec 主字段一致；`RunState` 可接受“后继已完成、前置仍 pending”，见 `AUDIT-M01`。 |
| P0-05 Context Engineering | PASS | 五节点 Prompt、预算、有限上下文和生产 Prompt 的不可信检索边界已有真实/回归证据；P0-18 注入样例没有调用这条真实边界，问题归入 `AUDIT-H01`。 |
| P0-06 API/PostgreSQL | PASS | 8 张核心表、迁移、真实 PostgreSQL 集成测试和 API 认证回归通过；部署默认凭据/端口属于 P0-16/P0-20 发布配置缺陷。 |
| P0-07 RAG/ACL/citation | PARTIAL | 真实 Qdrant+Embedding 20 例为 Recall@K=1、MRR=0.970588、citation=1、answerability=1、ACL leak=0；阈值同集校准且 CLI 只按 ACL 决定退出码，见 `AUDIT-H03`。 |
| P0-08 Hungarian 分配 | PASS | CTest 正反例和在线链路均通过，未发现分配契约/JSON 适配错配。 |
| P0-09 A*/reservation | FAIL | 预约、顶点/交换边、等待和 Dijkstra 基线测试通过，但 release_time 前禁止任何移动会拒绝物理可行计划，见 `AUDIT-H05`。 |
| P0-10 Fleet Validator | PASS | 独立 Validator 正反例通过；封路轨迹能在仿真前被拒绝，未发现 LLM 字段旁路。 |
| P0-11 Python 仿真 | PASS | 正常、低电量、离线、掉电和封路抽查符合安全停机语义；在线路线 37 个时间戳与 37 个 `amr.path_step` 逐项一致。异常之后的 Agent 恢复缺口属于 P0-15。 |
| P0-12 工具注册表 | PASS | 仅九个已注册工具、严格输入 Schema、角色/错误/超时/幂等门禁测试通过，未发现任意 Shell/SQL/HTTP/未注册工具执行面。 |
| P0-13 PEVR 主闭环 | FAIL | 真实正常链路完成 8/8 阶段与 5/5 工具，但正式 CLI 通过 legacy bool 派发，无 Principal/ApprovalGrant，见 `AUDIT-C01`。 |
| P0-14 Checkpoint/Effect Ledger | FAIL | 同计划跨 run 产生相同外部仿真 ID；数据库已有 6 条冲突记录，同 run 恢复实测失败，见 `AUDIT-C02`。LocalReplanner 组件测试本身通过。 |
| P0-15 故障恢复 | FAIL | 分类器、预算和 LocalReplanner 组件测试通过，但生产图不调用 `FaultRecoveryController`；超时/无解/blocked 直接抛错，见 `AUDIT-C03`。 |
| P0-16 RBAC/HITL/安全 | FAIL | JWT/RBAC/工具白名单的单元/接口反例通过，但发布入口绕过 HITL，默认 JWT 可伪造，Qdrant 可匿名读取 operator-only 文档，见 `AUDIT-C01`、`AUDIT-C04`。 |
| P0-17 Trace/验证报告 | PASS | 在线 Trace 序号、工具计数、时长、订单、仿真时间戳和报告指标独立重算一致；受控 pytest/CTest 白名单工作正常。 |
| P0-18 60 例 Eval | FAIL | 命令返回 60/60，但 runner 不消费 oracle、若干分支自生成期望证据，注入样例出现确定性假通过，见 `AUDIT-H01`。 |
| P0-19 策略对照 | FAIL | 实际为同一 P0-18 Trace 的标签/步数投影，不是三个独立策略执行，Token/资源/墙钟均未观测，见 `AUDIT-H02`。Smart 对照按用户要求不作为缺陷。 |
| P0-20 部署/演示 | FAIL | 一键脚本与现有 Compose 健康检查可运行，容器也能访问宿主 Fast；但安全默认值、恢复/审批问题和依赖复现不满足发布收口，见 `AUDIT-C01`～`C04`、`AUDIT-M02`。`AUDIT-H06` 已于 2026-09-02 按产品决定关闭（演示改为 GIF）。 |

## 4. P0-Critical

### AUDIT-C01：正式 PEVR/演示入口可用 legacy 布尔值绕过签名身份与 HITL

- **严重程度**：P0-Critical
- **涉及文件/函数**：`scripts/run_p013_e2e.py:78-90`；`agent/runtime/pevr.py:70-109` 的 `PEVRRequest`；`agent/runtime/graph.py:325-341` 的 `PEVRGraphRunner.__init__`；`agent/runtime/graph.py:1885-1943` 的 dispatch 审批分支；`docs/DEMO_SCRIPT.md:18-23`。
- **复现方式或证据**：本轮真实执行 `run_p013_e2e.py --approve-dispatch` 生成 `tmp/p0_audit_online_normal.json`，其中 `request.principal=null`、`approval_grant=null`、`approval_granted=true`，`dispatch_simulation` 仍成功并写入 Effect。数据库查询显示三个历史“正式连续成功” run 和本轮 audit run 均为 `effect_count=1`、`approval_count=0`。这不是缺少审计展示：实际 `approvals` 表没有审批票据。
- **为什么违反当前设计/P0 要求**：P0-16 规定 Principal 只能来自验签身份，副作用必须由同一 run/task/Validator 证据绑定的 HITL grant 放行。发布入口以 CLI 布尔值代替审批，使任何能启动脚本的人都能绕过 viewer/operator、审批状态和票据完整性；符合“错误执行/安全违规”。
- **推荐修复方向**：发布/演示构造 Runner 时强制 `security_required=True`，注入 PostgreSQL HITL Store 和 JWT 验签 Principal；首次运行必须保存 `waiting_approval` 并返回 interrupt，审批只能由受信任适配器签发 `ApprovalGrant`，随后从同一 checkpoint 恢复。legacy bool 只能保留在明确的 test fixture，发布 CLI/API 不得可达。
- **修复后的验收方法**：无身份、viewer、仅 `--approve-dispatch`、跨 run grant、跨 task grant、过期/篡改 grant 均在 handler 前拒绝且 Effect=0；合法 operator 首次进入 `waiting_approval`，数据库 Approval=1，恢复后 Effect=1；重复恢复 Approval/Effect/dispatch 均仍为 1。在线报告必须带 `principal_subject`、approval ID 与 checkpoint 证据。
- **是否可能影响其他模块**：会影响 P0-13 CLI、P0-14 waiting checkpoint、P0-16 API/HITL、P0-17 Trace、P0-18 安全评测和 P0-20 演示脚本；应与 `AUDIT-C02` 一起修，避免安全恢复再次触发外部 ID 冲突。

### AUDIT-C02：仿真外部执行 ID 未包含 run/effect 身份，跨运行冲突并使恢复失败

- **严重程度**：P0-Critical
- **涉及文件/函数**：`agent/tools/registry.py:680-769` 的 `_dispatch_handler`；`services/application/checkpoint_service.py:416-466` 的 `PostgresRuntimeStore.put/get`；`services/persistence/repositories.py:152-164` 的 `list_by_external_execution_id`；`agent/runtime/graph.py:139-239` 的外部状态查询适配器。
- **复现方式或证据**：`registry.py:689` 仅由 plan/seed/until 等 payload digest 生成 `simulation-<24hex>`，没有 run/effect namespace。PostgreSQL 实查有 **6 条、6 个不同 run** 的 Effect 共享 `simulation-b7551b825b817593d1e700fe`。调用 `PostgresRuntimeStore.get(...)` 稳定抛出 `PersistenceConflictError: 外部执行 ID 关联了多条 Effect`。在 Fast 在线且首轮已完成时，用相同 `run_id` 重跑真实 E2E 立即失败为“已完成账本与外部状态无法一致核对”，没有返回已完成结果。
- **为什么违反当前设计/P0 要求**：P0-14 要求恢复先核对唯一外部事实并证明副作用不重复。当前外部身份在合法的跨 run 重复计划中必然碰撞，恢复查询无法确定记录归属；这已经造成“无法恢复”，也使跨进程幂等证据不可信。
- **推荐修复方向**：把外部执行身份设计为稳定且全局唯一的 `run_id + plan_version + task_id/effect ledger id + payload digest`，或让查询始终以 Effect Ledger 主键/幂等键和 run scope 定位，不能只按 simulation ID 全局扫描。增加数据库唯一约束与安全迁移；既有 6 条冲突记录必须按原 run/effect 回填，无法证明归属的记录转人工而不是重放。
- **修复后的验收方法**：同一 plan/seed 连续三个新 run 得到三个可区分的外部 ID；每个 run 在 dispatch 前、handler 返回后、ledger complete 前分别强杀并跨进程恢复，最终每个 run 的 Approval/Effect/外部执行各一条、dispatch 调用一次；终态同 run 再次调用直接回放已核对结果。并发恢复也必须满足相同不变量。
- **是否可能影响其他模块**：影响 ToolResult `effect_id`、simulation evidence URI、Effect Ledger 数据迁移、Trace/报告引用、P0-18 duplicate metric 和所有历史演示 artifact；不能只改字符串生成而不迁移查询与报告契约。

### AUDIT-C03：P0-15 恢复状态机/LocalReplanner 没有接入生产 PEVR 图

- **严重程度**：P0-Critical
- **涉及文件/函数**：`agent/runtime/graph.py:396-415` 的固定线性图；`agent/runtime/graph.py:1984-2056` 的工具/Validator/仿真失败处理；`agent/runtime/graph.py:2337` 的 `requires_replan` Observation；`agent/runtime/faults.py:534-879` 的 `FaultRecoveryController`；`evals/p018/runner.py:585-735` 的离线异常分支；`tests/integration/test_p015_fault_recovery.py`。
- **复现方式或证据**：全仓排除测试后，`FaultRecoveryController(` 的唯一调用在 `evals/p018/runner.py:612`；生产 `agent/`、`services/`、`apps/`、`scripts/` 无调用。图只按八阶段添加固定边，没有 failure→controller→retry/replan/human 条件边。专项测试 `test_route_timeout_stops_validator_and_dispatch` 和 `test_simulator_success_envelope_cannot_hide_incomplete_order` 的正确预期都是抛 `PEVRExecutionError`，不是恢复。直接仿真抽查中 offline/掉电返回 `blocked + requires_replan`，但生产 dispatch 不允许 fault fixture 且图不消费该标记。数据库还保留三个 P0-20 超时 run 为 `status=planning`，其最后 Trace 已是 `TIME_BUDGET_EXCEEDED/failed`。
- **为什么违反当前设计/P0 要求**：P0-15 的交付物不是孤立分类器，而是实际主循环中的有界 retry/replan/fallback/human/fatal。当前正常链路成功，任何真实 timeout/infeasible/blocked 都直接退出或留下非终态数据库记录；低电量、离线、封路等核心异常闭环未完成。
- **推荐修复方向**：在 PEVR 图内或受信任的外层执行器接入唯一 `FaultRecoveryController`，所有失败先持久化 FaultSignal/Checkpoint，再按静态策略和 TaskContract 预算路由；REPLAN 必须调用 LocalReplanner 并复验完整 DAG，只失效受影响未完成节点；副作用不明先查外部状态，禁止盲 retry。预算耗尽必须持久化 human/fatal/failed 终态。
- **修复后的验收方法**：用真实 ToolRegistry/Checkpoint Store 对低电量、offline、封路、工位占用、工具 timeout、计划 infeasible、状态冲突各跑端到端轨迹；断言动作序列、retry/replan≤2、总步骤/时间/Token 有界、已完成 task/effect 不重做、未知副作用不重放、最终状态非永久 `planning`。至少包含并发恢复和反例测试。
- **是否可能影响其他模块**：影响 P0-13 图结构、P0-14 checkpoint/ledger、P0-16 审批恢复、P0-17 Trace 和 P0-18 异常指标；属于跨模块改动，应先固定公共状态机再更新评测。

### AUDIT-C04：Compose 默认部署公开已知凭据的 API/PostgreSQL/Qdrant，ACL 可被绕过

- **严重程度**：P0-Critical
- **涉及文件/函数**：`compose.yaml:4-13`、`:24-32`、`:61-75`；`agent/security/auth.py:24-137`；Qdrant collection `amr_warehouse_knowledge`。
- **复现方式或证据**：`docker compose ps` 显示 API 8000、PostgreSQL 5432、Qdrant 6333/6334 均绑定 `0.0.0.0` 和 `[::]`。Compose 默认 PostgreSQL 密码为 `123456`，JWT secret 为公开仓库字符串 `p016-development-only-change-this-jwt-secret-2026`。本轮用该默认 secret 本地签发 operator JWT，请求 `/agent/runs/p0-audit-online-normal-20260821-1659` 返回 HTTP 200。无需任何认证直接调用 Qdrant scroll 返回 70 个 chunk，其中 25 个 payload 是 operator-only。
- **为什么违反当前设计/P0 要求**：应用层 JWT/RAG ACL 无法保护可直接访问的数据库和向量库；公开默认签名密钥还能伪造任意 API operator。即使当前演示机在可信局域网，这一默认发布形态已经允许数据泄漏和审批面绕过，属于 P0 安全阻断。
- **推荐修复方向**：Compose 在缺少强随机外部 secret 时 fail startup；API 可按需要绑定宿主端口，PostgreSQL/Qdrant 默认只在内部 Compose network，不发布宿主端口；如确需宿主访问，显式绑定 `127.0.0.1` 并给 Qdrant 配 API key/TLS。轮换当前 JWT/DB/Qdrant 凭据，避免继续信任已经提交的默认值。
- **修复后的验收方法**：全新环境不提供 secrets 时 Compose 明确失败；提供 secrets 后，宿主/其他机器无法直接访问 PostgreSQL/Qdrant，匿名 scroll 被拒，旧默认 JWT 返回 401，合法新 JWT 按 viewer/operator 生效。把这些反例纳入 P0-20 部署测试，而不是只断言 YAML 含字段。
- **是否可能影响其他模块**：影响本地开发连接方式、RAG 索引脚本、smoke、API 测试和演示说明；需提供显式开发 profile，不能通过重新加入弱默认值恢复便利性。

## 5. P1-High

### AUDIT-H01：P0-18 oracle 未被消费，多个分区自生成期望证据并产生假通过

- **严重程度**：P1-High
- **涉及文件/函数**：`evals/p018/contracts.py:80-102`；`evals/p018/runner.py:192-238` 的 `run_case`；`:307-394` 正常分支；`:396-516` RAG 分支；`:585-735` 异常分支；`:819-875` 安全分支。
- **复现方式或证据**：把 `p018-normal-001.oracle` 改成 `{"must_fail": true, "duplicate_side_effect_count": 999}` 后，`run_case` 仍返回 `evaluation_passed=True`、`observed_outcome=completed`、duplicate=0；runner 全文没有读取 `case.oracle`。RAG 分支读取数据集预填的 `input_data.retrieved`，正常分支用 Python Manhattan `_walk`，异常分支单独构造 Controller 后自发 success 事件，没有运行生产 PEVR/C++/真实 registry。注入样例的 `input_data.text` 未传入 Prompt/模型；当前 Prompt 不含 runner 查找的两个中文标记时，runner 反而捕获自己抛出的 `PermissionError` 并记为 `denied + passed`。因此本轮 60/60 退出码 0 不能证明 60 条生产行为。
- **为什么违反当前设计/P0 要求**：P0-18 要求固定 oracle 对真实观察进行判定并保留可审计轨迹。当前 `expected_outcome` 与 runner 的 scenario 分支共同制造结果，数据集 oracle、生产安全边界、真实算法和真实恢复均可失效而报告仍全绿，直接损害评测可信度。
- **推荐修复方向**：将“纯契约 fixture 回归”和“P0-18 发布验收”分开命名；发布 Harness 必须通过真实 PEVR/ToolRegistry/C++/Simulator/HITL/Checkpoint adapter 产生观察，再由独立 oracle predicate 校验。所有 oracle 字段必须有消费覆盖，未知 oracle key fail closed；禁止用 catch 自己制造安全成功。
- **修复后的验收方法**：对 oracle、期望 code、路线、ACL、审批票据、effect ID 做 mutation testing，任一关键事实突变都必须使 case 非通过；注入文本必须真实进入不可信上下文，handler 调用计数为 0；异常样例能关联真实 checkpoint/effect/trace ID；报告中的每个指标可从原始轨迹独立重算。
- **是否可能影响其他模块**：会改变 P0-18 报告 digest、P0-19 输入、README/TEST_REPORT/RESUME_FACTS 指标和 CI 时长；旧报告只能保留为“离线 fixture 回归”，不可继续作为发布验收。

### AUDIT-H02：P0-19 只是同一轨迹的控制步投影，不是 Workflow/ReAct/PEVR 公平对照

- **严重程度**：P1-High
- **涉及文件/函数**：`evals/p019/replay.py:244-324` 的 `_projection/_case_result`；`:507` 的报告元数据；`evals/p019/contracts.py:249`；`docs/P019_STRATEGY_COMPARISON.md:3-76`。
- **复现方式或证据**：本轮生成 180 条结果，但三策略复制同一 P0-18 `observed_outcome`、工具结果、恢复和安全指标；ReAct 仅把源事件展开成 `think/act/observe`，`react_production_path_touched=false`。三策略均 60/60、任务 44/44、恢复 8/8、Trace P95 70ms；模型调用与 Token 未观测，CPU/RSS/GPU 未观测，延迟明确不是墙钟。差异只有派生步数。
- **为什么违反当前设计/P0 要求**：正式 P0-19 要求三种策略在相同任务、模型、工具和预算下各自执行，比较真实任务完成、合法性、恢复、错误、Token、P95 和资源。对同一终态重标标签无法验证策略质量或公平性，也不能支持面试/简历中的策略效果结论。
- **推荐修复方向**：实现三个评测层策略 runner，固定同一 60 case、Fast profile、Prompt/ToolSpec、seed、预算和外部快照；每个策略独立调用模型/工具并输出原始轨迹。ReAct 仍可隔离在 eval 进程，不能接入生产图。当前 replay 可保留为 Trace 可视化/步数投影，但不要称为 P0-19 验收。
- **修复后的验收方法**：180 条原始轨迹必须有独立 run/trace/call/effect identity；工具输入相同规则可审计，终态不由源报告复制；Token、墙钟 P95 和资源均为真实观测或明确不作为验收指标。用故意劣化某一策略的反例证明指标能够分离策略。Smart 继续禁用/延期，不作为本项验收条件。
- **是否可能影响其他模块**：依赖先修 `AUDIT-H01`，并影响 P0-17 Trace、模型吞吐、报告格式和简历事实；可能显著增加评测时间。

### AUDIT-H03：RAG 阈值同集校准，且严重指标失败时 CLI 仍返回 0

- **严重程度**：P1-High
- **涉及文件/函数**：`services/config/settings.py:229-234`；`evals/rag/run_eval.py:134-302` 的评测/阈值建议；`:321-324` 的阈值覆盖；`:380` 的退出码。
- **复现方式或证据**：真实 20 例结果为 Recall@K=1、MRR=0.970588、citation=1、answerability=1、ACL=0。`docs/RAG.md:82` 和交接记录明确默认 hybrid 0.809/vector 0.499 由这同一 20 例分布校准；本轮报告中当前 hybrid 分布已不可完全分离（`suggested_if_separable=null`），fallback vector 建议值为 0.498873、默认值为其舍入后的 0.499，仍没有独立 holdout。更直接的 fail-open 复现：同时传 `--minimum-hybrid-score 1 --minimum-vector-score 1` 后 citation correctness=0、answerability accuracy=0.15，但进程仍退出 0，因为 `main()` 只检查 `acl_leak_count`。
- **为什么违反当前设计/P0 要求**：P0-07 的拒答/citation 不只是展示指标，CI 需要在退化时失败；同集选阈值再同集报告会高估泛化，属于评测泄漏。当前命令的绿色退出码不能作为 RAG 发布门禁。
- **推荐修复方向**：固定互斥的 calibration/test/attack 集并记录版本；阈值只从 calibration 产生，发布指标只在未参与调参的 test/attack 上计算。CLI 同时门禁 Recall/MRR/citation/answerability/ACL，且 citation_total=0 不能当作可接受的 0 分母状态。
- **修复后的验收方法**：上述阈值 1 的命令必须非零；在固定 holdout 上达到正式阈值，ACL 仍为 0；修改 calibration 不会改写同次 test oracle；报告保存集合 digest、阈值来源和每例检索证据。
- **是否可能影响其他模块**：影响默认 RAG 配置、P0-13 retrieve、P0-18 RAG 分区和所有引用当前 1.0 指标的文档。

### AUDIT-H04：实际 Fast 模型 artifact/参数与仓库记录不一致，运行版本证据不完整

- **严重程度**：P1-High
- **涉及文件/函数**：`evals/p018/config.json:11-24`；`services/config/settings.py:75-107`；`services/model_gateway/contracts.py:39-50` 的 `ModelVersionRecord`；`services/model_gateway/provider.py:142-171` 的启动门禁；外部 `E:\Llama.cpp\start-qwen3.6-agent.cmd`。
- **复现方式或证据**：外部正式脚本实际参数是 alias `qwen3.6-fast`、`--ctx-size 16384`、`--temp 0.1`、top-p 0.95、top-k 20、parallel 1，模型为 `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf`。仓库 P0-18 配置记录 context 8192、temperature 0.0、quantization `GGUF`，`artifact_ref` 只指向外部脚本。实测模型文件 SHA-256 为 `B228C988C624DFFE0B57235A395FA79562D4362FED545820F9B7D78908F337E6`，脚本 SHA-256 为 `B1543FB46AA04E835E4BBD666F839567D4972AAD1BE3662F386F06842856F30F`，但 `ModelVersionRecord`/在线报告只保留 alias、URL、created/owned_by 等字段。
- **为什么违反当前设计/P0 要求**：P0-02 明确要求模型身份、量化、context、采样参数和真实文件 hash 可复现。alias 相同不能证明模型文件或启动参数相同；当前 P0-18/P0-19 指纹把不同配置称为同一 Fast 身份。
- **推荐修复方向**：建立仓库内可校验的 Fast artifact manifest，记录 llama.cpp build/version、GGUF 路径或 artifact ID、大小/hash、量化、context、采样、并发与 reasoning；启动预检从服务元数据和本地 manifest 核对并写入 `ModelVersionRecord`。配置不同必须生成不同 execution profile/report identity。
- **修复后的验收方法**：更换 GGUF、改 temp/context、改脚本或服务 alias 任一项都使启动门禁失败；合法启动的 health/Trace/Eval 报告包含上述两个 SHA、实际 IQ4_NL 和 16K/0.1 参数；在新环境仅凭 manifest 与文档能复建同一服务。
- **是否可能影响其他模块**：需要升级 P0-03/P0-17/P0-18/P0-19 Schema 和历史报告兼容；不会要求重新启用 Smart。

### AUDIT-H05：A* 把 release_time 解释为“此前整车只能等待”，拒绝可行时间窗计划

- **严重程度**：P1-High
- **涉及文件/函数**：`services/planner_cpp/src/route_planner.cpp:395-415` 的 `candidates_for`；`:497-539` 的 A* 搜索；`:678-693` 的 pickup/dropoff 两段规划；`services/planner_cpp/src/fleet_plan_validator.cpp:642-645`。
- **复现方式或证据**：10×3 空地图，AMR 从 `(0,1)` 朝东，pickup `(5,1)`、dropoff `(6,1)`，`release_time=5`、`deadline=7`、`max_time=7`。合法语义只要求 pickup 不早于 5，因此 AMR 可在 t=0..5 行驶到 pickup、t=6 到 dropoff。实际 `route_planner_cli --algorithm astar` 返回 `status=infeasible`、`no_safe_path_to_pickup`、空 path。代码在 `current.time < earliest_goal_time` 时只生成 wait，禁止预定位；独立 Validator 只检查实际 pickup 不早于 release，二者语义不一致。
- **为什么违反当前设计/P0 要求**：时间窗是订单不可在 release 前完成 pickup，不等于 AMR 在 release 前不能移动。过强隐含假设把可行计划误判为无解，真实晚释放订单会触发错误 replan/human/fatal。
- **推荐修复方向**：允许 release 前正常行驶与预约，只在 goal acceptance/pickup 事件处约束 `time>=release_time`；若提前到达可等待，并继续遵守 cell/edge reservations、终点占用和 deadline。明确是否允许占用 pickup 格，并让 Planner/Validator/Simulator 使用同一契约。
- **修复后的验收方法**：把本复现加入 A*/Dijkstra CTest，二者均在 t=5 pickup、deadline 前 dropoff且 Validator valid；另加提前到达等待、多 AMR 预约冲突、release 晚于物理可达窗口、真正 infeasible 的反例。
- **是否可能影响其他模块**：影响任务分配的迟到估计、P0-10 时间窗证据、仿真事件和 Eval case；路线输出时间戳变化会改变既有 snapshot/digest。

### AUDIT-H06：P0-20 要求的演示视频交付物不存在

- **严重程度**：P1-High（**2026-09-02 关闭**：产品决定不再录制正式演示视频，对外演示改为仓库 GIF `docs/media/demo_v0.gif`，已嵌入 `README.md`。本条不再作为发布阻塞项。）
- **涉及文件/函数**：正式 `docs/AMR_Agent_P0技术路线与实施ToDo.docx` 的 P0-20 交付物；`docs/DEMO_SCRIPT.md`；仓库发布资产。
- **复现方式或证据**：2026-08-21 审计时递归搜索 `.mp4/.mov/.mkv/.webm/.avi`（排除 `.git/tmp`）返回 `demo_media_files=0`。当时仓库只有演示口播/命令脚本。
- **为什么违反当时设计/P0 要求**：P0-20 曾把演示视频列为发布收口交付物。
- **推荐修复方向（已作废）**：原建议在功能修复后录制真实闭环。
- **修复后的验收方法（已替换）**：以 README 相对路径引用的 GIF 作为对外演示资产即可。
- **是否可能影响其他模块**：已同步更新 README、PROJECT_OVERVIEW、DEMO_SCRIPT、TEST_REPORT、RESUME_FACTS 和交接文档。

### AUDIT-H07：README/报告/简历事实把组件回归或离线投影写成已完成能力与指标

- **严重程度**：P1-High
- **涉及文件/函数**：`README.md:16-58`、`:364-430`；`docs/TEST_REPORT.md:21-57`；`docs/RESUME_FACTS.md:7-11`；`docs/DEMO_SCRIPT.md:37-75`；`docs/HANDOFF_CONTEXT.md` 当前状态；`docs/P018_EVAL.md`。
- **复现方式或证据**：README 声称 P0-14/15/16 已完成且审批绕过全部阻断，但正式连续 run 的 Approval 均为 0、生产恢复控制器无调用、同 run 恢复失败。`RESUME_FACTS.md:9` 把 P0-18 的 `rag_permission_approval` 10 例离线 fixture 描述成“RAG 20 例 MRR=1.0”；真实独立 RAG 20 例 MRR 是 0.970588。TEST_REPORT/P018 报告中的 recovery/security 1.0 来自 `AUDIT-H01` 所述自生成轨迹。P019 文档虽诚实注明 replay，首页仍把工作包整体标为完成。
- **为什么违反当前设计/P0 要求**：发布文档和简历只能写实际可复现的能力。组件测试通过不等于生产图接入，离线自生成指标不等于真实 20 例检索或在线策略对照；这些矛盾会在演示、评测或面试追问时暴露。
- **推荐修复方向**：Critical/High 修复前先把相关结论标为“发布审计未通过/组件级已实现”；修复后仅从最终不可变 artifact 生成指标表。每条简历事实链接到命令、report ID/digest 和执行模式；区分真实在线、真实离线组件、fixture oracle 和 Trace projection。
- **修复后的验收方法**：逐条对 README 状态表、专题报告、TEST_REPORT、DEMO_SCRIPT、RESUME_FACTS 做证据反查；所有数字能从引用 JSON 重算，执行模式和样本数一致；CI 可校验报告 digest/关键指标与 Markdown 不漂移。
- **是否可能影响其他模块**：文档改动广，但不应在业务修复前预写“通过”；历史 artifact 保留原结论并明确已废止，避免破坏审计链。

## 6. P2-Medium

### AUDIT-M01：RunState 不校验“已完成任务的所有依赖也已完成”

- **严重程度**：P2-Medium
- **涉及文件/函数**：`agent/runtime/state.py:143-218` 的 `RunState.validate_run_state`；`tests/unit/test_p004_contracts.py` 的 RunState 反例集合。
- **复现方式或证据**：构造 `TASK-B.dependencies=[TASK-A]`，把 B 标为 `completed` 并放入 `completed_task_ids`，A 保持 `pending`；`RunState.model_validate` 返回成功。现有校验只检查 DAG、列表与 task status 一致，没有检查 completed set 的依赖闭包。
- **为什么违反当前设计/P0 要求**：RunState 是 Checkpoint/恢复公共契约。它允许持久化不可能的中间状态，后续“跳过已完成任务”可能绕过前置任务或使局部重规划锚点失真。当前未在真实 run 中发现该坏状态，因此不提高到 Critical。
- **推荐修复方向**：增加状态闭包不变量：completed task 的所有直接/传递依赖必须 completed；running task 的依赖必须 completed；可同时明确 failed/pending 与 run status 的允许组合。
- **修复后的验收方法**：加入上述直接/传递依赖反例、恢复坏快照反例和合法局部重规划状态；Schema/Pydantic 入口、PostgreSQL 回读和 PEVR checkpoint 均 fail closed。
- **是否可能影响其他模块**：可能暴露历史坏 checkpoint；上线前应扫描并迁移/隔离，避免新校验使服务启动时无说明失败。

### AUDIT-M02：容器“锁文件”、基础镜像和宿主路径不足以精确复现发布环境

- **严重程度**：P2-Medium
- **涉及文件/函数**：`infra/requirements.api.lock`；`infra/Dockerfile.api:1-13`；`compose.yaml:5,25,49`；`scripts/start_local.ps1:1-10`、`:48-67`。
- **复现方式或证据**：`requirements.api.lock` 只固定 13 个直接依赖、无 transitive constraints 和 wheel hash，Docker build 时会重新解析传递依赖；基础镜像使用可漂移的 `python:3.12-slim`、`postgres:17` 等 tag 而非 digest。默认 Python/Fast 路径分别硬编码到 `E:\Anaconda\envs\torch128\python.exe` 和 `E:\Llama.cpp\start-qwen3.6-agent.cmd`，虽可覆盖但开箱仅适配当前机器。当前机器的一键脚本通过，不能证明干净机器字节级复现。
- **为什么违反当前设计/P0 要求**：P0-20 要求部署/配置/路径可复现。直接依赖版本相同仍可能因传递依赖或镜像 tag 更新产生不同环境；外部绝对路径没有 artifact bootstrap/manifest。
- **推荐修复方向**：为容器生成完整、带 hash 的 constraints/lock，固定基础镜像 digest和构建平台；用 `.env.example`/参数做路径必填预检，并提供 Fast artifact manifest/安装步骤。把“开发便利默认值”和“发布锁定配置”分离。
- **修复后的验收方法**：在无现有 volume/cache 的第二台机器或干净 VM 用固定 commit/manifest 构建两次，镜像 digest、依赖清单和健康检查一致；缺少路径/secret 时启动器在修改服务前明确失败。
- **是否可能影响其他模块**：会影响镜像更新流程、依赖升级和开发启动说明；不需要把模型权重复制进 Git 或 API 镜像。

### AUDIT-M03：正式 Fast 服务允许任意 CORS 且未配置 API key

- **严重程度**：P2-Medium
- **涉及文件/函数**：外部 `E:\Llama.cpp\start-qwen3.6-agent.cmd` 的 llama-server 参数；`compose.yaml:64-66`；模型网关 API key 配置。
- **复现方式或证据**：按正式参数启动时 llama-server 明确警告 `CORS is set to allow all origins ('*') and no API key is set`，宿主 `/health` 返回 200，API 容器也能访问 `host.docker.internal:8080`。服务只绑定 127.0.0.1，降低了局域网直接访问，但恶意网页仍可能利用开放 CORS 调用本机模型；当前 `OPENAI_API_KEY=dummy` 不构成鉴权。
- **为什么违反当前设计/P0 要求**：这不会直接获得 AMR 工具权限，因此不与 `AUDIT-C04` 同级；但发布演示机上的本地模型可被未授权消耗/读取响应，安全默认值不完整。
- **推荐修复方向**：为 llama-server 配置强随机 API key，关闭或限制 CORS origin；网关从 secret 注入同一 key，禁止仓库默认 dummy 进入发布 profile。继续绑定 loopback。
- **修复后的验收方法**：无 key 和错误 key 的 `/v1/models`/completion 被拒；合法网关门禁及在线 PEVR 通过；非允许 Origin 的浏览器预检/请求不获 CORS 放行。
- **是否可能影响其他模块**：影响启动脚本、Compose secret、模型网关健康检查和演示环境变量；需避免把真实 key 写入报告/Trace。

## 7. 可延期到 P1 阶段的问题

只有在第 2 节最小发布集合修复并复验后，以下问题才可延期：

- `AUDIT-M01`：RunState 依赖闭包强化；发布前至少扫描现有 checkpoint，确认没有坏状态。
- `AUDIT-M02`：字节级容器/依赖复现；如果 P0 发布仅限定当前演示机，可先固定当前镜像 digest和环境清单，再在 P1 完成第二机器复建。
- `AUDIT-M03`：Fast API key/CORS；前提是发布期间严格保持 loopback、关闭不可信网页，并且 `AUDIT-C04` 已完成网络/凭据收口。
- `AUDIT-H06`：2026-09-02 已按产品决定关闭（演示改为 GIF），不再作为发布阻塞项。

## 8. 实际运行过的命令与结果

### 8.1 全量回归与构建

| 命令 | 实际结果 |
|---|---|
| `.\scripts\run_smoke.ps1` | 退出码 0；环境检查通过；Alembic 8 张核心表；Qdrant healthy；pytest **238 passed**、1 个 jieba/pkg_resources warning；CTest **34/34 passed**。 |
| `E:\Anaconda\envs\torch128\python.exe -m pytest tests\integration -q -p no:cacheprovider` | 退出码 0；**18 passed**，1 warning。 |
| `ctest --test-dir build\cpp --output-on-failure` | 退出码 0；**34/34 passed**。 |
| `ctest --test-dir build\cpp -N` | 列出 34 个已注册 CTest。 |
| `E:\Anaconda\envs\torch128\python.exe -m pytest tests\unit\test_p013_pevr.py::test_route_timeout_stops_validator_and_dispatch tests\unit\test_p013_pevr.py::test_simulator_success_envelope_cannot_hide_incomplete_order tests\integration\test_p015_fault_recovery.py -q -p no:cacheprovider` | 退出码 0；**10 passed**。注意前两个测试证明生产图在 timeout/blocked 时抛错；后八个证明独立 Controller 组件有界，不等于二者已接线。 |
| `python -m pytest ...`（同一专项的首次误用） | 在系统默认 `E:\Anaconda` 环境收集失败，`ModuleNotFoundError: openai`；随后使用仓库指定 torch128 Python 得到上面的 10/10。该失败是环境选择错误，不计业务失败。 |
| `E:\Anaconda\envs\torch128\python.exe scripts\export_schemas.py --output-dir tmp\p0_audit_schemas` + 文件比较 | 导出 **51** 份 Schema，与 `docs/schemas` **0 differences**。 |

### 8.2 评测与对照

| 命令 | 实际结果 |
|---|---|
| `.\scripts\run_p018_eval.ps1 -OutputDir .\tmp\p0_audit_p018` | 退出码 0；60/60、七项零容忍 0；`report_id=p018-fd4d8ec462b7c9a7`。结果受 `AUDIT-H01` 影响，不能作为发布通过。 |
| `.\scripts\run_p019_compare.ps1 -SourceReport .\tmp\p0_audit_p018\p018_eval.json -OutputDir .\tmp\p0_audit_p019` | 退出码 0；180 条 projection；三策略各 60/60，Token/资源未观测，`react_production_path_touched=false`。结果受 `AUDIT-H02` 影响。 |
| `E:\Anaconda\envs\torch128\python.exe -m evals.rag.run_eval --output tmp\p0_audit_rag_eval.json` | 退出码 0；20 例真实 Embedding/Qdrant：Recall@K=1、MRR=0.970588、citation=1、answerability=1、ACL leak=0。 |
| `... -m evals.rag.run_eval --minimum-hybrid-score 1 --minimum-vector-score 1 --output tmp\p0_audit_rag_forced_fail_metrics.json` | **退出码仍为 0**；citation=0、answerability=0.15、ACL leak=0，复现 `AUDIT-H03`。 |
| P0-18 oracle mutation inline Python | 把首例 oracle 改为 must_fail/duplicate=999，仍 `evaluation_passed=True`、observed=completed、duplicate=0。 |
| P0-18 prompt-injection inline Python | 样例 text 未进入 Prompt；两个 runner 标记均不存在，结果仍 `denied/passed`，code=`prompt_injection_blocked`。 |

### 8.3 真实 Fast 与端到端

| 命令 | 实际结果 |
|---|---|
| 外部正式 llama-server 参数启动 + `scripts\check_model_gateway.py --profile fast` | alias/health 门禁通过；服务元数据观察到 n_ctx=16384、IQ4_NL；启动日志警告 CORS `*`/no API key。 |
| `E:\Anaconda\envs\torch128\python.exe scripts\run_p013_e2e.py --run-id p0-audit-online-normal-20260821-1659 --approve-dispatch --output tmp\p0_audit_online_normal.json` | 退出码 0；约 93 秒；8/8 阶段、5/5 工具、4 次模型调用、4/4 task、Validator error=0、ORDER 完成、仿真到 t=120。 |
| 对同一 `run_id` 再次执行同一命令（Fast 仍在线） | 退出码 1；`PEVRExecutionError: 已完成账本与外部状态无法一致核对`，复现 `AUDIT-C02`。 |
| 独立解析 `tmp/p0_audit_online_normal.json` | Trace sequence 连续；报告计数/时长/订单/evidence 可重算；C++ 路线 37 个 step 与 Python 仿真 37 个 `amr.path_step` 位置/时间 0 mismatch；pickup t=4、dropoff t=36 一致。 |
| `Get-FileHash` 外部 Fast 脚本/GGUF | 脚本 SHA-256=`B154...F30F`；GGUF SHA-256=`B228C988...F337E6`（完整值见 `AUDIT-H04`）；模型文件 19,779,278,976 bytes。 |
| 正式参数短暂启动 Fast；宿主 health + `docker exec amr-api ... host.docker.internal:8080/health` | 宿主 HTTP 200；容器访问成功，证明容器到宿主 Fast 网络链本身可用；随后 Ctrl+C 清理，8080 listener count=0。 |

### 8.4 异常轨迹、状态与安全

| 命令/轨迹 | 实际结果 |
|---|---|
| 直接调用 `AMRSimulator`：正常 | completed，订单 completed，AMR 最终 IDLE `(6,1)`。 |
| 低电量 25% | 运输完成后 AMR 为 `TO_CHARGE`，无瞬移。 |
| t=2 offline | 仿真 `blocked`，订单 blocked，AMR OFFLINE 停在 `(2,1)`，1 条 observation 要求 replan。 |
| t=1 battery drain 150% | 仿真 `blocked`，battery=0、AMR OFFLINE，1 条 observation 要求 replan。 |
| 路径中加入封路 cell `(4,1)` | Validator 在仿真前拒绝：`forbidden_zone_occupied`、`route_action_invalid`。 |
| A* release_time 最小复现 | `status=infeasible/no_safe_path_to_pickup`，但存在 t=0..5 到 pickup、t=6 到 dropoff 的合法物理路径，见 `AUDIT-H05`。 |
| RunState 依赖状态最小复现 | A pending、B depends A 且 completed，被 Pydantic 接受，见 `AUDIT-M01`。 |
| PostgreSQL 查询正式三 run + audit run | 四个 run 均 completed、Effect=1、Approval=0；另有 6 个 run 共享同一 external effect ID。三个 P0-20 TIME_BUDGET_EXCEEDED run 仍为 planning。 |
| 用 Compose 默认 JWT secret 签发 operator token并 GET audit run | HTTP 200。 |
| 匿名 Qdrant scroll | 返回 70 个点，其中 25 个 operator-only。 |

### 8.5 部署/文档

| 命令 | 实际结果 |
|---|---|
| `docker compose config --quiet` | 退出码 0。 |
| `.\scripts\start_local.ps1 -TimeoutSeconds 30` | 退出码 0；PostgreSQL/Qdrant/API 均 healthy；脚本未启动 Fast。 |
| `docker compose ps` | 三服务 healthy；API/PG/Qdrant 端口均发布到 IPv4/IPv6 全接口。 |
| 仓库视频文件递归搜索 | 2026-08-21 当时 `demo_media_files=0`。2026-09-02 起对外演示改为 GIF，本项不再作为发布核验。 |

本轮生成的临时审计 artifact 位于 `tmp/p0_audit_*`；它们是可删除生成物，不是源码交付物，也没有登记为公共契约。

## 9. 未能运行或有意未运行的验证

- **三个新的、带真实 HITL 的连续 Fast 闭环**：无法运行，因为当前发布 CLI 没有安全审批入口，只能走 `approval_granted` 布尔旁路。继续跑只会新增无审批 Effect 和外部 ID 冲突。已检查数据库中的历史三次结果，并额外跑了一次真实 Fast 正常链路来定位问题。
- **生产图内的离线/封路/timeout/不可行自动恢复**：无法运行，因为 dispatch 正式 ToolSpec 不接受 Eval fault 注入，生产图也未接 `FaultRecoveryController`。本轮分别运行了真实 Simulator/Validator 轨迹和 PEVR stub 失败测试，差距本身即 `AUDIT-C03`。
- **完整 OS 进程强杀矩阵**：本轮没有再次在 dispatch 多个时间窗执行强杀；现有 smoke 中的 P0-14 集成测试通过，但当前数据库外部 ID 冲突已使真实终态恢复失败。应在 `AUDIT-C02/C03` 修复后按每个事务窗口重跑。
- **真实在线 Workflow/ReAct/PEVR 60×3**：仓库没有三个独立执行 adapter，现有命令只能 Trace Replay；不能把未执行写成通过。Smart 按用户要求保持禁用，不在未运行缺陷中扣分。
- **干净 VM/第二台机器的无缓存部署**：未运行。删除现有 Docker volumes/cache 会改变用户环境且不是审查必要动作；当前仅验证已有环境的一键启动、Compose 配置和容器/宿主模型连通性。
- **演示 GIF**：`docs/media/demo_v0.gif` 已嵌入 README；不再核验正式演示视频。

## 10. 发布复验总门槛

修复 Todo 后，只有同时满足以下条件才可把 Verdict 改为 PASS 或 PASS WITH FIXES：

1. 全量 smoke、integration、CTest、Schema diff 全绿。
2. 新 secret/内部网络下的 JWT、Qdrant、PostgreSQL 反例全部 fail closed。
3. 三个全新 run 走真实 Principal→waiting approval→签名 grant→恢复→dispatch，每个 Approval/Effect/外部执行严格一次。
4. 至少一次在副作用关键窗口强杀并跨进程恢复，不重复 dispatch，终态同 run 可安全重放报告。
5. 七类异常通过真实生产恢复入口，动作有界、完成节点不重做、终态不残留 `planning`。
6. P0-18 oracle mutation 能发现故意破坏；60 例原始轨迹来自真实 adapters，零容忍可独立重算。
7. P0-19 三策略为独立执行而非投影；Smart 继续不作为 P0 门槛。
8. RAG 使用独立 holdout，坏阈值命令非零退出。
9. 模型/脚本/参数/hash 进入版本证据，README、报告、简历事实与最终 artifact 完全一致。
10. 演示 GIF（`docs/media/demo_v0.gif`）已嵌入 README；可复现部署制品齐全，Fast 最终停止状态或保留状态在交接中明确记录。
