 Verified AMR Agent

面向仓储 AMR 车队的本地、受控、可验证 Agent 系统。大模型只负责理解与规划，任务分配、路径规划、计划验证与仿真全部由确定性 Python/C++ 代码完成。

![自然语言下单、HITL 审批与轨迹回放演示](docs/media/demo_v0.gif)

## 核心闭环

```text
自然语言目标
  → 受限上下文与结构化任务合同
  → RAG 证据
  → 任务 DAG
  → 确定性分配/路径
  → 双层验证器 Guardrail（规则层 + STL 规约层）
  → 离散事件仿真
  → 观测验证与局部重规划/审批
  → 返回用户
```

![Verified AMR Agent 核心闭环架构](docs/media/core_loop_architecture.png)

## 功能特性

- **自然语言下单**：本地 Qwen 3.6 35B A3B 模型把任意运输请求抽取为结构化订单，进入完整 PEVR 闭环。
- **固定 PEVR 闭环**：`guard → understand → retrieve → plan → validate → execute → verify → finish`；任何计划必须先通过确定性 Validator 才能执行。
- **C++17 确定性规划**：任务分配、A* 路径规划。
- **双层验证器 Guardrail**：C++17 车队计划验证器由规则层与 STL 规约层组成，两层独立判定、缺一不可。规则层对时间窗、电量、载荷、禁行区及顶点/边冲突做约束检查；STL 层从 JSON 规约文件加载 8 条信号时序逻辑公式，独立提取轨迹信号计算定量鲁棒度与最薄弱时刻。两层在 453 个计划、3171 次公式级判定上布尔一致率 100%。
- **Python 离散事件仿真**：固定 1 秒 tick，只执行通过验证的计划。
- **RAG 证据与拒答**：章节切块、本地 Embedding + Qdrant/BM25 混合检索、检索期 ACL、引用标注与证据不足确定性拒答。
- **安全与人工接管**：HITL 审批、工具白名单。
- **可恢复执行**：Checkpoint、局部重规划、Token/步数/时长硬预算。
- **可观测与评测**：固定 60 例评测，与 ReAct / Plan-and-Execute / PEVR 三策略在线对照。
- **演示 Web 页面**：仓库地图可视化、自然语言任意下单、轨迹回放、内存历史轨迹、一键启动本地服务。
- **本地部署**：Docker Compose（API/PostgreSQL/Qdrant）+ 宿主机本地模型脚本

## 三策略在线实验结果对比

ReAct、Plan-and-Execute 和 PEVR 分别真实执行同一套在线闭环 60 例，共 180 个 strategy-case。
三者使用相同的数据集、Qwen3.6、ToolSpec、地图、seed、权限/HITL 门禁与计分器。

| 策略 | 全例符合 | 异常终态正确 | 任务完成 | 模型调用 / Token |
|---|---:|---:|---:|---:|
| 独立 ReAct | 46/60 | 6/10 | 30/44 | 326 / 888,837 |
| Plan-and-Execute | 52/60 | 3/10 | 36/44 | 117 / 757,143 |
| PEVR | 59/60 | 9/10 | 43/44 | 132 / 841,688 |


- Plan-and-Execute → PEVR 是消融，不是两种范式：两者共用同一张八阶段图、同一套
Prompt 和同一条四任务链，唯一差别是 verify→replan 的行为。异常终态正确率 3/10 → 9/10、全例符合 52 → 59，
  代价是 +15 次模型调用与 +11% Token。这一差值就是 verify→replan 的净贡献。
- ReAct 是跨范式对照：逐步决策在异常上强于不重规划的 Plan-and-Execute，但正常例更弱，且模型调用是 PEVR 的 2.5 倍。



## Prompt Cache 对照
在同一 Qwen3.6 上，对 PEVR 在线闭环先后开、关 cache_prompt
| 指标 | 无缓存 | 有缓存 | 降低 |
|---|---:|---:|---:|
| TTFT 中位数 | 5100 ms | 3299 ms | 35.3% |
| Prefill 中位数 | 4997 ms | 3176 ms | 36.4% |
| 端到端 中位数 | 72.6 s | 62.4 s | 14.0% |
| 缓存命中率 | 0.0% | 44.6% | — |

