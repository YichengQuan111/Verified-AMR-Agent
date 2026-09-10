# VeryFast 模型切换实验（2026-09-07）

目的：在同一台 32GB DDR5 / i5-13600K / RTX 5060 Ti 16GB 主机上，实测 Spark-X2.5-4B Q4_K_M（alias `VeryFast`）的推理配置，再执行完整 PEVR 60 例。Qwen3.6 仅沿用历史数据，不启动或重测。

## 对照与冻结条件

- 主基线：`tmp/p018_pevr_llm36_20260901/p018_online_eval.json`，虽然目录含 llm36，原报告是完整 60 例。SHA-256 `4daeaec24bf684a47b7f3a2ee80a8a7889431d17cd322335ec56fe062f98cd8c`。
- 基线实测 59/60 符合预期、正向任务 43/44、133 次模型调用；36 个 LLM 案例中 35/36。只有 `p018-exception-004` 未符合预期。
- 服务上下文 16,384 token、单并发；生产非流式；`cache_prompt=true`。使用原 Prompt 1.2.0、共享前缀、同一有限上下文结构、相同 RAG/地图/seed/工具/审批/恢复链。
- 保持网关总输出上限 4096、单次生成超时 120 秒以及原案例总时间、输入输出、工具步数与恢复预算。思考 token 计入原输出预算，不免费追加。
- 固定输入与 Prompt、seed 的历史 SHA-256 在开跑前逐项核对。全 60 例和固定 36 个 LLM case_id 分开汇总；24 条旁路不被写成模型成功率。
- Qwen 旧运行时与当前 llama.cpp、tokenizer、原生模板、思考模式不同；当前还有 P1-1 STL gate。属于历史系统配置对照，不能声称仅替换权重的同期因果实验。规则/STL 不为了对比回退。

## 思考与速度调优

