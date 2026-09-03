# 开发经验沉淀

本文件只记录会影响后续工作包的环境、接口和测试陷阱；一次性的普通编译报错不在这里重复登记。

## 2026-08-20 · MSVC 必须先导入开发环境

- 现象：直接从未初始化的 PowerShell 调用固定路径 `cl.exe`，新增 C++17 目标报标准头 `cstddef` 找不到；原有构建目录因为没有重新编译，容易让人误以为工具链正常。
- 原因：MSVC 的标准库、Windows SDK 和链接器路径由 `VsDevCmd.bat` 设置，单独知道 `cl.exe` 的绝对路径不等于拥有完整编译环境。
- 最终解决：复用 `scripts/run_smoke.ps1` 的方式，在同一个 PowerShell 进程中调用 `cmd.exe /c call VsDevCmd.bat ... && set`，把返回的环境变量写回后再执行 CMake/Ninja/CTest。
- 后续避免：所有 C++ 工作包优先使用 `scripts/run_smoke.ps1` 或先执行同等 MSVC 环境导入；不要把“增量构建未触发编译”当成新代码已验证。

## 2026-08-20 · 不把偶然 Python 环境里的 Boost 当作 C++ 公共依赖

- 现象：本机 Anaconda `Library` 中能找到 Boost.JSON，但项目的固定 CMake 配置没有声明 Boost 根路径；直接依赖它会让换机器或后续 Python 环境变更时 CLI 无法构建。
- 原因：跨语言边界只需要严格 JSON 的一个小子集，使用未锁定的环境路径会把运行时依赖和 Python 环境耦合，也无法满足“避免新增不必要依赖”的边界。
- 最终解决：实现只覆盖本模块契约的严格 JSON 编解码器，拒绝重复键/未知字段/非有限数值，并将不可行矩阵的内部 INF 序列化为标准 JSON 字符串 `"INF"`。
- 后续避免：若未来要替换第三方 JSON 库，必须先补固定版本、CMake 发现方式和离线构建验证；不能直接引用本机 conda `Library` 等个人环境路径（见 [`LOCAL_ENV.md`](LOCAL_ENV.md)）。

## 2026-08-20 · 等待测试必须控制动作代价与替代动作

- 现象：多车预约场景本来想验证低优先级 AMR 等待，但测试结果选择了连续原地转向；路线仍然安全，所以只断言“发生冲突就一定 wait”会把合法路径误判为失败。
- 原因：`(x,y,heading,t)` 状态允许转向占用时间步；默认转向代价低于等待时，A* 会在没有顶点预约的起点单元先改变朝向，再绕开冲突时刻。
- 最终解决：等待专测把转向代价调到与等待同量级，并同时检查路径中真实出现 `wait`、pickup 时间被推迟以及全车队没有顶点/交换边冲突。
- 后续避免：测试等待语义时必须限制可替代动作或显式配置代价；冲突安全测试仍应独立直接检查预约表的 `(cell,t)` 与反向 `(edge,t)` 门禁。

## 2026-08-20 · 终点预约必须保持到规划时域结束

- 现象：如果只登记路径实际到达时刻，后续 AMR 可能在更晚时刻穿过已经停靠的 AMR 终点，单看路线前缀不会发现这个顶点冲突。
- 原因：离散路径输出通常在 dropoff 处结束，但执行中的 AMR 会继续占用该单元；预约表只保存运动边而没有表达终点持续占用，就会把静态停靠误当成空闲。
- 最终解决：`ReservationTable::reserve_path()` 将最终单元从到达时刻保持预约到 `max_time`，后续搜索仍只能等待、绕行或返回不可行。
- 后续避免：P0-10 Validator 和 P0-11 仿真读取路线时必须保留终点占用语义，不能只校验 `path` 数组中显式列出的运动步。

## 2026-08-20 · 计划验证必须独立重算，不能信任上游声明

- 现象：路线规划器结果可能带有 `status=planned` 或审计字段，LLM/调用方也可能附带“已验证”标记；如果验证器直接复用这些字段，非法载荷、时间窗或冲突计划可能被旁路放行。
- 原因：规划、Prompt 和外部调用方都属于不可信输入边界，且 P0-09 负责生成路线，不负责完整的 P0-10 业务验收；P0-04 `TransportOrder` 也没有把运输载荷重量作为通用字段。
- 最终解决：P0-10 通过严格 JSON 白名单拒绝 `llm_valid`、`skip_validation` 等旁路字段，并独立重算每条路线的时间、载荷、电量、地图合法性、工位事件和全车队冲突；计划使用显式 `payload_kg` 输入，所有失败统一返回稳定错误码和定位证据。
- 后续避免：P0-12/P0-13 只能调用固定 `fleet_plan_validator_cli` 或库 API，并检查响应中的 `status`/`valid`；不得把上游 `status`、Prompt 文本或布尔标记作为安全结论。若 P0-11 引入服务持续时间，应先扩展离散事件契约，再同步更新容量规则和反例测试。

## 2026-08-20 · 错误字典必须和实际发出的 code 同步

- 现象：禁行边反例已经返回 `forbidden_edge_traversed`，但初版字典仍保留旧的 `blocked_edge_traversed`；错误虽然能被识别，证据的 `constraint` 却可能降级为 `unknown`，机器消费者也无法从字典解释它。
- 原因：错误码字符串分散在验证分支、文档和字典中，单独新增/重命名一个字符串不会触发编译错误；只检查字典排序和少数代表码也覆盖不到这种漂移。
- 最终解决：统一采用实际发出的 `forbidden_edge_traversed`，把字典作为 `find_definition()` 的唯一约束来源，并把该码加入错误字典 CTest；复测后 14/14 专项和 33/33 全量 CTest 通过。
- 后续避免：新增或重命名违规码必须同时修改发出点、字典、专题文档和反例测试；测试应优先断言错误码存在且 `constraint` 非空，不能只断言“有错误”。

## 2026-08-20 · 路线相邻性不等于朝向合法

- 现象：初版 Validator 只检查 `move` 的前后位置 Manhattan 距离为 1 且朝向数值保持不变，向侧方移动的伪造路径也可能通过几何检查。
- 原因：位置相邻检查没有使用 P0-09 的 heading 语义；攻击者可以保留看似合理的离散时间和代价，绕过运动方向约束。
- 最终解决：复用 0/90/180/270 到前进单元的确定性映射，要求 `move` 的目标单元恰好是当前朝向的前方，并加入反向移动反例；同时对极端 int 坐标做饱和距离和边界保护。
- 后续避免：任何路径安全检查都要同时验证时间、动作、位置、朝向和地图边；新增动作或转向语义时必须补一个“位置相邻但方向错误”的反例。

## 2026-08-20 · P0-11 必须复用已验证路径而不是重算第二条时间轴

- 现象：仿真若根据 pickup/dropoff 工位再次用 Manhattan 距离推进，会丢失 P0-09 的转向、等待、预约和终点保持语义；即使最终坐标正确，中间 Observation 也可能与 Validator 通过的计划不一致。
- 原因：P0-09 的 `path[*]` 是带 `time/action/heading/g_cost` 的执行期快照，P0-10 只验证这条完整路径；Python 侧重算会让合法性和执行轨迹出现两个真相。
- 最终解决：仿真开始前固定调用 P0-10 CLI，随后按 `path[*].time` 直接索引每个 tick；只对 `move` 按 Validator 同名配置扣电，路线结束后保持最后位置，不对 AMR 瞬移或隐式补路。
- 后续避免：P0-12/P0-13 传入的 dispatch 计划必须保留完整路线字段；若要改变路线，先重新走 P0-09，再重新走 P0-10，不能只修改 Python 仿真输入。

## 2026-08-20 · 充电状态不能通过瞬移满足

- 现象：低电量 AMR 不在充电站时，若仿真器直接把位置改为最近充电站，会产生没有 P0-09/P0-10 证据的运动，并掩盖后续需要重规划的真实阻塞。
- 原因：P0-10 当前只验证运输路线，未提供独立的充电路线；仿真器没有权利把未规划的移动当作执行事实。
- 最终解决：只有 AMR 已处于配置充电站坐标才进入 `CHARGING`；否则进入 `TO_CHARGE` 并原地等待，事件中明确记录不可用原因。
- 后续避免：后续若加入去充电站路线，必须把充电路径纳入同一时间戳/Validator 契约，并同步测试站点容量、终点占用和电量安全余量。

## 2026-08-20 · 可复现 Observation 不能使用墙上时间

- 现象：事件 ID 或 Observation 时间若使用 UUID/`datetime.now()`，同一地图、计划和 seed 的输出会因为运行时刻不同而无法逐字节重放。
- 原因：P0-11 的时间语义是离散仿真秒，外部 Trace 需要比较执行结果而不是比较一次调用的机器时钟。
- 最终解决：事件 ID 使用仿真内单调序号，`observed_at` 使用固定 Unix epoch 加 tick 秒，seed 作为结果元数据保留并为未来随机故障扩展预留。
- 后续避免：任何随机故障必须使用本次运行的局部 seeded RNG；任何日志墙上时间只能作为外围记录，不能进入结构化仿真结果。

## 2026-08-20 · 不同 C++ CLI 的 JSON envelope 不能共用多余字段

- 现象：P0-12 初次调用 `task_allocator_cli` 时，Python 把路线工具的 `blocked_cells/map_width` 一并放入分配请求，C++ 严格 codec 返回 `unknown field`。
- 原因：P0-08、P0-09、P0-10 虽共享 AMR/订单/工位语义，但顶层字段是有意分开的安全契约；把“公共字段”理解成“可互传字段”会推迟错误到外部进程。
- 最终解决：注册表为分配、路线和验证分别组装白名单 envelope，并在 handler 启动 C++ 前由 Pydantic/ID 选择完成预检；跨工具只复用领域对象，不复用顶层 JSON 字段集合。
- 后续避免：新增跨语言适配器时先逐字段对照对应专题文档和 C++ `require_exact_keys`，为多余字段和缺字段各写一个执行前/进程边界反例。

## 2026-08-20 · 幂等 digest 必须区分原始参数和规范化参数

- 现象：工具第一次调用把“填充默认值后的 input digest”写入审计，但重复调用比较的是原始顶层参数 digest，导致相同审批请求被错误判为 conflict。
- 原因：默认值填充发生在 Pydantic 校验之后，原始 JSON 和实际 handler 输入是两种表示；只保存其中一种会让重放语义不一致。
- 最终解决：查重前做只读输入规范化，允许原始/规范化 digest 命中同一请求，并同时比较工具和 principal role；不同请求复用 call_id 仍返回确定性 conflict。
- 后续避免：所有新增幂等层都要明确 digest 的规范化时点；审计记录实际输入，重放比较时兼容省略默认值的等价表示，不比较不安全的字符串 repr。

## 2026-08-20 · 正常 dispatch 不能携带故障注入参数

- 现象：P0-11 的 `FaultInjection` 已经可以在仿真器 API 中运行，若直接把 `faults` 加入 `dispatch_simulation`，Agent 就能通过正常白名单制造人为离线/电量故障。
- 原因：Eval 故障注入改变执行轨迹和安全结论，属于受控测试接口，不是业务 Agent 的运行时能力。
- 最终解决：`DispatchSimulationInput` 不声明 `faults`，`TOOL_ARGUMENT_POLICIES` 和 ToolSpec 顶层门禁共同拒绝该字段；专项测试确认 handler 在预检前不启动。
- 后续避免：任何测试/故障字段必须保留在独立 Eval 入口；若未来需要生产演练，先建立单独角色、审批、审计和隔离进程契约，不能复用正常工具名。

## 2026-08-20 · 幂等账本不能替代并发中的 in-flight 协调

- 现象：两个线程同时用相同 call_id、角色和参数调用副作用 handler 时，都在结果写入 ledger 前通过查重，导致 handler 实际执行两次，最终只留下其中一个结果。
- 原因：完成结果缓存只能处理串行重放，检查和执行之间存在竞态窗口；锁住整个 handler 又会阻塞所有不相关工具调用。
- 最终解决：在预检前按 call_id 原子登记包含请求指纹、取消事件和完成事件的 in-flight 记录；相同请求等待首个结果，不同请求立即 conflict，只有首个调用执行 handler 并原子落账/唤醒等待者。无法形成有限 JSON 指纹的请求不进入 ledger，避免用无效重试覆盖已有合法结果。
- 后续避免：任何“exactly once within process”语义都必须写并发测试，断言 handler 计数为 1 且所有调用拿到同一 ToolResult；持久化版本还需在数据库用唯一键/事务实现同等占位，不能只搬运内存 ledger。

## 2026-08-20 · ACL 既要约束候选，也要验证工具返回后置条件

- 现象：P0-07 正常实现会在候选阶段按角色过滤，但替换/回归的 retriever 若返回 operator-only chunk，P0-12 原实现会直接把正文写入 viewer 的 ToolResult。
- 原因：工具层把被注入后端视为可信，没有把 query、role_scope、top_k、document_ids 和逐条 ACL 当作输出安全契约。
- 最终解决：输出 Pydantic 校验后再次复核完整请求范围；任何越界均返回 `retrieval_output_scope_violation`，`output=None`，不让可疑正文进入 Agent 上下文。
- 后续避免：安全过滤应同时有“尽早过滤”和“出口熔断”；对外部服务、缓存和测试替身都要注入一个故意越权的响应，验证敏感载荷未出现在错误结果或审计文本中。

## 2026-08-20 · 受控测试选择不能允许空选集

- 现象：受控 P0-12 `security` case 使用 pytest `-k` 表达式，名称变化后选中 0 条测试并以退出码 5 结束；套件虽被标为失败，却没有执行预期安全门禁，容易形成覆盖错觉。
- 原因：模糊表达式不是稳定白名单标识，测试存在不等于指定 case 实际执行；只检查总套件是否可启动也发现不了选择器漂移。
- 最终解决：每个受控 case 固定列出明确的 pytest node id，并在契约测试中断言 argv 不含 `-k`、未知 case 在启动子进程前拒绝；真实经工具执行验证 4/4 case 通过。
- 后续避免：受控验证必须让“case id → 固定 node id/argv”成为静态映射；runner 结果还应校验非零 case_count，不能把 collected 0 当成通过或有效证据。

## 2026-08-20 · ToolSpec 错误声明必须覆盖执行器公共失败面

- 现象：部分工具的 `error_categories` 只列 handler 业务错误，但统一执行器还会产生 timeout、call_id conflict 和输出 Schema/internal；静态消费者据 ToolSpec 建分支时会漏掉真实结果。
- 原因：错误声明分散在九个定义中，执行器级失败没有被集中并入规格，单测也只检查了少量代表工具。
- 最终解决：注册 ToolSpec 时统一合并 `timeout/conflict/internal`，保留各 handler 专属类别，并逐一断言九个规格声明覆盖真实公共失败面。
- 后续避免：新增执行器错误类别时必须同时更新规格生成器、ToolResult Schema、专题文档和全工具参数化测试；不能只让运行时“能返回”而静态契约不可见。

