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
- 后续避免：若未来要替换第三方 JSON 库，必须先补固定版本、CMake 发现方式和离线构建验证；不能直接引用 `E:\Anaconda\Library` 等个人环境路径。

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
- 最终解决：仓库验证统一显式使用 `E:\Anaconda\envs\torch128\python.exe`；可移植 smoke 允许用参数或 `AMR_*` 环境变量覆盖 Python/CMake/Ninja/MSVC 路径，并把误用环境的收集失败与产品测试结果分开记录。
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