有缓存后 TTFT/prefill 延迟降低约35.3%/36.4%，端到端时间只降低约14.0%，因为Decoding仍占大头。


## 模型对照：Spark-X2.5-4B（Q4_K_M / Q8_0）vs Qwen3.6

用同一套 PEVR 在线闭环 60 例、同一 Prompt / 地图 / seed / 工具 / 门禁，把 Fast 模型换成 Spark-X2.5-4B（alias `VeryFast`，Q4_K_M 2.6 GB / Q8_0 4.4 GB，llama.cpp b10839，RTX 5060 Ti 16GB 全 GPU）。Qwen3.6 沿用 2026-09-01 的历史报告，不重跑；属于历史系统对照，不是同期随机实验。

Spark 共做了八轮条件探索（[docs/VERYFAST_MODEL_EXPERIMENT.md](docs/VERYFAST_MODEL_EXPERIMENT.md)）。下表固定同一组参数（思考预算 2048、单次输出上限 8192、累计输入/输出预算 60000/10000），比较两种量化，以及在 Q8_0 上加入**严格合同 Schema 适配层**后的结果：

| 指标 | Qwen3.6（IQ4_NL，思考关） | Spark 4B Q4_K_M | Spark 4B Q8_0 | Spark 4B Q8_0 + 严格 Schema |
|---|---:|---:|---:|---:|
| 全例符合预期 | 59/60 | 26/60 | 26/60 | **30/60** |
| 正向任务完成 | 43/44 | 11/44 | 10/44 | **15/44** |
| 固定 36 个 LLM 案例 | 35/36 | 2/36 | 2/36 | **6/36** |
| 充电任务完成率 | 1.00 | 0.20 | 0 | **1.00** |
| 模型调用 / Token | 133 / 841,688 | 109 / 1,070,709 | 102 / 987,576 | 83 / 1,149,888 |
| LLM 案例端到端 p50 | 64.7 s | 118.4 s | 165.2 s | 156.7 s |
| 七项安全零容忍 | 全 0 | 全 0 | 全 0 | 全 0 |

- 前七轮失败几乎全部卡在第一个 LLM 节点：36 个 LLM 案例中 34 个 TaskContract 首次输出未过 Schema（典型错误「运输合同必须包含至少 1 条订单」）。Q8_0 与 Q4_K_M 同参数成绩相同，量化精度不是瓶颈；关思考（30/60）与放宽输入预算也都无效。
- 根因不是模型能力而是**约束表达的位置**：`orders` 在 `TaskContract` 的 JSON Schema 里不是 `required`，llama.cpp 由该 Schema 生成的 grammar 只强制 required 字段，而「运输合同必须至少 1 条订单」只写在 Python validator 里，解码阶段不可见，4B 模型就恰好只写那 9 个必填字段。
- 第八轮把这条规则前移到 Schema（默认关闭的适配层开关 `strict_contract_schema`，运输/充电各一个 `TaskContract` 子类，`orders` 进 `required` 且 `minItems:1`），让 grammar 在解码阶段强制它：**36 个案例中 35 个首次调用即产出合法合同**，TaskContract 修复次数从 35 次降到接近 0，充电任务全部通过。默认路径逐字节不变（`TaskContract` Schema 与 Prompt 文本的 SHA-256 由单测锁定），Qwen 基线未重跑。
- 瓶颈随之前移到 `plan_tasks`：其首次输出的 JSON 是合法的，但被确定性计划校验器拒绝后，那次「语义修复」调用只剩约 1,600 token 额度，而该模型每次要先写 4,000–5,000 字符思考，于是正文写不出来或被截断。这是下一轮的实验条件，不是本轮结论。
- 安全层不依赖模型：无论模型质量如何，碰撞、禁区、低电量、越权等零容忍项始终为 0，说明门禁是确定性的规则层与验证器兜底。