## 2026-08-20 · P0-13 累计 Prompt 预算不能等同于单次网关上限

- 现象：P0-13 第一次真实运行在 `plan` 节点因理解节点已消耗输入 Token 而触发剩余预算 fallback；把请求输出提高到 2048 后，Planner JSON 仍在网关默认 1024 上限处 EOF 截断。
- 原因：四个独立 Prompt 的输入预算是整次运行累计值，不能沿用单节点示例的 8000/1024；同时 `ModelGatewaySettings.max_output_tokens` 会对节点传入值取更小值。
- 最终解决：P0-13 入口合同固定留出 30000 输入/5000 输出累计预算，节点按合同剩余输出动态收紧；本地默认 TOML 网关上限提高到 4096，单次 Fast context window 仍由 llama.cpp 的 8192 硬限制负责。
- 后续避免：新增多节点闭环时同时检查“累计预算、Provider 全局上限、单次 context window、结构化 JSON 最小长度”四者；不能只修改调用方的 `max_output_tokens`。

## 2026-08-20 · JsonValue 空 Schema 会诱导本地模型输出 type/value 包装

- 现象：真实 Fast Planner 把 `environment_ref`、数组、整数和 `$ref` 写成 `{type,value}`，或把固定事实写成 `task:.../input/...`、`fixed_execution_facts/...` 引用；Pydantic 仍能接收任意 JsonValue，直到确定性 Validator 才发现环境、时间和 seed 不可执行。
- 原因：Pydantic 的 `JsonValue` JSON Schema 是空定义，模型会把上下文中的“类型/引用”当作可执行数据流，Prompt 约束不足以消除本地模型的格式偏差。
- 最终解决：P0-13 增加严格规范化层，只还原明确原语、两个正式业务 `$ref` 和当前合同/请求中字段名完全匹配的固定事实别名；不执行表达式、不读取路径，未命中的引用保持原样并由 Validator 拒绝，且覆盖了对应单测反例。
- 后续避免：若扩展正常 DAG 字段，先更新固定引用白名单和 Validator 反例；不能用“把任意 `$ref` 求值”换取模型成功，也不能让规范化层静默覆盖不同环境、订单或 seed。

## 2026-08-20 · 报告节点不应重复注入完整 RAG 正文

- 现象：真实闭环已通过 C++/仿真/Observation，`finish` 仍因完整 RAG 正文与工具上下文叠加把请求推到 8757/8192 而被 Fast 网关拒绝。
- 原因：报告需要的是 citation/evidence ref 和确定性指标，不需要再次阅读五段完整检索正文；上下文来源分区正确但没有按节点职责做最小化。
- 最终解决：报告 node_input 保留真实 citations/evidence_refs/metrics，报告上下文的 `rag_evidence` 置空；understand/plan/verify 仍按职责保留 RAG 证据，最终报告由真实 RetrievalResponse 和 ToolResult 索引补齐。
- 后续避免：每个命名 Prompt 都要独立做 token 预算估算；报告/审计节点优先传引用和摘要，不复制正文或完整仿真事件轨迹。

## 2026-08-20 · Checkpoint 完成不等于外部副作用完成

- 现象：如果进程在工具调用和 Checkpoint 更新之间退出，旧快照可能仍显示任务未完成，或 Effect Ledger 只有 `reserved`；直接按快照重跑会重复派发真实仿真/工具。
- 原因：数据库事务只能证明本地账本已经提交，不能证明外部系统是否已经产生副作用；`reserved` 与外部完成之间天然存在跨系统窗口。
- 最终解决：副作用先按 `run_id + plan_version + task_id` 三元组写唯一账本（当前字符串键为规范三元组 SHA-256），再调用工具；恢复前通过只读外部状态核对器区分 completed/not_found/in_progress/failed/unknown。未知、进行中和账本/外部不一致均禁止自动重放；外部失败落 `compensation_required`。
- 后续避免：任何恢复流程都必须同时测试“本地 reserved、外部 completed”和“外部 unknown”两个反例；不能把 Checkpoint 的 `completed` 或没有查询结果分别推断成外部事实。

## 2026-08-20 · 局部重规划必须保留完成锚点并切换业务版本

- 现象：全量重建计划会丢失已经完成的任务、副作用和证据；只替换失败节点又可能留下引用旧路线/旧验证结果的下游任务。
- 原因：故障影响沿 DAG 依赖向后传播，完成节点是下游继续执行所需的事实锚点；副作用身份又绑定计划版本，不能用 attempt 或随机 ID 模糊合并新旧计划。
- 最终解决：`LocalReplanner` 先按 AMR、通道 cell/edge、工位、工具和任务精确匹配，再传播未完成后继；已完成节点及 `effect_id` 原样保留，替换任务使用新 ID，计划版本恰好加一并重新验证 DAG。
- 后续避免：LLM 的 retained/invalidated 集合必须与确定性分析逐项比对；P0-15 调用在线重规划时不能直接接受模型给出的“全量新计划”，也不能删除旧 Effect Ledger。

## 2026-08-21 · 模型 alias 在线不等于节点行为验收通过

- 现象：Smart 的 `/v1/models` alias 和单个结构化样例可以通过，但五个 P0-05 节点真实在线只通过 2/5；`understand/plan/replan` 会因推理占满输出预算而留下空 final。
- 原因：启动门禁只能证明服务身份，单个简单 Schema 也不能代表长 Prompt、累计上下文和业务后置条件。把两者写成“Smart 已验收”会隐藏真实失败。
- 最终解决：给 Profile 增加显式 `enabled/disabled_reason`，默认把 Smart 设为 `enabled=false`；Provider 在创建任何 `/v1/models` 或 completion 请求前返回 `MODEL_PROFILE_DISABLED`。保留版本参数供日后复验，但环境变量不能偷偷启用。
- 后续避免：模型启用必须同时满足 alias、完整节点集、业务断言和连续端到端运行；未运行或部分通过必须原样记录。只有收到用户明确指示并完成相同在线门槛后才能恢复 Smart。

## 2026-08-21 · 可读分隔符不是无碰撞幂等编码

- 现象：`run_id:plan_version:task_id` 在 ID 自身允许冒号时会碰撞，例如不同三元组可以拼成同一个字符串。
- 原因：数据库列分别唯一并不自动保证派生的字符串键无歧义；简单拼接缺少长度前缀或结构化编码。
- 最终解决：仍以 `run_id + plan_version + task_id` 为唯一业务身份，但对规范 JSON 数组做 SHA-256，生成 `p014:<digest>`；数据库继续保存并约束原三列，加入分隔符碰撞反例。
- 后续避免：复合身份必须使用规范结构序列化、长度前缀或元组列，不能把“容易读”误当作“唯一”。

## 2026-08-21 · Ledger 与 ToolResult 必须散列同一份规范化输入

- 现象：Effect Ledger 曾对 `{tool, role, args}` 求摘要，而 ToolExecutor 对 Pydantic 填充默认值后的参数求摘要；同一次合法 dispatch 会在完成落账时看起来像输入冲突。
- 原因：两个层各自发明了 digest 语义，字段名称相同却不是同一字节表示。
- 最终解决：副作用预留和 ToolResult 都对 Pydantic 规范化后的工具参数求 SHA-256，并在 `complete_effect` 再次校验输入 digest；结果与账本不一致直接拒绝。
- 后续避免：公共 digest 必须定义“对象、规范化时点、JSON 排序/编码”三件事，并共享实现或共享测试向量，不能只约定字段名。

## 2026-08-21 · 进程内外部状态不能证明跨进程恢复

- 现象：旧测试用两个 Runner 但复用同一 Python 内存 store，能测绿“恢复不重派”；真正杀进程后，仿真事实随堆内存一起消失。
- 原因：测试替身保留了生产崩溃时不存在的共享状态，绕过了 `reserved → 外部完成 → ledger completed` 的危险窗口。
- 最终解决：真实 PEVR 把 `PostgresRuntimeStore` 同时注入运行图和 ToolRegistry；dispatch handler 在返回前先独立提交 `external_execution` 快照及摘要到 Effect 行。新增子进程在该提交后立即 `os._exit(73)` 的集成测试，再用新 Engine/Runner 恢复并断言 handler 总调用次数仍为 1。
- 后续避免：崩溃恢复必须用操作系统级进程终止验证，且终止点要落在最危险的事务间隙；复用对象实例、线程或内存字典只能算单元测试。

## 2026-08-21 · `completed` 状态不是副作用身份凭证

- 现象：外部查询只要返回 `completed` 和一个 ToolResult，旧恢复器就可能复用；错误 effect、另一份输入或被替换的输出也可能被当作本次结果。
- 原因：状态枚举只描述生命周期，不证明“谁、对什么输入、产生了哪份输出”。
- 最终解决：核对外部 effect ID、工具名、业务键、规范化输入 digest 和输出 digest；任何一个不一致都转安全重规划，不能 SKIP。
- 后续避免：跨系统 reconcile 必须把身份与内容完整性作为联合条件，不能只比较状态字符串或对象是否非空。

## 2026-08-21 · 局部重规划必须使用运行时资源 provenance

- 现象：Planner 的 route/dispatch 参数常只有 `$ref`，静态任务文本里没有 Hungarian 实际选中的 AMR 和 A* 实际经过的 cell/edge；仅扫描 PlanTask 会漏掉受影响路线。
- 原因：计划描述的是数据流，真正资源选择发生在确定性工具输出中。只做 DAG 校验还可能接受一条缺 allocation/validation/dispatch 的 route-only 替换。
- 最终解决：从成功的 allocation/route ToolResult 和地图快照构建任务 provenance，按真实 AMR/cell/edge/通道/工位匹配，再只传播到未完成后继；新版本必须重新通过带合同、ToolSpec 和 seed 的完整 PEVR Validator。
- 后续避免：影响分析应读取“实际执行证据”，不是只读“计划意图”；局部计划验收必须复用正常计划的全部安全门槛。

## 2026-08-21 · 未分配 AMR 仍然占据物理空间

- 现象：P0-09 prioritized planner 只给已分配 AMR 建预约，另一台空闲 AMR 停在单格通道时，A* 仍可能规划路线穿过它。
- 原因：未参与订单不等于从仓库消失；其初始 cell 在规划时域内仍是确定性障碍。
- 最终解决：所有未分配 AMR 从 `start_time` 到 `max_time` 保留初始 cell，并增加单格通道不可行 CTest；完整 CTest 从 33 增至 34。
- 后续避免：多机器人规划测试必须包含空闲车、终点保持和未被选择资源，不能只构造所有 AMR 都有任务的正常样例。

## 2026-08-21 · 损坏 Checkpoint 不能“过滤后继续”

- 现象：恢复代码对列表中的非对象项做条件过滤，会把损坏证据静默删掉，再用残缺状态继续执行。
- 原因：宽松反序列化把数据腐败误当作兼容性，可能改变已完成任务、工具结果和 Trace 的对应关系。
- 最终解决：恢复时严格检查允许键、必需对象、每个列表元素类型、tool result/task id 等长、Trace 唯一有序，并把 seed 纳入恢复请求一致性；任一错误统一为 `checkpoint_corrupt`。
- 后续避免：安全恢复应 fail closed。旧版本兼容必须使用显式迁移和版本号，不能靠丢弃未知/畸形内容实现。

## 2026-08-21 · 空数组种子会让地图能力测试变成空命题

- 现象：固定地图的 obstacles、窄通道、禁行边、单向边和临时封锁为空时，代码可以声称支持这些资源，但真实 seed 从未经过相应分支。
- 原因：测试只验证字段存在和正常订单，不验证非空资源的解析、契约和上下游传播。
- 最终解决：增加严格 `WarehouseMap` 公共契约，seed 提供不干扰 ORDER-001 主链的确定性非空资源，并由 snapshot provider 统一解析；布尔/浮点坐标也被 strict integer 拒绝。
- 后续避免：能力验收不能依赖 vacuous truth；每类冻结资源至少要有一个有效 fixture 和一个非法反例，并从公共 Schema 导出 JSON 契约。

## 2026-08-21 · 文档工具运行时不是项目 pytest 运行时

- 现象：直接执行 PATH 中的 `python -m pytest` 落到 Anaconda base，因依赖缺失在收集阶段失败；文档渲染运行时虽然带 `python-docx`，却没有项目 pytest 依赖。
- 原因：同一桌面会话同时存在项目环境和文档工具环境，依赖能力不同；省略解释器绝对路径会产生与代码无关的假失败。
- 最终解决：仓库验证通过 `AMR_PYTHON_EXE`（本机值见 [`LOCAL_ENV.md`](LOCAL_ENV.md)）或 smoke 参数覆盖 Python/CMake/Ninja/MSVC 路径，并把误用环境的收集失败与产品测试结果分开记录。
- 后续避免：运行任何验收前先打印解释器和锁依赖报告；工具专用 runtime 只用于其技能任务，不能据其缺包判断仓库失败。

## 2026-08-21 · 非终态故障不能简单按 fault_id 永久去重

- 现象：最初的恢复控制器对相同 `fault_id` 直接复用第一次 `retry/replan` 决策；同一工具连续失败时，调用方可以无限得到非终态动作。
- 原因：故障事实去重和恢复尝试计数是两件事；P0-14 的业务幂等键能防止副作用重放，但不会替 P0-15 终止循环。
- 最终解决：终态决策继续幂等复用；非终态重复事实沿同一策略继续消耗全局 retry/replan 额度，达到上限后进入 `fallback`/`human`，并在 `FaultRecord` 中只保留一条可更新的审计事实。
- 后续避免：任何“去重”逻辑都必须同时测试重复正常提交、重复非终态失败和重复终态失败三条路径，不能只断言 fault_id 不变。

## 2026-08-21 · 重规划建议与重规划版本不能在同一状态写入

- 现象：在调用 `LocalReplanner.apply()` 之前先记录 `replan_count=1`，`RunState` 会认为计数超过当前版本；直接把计数提前写入又会在真正应用时加倍。
- 原因：故障控制器既要支持“先落准备重规划 Checkpoint”，也要支持“应用新版本后再落账”；这两个时点的预算事实不同。
- 最终解决：`record_on_run_state()` 只记录当前实际 `RunState.replan_count`；`apply_replan()` 由 LocalReplanner 完成版本加一后更新同一 `FaultRecord`，旧 Effect Ledger 不删除。
- 后续避免：涉及版本号的恢复记录要区分 decision、apply 和 checkpoint 三个事务边界，并为“先记录后应用”和“直接应用”各写一个测试。

## 2026-08-21 · 错误载荷的映射字段也必须进入稳定分类

