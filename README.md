 Verified AMR Agent

面向仓储 AMR 车队的本地、受控、可验证 Agent 系统。大模型只负责理解与规划，任务分配、路径规划、计划验证与仿真全部由确定性 Python/C++ 代码完成。

![自然语言下单、HITL 审批与轨迹回放演示](docs/media/demo_v0.gif)

## 核心闭环

```text
自然语言目标
  → 受限上下文与结构化任务合同
  → RAG 证据
  → 任务 DAG
  → 确定性分配/路径/验证
  → 离散事件仿真
  → 观测验证与局部重规划/审批
  → 返回用户
```

![Verified AMR Agent 核心闭环架构](docs/media/core_loop_architecture.png)

## 功能特性

- **自然语言下单**：本地 Qwen 3.6 35B A3B 模型把任意运输请求抽取为结构化订单，进入完整 PEVR 闭环。
- **固定 PEVR 闭环**：`guard → understand → retrieve → plan → validate → execute → verify → finish`；任何计划必须先通过确定性 Validator 才能执行。
- **C++17 确定性规划**：A* 路径规划、物理安全验证器。
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

两条结论要分开读：

- **Plan-and-Execute → PEVR 是消融，不是两种范式**：两者共用同一张八阶段图、同一套
Prompt 和同一条四任务链，唯一差别是 verify→replan 的行为。异常终态正确率 **3/10 → 9/10**、全例符合 **52 → 59**，
  代价是 +15 次模型调用与 +11% Token。这一差值就是 verify→replan 的净贡献。
- **ReAct 是跨范式对照**：逐步决策在异常上强于不重规划的 Plan-and-Execute，但正常例更弱，且模型调用是 PEVR 的 2.5 倍。



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
| [docs/SERVICES_STARTUP.md](docs/SERVICES_STARTUP.md) | 服务启动手册 |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | 测试与验证报告 |
| [docs/HANDOFF_CONTEXT.md](docs/HANDOFF_CONTEXT.md) | 跨会话交接上下文 |
| [docs/FILE_PURPOSES.md](docs/FILE_PURPOSES.md) | 文件职责登记表 |