官方模型卡确认 `enable_thinking` 控制思考开关；本机 GGUF 模板无命名的 effort 分档。最高可用设置是 `enable_thinking=true`、`reasoning_budget=-1`，表示不单独截断思考，仍受总输出/上下文/时间限制。不把任意 `high/max` 字符串当成已生效能力。[官方模型卡](https://huggingface.co/XHToken/Spark-X2.5-4B)

1. 模型与所有运行时 DLL 实际计算 SHA-256，记录命令行与服务返回的 16K/单槽属性。先探测结构化响应能否同时返回最终 JSON 和独立 reasoning 字段。
2. 单因素候选：全 GPU + Flash Attention 固定开启；KV f16/q8_0；batch 2048/4096，ubatch 512/1024；threads 6/14，threads-batch 14/20。不沿用 Qwen MoE 的 `--n-cpu-moe 12`。
3. 独立合成输入（本机实际 4,441 token）、固定生成 256 token，每组预热 1 次、测量 3 次，关闭缓存，记录 prompt/decode timing、显存和墙钟。投影 `prefill_ms + 4096/decode_tps*1000` 作为选择指标；5% 以内优先 f16。只是候选集合内选择，不声称穷举全机最优。
4. 选定运行参数后，用现有五节点虚构样例比较 Qwen 采样（0.1/0.95/20）与官方建议（1.0/0.95/不限 top-k，llama.cpp 用 0）。优先结构与业务检查通过数，同分优先 Qwen 采样以减少混杂。校准采用正式 4096 单请求上限。
5. 将选择写入 `selected.json` 后冻结；正式 60 例失败仍保留，不改 Prompt/数据集/预算后挑最好一轮。

## 执行与证据

本机路径见 `LOCAL_ENV.md`。设置 `VERYFAST_MODEL_PATH` 与 `LLAMA_SERVER_PATH` 后执行：

```powershell
python -u -m evals.perf.veryfast tune --output tmp/veryfast_20260907
python -u -m evals.perf.veryfast run --output tmp/veryfast_20260907
python -m evals.perf.veryfast summarize --output tmp/veryfast_20260907
```

VeryFast 独立使用后端 `127.0.0.1:18081`；正式 PEVR 经过原 Bearer 代理的 `127.0.0.1:8081` 实例。每阶段只回收自身子进程。Fast 默认入口、Smart 禁用、原历史目录不变。

输出为 `artifact_manifest.json`、`tuning/`、`selected.json`、`pevr60/`、`comparison.json`。每次请求记录成功/失败、usage、实际思考字符数、服务 timings、最终输出和客户端 E2E；不保存思考正文或凭据。生产非流式没有真实 TTFT，保持 null。

验收按 60 例完整性、预期终态符合率、43/44 口径的正向任务完成、恢复终态、七项零容忍分别报告。既有 harness `status=passed` 只表示运行完整且安全零容忍通过，不能解释为所有案例成功。延迟报告全体与两模型共同成功的 LLM 配对，避免快速失败抬高速度。

用户执行中确认：严格沿用 Qwen 的输出与时间预算，预算耗尽也计失败。

## 实测结果（2026-09-07，`tmp/veryfast_20260907`）

- 制品：模型 SHA-256 `dc08c219…633726a`，llama.cpp b10839；VeryFast 报告 `pevr60/p018_online_eval.json` SHA-256 `aba55951…f96ef365`；对比文件 `comparison.json`。固定输入/seed/Prompt/工具版本与 Qwen 基线逐项一致。
- 调参：5 组候选投影值 39.9–41.0 秒，差距在 5% 内；选中 `f16_b2048_u512_t14`（prefill ≈921 ms/4.4K token，decode ≈105 tok/s）。采样按用户指定 0.1/0.95/top_k 0。五节点校准 2/5 通过（verify_observation、compose_report），其余三节点为空响应。
- 60 例结果：VeryFast 25/60 符合预期，正向任务 10/44；Qwen 基线 59/60、43/44。固定 36 个 LLM 案例 VeryFast 仅 `p018-security-001` 通过（1/36），Qwen 35/36。通过的 25 例集中在 security 10、rag 8、verification 5、exception 2，主要是规则/门禁旁路。
- 根因：36 次模型调用全部 `finish_reason=length`、completion_tokens 均值 3996（上限 4096）、content 全部为空，思考正文均值 9,294 字符、最长 16,287 字符。模型把 4096 输出预算全部耗在思考上，从未输出最终 JSON，每个 LLM 案例在第一次调用即 `MODEL_EMPTY_RESPONSE` 终止。harness `status=passed` 只表示运行完整、七项零容忍为 0，不代表案例成功。
- 延迟：LLM 案例墙钟 p50 44.1 秒、p95 44.6 秒（都是跑满 4096 token 的失败时间），不能与 Qwen 的 64.7/69.2 秒作速度比较；共同成功配对仅 1 例，无统计意义。
- 结论：在冻结的相同预算下（4096 输出、思考计入预算、120 秒），Spark-X2.5-4B 无法完成本工作流。若要继续评估，需要新的实验条件（例如关闭思考或设置有限 `reasoning_budget`），且必须作为另一次独立实验记录，不得改动本次结果。

## 第二轮：关闭思考（2026-09-07，`tmp/veryfast_nothink_20260907`）

独立实验，配置 `config/veryfast_nothink_experiment.json`（`reasoning_enabled=false`，llama-server `--reasoning off`，与 Qwen 基线一致）；其余候选、采样 0.1/0.95/top_k 0、4096/120 秒与累计预算全部不变。入口 `python -m evals.perf.veryfast tune|probe1|run --config config/veryfast_nothink_experiment.json --output tmp/veryfast_nothink_20260907`，`probe1` 为新增的单例试探阶段（只缩小执行列表，不改数据集指纹，不计入 60 例）。

- 调参：仍选 `f16_b2048_u512_t14`；五节点校准 5/5 通过（首轮 2/5），单节点 1.8–7.9 秒。
- probe1（p018-normal-001）：4 次调用全部 `stop`，输出 782–1284 token，思考 0，输出预算未爆；但 understand_goal/plan_tasks 各触发一次 Schema 修复，累计输入 31,725 token 超过合同 30,000 上限，案例 `recovery_fatal`。驱动按「输出预算未爆」条件继续 60 例。
- 60 例：VeryFast 30/60 符合预期、正向任务 15/44；固定 36 个 LLM 案例通过 6/36（normal-021 至 025 充电场景 + security-001），Qwen 为 59/60、43/44、35/36。七项零容忍全部为 0，charging_completion_rate 1.0，normal_order_completion_rate 0。报告 SHA-256 `c4e5515a…4228bb6d8`。
- 失败码：30 例全部 `recovery_fatal`，其中 27 例 `ACTUAL_INPUT_TOKEN_BUDGET_EXCEEDED`、3 例 `MODEL_SCHEMA_VALIDATION_FAILED`（PlanTasksOutput JSON 在字符串中途截断，对应 4 次 `finish_reason=length` 中的 3 次，剩余输出额度 1,991–2,496）。失败集中在 normal 20、exception 8、rag 2。
- 根因：141 次请求中 40 次是 Schema 修复重试（TaskContract 35、PlanTasksOutput 5），首次结构化输出通过率低；修复把上一次输出塞回上下文，prompt 均值 7,621、最高 11,556 token，四步链路累计输入很快越过 30,000。Qwen 同案例四节点各一次调用、累计约 23,500。
- 延迟：共同成功配对 6 例（5 例充电 + 1 例 security），VeryFast 均值 26.6 秒对 Qwen 32.1 秒；样本太少，只能说「不比 Qwen 慢」。
- 结论：关思考解决了输出预算问题，但模型在四步 PEVR 的结构化 JSON 质量不足，靠一次修复配额撑不过累计输入预算。若继续，需要另开实验讨论放宽输入预算或修复策略，且这属于改变实验条件，不能与本轮结果合并。

## 第三轮：开思考 + 放宽累计输入预算（2026-09-07，`tmp/veryfast_r3_20260907`，未跑 60 例）

配置 `config/veryfast_r3_experiment.json`：`reasoning_enabled=true`、`reasoning_budget_tokens=-1`，`entry_budget_overrides.max_input_tokens=60000`（脚本在实验进程内临时覆盖 `PEVRGraphRunner.ENTRY_BUDGETS`，写入 understand_goal 的 fixed_execution_defaults 后由模型回写到合同；生产 `SHARED_ENTRY_BUDGETS` 不变）。网关 4096 输出上限与 120 秒不变。

- 调参：仍选 `f16_b2048_u512_t14`；五节点校准 2/5，与首轮相同。
- probe1（p018-normal-001）：1 次调用即 `finish_reason=length`，completion 4096、思考 9,172 字符、最终内容为空，`MODEL_EMPTY_RESPONSE`。输入预算未被触及。
- 驱动按「输出预算已爆」跳过 60 例：结果会与首轮逐字重复，放宽输入预算对该失败点无效。若要继续，需同时改输出侧条件（提高 4096 上限或设置有限 `reasoning_budget`），另开一轮记录。

## 第四轮（已中止）与第五轮：有限思考 + 放宽输出/输入预算（2026-09-07）

第四轮 `config/veryfast_r4_experiment.json`（思考 2048、网关/申请输出 8192、累计输出 10000、累计输入 60000）在调参完成、单例试探进行中被用户停止，目录 `tmp/veryfast_r4_20260907_stopped`；其五节点校准 5/5 通过是唯一可用信息。

第五轮 `config/veryfast_r5_experiment.json`：思考预算改为 4096，其余同第四轮。脚本新增 `max_output_tokens`/`requested_output_tokens` 配置项，在线 harness 增加可选 `requested_output_tokens` 参数（默认 None，不影响 P0-18/P0-19 路径）。目录 `tmp/veryfast_r5_20260907`，报告 SHA-256 `4ae923b8…c0104cd`。

- 调参：五节点校准 0/5，因校准环节固定申请 4096 输出，被 4096 思考占满；该口径不适用于正式链路，仅记录。
- probe1：2 次调用均 `stop`，首次输出 5279（思考约 4096 + 正文），修复调用剩余 2913 额度，仍 `MODEL_SCHEMA_VALIDATION_FAILED`。输出预算未爆，按门槛进入 60 例。
- 60 例：25/60 符合预期、正向任务 10/44、36 个 LLM 案例 1/36（仅 security-001），与首轮持平。七项零容忍为 0。通过的 25 例仍是规则旁路。
- 失败码：MODEL_EMPTY_RESPONSE 19、MODEL_SCHEMA_VALIDATION_FAILED 8、recovery_fatal 8（后者内层也是空响应）。80 次请求中 29 次 `length`，全部发生在修复调用。
- 机制：36 个 LLM 案例的 understand_goal 首次调用全部正常结束（约 5,250 token），但 TaskContract 34 次触发修复，校验错误集中在「运输合同必须包含至少 1 条订单」。修复调用与首次共享单次 8192 额度，只剩约 2,900，而思考预算 4096 大于剩余额度，修复调用把额度全耗在思考上后 `length` 截断、正文为空。没有一例越过 understand_goal。
- 延迟：LLM 案例 p50 89.6 秒、p95 112.6 秒，均为失败路径耗时；配对成功仅 1 例。
- 结论：思考开到 4096 后，瓶颈不在预算而在结构化输出质量（合同漏写订单）以及修复调用无法容纳一次完整思考。若继续，可选方向是修复调用单独配额或降低思考预算（第四轮 2048 校准 5/5，但未跑 60 例），均属新实验条件。

### 五轮汇总

| 轮次 | 思考 | 单次输出上限 | 累计输入/输出 | 60 例 | 正向 | LLM36 | 主失败码 |
|---|---|---|---|---|---|---|---|
| Qwen3.6 基线 | 关 | 4096 | 30000/5000 | 59 | 43/44 | 35/36 | — |
| R1 `tmp/veryfast_20260907` | 开，不截断 | 4096 | 30000/5000 | 25 | 10/44 | 1/36 | MODEL_EMPTY_RESPONSE（思考吃满 4096） |
| R2 `tmp/veryfast_nothink_20260907` | 关 | 4096 | 30000/5000 | 30 | 15/44 | 6/36 | ACTUAL_INPUT_TOKEN_BUDGET_EXCEEDED（修复重试） |
| R3 `tmp/veryfast_r3_20260907` | 开，不截断 | 4096 | 60000/5000 | 未跑 | — | — | probe1 同 R1，跳过 |
| R4 `_stopped` | 开 2048 | 8192 | 60000/10000 | 用户中止 | — | — | 校准 5/5 |
| R5 `tmp/veryfast_r5_20260907` | 开 4096 | 8192 | 60000/10000 | 25 | 10/44 | 1/36 | 合同缺订单 → 修复调用被思考占满 |
| R6 `tmp/veryfast_r6_20260908` | 开 2048 | 8192 | 60000/10000 | 26 | 11/44 | 2/36 | 合同缺订单，12 例到 plan_tasks |
| R7 `tmp/veryfast_r7_20260909`（Q8_0） | 开 2048 | 8192 | 60000/10000 | 26 | 10/44 | 2/36 | 同 R6；解码慢致 2 次超时，7 例到 plan_tasks |

## 第六轮：思考 2048 + 放宽输出/输入预算（2026-09-08，`tmp/veryfast_r6_20260908`）

配置 `config/veryfast_r6_experiment.json`，与第四轮条件相同（思考 2048、网关/申请输出 8192、累计输入 60000、累计输出 10000），本轮不设单例门槛直接跑完 60 例。报告 SHA-256 `aae0eaba…8cb40fd8`。

- 调参：五节点校准 4/5（understand_goal 空响应；第四轮同配置 5/5，0.1 温度下有抖动）。
- probe1：2 次调用均 `stop`，3198 / 3217 token，仍因「运输合同必须包含至少 1 条订单」两次校验失败。
- 60 例：26/60 符合预期、正向任务 11/44、36 个 LLM 案例 2/36（normal-021 充电、security-001）。七项零容忍为 0，充电完成率 0.2，普通订单完成率 0。
- 失败码：recovery_fatal 20（内层 13 例空响应、7 例 Schema）、MODEL_SCHEMA_VALIDATION_FAILED 12、MODEL_EMPTY_RESPONSE 2。失败集中 normal 24、exception 8、rag 2。
- 请求：109 次，`stop` 86、`length` 23（全部在修复调用）；completion 均值 2,702、prompt 均值 7,121、思考均值 4,819 字符；总 Token 1,070,709（Qwen 基线 841,688）。TaskContract 修复 34 次、PlanTasksOutput 修复 8 次；仅 12 例走到 plan_tasks，3 例到 verify_observation，1 例到 compose_report。
- 延迟：LLM 案例 p50 118.4 秒、p95 121.2 秒；共同成功配对 2 例，VeryFast 51.4 秒对 Qwen 23.5 秒。
- 结论：六轮中最好的是关思考的第二轮（30/60），本轮 26/60 与其相近；有限思考让更多案例越过 understand_goal（12 例到 plan_tasks），但换来更长耗时与更多 Token。根因一致：4B 模型对 TaskContract/PlanTasksOutput 的结构化 JSON 首次通过率低，一次修复配额不足以纠正。本轮结果已写入 README「模型对照」一节。

## 第七轮：Q8_0 量化，参数同第六轮（2026-09-09，`tmp/veryfast_r7_20260909`）

用户认为 Q4 量化损失对小模型致命，换用 `Spark-X2.5-4B-Q8_0.gguf`（4.4 GB）。配置 `config/veryfast_r7_experiment.json`：思考 2048、网关/申请输出 8192、累计输入 60000 / 输出 10000，与第六轮完全一致；脚本量化标识改为读配置。报告 SHA-256 `e1dece4e…ef019962`。

- 调参：解码约 68 tok/s（Q4 约 105），prefill 约 900 ms/4.4K，选 `f16_b2048_u512_t6`；五节点校准 3/5（understand_goal Schema 失败、replan 空响应）。
- probe1：4 次调用，understand_goal 经修复通过，plan_tasks 修复调用剩余 1255 额度被思考占满后截断，`recovery_fatal`，耗时 177 秒。
- 60 例：26/60、正向任务 10/44、36 个 LLM 案例 2/36（rag-010、security-001），与 Q4 第六轮的 26/60、11/44、2/36 基本相同；充电完成率 0（Q4 为 0.2）。七项零容忍为 0。
- 失败码：recovery_fatal 16、MODEL_SCHEMA_VALIDATION_FAILED 12、MODEL_EMPTY_RESPONSE 4、TIME_BUDGET_EXCEEDED 2。TaskContract 修复 35 次、PlanTasksOutput 修复 10 次；仅 7 例到 plan_tasks（Q4 为 12 例）。
- 请求：102 次，2 次 `APITimeoutError`（Q8 解码慢，8192 输出在 120 秒内跑不完），其余 `stop` 79、`length` 21；总 Token 987,576。
- 延迟：LLM 案例 p50 165.2 秒、p95 182.3 秒（Q4 第六轮 118.4 / 121.2）；配对成功 2 例，VeryFast 90.6 秒对 Qwen 27.1 秒。
- 结论：Q8 与 Q4 在同参数下成绩相同，量化精度不是瓶颈；Q8 反而因解码慢触发生成超时和时间预算，越过第一节点的案例更少。模型本身对 TaskContract 结构化输出的能力不足是主因。

## 第八轮：严格 Schema 适配层（2026-09-09，`tmp/veryfast_r8_20260909`）

第七轮定位到最大单一失败原因：understand_goal 输出的 TaskContract 缺 `orders`。根因是 `orders`/`charging` 在 `TaskContract` 的 JSON Schema 里都不是 `required`，llama.cpp 由该 Schema 生成的 grammar 只强制 required 字段，模型就恰好只写这 9 个字段；「运输合同必须包含至少 1 条订单」只存在于 Python `@model_validator`，在解码阶段不可见。本轮把这条规则前移到 Schema 与 Prompt 正文，作为**默认关闭的小模型适配层**：`ModelProfileSettings.strict_contract_schema=true` 时，`SharedPrefixService.understand` 按 harness 的 `injected_charging` 选择 `TransportContract`（`orders` 进 required 且 `minItems:1`，`charging` 为 `type:null`）或 `ChargingContract`（`charging` 进 required，`orders` 为 `maxItems:0`），并在 system 文本末尾追加一段显式规则。配置 `config/veryfast_r8_experiment.json` 除 `experiment_id` 与新开关外与 r7 逐字段相同（Q8_0、思考 2048、输出 8192、入口 60000/10000），**唯一变量就是严格 Schema**。报告 SHA-256 `39a78f55…c01a4162e0`；`same_inputs` 九项全 True（含 `prompt_files`/`prompt_versions`），默认路径的 `TaskContract` Schema 与 understand_goal 渲染文本 SHA-256 未变，Qwen 基线未重跑。

- 调参：选 `f16_b2048_u512_t14`（投影 56.9s/4096 token）；五节点校准 4/5（plan_tasks 空响应）。校准走默认路径，不受适配层影响。
- probe1：3 次调用，**第 1 次即输出含 1 条 `orders` 的合法合同、`finish_reason=stop`、零次 Schema 修复**；失败点前移到 plan_tasks（第 3 次调用 `length` 截断，`recovery_fatal`）。
- 60 例：**30/60**（r7 26/60），正向任务 **15/44**（r7 10/44），36 个 LLM 案例 **6/36**（r7 2/36；通过 normal-021～025 五个充电例与 security-001）。充电完成率 **1.0**（r7 为 0，Q4 第六轮 0.2），普通订单完成率 0.0。七项零容忍全为 0，注入拦截率、越权拦截率、可答性准确率均 1.0。
- 适配层直接效果：36 个有模型调用的案例中，**35 例首次调用就产出合法 TaskContract**（30 份带非空 `orders` 的运输合同、5 份充电合同），TaskContract 修复次数从 r7 的 35 次降到接近 0；`orders` 缺失导致的合同校验失败在本轮报告中消失。
- 失败码：`recovery_fatal` 21、`MODEL_EMPTY_RESPONSE` 8、`recovery_fallback` 1。失败集中在 normal 20、exception 8、rag 2，全部止步于 `plan` 节点（trace 事件 `node plan failed`）。
- 请求：113 次，`stop` 83、`length` 30；completion 均值 3,005、prompt 均值 7,171、思考均值 5,136 字符；总 Token 1,149,888。
- 延迟：LLM 案例 p50 **156.7** 秒、p95 165.2 秒（r7 165.2 / 182.3）；共同成功配对 **6** 例（r7 仅 2 例），VeryFast p50 127.5 秒对 Qwen 35.1 秒。

### 失败机制（逐调用核对，不是「输出体积大被截断」）

30 个失败例几乎共用同一条链，以 `p018-normal-001` 为例：

| 调用 | 节点 | 申请 max_tokens | 实际 completion | 思考字符 | 结果 |
|---|---|---|---|---|---|
| 1 | understand_goal | 8192 | 3,678 | 4,237 | `stop`，合法合同 |
| 2 | plan_tasks 首次 | 6,322 | 4,719 | 5,391 | `stop`，**PlanTasksOutput 校验通过、4 个任务** |
| 3 | plan_tasks 语义修复 | **1,603** | 1,603 撞顶 | 3,947 | `length`，**正文 0 字节** |

1. **plan_tasks 的首次输出是合法的**。离线用 `PlanTasksOutput.model_validate` 复验本轮全部含 `tasks` 的输出：30 份里 28 份通过，仅 2 份因 `tool_arguments` 多写 `pickup`/`dropoff` 未授权参数被拒。失败不发生在 Pydantic 这一层。
2. **拒绝发生在确定性校验器**。`PEVRGraphRunner._plan_node` 在 `canonicalize_normal_pevr_plan` 之后调用 `validate_normal_pevr_plan`（工具链顺序、`data_ref`、参数白名单、seed）；不通过时按设计再发起**一次**「语义修复」调用，把确定性错误逐条反馈。
3. **语义修复调用被饿死**。该调用的 `requested_output_tokens` 来自剩余累计预算：入口 `max_output_tokens=10000`，前两次已用掉 3,678 + 4,719 = 8,397，只剩约 1,600。而该模型每次开口前稳定要写 4,000–5,000 字符思考，额度全被思考吃光——8 例正文 0 字节（`MODEL_EMPTY_RESPONSE`），其余写到一半被截断（`Invalid JSON: EOF while parsing…`，报告里的 `after 1 attempt(s)` 说明 provider 因 `remaining_output_tokens <= 0` 直接 break，Schema 修复配额根本没用上）。

铁证是 max_tokens 的分布：**30 次 `length` 调用的申请值全部落在 142–3,212**，而 83 次 `stop` 调用多为 8,192 或 6,300+。截断从不发生在首次调用，只发生在第二次及以后。

另有一项与本适配层无关的发现：配置 `reasoning_budget_tokens=2048`，但实测思考字符 p50 4,704、max 8,329，明显超出，`--reasoning-budget` 对该模型没有形成硬截断（与模板 `supports_reasoning_effort=false` 一致）。思考在每次调用里稳定占掉 50–80% 的输出额度。

- 结论：把业务规则写进 JSON Schema 让 grammar 在解码阶段强制，对 4B 模型是有效的——understand_goal 从「最大失败源」变成「几乎不失败」，整体从 26/60 提高到 30/60、正向任务从 10/44 提高到 15/44，且全部充电例通过。但失败点被推到了预算更紧张的位置：以前案例在第 1 次调用就死了，现在活到第 3 次，而第 3 次结构性地饿死——26 例走到 plan_tasks（r7 仅 7 例）是进步，同时也让「语义修复调用没有额度」从偶发变成必然。充电例全过、普通订单完成率 0，差别就在计划体积（充电两次调用即可收敛，运输 4 任务首次生成就要 4,700 token）。下一步该动的不是 `orders`，而是以下之一，且都属于新的实验条件：关闭思考（历史最好成绩 30/60 正是第二轮无思考）、给语义修复调用独立的输出额度、或拆小 `PlanTasksOutput`。不要在本轮条件里改预算。
