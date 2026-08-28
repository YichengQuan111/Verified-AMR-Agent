# Verified AMR Agent

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
- **可观测与评测**：固定 60 例评测与 Workflow/ReAct/PEVR 三策略对照。
- **演示 Web 页面**：仓库地图可视化、自然语言任意下单、轨迹回放、内存历史轨迹、一键启动本地服务。
- **本地部署**：Docker Compose（API/PostgreSQL/Qdrant）+ 宿主机本地模型脚本

## 三策略在线实验结果对比

固定 Workflow、有界 ReAct 和 PEVR 分别真实执行同一套在线闭环 60 例，共 180 个。三者使用相同的数据集、Qwen3.6、Prompt、ToolSpec、地图、seed、
权限/HITL 门禁与计分器；完整实验口径见[P0-19 三策略完整在线闭环对照](docs/P019_STRATEGY_COMPARISON.md)。

| 策略 | 全例符合 | 异常终态正确 | 模型调用 / Token |
|---|---:|---:|---:|
| 固定 Workflow | 53/60 | 3/10 | 118 / 763,108 |
| 有界 ReAct | 54/60 | 4/10 | 123 / 783,192 |
| PEVR | 59/60 | 9/10 | 132 / 842,143 |

### 总体对比

PEVR 的主要收益集中在异常恢复，而不是通过放宽安全约束换取完成率。相较固定 Workflow，
PEVR异常终态多正确 6 例；代价是增加 14 次模型调用和 79,035 Token。相较有界 ReAct，PEVR 异常终态多正确 5 例，
增加 9 次模型调用和 58,951 Token。安全违规次数均为 0。

固定 Workflow 遇到异常后直接终止；本项目的有界 ReAct 只允许做一次
`retry`，不允许 `replan`；PEVR 则能按故障类型在有限预算内选择重规划。

### PEVR 异常恢复案例

1. **初始AMR不可用**：路径规划阶段发现“被选择的AMR 电量低于新任务阈值”或者“被选择的AMR已经离线”。固定Workflow 和有界 ReAct 都失败；PEVR生成新计划、重新校验并完成任务。
2. **部分计划不可行**：分配步骤已经完成后，路线
   被物理安全验证工具判不可行。固定 Workflow 和有界 ReAct 无法替换后续计划，均失败；PEVR 保留已完成分配结果，只重新规划路径后完成，避免重复执行已完成
   副作用。
3. **工具使用超时**：调用检索工具时，等待返回结果超时。由于该工具幂等且无副作用，ReAct 和 PEVR 通过安全检查后各重试 1 次并成功完成任务；固定 Workflow 不执行重试，因此直接失败。
4. **工位持续占用**：由于工位被持续占用，PEVR一直Replan，直到超过重规划预算时目标工位仍然不可用。这是本轮唯一异常终态不正确的用例，也是 PEVR 异常恢复为 9/10 而不是 10/10 的原因。

## RAG 检索评测

RAG 结果来自本地Qwen3-Embedding-0.6B、Qdrant 与 BM25 混合检索
| 指标 |  结果 | 衡量内容 |
|---|---:|---|
| Recall@K | 1.000 | 可回答问题的预期文档是否出现在 Top-K；取 K=5 |
| MRR | 1.000 | 首个相关结果排名的倒数均值 |
| Precision@K | 0.236 | 每个可回答问题在 Top-K 中首次命中的唯一相关章节数除以 K，再对问题宏平均 |
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