- 现象：分类器只把 Pydantic 错误转换为 `source_mapping`，普通字典中的 `code`、`status` 和 `retryable` 只能通过全文文本间接命中，raw_code/不可行状态等字段会丢失。
- 原因：工具适配器、HTTP 层和测试替身经常返回 Mapping，而不是统一的 Pydantic 类型；依赖 `getattr(dict, ...)` 会静默得到 `unknown`。
- 最终解决：对 Mapping 先做浅层字段映射，再保留原始嵌套错误；同时从严格整数坐标和 `from/to` 位置构造 cell/edge 影响标签。
- 后续避免：分类器测试必须同时使用 BaseModel、ToolResult、ToolError 和普通 JSON Mapping，并断言稳定 code、retryable 和 affected entities，而不只断言类别。

## 2026-08-21 · HITL waiting Checkpoint 必须保存完整执行进度

- 现象：高风险工具第一次暂停时，如果只保存当前任务和审批 ID，恢复后会丢失暂停前
  已完成工具结果、任务状态或资源 provenance，进而可能重复派发或无法完成闭环。
- 原因：interrupt 是执行流暂停，不是从空白状态重新开始；Checkpoint 若没有保存局部
  进度，审批恢复就无法复用 P0-14 Effect Ledger 的已完成事实。
- 最终解决：waiting Checkpoint 同时保存工具结果、task ID、derived plan、observations、
  budget、resource provenance、HITLInterrupt 和审批绑定摘要；恢复先严格读取这些事实，
  再验签/核对计划与 Validator，未完成任务才进入 handler。
- 后续避免：任何人工暂停都必须测试“暂停前已有副作用、批准后只执行剩余任务、恢复一次”
  三个断言，不能只测试 pending 状态存在。

## 2026-08-21 · JWT role 和 approval_granted 都不能作为调用方自声明开关

- 现象：如果 API 接受 body 中的 role/decided_by，或 PEVR 继续把
  `approval_granted=true` 当作安全审批，攻击者可以伪造 operator 或跳过人工决定。
- 原因：角色和审批是外部安全事实，不能由自然语言、检索文本、工具参数或普通布尔字段
  证明；签名身份、审批状态、计划摘要和 Validator 摘要必须绑定在同一安全上下文。
- 最终解决：固定 HS256 JWT 验签后才创建 Principal；安全 PEVR 禁止 Principal 与 legacy
  approval_granted 同时出现；审批 Store 只向 operator 签发绑定请求/计划/Validator 的
  HMAC ApprovalGrant，恢复前再次从 Store 核对。
- 后续避免：新增任何权限或审批入口时，至少加入篡改令牌、viewer 冒充 operator、伪造
  approved 状态、摘要漂移和过期票据反例，并断言 handler 调用次数为 0。

## 2026-08-21 · P0-17 验证结论必须绑定真实退出码

- 现象：如果报告层只接收 status 字符串，调用方可以把 exit_code=1 的测试伪装成 passed。
- 原因：日志解析、工具输出和报告契约各自保存状态，缺少从进程事实到结论的最后一道一致性校验。
- 最终解决：固定 runner 先用 subprocess 返回码/TimeoutExpired 产生 ParsedVerificationCase，再由
  VerificationReportGenerator 重算计数、状态和 report_digest；passed 必须是 exit_code=0，
  timeout 必须是 exit_code=null，失败必须带 failure_type 和 evidence。
- 后续避免：任何新验证 adapter 都只能返回解析后的 case，不能让外部文本或调用方直接传入报告结论。

## 2026-08-21 · P0-17 首条 Trace 可能早于 PostgreSQL runs

- 现象：understand 的模型事件最早发生在 TaskContract 解析完成之前，而 events 表要求
  run_id 外键；直接写 Trace 会在生产首次运行时触发外键失败。
- 原因：运行身份和模型审计的事务边界不同，不能为了写审计提前创建不完整的业务 run。
- 最终解决：PostgresRuntimeStore 对未创建的 run 暂存 Trace，ensure_run 成功后按 Trace
  sequence 补写；后续事件使用确定性 event_id 幂等插入，Trace 仍不允许跳号或混用身份。
- 后续避免：新增运行级审计事件时先检查 runs 外键建立时点，并测试“首事件早于 run 创建”和
  “同一事件重试”两条路径。

## 2026-08-21 · P0-17 固定仿真入口必须无参数

- 现象：把计划、seed 或 Python 表达式作为验证命令参数，会让受控 suite 重新形成任意
  命令/代码执行面，也使报告无法证明实际执行的是哪份固定验证。
- 原因：验证入口把业务参数误当成子进程选择器，白名单无法覆盖组合爆炸和路径穿越。
- 最终解决：p0_simulation 只调用无参数 services.validation.simulation_entry，由入口内部
  构造固定 plan/seed/simulation_id，stdout 输出真实 SimulationResult，失败使用非零退出码。
- 后续避免：新增仿真/测试 adapter 只注册无参数或有限枚举 case，并对 unknown case 断言
  子进程调用次数为 0。

## 2026-08-21 · P0-18 数据集类别不能只靠 case 数量证明

- 现象：固定 JSON 具备 60 条记录且总数正确时，部分正常订单的 `scenario` 标签曾漂移
  到充电分支，运行器按场景路由后把应完成订单错误观察为 `charged`。
- 原因：数据描述、场景分发和预期终态是三个独立字段；只检查总数或 `category` 配额，
  不能发现场景标签改变了执行路径。
- 最终解决：在严格数据契约中同时固定类别/场景/预期终态，并让专项测试执行两次完整
  Harness；修正固定记录后，25/10/10/5/10 配额、60/60 预期和报告 digest 均稳定。
- 后续避免：新增评测例必须同时审查 `category`、`scenario`、`expected_outcome`、
  `expected_code` 和 oracle，不能只追加一条“看起来数量正确”的 JSON。

## 2026-08-21 · P0-18 正确拒绝不是应该被删除的失败

- 现象：安全评测若只统计 completed/answered，`denied`/`blocked` 会被误报为失败或从
  报告过滤，无法证明注入、越权和审批绕过确实被阻断。
- 原因：负向场景的观察终态本来就是拒绝/阻塞；“评测符合预期”和“观察到负向终态”是
  两个不同维度，零容忍计数也必须来自实际违规事实而不是期望值。
- 最终解决：`EvalReportCase` 同时保存 expected/observed/status/evaluation_passed、
  failure_code/reason、Trace 和证据；报告另列 `observed_negative_cases`，正确阻断仍算
  评测通过，意外失败才进入顶层 failures。
- 后续避免：任何汇总器都必须保留全部逐例结果和负向轨迹，并为“正确阻断”和“意外失败”
  分别写断言。

## 2026-08-21 · P0-18 离线 oracle 不能冒充在线模型验收

- 现象：统一 Harness 使用固定 fixture 后可以稳定通过，但如果只在报告中写
  `qwen3.6-fast`，读者容易误以为 60 例已经由在线 LLM 生成并完成模型质量验收。
- 原因：复现所需的模型 alias/量化记录与实际是否发生模型调用是两件事；P0-18 需要可
  离线运行，同时又不能掩盖模型服务未启动的事实。
- 最终解决：固定 `execution_mode=offline_deterministic_oracle`，记录模型/Prompt/工具
  版本和 `online_service_required=false`，报告明确 `model_call_count=0`；Smart 继续硬禁用，
  在线模型验收仍沿用独立 P0-05/P0-13 入口。
- 后续避免：引入在线模式时新增独立 execution mode、独立报告字段和独立验收命令，不能
  覆盖或改写离线报告的运行事实。

## 2026-08-21 · Fast 在线闭环必须把输入上下文也纳入验收

- 现象：Fast `qwen3.6-fast` 的 `/v1/models` 门禁、20 例结构化输出和 P0-05 五个 Prompt
  节点均通过，但真实 P0-13 在 `plan_tasks` 的修复请求返回 HTTP 400。
- 原因：模型实际收到的请求为 9086 tokens，而服务固定 `--ctx-size 8192`；单个节点能
  通过不代表 PEVR 拼接后的 system Prompt、Schema、合同、地图/RAG 摘要仍能放入窗口。
- 最终解决/当前处置：用户将外部 Fast 服务上下文改为 `--ctx-size 16384` 后重新运行，
  P0-13 退出码 0，8/8 阶段、5/5 工具和仿真均通过；仓库 P0-18 离线 oracle 仍保留
  `context_window=8192` 的独立固定配置，在线 16K 结果单独记录，不能覆盖离线指纹。
- 后续避免：任何在线 E2E 验收都必须同时记录 prompt tokens、模型上下文上限和失败节点；
  如果调整窗口，必须在在线报告中显式记录实际服务命令行，并重新运行完整 P0-13，不能只
  重跑单个 Prompt 节点。

## 2026-08-21 · 在线重测必须区分恢复运行和全新运行

- 现象：P0-13 CLI 默认根据请求摘要生成稳定 `run_id`；上下文从 8K 调到 16K 后复用同一
  `run_id`，运行可能从旧的失败 Checkpoint 继续，表面上阶段全通过但不一定重新执行全部节点。
- 原因：P0-14/P0-17 的恢复设计会优先读取已有 Checkpoint，并跳过已完成节点；这是正确的
  生产恢复语义，却不等同于从头验收。
- 最终解决：先保留恢复复测结果，再显式使用新的
  `p013-e2e-fast-16k-fresh-20260821` 运行 ID；fresh run 确认 8/8 节点、4 次模型调用、
  5/5 工具和仿真完整通过。
- 后续避免：在线配置或模型变化后的验收必须同时区分“恢复兼容性测试”和“全新 E2E 测试”；
  后者必须使用新的 run_id，并把 run_id、Checkpoint 是否存在和完整 Trace 数量写入报告。

## 2026-08-21 · P0-19 同源策略回放不能冒充在线模型对照

- 现象：P0-18 报告虽然记录了 `qwen3.6-fast`、Prompt 和 ToolSpec 指纹，但执行模式是
  `offline_deterministic_oracle`；如果 P0-19 只按模型别名写结果，读者会误以为 60 例
  已由在线 Fast 分别跑过 Workflow、ReAct 和 PEVR，并且 Token/资源为零。
- 原因：模型身份、源 Trace 和真实在线采样是三个不同事实。P0-18 源 Trace 的延迟是
  固定可复现字段，既没有 model usage，也没有 CPU/RSS/GPU 采样。
- 最终解决：P0-19 先验证 P0-18 report digest/60 例/失败列表/零容忍项，再保存源 case 和
  Trace；固定 Workflow/PEVR 保留源事件，ReAct 只生成带 source_sequence 的 think-act-observe
  投影。Token/资源用 `observed=false` 表示，延迟标注 `wall_clock=false`，报告明确
  `offline_trace_replay` 和后续在线 adapter 的边界。
- 后续避免：任何在线策略对照都必须新增独立 execution mode、真实模型调用、统一采样器、
  新报告 digest 和新验收门槛，不能覆盖本次离线报告；负向 denied/blocked 轨迹也不能删除。

## 2026-08-21 · P0-19 源报告要同时保留报告 digest 和原始文件 hash

- 现象：P0-18 报告的 digest 会排除 `generated_at`，但真实固定验证 runner 产生的嵌套
  `VerificationReport` digest 仍可能随实际验证输出变化；源 JSON 文件的原始 SHA-256
  也会随重新落盘变化。把其中任一个当成唯一身份都会丢失复核信息。
- 原因：原始文件 hash 用于证明“读的是哪一个 artifact”，P0-18 report digest 用于绑定
  “这次源报告正文”，两者生命周期和稳定性不同；确定性 Trace 不等于所有外部验证日志
  的摘要都稳定。
- 最终解决：P0-19 报告同时记录源文件 SHA-256、源 P0-18 report_id/report_digest，并在
  单测中对同一路径验证稳定身份；重新生成源报告后必须把新 hash/digest 当作新的源
  artifact 记录，不静默覆盖旧对照结论。
- 后续避免：在线/离线评测的报告身份至少拆分原始 artifact hash、报告 digest 和去除
  wall-clock 的业务 Trace digest；不要把一次外部验证运行的可变日志摘要误当成永久基线。

## 2026-08-21 · Compose API 镜像不能直接复用包含本地 Embedding 的全量锁

- 现象：直接在 `python:3.12-slim` 中安装根目录 `requirements.lock` 会解析出
  `sentence-transformers`、PyTorch 和 CUDA 运行时；改成最小依赖后，纯 `psycopg` 又因
  缺少 libpq/`psycopg_c` 使容器迁移失败。
- 原因：Windows 宿主机的全量 P0 环境与 Compose API 的 HTTP/数据库边界不是同一个运行时；
  slim 镜像也不自带 PostgreSQL 客户端库。
- 最终解决：新增 `infra/requirements.api.lock`，保留 API 实际导入所需的固定版本并使用
  `psycopg[binary]`；Embedding、Fast 模型和权重继续留在宿主机，容器构建和真实迁移/健康检查均通过。
- 后续避免：部署镜像应按进程职责拆分锁文件；每个新镜像至少实测“构建→迁移→健康→导入”，
  不能只依赖开发环境已安装的包。

## 2026-08-21 · Compose 健康检查要以目标镜像实际工具为准

- 现象：Qdrant 镜像不保证提供 `curl` 或 `wget`，用常见 HTTP 命令写健康检查会在服务正常时误报 unhealthy。
- 原因：健康检查脚本运行在容器内部，不是宿主 PowerShell；镜像内置工具集合和宿主不同。
- 最终解决：对固定 Qdrant 镜像用 Bash `/dev/tcp` 请求 `/readyz` 并匹配 HTTP 200；宿主启动器另行调用真实 URL 和 `check_qdrant.py`。
- 后续避免：新增 Compose healthcheck 前先在目标镜像中手工执行同一命令，并同时保留客户端层检查。

## 2026-08-21 · 本地 Fast 长 Prompt 冷启动必须与成功 E2E 证据分开

- 现象：本次用全新 `p020-*` run_id 重测时，Fast alias/20 例短结构化/5 个 P0-05 节点均通过，
  但较长 PEVR `plan_tasks` 请求受宿主机 GPU/CPU/MoE 吞吐影响，在固定 300 秒累计预算内超时。
- 原因：健康接口只证明服务进程可访问；短 Prompt 不代表 6K～9K token 上下文的生成耗时，且复用旧
  `run_id` 还可能把恢复结果误当从头运行。
- 最终解决：不放宽 PEVR 预算、不把失败重测算通过；正式连续成功证据继续绑定三个独立的已完成
  run_id，并在 P0-20 报告单列 fresh 尝试的失败原因。Smart 仍未启动。
- 后续避免：在线验收必须记录实际服务器命令行、上下文窗口、输入/输出 token 和冷启动/热启动
  状态；三次成功必须使用新 run_id，并把失败尝试与通过样本分开统计。

## 2026-08-21 · PowerShell 冒号插值会让一键启动器在执行前失败