## RAG 检索评测

RAG 结果来自本地Qwen3-Embedding-0.6B、Qdrant 与 BM25 混合检索
| 指标 |  结果 | 衡量内容 |
|---|---:|---|
| Recall@K | 1.000 | 可回答问题的预期文档是否出现在 Top-K；取 K=5 |
| MRR | 1.000 | 首个相关结果排名的倒数均值 |
| nDCG@K | 1.000 | 按章节级二元相关性评价排序质量 |
| ACL 泄漏数 | 0 | 检索候选中没有出现当前角色无权访问的文档 |


## 双层验证器 Guardrail：规则层 + STL 规约层

LLM 只负责理解与规划，任何计划在派发前都必须通过 C++17 车队计划验证器；它是“LLM 不能绕过 Validator”
这一安全论证的落点。验证器由两个独立实现的判定层组成，任一层拒绝即计划 `invalid`：

```text
LLM 任务 DAG →  分配 →  A* 路径
  → 规则层：时间窗、电量、载荷、禁行区/边、工位容量 → 稳定错误码 + 定位证据
  → STL 规约层：独立提取轨迹信号 → 布尔结论 + 鲁棒度 + 最薄弱时刻
  → 两层都通过 → 离散事件仿真
```

| 层 | 判定方式 | 输出 | 作用 |
|---|---|---|---|
| 规则层 | 条件语句 | 通过/失败 + 任务、AMR、坐标、时刻、观测值 | 派发门禁，拒绝 `llm_valid`/`skip_validation` 等旁路字段 |
| STL 规约层 | 信号时序逻辑：`F[release,deadline]` 交付、`G(battery ≥ margin)`、`G ¬in_zone`、`¬pickup U dropoff`、`G(occupancy ≤ cap)`等| 每条公式的布尔结论、定量鲁棒度、最薄弱时刻 | 与规则层布尔结论不一致即 Bug；鲁棒度记录可作为 Agentic RL 奖励信号 |

两层不共享代码：STL 层重新提取位置、电量、载荷、距离和事件裕量信号，不读取规则层的中间结果，


| 指标 | 结果 |
|---|---:|
| 布尔一致  | 453/453 |
| 单次验证增量开销 | +1.2 ms（5.8 → 7.0 ms） |
 

 


## 快速开始

```powershell
# 安装锁定依赖
python -m pip install -r .\requirements.lock -r .\requirements-dev.lock

# 一键启动 API + PostgreSQL + Qdrant（需要 Docker Desktop）
.\scripts\start_local.ps1

# 启动本地LLM
.\scripts\start_local.ps1 -StartFast
```

启动后打开演示页 `http://127.0.0.1:8000/demo`：输入自然语言订单即可看到规划结果与轨迹回放。

统一回归：

```powershell
.\scripts\run_smoke.ps1
```

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | 项目完整说明：固定范围、工作包状态、架构与各子系统细节 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构图与数据流 |
| [docs/API.md](docs/API.md) | HTTP 接口契约 |
| [docs/FLEET_PLAN_VALIDATOR.md](docs/FLEET_PLAN_VALIDATOR.md) / [docs/P1_STL_VALIDATOR.md](docs/P1_STL_VALIDATOR.md) | 双层验证器 Guardrail：规则层契约与错误字典 / STL 规约层 DSL、语义与一致性核对 |
| [docs/SERVICES_STARTUP.md](docs/SERVICES_STARTUP.md) | 服务启动手册 |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | 测试与验证报告 |
| [docs/VERYFAST_MODEL_EXPERIMENT.md](docs/VERYFAST_MODEL_EXPERIMENT.md) | Spark-X2.5-4B 模型切换实验：六轮条件、结果与根因 |
| [docs/HANDOFF_CONTEXT.md](docs/HANDOFF_CONTEXT.md) | 跨会话交接上下文 |
| [docs/FILE_PURPOSES.md](docs/FILE_PURPOSES.md) | 文件职责登记表 |
