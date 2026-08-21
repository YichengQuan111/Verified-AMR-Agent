# 可对外表述的已验证事实

本页只收录仓库内已经实现并有实际命令/报告支撑的事实。不要把离线评测、Trace Replay、单元测试或 Smart 预留配置写成在线模型能力。

## 可以写入简历/项目介绍

- 设计并实现面向 4 台仓储 AMR 的受控 PEVR 闭环：自然语言订单 → 严格任务合同 → 本地 RAG → DAG → C++ 确定性分配/路径/验证 → Python 离散仿真 → Observation 验证 → 证据报告。
- 真实本地 Qwen3.6 Fast（alias `qwen3.6-fast`）P0-13 HITL 闭环正式连续实测 3 次：`p020-release-hitl-{1,2,3}-20260821-2028`。每次先 `waiting_approval`（退出码 3），再签名批准恢复为 `completed`；8/8 阶段、5/5 工具、Validator 错误 0、仿真完成、`ORDER-001`、4 次模型调用、Approval=1、Effect=1。历史 `p014-fast-online-*` 为 Approval=0，不能再写成 HITL 发布证据。
- P0-18 固定 60 例是 `offline_deterministic_oracle` 契约回归：现已独立消费 `case.oracle`，mutation 会失败。它证明固定安全/恢复/工具门禁，不证明 60 例在线 LLM 生成质量。
- P0-19 发布口径是三种策略独立离线执行。2026-08-21 收口实测：Workflow 52/60、ReAct 53/60、PEVR 60/60。同源 Trace Replay 只用于可视化。
- 真实 RAG holdout（8 test + 4 attack）本步测得 Recall@K=1、MRR=1、citation=1、answerability=1、ACL=0；`--minimum-hybrid-score 1 --minimum-vector-score 1` 退出码 2。
- 实现 PostgreSQL Checkpoint/Effect Ledger、生产图有限 retry/replan、JWT/RBAC/HITL 和固定验证白名单；七类异常的生产图测试使用 FakeRegistry，不等于现场设备/真实 C++ 故障注入已关闭。
- P0-20 Compose 已改为必需 secrets、内部数据网络和 loopback API；正式演示视频仍缺失，不能用文字脚本替代。

## 必须同时注明的限定

- P0-18 的 60 例执行模式是 `offline_deterministic_oracle`，模型调用数为 0；修复后的 oracle 消费只提升离线回归可信度，仍不证明在线 LLM 质量。
- P0-19 默认 `offline_independent_oracle`：三种策略各自跑同一数据集，异常恢复指标应能分开；Token、CPU、RSS、GPU 未观测，Trace 延迟不是墙钟。
- Qwen3.8 Smart alias 保留但 `enabled=false`；历史 P0-05 在线验收仅 2/5，Smart 对照延期，未完成。
- P0 范围不含 ROS 2、Gazebo、真实底盘、CBS/ECBS、MILP、Redis/Celery/Kubernetes 或任意代码执行 Sandbox。
- 正式演示视频（`.mp4/.mov/.mkv/.webm/.avi`）当前为 0；在真实 HITL 闭环录制并登记 SHA-256 之前，不得把口播脚本写成 P0-20 视频交付。