- 现象：`Write-Host "[ok] $Name: $Uri"` 在启动脚本解析阶段报变量引用错误，Compose 实际还未执行。
- 原因：PowerShell 将紧邻冒号的 `$Name:` 解析成变量名的一部分。
- 最终解决：改用 `"[ok] ${Name}: $Uri"`，随后一键启动和 `-StartFast` 两条路径均通过真实健康检查。
- 后续避免：启动脚本中变量后紧接冒号、点号或路径分隔符时显式使用 `${variable}`，并在交付前直接执行脚本而不是只做文本审查。

## 2026-08-21 · 发布 Eval 必须用独立 oracle 判真实观察，不能由 scenario 分支自证

- 现象：P0-18 报告稳定 60/60，但把首例 `oracle` 改成“必须失败、重复副作用 999”后仍通过；
  prompt-injection 样例的攻击文本没有进入 Prompt，runner 仍能通过捕获自己抛出的异常记为阻断。
- 原因：`run_case` 只比较 runner 根据 scenario 自己产生的 outcome/code，不消费 `case.oracle`；正常、
  RAG、恢复和安全分支又有自生成路径，期望与观察没有独立来源。
- 当前处置：发布审查将 P0-18 标为 FAIL，并在 `docs/P0_AUDIT_TODO.md` 登记 `AUDIT-H01`；本轮按
  用户要求不修改 runner，旧 60/60 只可称为 fixture 回归，不能作为发布验收。
- 后续避免：为每个 oracle 字段做消费覆盖和 mutation test；发布 Harness 必须从真实
  PEVR/ToolRegistry/C++/Simulator/HITL/Checkpoint 取得观察，再由独立 predicate 判定。故意破坏
  oracle 或生产 adapter 时报告必须变红。

## 2026-08-21 · 外部执行 ID 不能只由业务 payload 摘要充当全局身份

- 现象：相同计划/seed 的 6 个不同 run 都生成
  `simulation-b7551b825b817593d1e700fe`；按该 ID 查询恢复快照得到多条 Effect 并抛
  `PersistenceConflictError`，已完成 run 无法重放核对。
- 原因：payload digest 适合证明“输入是否相同”，不等于“哪一次外部执行”；外部状态查询却把它
  当成全局唯一执行 ID，缺少 run/effect namespace 和数据库唯一性约束。
- 当前处置：发布审查将其列为 `AUDIT-C02` Critical；未迁移数据、未重放副作用，也没有用删除冲突
  行掩盖问题。
- 后续避免：执行身份至少绑定 `run_id + plan_version + task/effect id`，payload digest 作为另一个
  不可变校验字段；恢复测试必须包含相同 payload 的跨 run、并发恢复和 handler/ledger 事务窗口强杀。

## 2026-08-21 · 安全兼容开关必须在发布装配层 fail closed

- 现象：P0-16 的 JWT/HITL 单测全部通过，但真实演示 CLI 以 `principal=null`、
  `approval_grant=null` 和 legacy `approval_granted=true` 完成 dispatch；数据库正式 run 有 Effect
  而无 Approval。
- 原因：底层支持安全模式不等于发布入口启用了安全模式；Runner 默认 `security_required=false`，
  测试兼容布尔值仍被正式脚本调用。
- 当前处置：发布审查列为 `AUDIT-C01` Critical，相关历史在线 run 只证明正常功能链，不再证明
  HITL 安全链；本轮未删除兼容字段或伪造 Approval 记录。
- 后续避免：安全属性必须从 release composition root 强制注入并用真实入口反例验收；test-only
  开关应在类型、模块或构建 profile 上与发布入口隔离。验收至少核对 Principal、Approval、Checkpoint、
  Effect 四类数据库事实，而不是只看工具返回 success。

## 2026-08-21 · 组件恢复测试通过不代表生产状态图已经接线

- 现象：`FaultRecoveryController`/LocalReplanner 集成测试通过，但生产 PEVR timeout、infeasible 和
  blocked 仍直接抛错；三个模型超时 run 的最后 Trace 已 failed，数据库状态却长期为 `planning`。
- 原因：分类器/控制器与固定八阶段图分别实现，非测试生产代码没有调用 Controller，也没有
  failure→retry/replan/human/fatal 条件边或受信任外层循环。
- 当前处置：发布审查列为 `AUDIT-C03` Critical；组件测试结果继续保留，但文档不得再把它描述成
  已完成的生产异常闭环。
- 后续避免：工作包验收矩阵必须包含“定义→调用点→状态转移→持久化终态→真实故障轨迹”五层证据；
  `rg` 调用图和生产入口失败注入应成为恢复能力的固定发布检查。

## 2026-08-21 · 应用层 ACL 不能保护可匿名直连的底层数据服务

- 现象：应用 RAG ACL 评测为 0 leak，但匿名 Qdrant scroll 可读取全部 70 个 chunk，包括 25 个
  operator-only；Compose 还公开 PostgreSQL/Qdrant 端口和已知默认 JWT/数据库 secret。
- 原因：只测试经过 FastAPI/Retriever 的授权路径，没有把容器端口、服务认证和默认 secret 纳入
  同一威胁模型；调用方可绕过应用层直接访问存储。
- 当前处置：发布审查列为 `AUDIT-C04` Critical；当前 Compose 保持运行供用户环境使用，但明确记录
  实际端口绑定和风险，没有把 localhost 文档描述当作网络隔离证据。
- 后续避免：数据服务默认只加入内部 network，缺强 secret 时 fail startup；部署反例必须从应用外
  直接探测数据库/Qdrant，并验证匿名、旧 secret、viewer 和 operator 四种路径。

## 2026-08-21 · P0-18 oracle 必须独立于 runner 自生成证据

- 现象：把 `oracle.must_fail` 或 `duplicate_side_effect_count=999` 写入样例后，旧 runner 仍全绿；注入文本未进入 Prompt 也被记为 blocked。
- 原因：`run_case` 只比较 scenario 分支自己的 outcome；安全分支捕获自己抛出的 PermissionError。
- 最终解决：新增 `evaluate_oracle()`，未知键 fail closed；注入路径调用 `PromptDefinition.build_messages()`，缺失安全边界记 FAILED 而不是 DENIED+passed。
- 后续避免：每个 oracle 字段都要有消费覆盖和 mutation 测试；禁止用 catch 自己的异常制造安全成功。

## 2026-08-21 · P0-19 不能把同一 Trace 投影写成三策略对照

- 现象：180 条结果复制同一 `observed_outcome`，三策略都是 60/60。
- 原因：replay 只改控制步标签，不改变恢复额度或工具调用。
- 最终解决：默认 `offline_independent_oracle`，Workflow/ReAct/PEVR 各自跑 P0-18 Harness；replay 保留为 `--mode replay`。
- 后续避免：策略对照报告必须证明至少一例终态可分离；缺失 Token/墙钟时保持 `observed=false`。

## 2026-08-21 · 生产 VALIDATE 不能把首轮 version=1 规则套到重规划计划

- 现象：七类故障生产图测试在第一次 replan 后 `plan_version=2` 即 FAILED；成功 replan 用例抛 `plan_version_invalid` / `task_has_runtime_evidence`，外层再被分类成未知故障 fail closed。
- 原因：`_apply_production_replan` 已丢掉 VALIDATE/EXECUTE 轨迹并提交 v2 计划，但 `_validate_node` 仍调用 `validate_normal_pevr_plan(expected_plan_version=1)`，把合法的 completed 锚点和 pending 替换子图当成首轮非法计划。
- 最终解决：`plan_version>1` 或存在 `completed_task_ids` 时改走 `validate_replanned_pevr_plan`；并将 `plan_validation_failed` 归入 `PLAN_INFEASIBLE` 作为安全网。
- 后续避免：重跑 VALIDATE 必须与 LocalReplanner 使用同一套 completed/pending/version 不变量；不要用首轮门禁“顺便”复验 v2。

## 2026-08-21 · `AppSettings()` 不会读取轮换后的 `.env`

- 现象：C04 轮换 PostgreSQL 密码后，集成测试对 `amr` 认证失败，而 `check_postgres.py` 在启动脚本加载 `.env` 时可以连通。
- 原因：测试夹具写 `create_database_runtime(AppSettings().database)`，只拿到代码/TOML 默认 `123456`；`load_settings()` 原先也不读 `.env`。
- 最终解决：`load_settings()` 在调用方未传入 `environ` 时解析项目 `.env` 白名单键；集成测试和 worker 改为 `load_settings()`。
- 后续避免：凡是要连真实 Compose 数据面的代码，用 `load_settings()` 而不是直接构造 `AppSettings()`。

## 2026-08-21 · release_time CTest 夹具必须遵守代价与时间窗契约

- 现象：预定位主例通过后，提前到达要求 `wait` 失败（A* 用更便宜的转向凑时间）；idle AMR 占用走廊时 `max_time=7` 不够绕行；`deadline==release_time` 在 normalize 阶段抛 `invalid_order` 而不是返回 infeasible。
- 原因：默认 `turn_cost=0.25 < wait_cost=1`；未分配 AMR 会预约所在格至时域结束；订单契约要求 `deadline > release_time`。
- 最终解决：等待例压低 `wait_cost`；冲突例放宽 `max_time/deadline`；反例改为 `deadline=6, max_time=5`。
- 后续避免：验证 wait 语义时显式设置代价；构造“时间窗无解”时不要触碰非法订单字段。

## 2026-08-21 · 当前 `put()` 已写 lookup_id，不能用来模拟历史 collision

- 现象：遗留外部 ID 迁移测试 `migrated >= 2` 得到 0，尽管 `get(old_id)` 仍能因 JSONB `run_id` 冲突而报错。
- 原因：新 `put()` 在写入快照时已经带上 `lookup_id` 与 `identity_version=p014.v2`，迁移循环会 skip。
- 最终解决：测试直接向 Effect JSONB 写入无 lookup_id 的历史快照后再跑迁移。
- 后续避免：回归“旧数据迁移”必须构造旧载荷，而不是调用已经修复的写入路径。

## 2026-08-21 · Windows PowerShell 5.1 不能直接跑无 BOM 的 UTF-8 中文脚本

- 现象：`start_local.ps1 -StartFast` 用 `powershell.exe` 隐藏启动 `start_fast_secure.ps1` 后，8080 永远不起来；前台用 5.1 解析会报中文字符串把引号吃掉（`MissingCatchOrFinally`、乱码 `�`）。
- 原因：Windows PowerShell 5.1 默认按系统代码页读脚本；UTF-8 中文 `throw "..."` 会变成乱码并拆掉字符串。
- 解决：给启动器写 UTF-8 BOM，并优先用 `pwsh.exe`。**2026-08-22 复发过一次**：工作区的 `start_local.ps1` 被编辑器重存丢了 BOM（HEAD 本来有），用户双击/5.1 调用再次解析失败；已把 `scripts/` 下全部 6 个含中文的 `.ps1`（`start_local`、`start_fast_secure`、`run_smoke`、`run_p018_eval`、`run_p019_compare`、`bootstrap_local_secrets`）统一补上 BOM，并用 PS 5.1 的 `Parser::ParseFile` 逐个验证通过。
- 避免：仓库里带中文的 `.ps1` 必须带 BOM（编辑器保存时选「UTF-8 with BOM」），或避免在双引号字符串里放非 ASCII；改完用 5.1 解析器跑一次再交付。

## 2026-08-21 · HITL HTTP 测试的冻结时钟不能拿去对照墙钟 approve

- 现象：`test_api_hitl_routes_are_run_scoped_and_operator_only` 在 12:15Z 之后变成 409。
- 原因：请求用 `2026-08-21 12:00Z` 加上 900s TTL，`store.approve()` 用 `datetime.now(timezone.utc)`；过期也映射成 `HITL_NOT_PENDING`。
- 解决：该用例改为墙钟构造 `requested_at`。
- 避免：凡是走真实 `now()` 的存储路径，夹具时间必须未过期，或把 `now=` 注入到底层。

## 2026-08-21 · Fast 19GB 哈希加 GPU 加载不能用 300s 父进程超时去“等健康”

- 现象：即使子进程其实能起来，父脚本 300s 超时抛错，工具进程树可能把还在加载的 llama-server 一起杀掉。
- 原因：`Get-FileHash` 大文件 + 35B IQ4_NL 上 GPU 经常超过 5 分钟。
- 解决：健康等待改为 600s，并与编码修复一起保证子进程真的在跑。
- 避免：不要把“没在超时内 /health”直接写成模型坏了，先看 18080/8080 监听和启动器日志。

## 2026-08-21 · 演示启动不能默认扫描 19GB GGUF 哈希

- 现象：`start_local.ps1 -StartFast` 在打印任何 `[ok]` 前静默 SHA-256，看起来像没反应。
- 原因：`verify_fast_artifact.py` 和 `Get-FileHash` 会整文件扫描 IQ4_NL 权重；`check_model_gateway.py` 随后再扫一遍。
- 解决：manifest `verify_sha256=false`；启动只检查存在和大小。需要发布级哈希时把该字段改回 true 再跑 `verify_fast_artifact.py`。
- 避免：不要把报告里的 model_sha256 在关闭校验后写成“本次启动已重算”。

## 2026-08-21 · Hidden 启动器必须先具备可观察的失败通道（2026-08-28 更新）

- 现象：终端只打印“等待 /health”就回到提示符；任务管理器没有 llama-server。
- 原因：问题不在 Hidden 本身，而在隐藏前没有禁用 PowerShell 进度输出、没有 transcript/标准流重定向、父进程不检查 launcher 退出，以及 `Start-Process -ArgumentList` 给 `--model` 再套一层引号导致 llama-server 起不来。
- 最终解决：`start_local.ps1` 的父包装器可以用 `WindowStyle Hidden`，但父子脚本都设置 `ProgressPreference=SilentlyContinue`；安全启动器写 `tmp/fast_secure.transcript.log`，llama-server 的 stdout/stderr 写独立日志，父包装器发现 launcher 退出立即抛日志尾。模型路径保持单独 argv。2026-08-28 已实际验证隐藏父包装器能成功启动 8080/18080；内层 llama-server 仍不叠加 `WindowStyle Hidden`。
- 后续避免：Hidden 只能是 UI 选择，不能删除诊断通道。没有 PID/退出码/transcript/标准流/健康超时中的任一项时，不要把空等当成“正在加载”，也不要留下无法精确停止的隐藏子进程。

## 2026-08-21 · Planner 的 release_time 预定位会提前踩到 pickup 格，与 Validator 首次到达语义冲突

