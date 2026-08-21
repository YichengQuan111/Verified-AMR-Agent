# 可对外表述的已验证事实

本页只收录仓库内已经实现并有实际命令/报告支撑的事实。不要把离线评测、Trace Replay、单元测试或 Smart 预留配置写成在线模型能力。

## 可以写入简历/项目介绍

- 设计并实现面向 4 台仓储 AMR 的受控 PEVR 闭环：自然语言订单 → 严格任务合同 → 本地 RAG → DAG → C++ 确定性分配/路径/验证 → Python 离散仿真 → Observation 验证 → 证据报告。
- 真实本地 Qwen3.6 Fast（alias `qwen3.6-fast`）P0-13 正常闭环正式连续实测 3 次；每次 8/8 阶段、5/5 工具、Validator 错误 0、仿真完成、`ORDER-001` 完成、4 次模型调用。
- P0-18 固定 60 例离线评测通过 60/60：类别配额为 25/10/10/5/10；六类领域覆盖均为 1.0；16 例负向安全观察均正确阻断；RAG 20 例指标为 Recall@5=1.0、MRR=1.0、Citation=1.0、ACL 泄漏=0；七项零容忍计数均为 0。
- P0-19 在同源 P0-18 Trace 上完成 Workflow、ReAct、PEVR 三策略各 60/60 预期符合；固定 Workflow/PEVR 步数均值/P95 为 5.816667/14，ReAct 为 12.15/28。
- 实现 PostgreSQL Checkpoint/Effect Ledger、有限 retry/replan/fallback/human/fatal 策略、局部重规划、JWT/RBAC/HITL 和固定验证白名单，相关失败路径与恢复测试已纳入回归。
- P0-20 已将 API、PostgreSQL、Qdrant 通过 Docker Compose 编排，并保留 Fast/Embedding/C++/仿真在 Windows 宿主机的本地边界。

## 必须同时注明的限定

- P0-18 的 60 例执行模式是 `offline_deterministic_oracle`，模型调用数为 0；它证明固定契约/安全/工具/恢复回归，不证明 60 例在线 LLM 生成质量。
- P0-19 是 `offline_trace_replay`；Token、CPU、RSS、GPU 未观测，Trace 延迟不是墙钟采样，不是在线三策略资源对照。
- Qwen3.8 Smart alias 保留但 `enabled=false`；历史 P0-05 在线验收仅 2/5，Smart 对照延期，未完成。
- P0 范围不含 ROS 2、Gazebo、真实底盘、CBS/ECBS、MILP、Redis/Celery/Kubernetes 或任意代码执行 Sandbox。
