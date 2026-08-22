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

## 2026-08-21 · Hidden 启动器空等 /health 不等于模型在加载

- 现象：终端只打印“等待 /health”就回到提示符；任务管理器没有 llama-server。
- 原因：父脚本 `Start-Process -WindowStyle Hidden` 后，子进程卡住或立刻失败都看不见；Ctrl+C 只停父进程，隐藏子进程可能还在。`Start-Process -ArgumentList` 给 `--model` 再套一层引号也会让 llama-server 起不来。
- 解决：最小化窗口、写 `tmp/fast_secure.transcript.log`，子进程一退出就把日志尾抛给父脚本。
- 避免：不要在没看到 `llama-server.exe` 之前把空等当成“正在加载”。

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