- 现象：演示链路用真实 C++ 跑 ORDER-002（release_time=10）时，Validator 同时报 `pickup_before_release`（路径首次到达 P2 是 t=4）和 `pickup_time_mismatch`（路线 pickup_time=10 ≠ 首次到达 4），计划被判 invalid。
- 原因：H05 允许 A* 在 release_time 前移动并“预定位”；当 AMR 离 pickup 很近时，最小代价策略是提前到达后在 pickup 格上原地转身消耗时间（5 次 turn 的代价低于 6 次 wait）。但 P0-10 Validator 把「路径第一次到达 pickup 坐标」认定为 pickup 时刻，两个 C++ 工具对同一合法行为理解不一致。CTest 只分别覆盖 planner 输出和手工构造的 validator 路线，没有 planner→validator 的 release_time>0 端到端用例，所以只跑 ORDER-001（release_time=0）的 P0-13 从未暴露。
- 最终解决：本步不修 C++（跨工具语义裁决，超出演示范围）；演示 API 把该拒绝如实映射为 422 `fleet_plan_invalid` 并回传 C++ 证据，前端只展示错误不画轨迹。
- 后续避免：release_time>0 的订单（ORDER-002/003）走真实 C++ 链路都要预期这个结果；P0-18 数据集的 normal-002/003 期望 completed，评测前必须先裁决语义：要么 Validator 把「release_time 前经过 pickup 格」视为预定位而非 pickup 事件，要么 Planner 禁止在 pickup 格上提前等待（改在邻格等待）。裁决前不要把 ORDER-002 当成功案例演示。

## 2026-08-21 · FastAPI dependency_overrides 不能直接给「类」

- 现象：演示测试写 `app.dependency_overrides[get_demo_service] = _OverloadPlanService`（传类而非工厂）后，路由注册阶段报 `FastAPIError: Invalid args for response field`。
- 原因：FastAPI 会把覆盖 callable 的签名当请求参数建模；类的 `__init__(snapshot_provider=...)` 被当成 query 字段，而 `DefaultWarehouseSnapshotProvider | None` 不是合法 Pydantic 字段类型。
- 最终解决：覆盖一律写成 `lambda: _OverloadPlanService()`。
- 后续避免：给 FastAPI 传任何可调用依赖/覆盖时，先想它的签名会不会被当成请求参数；构造函数带注入参数的类必须用工厂包一层。

## 2026-08-21 · compose 的 amr-api 容器一直占着 8000，宿主机调试要用别的端口

- 现象：本机 `uvicorn apps.api.main:app --port 8000` 启动即报 WinError 10048；`/health` 仍能通，但返回的是容器旧镜像的路由表，新加的 `/demo/*` 全部 404。
- 原因：`compose.dev.yaml` 把 `amr-api` 发布到 `127.0.0.1:8000`，容器镜像是构建时点快照，不含新代码；`Get-NetTCPConnection` 对 Docker 代理端口偶尔查不到，容易误判端口空闲。
- 最终解决：宿主机演示/调试改用 `--port 8010` 与容器并存。演示仿真依赖 Windows 版 C++ exe，本就只能跑在宿主机 uvicorn 上（容器是 Linux，没有 `build/cpp` 产物，容器内 `/demo/simulate` 会如实返回 503 `cpp_executable_unavailable`）。
- 后续避免：验证新 HTTP 路由前先用 `/openapi.json` 确认路由表来自新进程；需要容器提供 `/demo` 页面时先 `docker compose build api`，并接受容器内仿真不可用这一边界。

## 2026-08-22 · 签发 JWT 不能用 `AppSettings()`，它不读 `.env`

- 现象：用 `AppSettings()` 取 `jwt_secret` 签发的演示令牌，被正在运行的 API 一律拒绝（401 `AUTH_REQUIRED`「JWT 无效或已过期」），且令牌本身格式、有效期完全正常。
- 原因：`AppSettings` 是普通 `BaseModel`（`StrictSettingsModel`），直接构造只拿 Python 默认值；`.env` 里的 `AMR_JWT_SECRET` 只有走 `load_settings()`（默认值 → `config/default.toml` → `.env` → 环境变量）才会生效。API 进程启动时用的是 `load_settings()`，两边密钥不同即验签失败。
- 最终解决：签发脚本/测试需要与线上一致的密钥时，必须 `load_settings()`；改后令牌对 8010 实测 200。
- 后续避免：凡是要和「正在运行的服务」共享密钥/配置的场景（签 JWT、算 HITL 签名、连 Qdrant），一律走 `load_settings()`；`AppSettings()` 只适合不依赖本机 `.env` 的纯默认值测试。

## 2026-08-22 · Fast 会把 `$ref` 引用语法当字面值照抄，固定字段不该交给 LLM（已根治）

- 现象：自然语言闭环演示中，同一请求文本 6 次运行 4 次失败：LLM 的 plan 把数据流引用语法抄成字面值（`{"$ref": "fixed:order_ids"}`、`{"$ref": "task:TASK-ROUTE-002/input/max_time"}` 等伪引用），确定性校验按 `environment_ref_mismatch`/`simulation_seed_invalid`/`blocked_cells_mismatch` 等多项拒绝；图内一次带反馈重规划后仍不收敛。同日同时刻同模型的对照运行一次通过，说明是 temperature=0.1 下的采样方差，不是接线错误。
- 原因：Prompt 里文档化了 `task:.../output/...` 与 `derived:...` 引用语法，35B 本地模型会过度泛化出 `fixed:*` 等不存在的命名空间；而 order_ids、environment_ref、seed、latest_deadline、blocked_cells、ruleset_version 这些字段本来就是请求/快照里的确定真值，LLM 填错没有任何收益、只有失败风险。
- 最终解决（同日晚实施）：`canonicalize_normal_pevr_plan` 不再只解析白名单引用别名，而是把上述固定事实字段**一律覆盖**为合同/请求真值并记录 `fixed_fact_override` note；`max_time` 是唯一例外——Validator 接受 ≥ 最晚 deadline，故只在缺失/非整数/不足时拉回真值，保留合法的更大 horizon。覆盖只能让计划更贴近合同，Validator 门禁对最终计划逐项生效，`assignments`/`plan` 数据流引用不在豁免范围。修复后 6 次真实 Fast 运行 6/6 到达 `waiting_approval`（修复前 2/6）。
- 后续避免：给 LLM 的结构化输出里，凡是有确定真值的字段都不要让它填；修复类测试夹具要破坏 canonicalize **不**豁免的字段（如 assignments 引用），否则重问/拒绝路径测不到；评估 LLM 链路可靠性时样本量要足够大（≥10 次）并记录成功率。

## 2026-08-22 · PEVR 正常闭环按设计拒绝种子外订单；任意下单要另建轻量链，别在闭环里开口子

- 现象：演示页提交「把 MAT-001 从 P3 运到 S3」失败，`understand` 阶段抛 `missing_information`——MAT-001 在种子里属于 ORDER-001（P1→S3），P3 是 ORDER-003 的取货点，请求与三份种子订单都对不上。
- 原因：这是 fail-closed 设计而非 bug：`_validate_contract_against_snapshot` 要求合同订单与固定快照逐字节一致（`order_snapshot_mismatch`），prompt 又明确禁止 LLM 编造订单，于是 LLM 只能填 `missing_information`，校验门禁如实拒绝。失败的 understand 运行甚至不落库（`ensure_run` 在校验通过后才调用），排查时 PostgreSQL 里查不到记录是正常的。
- 最终解决：用户要的效果是「任意下单」，但没有要求为此放开 PEVR 闭环的审批/Ledger 语义。正确做法不是放宽 `order_snapshot_mismatch`（那会动摇发布证据链），而是新增轻量演示链 `POST /demo/order`：LLM 只抽 material_id/pickup/dropoff/deadline 四要素，订单 ID、地点白名单、deadline 下限全部由服务端对照快照重建/校验，再走与 `/demo/simulate` 完全相同的 C++ 链。闭环保持 fail-closed，演示获得任意性，两者互不混用。
- 后续避免：遇到「演示想要 X，但生产闭环按设计拒绝 X」时，先确认 X 是否属于证据链语义；**但若用户明确要求演示闭环也要 X 并豁免安全**，应按用户指令改快照注入/规范化，而不是继续把需求赶到轻量链。

## 2026-08-22 · 演示闭环按用户指令匿名审批；任意订单用快照包装而不是放宽逐字段校验


- 现象：轻量链能跑任意 P/S，正式链 understand 对非种子订单报 `missing_information`/`order_snapshot_mismatch`；演示页又需要完整 PEVR（HITL + Ledger）。
- 原因：合同必须与快照订单逐字段相等，而默认快照只读 `orders_seed_v1.json`。
- 最终解决：`DynamicOrderSnapshotProvider` 把服务端重建的动态订单写入快照；understand 先 canonicalize 再校验；CLI `--order-json` 只读 `tmp/demo_nl_order_*.json`。HITL HTTP 与 `/demo/nl/*` 按 2026-08-22 用户指令去 JWT。
- 后续避免：不要把「追加种子订单」当成注入——Hungarian 会把 ORDER-001 一起分配掉；不要用放宽 `order_snapshot_mismatch` 来实现任意下单。


## 2026-08-22 · `WarehouseLocation` 序列化是 `{id, x, y}`，`position` 只是计算属性

- 现象：测试里按 `item["position"]` 读 `pickup_points` 元素直接 `KeyError: 'position'`。
- 原因：`WarehouseLocation` 的 `position` 是 `@property`（返回 `GridPosition`），不参与序列化；JSON 里只有 `id`/`x`/`y` 三个字段。
- 最终解决：读坐标用 `{"x": item["x"], "y": item["y"]}` 自行拼装。
- 后续避免：消费 `DemoWarehouseMap` 的 P/S/C 清单时记得它是扁平 `{id,x,y}`；只有 `location_positions`、`path_step.position` 等明确建模为 `GridPosition` 的字段才是 `{x,y}` 嵌套形。

## 2026-08-22 · 本地 HTML 教程对照外部 PDF 时，讲法跟 PDF、事实跟仓库

- 现象：按框架名词堆出来的课程读起来晦涩；若把 PDF 里的一般表述直接当成仓库已测数字，又容易把离线 oracle、拒答门槛或演示豁免写错。
- 原因：学习路线负责「怎么讲、先学什么」；本仓库的门槛、分数、禁用项来自已运行验证，两边不是同一份权威。
- 最终解决：`tutorial/` 用仓管员比方、贯穿订单、每课三句话带走；PDF 章节只做对照表。数字与安全口径仍指向 `docs/`。
- 后续避免：改课程结构时核对 `parts` 里每个 id 在 `lessons` 里都有对象，否则导图会报错；课号大改才换 `amr-learn-done-v2` 这类进度键。

## 2026-08-22 · 五模块手册的 hash 与进度键不能混用

- 现象：总册出现后，旧链接 `#learn`/`#exam` 若乱接到大模型课，会看到 Agent 课文；若四科共用 `amr-learn-done-v2`，Agent 的 24 课进度会污染只有 10 课的 OS 进度条。
- 原因：同一套 `app.js` 壳子渲染五科数据；课号都从 `l00` 起跳，键必须带科名前缀。兼容旧书签时只能**固定落到 Agent**，不能按「最后停留的科」猜。
- 最终解决：新地址 `#llm/learn`、`#cpp/exam` 等；Agent 沿用原键，其余用 `amr-learn-done-v2-llm` 这类后缀。侧栏「课程导图」按当前科写 hash，避免写死 `#learn`。
- 后续避免：不要把 4 台 AMR、4 个端口理解成 4 个聊天 Agent；不要在切科后假设笔试 DOM id（`tf01`）仍属于上一科——必须重绘。

## 2026-08-22 · 秋招手册若写成提纲，没学过的人读不下去

- 现象：四科第一版像面试复习笔记：一课两段、术语未定义就出现、对照仓库抢在原理前面。
- 原因：按考点清单堆句子，默认读者已经上过课。
- 最终解决：改成教材顺序——先修、用词表、比方、三句、从已有画面细讲、带步骤的例子，最后才对照仓库。渲染增加 `prereq`/`terms`。
- 后续避免：不要为了「像八股」把正文压回提纲；笔试可以短，系统课必须能自学。

## 2026-08-22 · 教材 JS 数据里的口语引用必须用「」，不要未转义英文双引号

- 现象：课程字段是双引号 JS 字符串；若正文里再写 `"派车"` 这类英文引号且忘记 `\"`，整份 `*-learn.js` 会在引号处被拆断，导图空白或控制台语法错。
- 原因：渲染器把字段当 `innerHTML` 插入，数据文件本身仍是 JavaScript 源码，不是 Markdown。
- 最终解决：中文口语、强调、假想对话一律用「」；文件头注明此约定。`os-learn.js`、`net-learn.js` 按入门教材重写时已遵守。
- 后续避免：改 `tutorial/*-learn.js` 后先数「」是否成对，再用括号配平检查；本机常无 `node`，不要把未跑 `node --check` 写成已语法通过。不要把「四个端口 / 四台 AMR」写成四个聊天 Agent。

## 2026-08-22 · 手册公式要本地 KaTeX，不要指望 VS Code 的 LaTeX 扩展去渲染 HTML

- 现象：本机 VS Code 能预览 `.tex` / Markdown 公式，但双击 `tutorial/index.html` 时公式仍是生 TeX，或完全没有公式。
- 原因：LaTeX Workshop 和 `markdown.math` 只在编辑器里工作，不会给用浏览器打开的静态页排版。
- 最终解决：把 KaTeX 放进 `tutorial/vendor/katex/`，课文用 `formulas[].tex` 和行内 `\\( ... \\)`；KaTeX 失败则显示纯字符。
- 后续避免：不要为手册去改用户的 VS Code 设置；也不要把 CDN 当离线依赖。扩展若要自己找：`C:\Users\QYC\.vscode\extensions\james-yu.latex-workshop-10.18.0`。

## 2026-08-22 · 秋招手册导图要 S/A/B 三个集合，不要和 P0 工作包混用

- 现象：清单按熟练度分 S/A/B，旧手册却按章节 p1/p2 或仓库 P0/P1/P2 排，读者分不清「必须滚瓜烂熟」和「知道即可」。
- 原因：P0 是本仓库交付优先级，S/A/B 是秋招知识点熟练度，两套标签不是同一轴。
- 最终解决：导图 `parts` 固定为 s/a/b 三张卡片；课文 `prio` 写 S/A/B；渲染器把遗留 P0/P1/P2 映射过去。Agent 原八章顺序只留在阅读建议。
- 后续避免：不要再给课文标 P0 当秋招分级；也不要把清单没有的科目硬拆出空的 A/B 集（C++ 可以只有 S）。

## 2026-08-22 · 三个集合不要放在长导读后面，否则 Agent 会看起来只剩一张课程导图

- 现象：Agent 系统学习侧栏只有一个「课程导图」，主栏先是大段建议怎么读，S/A/B 课表要翻很久才出现。
- 原因：集合卡片排成单列，又插在九段阅读建议之后；Agent 的 S 集有 24 课，A/B 更不容易看见。
- 最终解决：简介之后立刻并排三张集合导图；侧栏增加 S/A/B 跳转；阅读建议下移。
- 后续避免：不要把 `course-map` 放在比它更长的导读卡片后面。

