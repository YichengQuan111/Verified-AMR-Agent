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
| [docs/HANDOFF_CONTEXT.md](docs/HANDOFF_CONTEXT.md) | 跨会话交接上下文 |
| [docs/FILE_PURPOSES.md](docs/FILE_PURPOSES.md) | 文件职责登记表 |