## 2026-08-22 · Agent 课表 0/0 课：learn.js 对象数组漏逗号会导致整文件不执行

- 现象：Agent 侧栏只剩「三个集合总览」，进度显示「已学 0 / 0 课」，S/A/B 目录为空。其他科正常。
- 原因：`learn.js` 作为一整份脚本先解析再执行。`story` 里相邻 `{ h, p }` 漏了逗号，整文件 SyntaxError，`window.LEARN` 从未赋值。
- 最终解决：补上 5 处 `},`。花括号配平检查发现不了漏逗号。
- 后续避免：改 `tutorial/*-learn.js` 后除了数「」和括号，还要搜 `}\\s*\\n\\s*\\{`（`}{` 中间没有逗号）。

## 2026-08-22 · 本地 HTML 手册：手机不能用 127.0.0.1，也不要把产品口绑到 0.0.0.0

- 现象：电脑双击 `tutorial/index.html` 正常，安卓 / iPad 浏览器打不开或样式/公式残缺。
- 原因：手机上的 `127.0.0.1` 是手机自己。iPad 用「文件」打开 `file://` 时，相对 CSS/JS 和 KaTeX 字体经常加载不全。另：窄屏 CSS 曾把 `.rail` 设为 `display: none`，课程目录会消失。
- 最终解决：同一 Wi-Fi 下只用 `tutorial/serve.ps1` 共享静态目录（`0.0.0.0:8765`）；手机打开局域网 IP。窄屏改为侧栏叠在正文上方并可滚动。
- 后续避免：不要为了手机预览去改 FastAPI `:8010` 或模型 `:8080` 的绑定。访客 Wi-Fi / AP 隔离、电脑防火墙、手机 VPN 都会让局域网打不开。本机 `ipconfig` 里的 `172.30.*` 多半是 WSL/Hyper-V，不是手机该用的地址。

## 2026-08-22 · 仿真 ExecutionStateStore 不能拿 Checkpoint 适配器顶替

- 现象：在线 PEVR 在 HITL 批准后 dispatch 失败，P0-15 收成 `recovery_fatal`（“未知故障 fail closed”），`model_call_count` 看起来像没跑完。
- 原因：`InMemoryRuntimeStore`（Checkpoint）没有 `put()`。仿真状态要走 `InMemoryExecutionStateStore.put/get`。把 checkpoint store 传给 `execution_store=` 会在 handler 里 `AttributeError`。
- 最终解决：Harness 分开构造：`execution_store=InMemoryExecutionStateStore()`，`checkpoint_store=InMemoryRuntimeStore()`。
- 后续避免：凡是 `build_tool_registry(..., execution_store=...)` 必须传实现 `put/get` 的 ExecutionStateStore；不要因为都叫 InMemory 就混用。

## 2026-08-22 · Trace 字段是 Literal str，不能一律 `.value`

- 现象：真实 Fast PEVR 已经跑完（约 100s、多次模型调用），报告组装时 `AttributeError: 'str' object has no attribute 'value'`，整例被记成 `online_harness_exception`、`model_call_count=0`。
- 原因：`TraceEvent.event_type` / `status` 是 `Literal["node", ...]` 字符串；`FinalReportStatus` 才是 Enum。对 str 取 `.value` 会在成功路径上把结果丢掉。
- 最终解决：统一 `_as_str()`：Enum 取 `.value`，其余直接 `str()`。PEVR 成功后的序列化失败也收敛进 `_pevr_failure`，并逐例写 `tmp/p018_online_eval/p018_online_progress.jsonl`。
- 后续避免：序列化 Pydantic 事件前先看字段类型；不要假设所有 `status` 都是 Enum。

## 2026-08-23 · 硬地图 Provider 漏了 injected_orders，充电会在 understand 被 fail-closed

- 现象：P0-18 在线 60 例充电 5/5 `missing_information`，各 1 次模型调用，从未进分配/路径。
- 原因：`_understand_node` 用 `getattr(snapshot_provider, "injected_orders", None)` 决定是否 canonicalize（覆盖快照订单并清零 `missing_information`）。演示 `DynamicOrderSnapshotProvider` 有该属性；`HardMapSnapshotProvider` 已把占位订单写进 `get_snapshot()`，但没暴露同名属性，duck-type 失败。充电 NL 不填运输必填项，模型留下 `missing_information` 后被门禁拒绝。空列表在 getattr 里是 falsy，和 `orders is None`（回退种子、不设属性）必须分开。
- 最终解决：非空 `orders` 时设置 `injected_orders` 深拷贝，与演示 Provider 对齐。单测覆盖暴露属性、canonicalize 清空 `missing_information`、`orders=None` 不假装有注入。未改合同 Schema，也未把充电改成零订单。
- 后续避免：凡是给 PEVR 用的 SnapshotProvider，只要快照订单是服务端注入的，就必须暴露 `injected_orders`；不要只改 `get_snapshot()` 而漏 duck-type 属性。不要为了完成率去改离线 `expected_outcome`。

## 2026-08-23 · REPLAN 失败后不能硬编码 recovery_fatal，也不能靠 plan_invalid 子串再记一笔额度

- 现象：加难地图上大量订单 2 次模型调用后 `recovery_fatal`，原因却是「允许第 2 次局部重规划」，`replan_count` 仍为 0。同一张图仍有 6 单完整成功，不是无路。
- 原因：`_recover_graph_failure` 在 `_apply_production_replan` 抛错后，不论第二次决策是什么都抛 `recovery_fatal`。错误码 `local_replan_invalid` 含子串 `plan_invalid`，被分类器收成 `PLAN_INFEASIBLE`，额度自增、reason 写成「允许第 N 次…」，真实异常（例如 `ValueError: 故障没有定位到可替换的未完成任务`）被丢掉。
- 最终解决：失败码改为稳定的 `local_replan_rejected`（精确映射 `PLAN_INFEASIBLE`，排在子串规则之前）。额度未尽且仍是 REPLAN 则真正再 apply；耗尽则 `recovery_human`/`recovery_fatal`，reason 优先真实 apply 异常。循环另受 `max_replans` 硬上限。
- 后续避免：看恢复是否发生要查 `replan_count` 和新计划版本，不要读「允许第 N 次」文案。分类器不要用易碰撞的子串码。`invalidated_task_ids` 仍为空时现在会用尽 apply 次数后 `recovery_human`，不会 magically 找出子图，也不放宽 Validator。

## 2026-08-23 · 充电对齐后会把占位运输单跑完，不能当成 charged

- 现象：修复 `injected_orders` 后 5 个充电例 4 次模型调用 + HITL，观察 `completed`、评测仍失败（期望 `charged`）。任务完成率 24/44 含这 5 例，是因为观察终态属于正向集合。
- 原因：harness 为满足 `TaskContract` 最少 1 条订单而注入占位 `TransportOrder`；canonicalize 之后模型按运输主链执行，仿真完成订单而不是充电终态。
- 最终解决：当时重跑如实记录充电完成率仍为 0。同日后续已改为独立 `ChargingGoal` 合同（空订单 + `charging.completed`），见下条；本条只解释占位单为什么会把完成率算进运输 `completed`。
- 后续避免：若要测充电，合同/NL/计分必须是充电终态，不能靠占位运输单混过去。

## 2026-08-23 · PLAN_INFEASIBLE 空影响集合必须写回 affected_entities 才能生成 v2

- 现象：加难地图上大量订单 2 次模型调用后 `recovery_human`，`replan_count=0`，原因是 `故障没有定位到可替换的未完成任务`。
- 原因：Validator/C++ 失败常被收成 `PLAN_INFEASIBLE` 且 `task_id`/影响集合为空。`apply_replan` 会再 `analyze()`，只改局部 analysis 而不写回 `decision.fault.affected_entities` 等于没换子图。
- 最终解决：仅当类别是 `PLAN_INFEASIBLE` 且影响集合为空时，把计划里未完成的 `plan_multi_amr_routes` / `validate_fleet_plan` / `dispatch_simulation` 并进 `tool_names` 并 `model_copy` 写回 decision。同时必须把这三条从 `completed_task_ids` 拿掉（unfinish）：C++ 拒绝时 route 往往已 completed，`analyze` 的 `invalidated = direct - completed` 否则只会克隆 validate/dispatch，继续拿同一份 `derived_plan`。非 `PLAN_INFEASIBLE` 保持原错误。第一次 apply 用确定性克隆出 v2；仍失败再走 Fast `replan`。
- 后续避免：看恢复是否发生要查 `replan_count` 和新 `plan_version`，不要读额度文案。不要把空影响 fallback 扩到未知故障。clone 已完成的 route 时不要指望 A* 自动换路。

## 2026-08-23 · 终态 reason 必须留下 Validator/C++ 原文

- 现象：LocalReplanner apply 失败后终态只剩 `recovery_human` 和「额度耗尽」，现场第一个 PEVR 错误被丢掉。
- 原因：第二次 `handle_failure(local_replan_rejected)` 覆盖了第一次故障 message；终端只拼 apply 异常和额度句。
- 最终解决：`_compose_terminal_recovery_reason` 固定带 `原始错误: {第一次错误}`。v2 已经落地后再因额度 HUMAN、没有第二次 apply 异常时，`apply_error` 为空也不能只抛额度文案。
- 后续避免：分类器 message 截断到 2000 时仍要能从终态反查原始 `plan_validation_failed` / C++ code。不要只在 `last_replan_error is not None` 分支里拼接原文。

## 2026-08-23 · 额度空转后再走 replan 节点，不要一上来就扩 Smart

- 现象：确定性克隆出的 v2 若仍过不了门禁，只循环 clone 不会改变 DAG 结构。
- 原因：第一次失败往往是 LLM DAG/`plan_validation_failed`；克隆只换 task ID。
- 最终解决：`plan_version>1` 或 `apply_attempts>1` 时调用 P0-05 `replan()` + `apply_model_replan`。单测用 FakeProvider 从 `node_input.current_plan` 克隆子图；反例：LLM `invalidated_task_ids` 与 analyze 不一致则拒绝。不改 `max_replans`、不改 `ReplanOutput` schema、不启用 Smart。
- 后续避免：FakeProvider 不能在 `ReplanOutput` 分支之前 `del messages`。空影响单测必须用真实 `FaultDecision.model_copy`，不能塞简易 namespace。

## 2026-08-23 · 充电必须单独成合同，按 charging.completed 计分

- 现象：占位 `TransportOrder` 让 5 个充电例走完运输主链，观察 `completed`、期望 `charged`。
- 原因：`TaskContract` 曾要求至少 1 条订单；harness 注入假运输单。
- 最终解决：`ChargingGoal` 与订单互斥；understand 走 `injected_charging`；plan 合成仅 `dispatch_simulation` 的 idle plan；AMR 放到充电站坐标；评测 Registry 注入 `SimulatorConfig.charging_stations`（默认 `{}` 不会充电）；计分只认仿真 `charging.completed` 且电量达标。生产 `dispatch.faults` 仍为空。
- 后续避免：不要把运输 `completed` 改写成 `charged`。充电 retrieve 仍要 live RAG；仿真必须带充电站配置。

## 2026-08-23 · 异常恢复率不要用硬地图碰巧完成来充数

- 现象：未注入故障时，8 个期望完成的异常例只是在硬地图上再跑一遍订单，恢复率随碰巧完成抖动。
- 原因：生产 `dispatch_simulation.faults` 必须保持空序列；评测又没有包装 Registry。
- 最终解决：快照写入 `fault_code`；`FaultInjectingRegistry` 只包装评测 `execute`，前 N 次返回失败 `ToolResult`。恢复率：期望 replan 的例要求 `replan_count>=1` 且 `plan_version>=2`；timeout 看 `retry_count`；duplicate 看 completed 且无重复副作用；007/008 仍 sidecar。失败例也从 Checkpoint 读回计数。
- 后续避免：不要把 `evaluation_passed or 硬地图 completed` 当成恢复成功。不要把注入能力暴露给生产 ToolRegistry。

## 2026-08-23 · 给 Fast replan 的 current_plan 不能抄硬地图，也不能删掉 plan $ref

- 现象：第二次 apply 走 `ReplanOutput` 时 JSON 在约 300 行被截断；或者门禁报 `simulation_plan_ref_invalid` / `environment_ref_mismatch`。
- 原因：硬地图 `blocked_cells` 被 Fast 原样抄进输出。Compact 若直接丢掉 `plan` 键，模型就省略 `$ref`，落地失败。替换任务若带着旧 `evidence_refs`，会报 `task_has_runtime_evidence`。`_clone_replan_subgraph` 曾是 staticmethod 却调用 `self`，第一次 clone 直接 `NameError`。
- 最终解决：compact 只去掉障碍数组，把内联 `plan` 压成 `{"$ref":"derived:simulation_plan"}`。`canonicalize_replanned_pevr_plan` 在 LocalReplanner.apply 里覆盖环境引用/seed/$ref/链依赖。pending 替换任务清空 `evidence_refs`。clone 改为 classmethod，且不再剥执行参数。
- 后续避免：给模型的 current_plan 只保留数据流引用和标量；落地前用合同真值覆盖固定字段。不要在 staticmethod 里写 `self`。

## 2026-08-23 · A* 对同一分配+同一地图是确定性的，v2 不自动抬订单完成率

- 现象：C++ 拒绝后 `plan_version=2`、`replan_count=1`，再执行仍 `fleet_plan_invalid`。
- 原因：unfinish 后重新跑 A*，输入（分配、障碍、起终点）没变，路径相同，Validator 再拒一次。当时误以为是货架墙无解。
- 最终解决：接受那一轮正常订单完成仍约 6/20。**后续已证实真正卡点不是货架**：ORDER-002 的 `pickup_before_release`（首次踏上工位 vs 装货事件）和 ORDER-003 种子依赖被当成活订单。
- 后续避免：不要把 `fleet_plan_invalid` 一律写成“地图太难”。先看 Validator 错误码。

## 2026-08-23 · 在线 6/20 不是货架太密，是装货时刻和种子依赖

- 现象：加难地图上正常订单 6/20；减障碍到 0 仍是同一 6 例（全是 `release_time=0` 的 ORDER-001）。
- 原因：P0-09 A* 允许 `t<release_time` 预定位并在 pickup 等待，`pickup_time` 是装货事件。P0-10 却把**首次踏上 pickup 格**当成装货，报 `pickup_before_release`/`pickup_time_mismatch`。ORDER-003 种子依赖 ORDER-001：评测把前置当成第二条活订单，Hungarian 无法分配主单，understand 也会因合同漏写前置 SCHEMA 失败。
- 最终解决：Validator 以 `route.pickup_time` 且当时停在 pickup 为装货事件；评测只注入主订单，种子前置写入 `completed_order_ids`；通道对齐工位行、每例额外障碍 6→2。生产 `warehouse_v1.json` 未改。C++ 离线探测 20/20 过 Hungarian+A*+Validator。在线最终 **43/44**。
- 后续避免：改评测地图前先跑无 LLM 的 C++ 可行性。不要为抬完成率去改离线 oracle。

## 2026-08-23 · 评测包装 Registry 丢掉 HITL grant / verifier

- 现象：正常订单 20/20 后，8 个期望完成的异常例在 `dispatch_simulation` 报 `approval_required` 或 `approval_verifier_unconfigured`。
- 原因：`FaultInjectingRegistry.execute` 只有 `**kwargs`，PEVR 用 `inspect.signature` 判断时不传入 grant；包装器不是 `ToolRegistry` 子类，安全模式把 verifier 写在包装对象上，内层生产表仍是 `None`。
- 最终解决：包装器关键字与生产 `execute` 对齐并转发属性；`_registry_execute` 把 `**kwargs` 视为可接受 grant；图初始化把 verifier 写到 `_inner`。
- 后续避免：任何评测包装必须是生产 Registry 的签名超集，且 `isinstance(..., ToolRegistry)` 不能作为唯一绑定条件。

## 2026-08-23 · 演示不能直接复用评测每例的通道障碍

- 现象：在线 60 例按 seed 只保证「这一单」起点→P→S 连通。演示页允许任意自然语言选 P/S。
- 原因：把 ORDER-001 的 2 个 extras 原样画进演示图，可能堵住别的 P→S，UI 与规划会出现「看得见货架、下单却无解」。
- 最终解决：演示用 `extra_obstacles_for_demo`，在通道上放同样 2 格，但 BFS 要求全部 AMR 起点、全部 P、全部 S、充电站仍四邻域可达；规划 Provider 与 GET 地图 overlay 同一组 extras。
- 后续避免：演示地图与评测地图可以同货架墙，不要同「按单例 seed 生成的 extras」。生产 `warehouse_v1.json` 继续只服务离线/生产种子路径。

## 2026-08-23 · 演示 HITL 批准后同一张卡死循环

- 现象：自然语言闭环停在 `waiting_approval`；点「批准并继续执行」后又弹出同一张 HITL。再点「拒绝」报审批不是 pending。
- 原因：approve 已把行写成 approved。resume 若没把 grant 放进 `PEVRRequest`，execute 仍看到 checkpoint 里的 interrupt，再次 `raise PEVRInterrupt`（exit 3），产物还是 waiting。页面以为还要审。拒绝打到已批准行，store 只允许 pending。
- 最终解决：已批准时从 store `get_grant` 恢复并继续 dispatch。status 在 CLI 非 0/3 退出时即使有旧 waiting JSON 也报 failed。拒绝已批准返回「审批已批准，不能再拒绝」。页面记住刚批准的 `approval_id`，同一张卡禁用按钮。
- 后续避免：不要把「产物 JSON 仍是 waiting」当成唯一真相；要看进程退出码和数据库审批状态。HITL 是一次性决定，approved 后只能 resume，不能再 reject。

## 2026-08-28 · 在线配置中的制品指纹必须在启动前对照实物

- 现象：P0-18 在线配置仍写旧 manifest/launcher SHA-256，而当前受控 manifest、模型、运行时和启动脚本已经是另一组固定制品；若 P0-19 只比较配置文字，三策略会在错误身份上“公平”。
- 原因：配置记录和文件实物的生命周期不同。此前更新安全启动器后没有同步评测配置，模型 alias 相同掩盖了制品漂移。
- 最终解决：在线三策略启动前分别检查 alias、量化、ctx、temperature、manifest/model/runtime/launcher 哈希；任一不一致 fail closed。本步只校正 P0-18 配置中两个过期哈希，未改运行行为。
- 后续避免：报告必须同时保存配置 SHA、manifest SHA、模型 SHA 和启动器 SHA。不能把“配置里写了某哈希”表述为“本次重新计算并通过”，除非启动预检确实对照了文件。

## 2026-08-28 · 在线策略对照不能裁掉源 Trace 的 Token、时间和失败尾部

- 现象：旧 P0-18 在线适配把 P0-17 Trace 投影成少数字段，模型 attempts、Token、时间戳、Prompt/模型版本、metadata 和失败终态丢失；P0-19 因而无法真实比较调用量与墙钟。
- 原因：为了缩小报告而重造 Trace，破坏了已有可观测性公共契约；失败时只看异常对象又会漏掉 Checkpoint 中已经完成的节点和最后错误事件。
- 最终解决：在线 Harness 保留完整 P0-17 `TraceEvent`，失败也回读 Checkpoint 并追加终态；模型调用数从实际 model event/attempts 统计，ReAct 控制器单独计量。策略只改控制动作，不改历史证据。
- 后续避免：评测层可以新增 derived metric，不能用有损投影替代 source trace。报告变大应通过 JSONL/压缩解决，不要删除复现与计费字段。

## 2026-08-28 · 有界 ReAct 的安全门必须在模型决定之前

- 现象：如果先问模型“要不要 retry”，再检查副作用和幂等性，模型即使输出结构化 `retry` 也可能诱导重复发车；保存自由文本推理还会把不可信上下文和敏感证据带进报告。
- 原因：LLM 不能成为副作用安全性的最高裁判，Schema 只约束格式，不保证决定安全；完整思维链也不是审计所必需。
- 最终解决：只在 `retryable && idempotent && (!has_side_effects || side_effect_not_found)` 时调用恢复控制器；最多一次 retry、零 replan。Trace 仅保存 action、reason code、简短 observation summary 和确定性安全事实，明确 `raw_chain_of_thought_stored=false`。
- 后续避免：任何开放 ReAct 扩展都必须先定义确定性动作白名单、预算、幂等/副作用证明和停止出口。不要把“模型认为安全”当成安全证据。

## 2026-08-28 · 高频 `nvidia-smi` 会把资源评测变成自己的噪声源

- 现象：若 CPU/RSS/GPU 都按 0.5 秒调用外部命令采样，短 sidecar case 的大部分开销来自启动 `nvidia-smi`，而 Windows/WDDM 也不保证能按 PID稳定返回显存。
- 原因：CPU/RSS 可从 `psutil` 低成本读取，GPU 查询却要新建进程；三类指标不能机械使用同一周期。
- 最终解决：CPU/RSS 每 0.5 秒采样评测进程、子进程和 8080/18080 监听进程；GPU 查询降到约 5 秒，并把缺失样本保留为不可观测/近似说明。汇总只报告进程级峰值，不解释为单节点因果成本。
- 后续避免：资源报告必须同时给采样对象、周期、样本数和缺失原因。GPU 0 或空值不能直接推导为“模型没占显存”。

## 2026-08-28 · 长在线矩阵续跑必须绑定不可变 manifest

- 现象：三策略 60 例是 180 次在线调用，运行跨小时；中断后按行号手工“从第 100 条开始”容易重复副作用、漏 case，或在配置已变时拼接两次实验。
- 原因：行号只是当前调度视图，不是稳定身份；真正唯一键是 `strategy + case_id`，还必须绑定数据集、配置和调度摘要。
- 最终解决：先写运行 manifest，再逐例原子追加 JSONL；`--resume` 校验 dataset/config/schedule digest 后只跳过完整的唯一键。本轮第 100 条已落盘，后台继续完成到 180/180，没有重跑第 100 条。
- 后续避免：不要用 `Select-Object -Skip 99` 之类手工切数据集。恢复后仍要核对 180 行、180 个唯一键和三策略各 60 个完整 case ID 集。

## 2026-08-28 · 全量 Schema 重导出会暴露既有 description 漂移

- 现象：为 P0-19 重导 Schema 时，三个 Demo Schema 的中文 description 也发生变化，虽然本步没有修改 Demo 字段或行为。
- 原因：运行时 Demo Pydantic docstring 已在此前更新，checked-in Schema 没有同步；统一导出会把所有当前模型重新物化。
- 最终解决：逐项审查 diff，确认只校正 description 后保留这三份机械更新，并在文件职责/交接中明确“既有漂移修复、无契约字段变化”。
- 后续避免：Schema 导出后不能只看目标文件；必须审查全量 diff，并区分本步接口变化与暴露出的历史生成物漂移。

## 2026-08-28 · 恢复动作达标不等于最终异常终态正确

- 现象：P0-19 的 PEVR 自动汇总显示 `recovery_terminal_correct_count=10/10`，但 10 个异常例中
  `p018-exception-004` 的期望终态是 `completed`，实际终态是 `failed/recovery_fallback`；若直接
  把字段名抄到 README，就会与全例符合 59/60、唯一失败例的逐例事实矛盾。
- 原因：评测侧 `_exception_recovery_ok` 对需要 replan 的异常主要检查 `replan_count>=1` 和
  `plan_version>=2`，衡量的是“预期恢复动作/新版本是否发生”，没有再次要求最终
  `expected_outcome == observed_outcome`。字段名称把动作级指标误写成了终态级指标。
- 最终解决：评测聚合改为 `recovery_terminal_correct_count` 严格按 `expected==observed`，
  `successful_recovery_count` 只统计确实发生恢复动作且最终完成的异常路径；v1 在线报告因不是独立
  ReAct 已整体作废，不能继续引用其中 3/10、4/10、9/10 或 10/10。
- 后续避免：报告同时保留“恢复动作达标”和“最终终态正确”时，必须使用不同字段名和测试；面向
  用户的成功率一律从最终终态重算。修复聚合器必须版本化报告契约并重新实跑，不能手改历史 artifact。

## 2026-08-28 · 不能把异常后一次 retry 称为独立 ReAct

- 现象：P0-19 在线 ReAct 实例化 `PEVRGraphRunner`，复用 `guard → understand → retrieve → plan → validate → execute → verify → finish`，只在图抛异常后调用一次模型做 `retry/stop`。报告却把它写成独立 ReAct，并给出 54/60 等分数。
- 原因：评测层想快速得到“有界恢复”对照，把安全 retry adapter 挂到 ReAct 名字下；公平性还宣称 `same_prompts=true`，掩盖了控制策略并未独立运行的事实。
- 最终解决：抽出策略无关 `SharedPrefixService`；新增评测层 `ReActRunner`/`ReActDecision` 持续循环；P0-18 在线 Harness 拒绝再执行伪 ReAct；配置升级为 `p0-19.online.v2`，旧 v1 progress 不能 resume；异常终态按 `expected==observed` 重算，恢复动作另计。
- 后续避免：策略对照必须能证明“未调用生产图”和“正常 case 也有多轮 decide/act/observe”。名字、Prompt 版本和 Runner 版本都要进 manifest；一次边界 retry 只能叫 recovery adapter，不能叫 ReAct。

## 2026-08-28 · 共享 Understand 的单次输出 Token 不能比 PEVR 更紧

- 现象：独立 ReAct 在线正常例约 19s 即以 `StructuredOutputError` 失败，`model_call_count=0`；同一例 Fixed/PEVR 能完成。原始 JSON 在约第 177 行被截断。
- 原因：`ReActRequest.requested_output_tokens` 默认 1024，共享 `SharedPrefixService.understand` 把该值当作 TaskContract 单次上限；`PEVRRequest` 默认 4096。decide 循环的 512 上限被误用到前置 Understand。
- 最终解决：ReAct 入口默认并对齐为 4096；在线 Harness 显式传入；decide 仍单独 `min(512, …)`。用单测锁住与 `PEVRRequest` 相同的默认值。
- 后续避免：策略无关前置阶段的单次生成上限必须与生产 PEVR 入口一致；循环内的短决策 Schema 只能在 decide 调用处收紧，不能改入口默认。

## 2026-08-28 · 封闭参数契约必须进入每轮 ReAct 上下文，账本 digest 必须与 Registry 相同

- 现象：Understand 修好后，正常例仍失败。一例 `complete_effect` 抛 `ToolResult.input_digest 与 Effect Ledger 不一致`，评测只留下无轨迹 harness 异常；另一例 12 轮 decide 中 11 次 `extra_argument`，模型给 `plan_multi_amr_routes` 传了 `order_ids`/`seed`。
- 原因：账本对原始 dict 做 SHA，真实 Registry 对 `input_model`（`by_alias` dump）做 SHA。decide 上下文只给了工具名，没有给 P0-04 顶层键白名单，系统提示还把 `$frozen order_ids/seed` 写成通用写法。
- 最终解决：`_effect_input_digest` 与 PEVR `_task_input_digest` 对齐；每轮上下文写入 `tool_argument_policies`；系统提示列出各工具允许键。额外参数仍拒绝，不静默丢弃。
- 后续避免：策略无关的“封闭参数契约”必须出现在模型可见上下文里；副作用预留指纹必须复用生产 Registry 的规范化 digest，不能另写一套。

## 2026-08-28 · 稀疏必需证据标注会形成低 Precision、高 Recall/nDCG

- 现象：同一真实 RAG holdout 的 Recall@K、Section Recall@K、MRR 和 nDCG@K 都是 1，但
  Precision@K 只有 0.236364。若只看百分比，容易误判为其余 76% 候选都是错误检索。
- 原因：当前 oracle 只标回答所必需的 1～2 个 `(doc_id, section)`，不是对 Top-K 全部候选的完整
  人工相关性标注；普通问题取 K=5、跨文档问题取 K=6，宏平均自然是 8 个 `1/5` 与 3 个 `2/6`。
  此外，同一章节可能被切成多个 chunk；若每个重复 chunk 都计相关，会反向虚高 Precision/nDCG。
- 最终解决：冻结章节级二元口径，相关章节只在第一次命中时记 1，重复 chunk 记 0；Precision 固定
  除以用例 K，候选不足按尾部 0 处理；nDCG 使用同一增益和唯一相关章节数构造 IDCG；没有相关性
  oracle 的不可答例排除。报告写入完整口径，公式和重复/空 oracle 均有反例测试。
- 后续避免：不得依据本次 holdout 分数事后设置默认门禁，也不能把未标注候选直接称为语义无关。
  若要做 MAP、分级 nDCG 或更有解释力的 Precision，应先独立补齐候选池的完整/分级人工标注并冻结
  版本，再运行新的 holdout；不能在看完结果后补标签抬分。

## 2026-08-30 · llama.cpp 前缀缓存要求稳定内容必须在节点 Prompt 之前

- 现象：五个 P0-05 节点和 ReAct 都有很长的 system 文本，但安全边界原先追加在节点专属 2-shot/Schema 之后。llama.cpp `cache_prompt` 只从 Token 序列开头匹配公共前缀；`parallel_slots=1` 时槽内只保留上一请求的 KV。跨节点调用几乎无法复用那段安全文本。
- 原因：前缀匹配是“从左到右的最长公共 Token”，不是“任意相同段落”。节点正文一旦分叉，后面的稳定规则不再是前缀。另外 `agent.runtime.prefix` 是 Guard/Understand/Retrieve 共享前置，名字容易让人误以为已经做了模型 KV 缓存。
- 最终解决：抽出 `amr.shared.system_prefix@1.0.0`，放到每个节点 system 消息最前面；网关 `extra_body` 显式发送 `cache_prompt`；`TokenUsage.cached_input_tokens` 记录命中。P0-05 升为 `1.2.0`，ReAct 升为 `2.1.0`。同一节点连续调用（ReAct 多轮、Schema 修复）仍可命中整段 system；跨节点只保证共享前缀命中。
- 后续避免：不要把随请求变化的合同、RAG、run_id 放进共享前缀。不要为了“整段节点 Prompt 都命中”去把 `parallel_slots` 改成 5，除非同步改 Fast manifest、显存预算和启动脚本哈希。关闭缓存用 `LLM_PROMPT_CACHE_ENABLED=false`；llama.cpp 文档指出缓存命中会改变 prefill batch，logits 不一定与冷启动逐 bit 相同。

## 2026-08-30 · 关闭 `cache_prompt` 时长 Prompt 节点会打满默认 120s，加速比不能含超时

- 现象：同一套 P0-05 在线样例，`cache_off` 下 `understand_goal`/`plan_tasks`/`replan` 连续两次都在约 120 013 ms 以 `TIME_BUDGET_EXCEEDED` 失败，usage 为空；`cache_on` 则 12/12 成功，同节点第二轮 `cached_input_tokens` 几乎等于 `input_tokens`。若用 12 次全量 wall 合计算加速比（约 3.5x），会把 7 次超时的 120s 罚时算进“无缓存更慢”。
- 原因：仓库默认 `LLM_GENERATION_TIMEOUT_SECONDS=120`。关闭缓存时约 3k–4k prompt 的 prefill 可到 70–90s，再加上 JSON 生成，墙钟经常超过 120s；客户端先放弃，llama.cpp 槽上可能仍在跑。Python 默认 stdout 块缓冲时，对照脚本的进度行要等阶段结束才出现。
- 最终解决：公平口径只比较两侧都成功的同一节点/轮次（本次 5 对，加速比 4.639）。`cache_off` 成功调用必须看到 `cached_input_tokens=0` 才算开关有效。对照脚本改为逐行 flush，并仅在该脚本进程内把生成超时默认放到 180s，不改仓库默认。证据文件是 `tmp/` 生成物。
- 后续避免：不要把超时失败写成模型语义失败；不要用含超时的全量 wall 当发布数字。生成阶段仍占墙钟大头，前缀缓存主要缩短 prompt eval。不要把本次节点级 4.6x 外推成 P0-19 180 例同等加速。

## 2026-08-30 · PEVR 60 例全量墙钟加速比不能拿 0 次模型调用的超时跟完整 LLM 跑对打

- 现象：生产 PEVR 的 P0-18 在线 60 例，关缓存 26/60、只有 12 次模型调用，开缓存 50/60、117 次调用；全量墙钟比是 0.59（开缓存更慢）。
- 原因：关缓存组大量正常订单在默认 120s 内 `model_calls=0` 失败，墙钟停在超时罚时；开缓存组才真正跑完 Understand/Plan 等主链，单例可到约 300s。短路径（核验、安全拒绝）本来就不吃前缀 KV。
- 最终解决：把 50/60 与 24 例翻盘记成“关缓存打不到模型、开缓存能跑完”的可用性结果，不把 0.59 写成缓存负优化。两侧都有模型调用的只有 4 例，墙钟比约 1.03，样本太小不能当加速结论。
- 后续避免：PEVR/在线评测对照必须同时报 `evaluation_pass_count` 和 `model_call_count`。全量 60 含 sidecar 短例。不要覆盖正式 P0-18 报告身份。

## 2026-08-31 · 8080 代理还在不代表 llama-server 还在；有缓存 60 例不要覆盖旧对照目录

- 现象：交接仍写 Fast 在跑，但 `127.0.0.1:18080` 无监听；旧 `secure_proxy`（PID=21720）仍占 8080，无密钥 401、带密钥 502。
- 原因：llama-server 已退出，启动器 pwsh 还卡在代理子进程上。`start_local.ps1 -StartFast` 若发现 8080 被占用且 `/v1/models` 不健康会直接抛错，不会覆盖。
- 最终解决：精确停止代理与 `start_fast_secure.ps1` 启动器后重新 `-StartFast`。有缓存 60 例写到新目录 `tmp/p018_pevr_cache_compare_20260831/cache_on/`。
- 后续避免：启动前同时查 8080 和 18080。用户只要有缓存阶段时，不要跑会先执行 `cache_off` 的 `compare_pevr_prompt_cache.py`。不要覆盖已有对照证据。

## 2026-08-31 · 热机后关缓存也能跑完 PEVR；8/30 的超时不能当成 `cache_prompt=false` 的必然结果

- 现象：同日有缓存 59/60、133 次调用、约 38.5 分钟；随后 breaker + 关缓存同样 59/60、133 次调用、约 41.8 分钟。全量加速比 1.086，两侧都有模型调用且都通过的 35 对是 1.088。这与 8/30 关缓存 26/60、仅 12 次调用、大量 120s 超时完全不同。
- 原因：本次关缓存是在 Fast 已跑完有缓存 60 例、GPU/权重已热的条件下进行的，默认 120s 够用；8/30 先跑关缓存，prefill 更容易打满超时，客户端放弃后槽上可能仍在算，后续例继续恶化。`cache_prompt` 主要砍 prompt eval，生成仍占 PEVR 墙钟大头，所以热机公平对照只有约 8–9%。
- 最终解决：把 2026-08-31 的 1.086 记成热机公平墙钟差；保留 8/30 作为冷/先关缓存时的可用性证据。两套目录都不覆盖。
- 后续避免：不要用热机关缓存成绩去否定冷启动超时。不要把 PEVR 闭环 1.09x 写成节点级 4.6x。配对必须同时报通过数和 `model_call_count`。

## 2026-08-31 · 非流式日志里 `prompt eval time` 印在请求结束；TTFT 不能用 launch 到该行的间隔

- 现象：若把 `launch_slot` 到 `prompt eval time` 日志行的时间差当成 TTFT，会得到约 16s，和整次 `total time` 几乎一样。
- 原因：llama.cpp 在生成结束后才打印 `prompt eval time`/`eval time`/`total time`。当时误以为 `progress=1.00` 才是 Prefill 结束。
- 当时解决（**已作废**）：TTFT 用 launch→progress=1.00；无 progress 行时用 `prompt eval time` 回填。
- 后续避免：见 2026-09-01 条目。不要再引用 8/31 JSON 里的 TTFT 数字；Prefill 与命中率仍可用。

## 2026-09-01 · rounded `progress=1.00` 不能作为 Prefill/TTFT 完成信号

- 现象：旧统计把 `prompt processing, progress = 1.00` 当作 TTFT 终点，得到的 TTFT 中位数比 Prefill 中位数还低（约提前 110–264 ms）。没有 progress 时又把 `TTFT = prompt eval time`，并误判为 prefix KV 命中。
- 原因：progress 只保留两位小数，且该日志在下一批 Prompt token **之前**打印。当前 llama.cpp 还可能把最后约 4 个 Prompt token 单独处理，因此 `(N-4)/N` 已可能显示成 `1.00`，Prefill 实际尚未结束。progress 默认约 3 秒才打一条，短 Prefill 没有 progress 不等于缓存命中。生产 `ModelProvider` 使用 `stream=false`，非流式响应也测不到客户端首 token。
- 最终解决：真实 TTFT 定义为客户端 `perf_counter` 下“发出请求 → 第一个非空生成 `delta.content`”。只在 `evals.perf` Benchmark 使用 `stream=true`；生产网关保持非流式。Prefill 只用最终 `prompt eval time`。缺失 TTFT 时输出 `null` 和原因，禁止用 Prefill 回填。安全代理对 `stream=true` 做 SSE 透传，否则测到的是 E2E。请求与日志用串行 + 文件偏移关联，错配则 Prefill 缺失。
- 后续避免：不要把 progress、缺少 progress、或非流式整包 JSON 的到达时间写成 TTFT。旧 `llm_only_cache_metrics.json` 的 TTFT 列必须标作废；Prefill 1.44× 与案例 E2E 1.10× 仍可引用。

## 2026-09-01 · 当前 llama.cpp 日志不打 `n_prompt`，不能据此放弃命中率

- 现象：9/1 重跑 PEVR 时 `tmp/llama-server.err.log` 的 `launch_slot_` 行没有 `n_prompt`/`n_past`，若只认这两个字段，133 次调用的命中率会全部变成未知。
- 原因：当前 Fast 启动参数下 verbosity=3 仍不打印 `n_prompt`。缓存命中时 `prompt eval time` 只统计未命中 KV 的 token，`total time` 的 token 数也是 prefill+decode，不含已缓存前缀。
- 最终解决：用同一次 task 的 `stop processing: n_tokens` 减去 `eval time` 的生成 token，得到 Prompt 长度，再减 `prompt eval` token 得到缓存命中。没有 progress 仍然不得判为命中。
- 后续避免：不要因为日志缺 `n_prompt` 就把命中率写成 0 或缺失后用 progress 代替。核对时应用模型调用次数对齐 `prompt eval time` 条数（本次 133/133）。

## 2026-09-01 · 测 PEVR TTFT 必须换评测 Provider，不能改生产 `stream=false`

- 现象：生产 `ModelProvider` 整包返回，客户端收不到首 token；若为了指标把 `"stream": False` 改掉，业务路径和评测路径会缠在一起。
- 原因：真实 TTFT 只能来自 SSE 第一个非空 `delta.content`。PEVR 节点走 `generate_structured`，还要保留 Schema 校验和一次修复。
- 最终解决：新增仅评测使用的 `TtftEvalProvider`，覆盖 `_request_completion` 发 `stream=true`，默认关闭，需 `--measure-ttft` 才替换。生产网关不变。
- 后续避免：不要把 TTFT 探针设成仓库默认，也不要在生产 `provider.py` 里加流式开关。无 `--run` 的 `pevr-ttft` CLI 不得自行开跑 60 例。

## 2026-09-01 · 正式 EvalReport 仍是 60 例；llm36 实验不能改 Schema 配额

- 现象：只跑 36 个 LLM 例时，`EvalReport.cases` 的 `min_length=60` 会让正常构造失败。
- 原因：P0-18 正式身份就是完整 60 例；缩短配额会让离线/在线发布报告失去机器校验。
- 最终解决：实验路径用 `EvalReport.model_construct` 落盘，并写 `experiment_scope=llm36`、`official_p018_publish=false`。数据集指纹仍按 60 例计算。
- 后续避免：不要为了实验把 `min_length` 改成 36。引用本次数字时必须标明不是正式 P0-18 发布分数。

## 2026-09-01 · 流式 TTFT 略高于 Prefill 才正常；Harness 调用次数可以比 TTFT 样本多 1

- 现象：两侧 TTFT p50 都比 Prefill p50 高约 100–120ms；Harness `model_call_count=133`，流式样本 132。
- 原因：TTFT 含鉴权代理与到首个生成 delta 的网络；Prefill 只是服务端 `prompt eval time`。少的 1 条来自失败例 `p018-exception-004`（`TOOL_BUDGET_EXHAUSTED`）计了 3 次调用、只录到 2 条流式完成。
- 最终解决：百分位只用 132 条有效流式样本；缺失不得用 Prefill 回填。把 TTFT>Prefill 的小差距当成预期，而不是旧 `progress=1.00` 那种 TTFT<Prefill。
- 后续避免：不要因为 133≠132 就用日志 Prefill 补那一条。不要把案例墙钟 1.16× 写成 TTFT 1.55×。

## 2026-09-03 · MSVC 原始字符串里 `)"` 会提前终止：JSON 测试夹具用 `R"json(...)json"`

- 现象：`stl_monitor_tests.cpp` 里用 `R"({...})"` 内嵌规约 JSON，MSVC 报出几十个 C2059/C2001 语法错误，位置都落在 `"formula":"G(battery >= 0)"` 之类的行。
- 原因：JSON 里 `G(battery >= 0)",` 含有 `)"` 序列，正好是默认分隔符原始字符串的结束标记，字符串在中途被截断。
- 最终解决：所有含公式的原始字符串改用自定义分隔符 `R"json( ... )json"`。
- 后续避免：凡是把 DSL/JSON 文本嵌进 C++ 测试，一律用带分隔符的原始字符串；语法错误密集出现在字符串字面量附近时先查 `)"`。

## 2026-09-03 · STL 第二判定层的“险胜”大多是 A* 的最优解本身

- 现象：60 例派生的 32 个合法计划里 31 个被记为险胜（鲁棒度 0）：`time_window` 20 例、`fleet_separation` 17 例。
- 原因：A* 按 release_time 预定位并恰好在 release 当刻装货（装货裕量 0）；加难地图上路径贴着货架和停放的空闲 AMR 走（间距恰好等于最小安全距离 1）。这些都是规则意义上“恰好合法”的最优解，不是 Bug。
- 最终解决：如实报告，并把 `warn_below` 放进规约文件按公式配置；`traffic_rules`/`workstation_capacity` 这类“贴边即正常”的公式设为 `null` 不统计。
- 后续避免：解读险胜数字时先看是哪条公式；要把险胜当作预防性重规划阈值前，先按现场需要调阈值，而不是改监控器语义去“降低数字”。

## 2026-09-03 · 混入布尔子式的 STL 公式鲁棒度会饱和在 1

- 现象：`fleet_separation` 在两车相距 4 格时鲁棒度也只有 1。
- 原因：`no_edge_swap`、`edge_legal` 这类布尔信号只能编码成 ±1，与 `pair_distance >= 1` 取 min 后上限就是 1。
- 最终解决：接受并在规约描述与 `P1_STL_VALIDATOR.md` 明确“饱和在 1”；一致性核对只看布尔，不受影响。
- 后续避免：需要连续裕量的约束（如“到最近他车的距离”）应单独成公式，或把布尔谓词放到独立公式里，不要与距离类谓词做同一个 `and`。

## 2026-09-03 · 两层验证的 witness 必须优先指向未满足的时刻

- 现象：早期实现按“鲁棒度最小”选最薄弱时刻，`G(x >= 0 and y > 0)` 在 `x=0` 满足、`y=0` 严格不满足时，最薄弱时刻会落在一个鲁棒度同为 0 但其实满足的点上。
- 原因：严格比较让“未满足”与“鲁棒度为 0”不再等价，单看鲁棒度选不出违反点。
- 最终解决：min/max 归约时先比较满足性（违反优先），再比鲁棒度；CTest `stl_globally_eventually` 锁定该行为。
- 后续避免：任何“证据定位”都要用布尔结论驱动，鲁棒度只用来在同类点里排序。
