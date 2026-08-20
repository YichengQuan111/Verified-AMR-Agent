\# AMR Agent V1 Scope



\## 1. 系统目标



构建“可验证的多 AMR 仓储作业规划与工具编排 Agent 平台”。



V1 由一个受控 LLM Agent 编排 4 台差速驱动 AMR。



LLM 负责：

\- 理解自然语言订单

\- 生成结构化任务 DAG

\- 选择受控工具

\- 根据真实 Observation 处理异常

\- 触发局部重规划或人工审批



确定性程序负责：

\- AMR 任务分配

\- 路径规划

\- 多车冲突处理

\- 计划约束验证

\- 仿真状态推进



LLM 不直接控制机器人底盘，也不能宣布计划合法。





\## 2. V1 固定场景



地图：

\- 30 × 20 二维栅格

\- 每格代表 1 m



AMR：

\- 4 台

\- 同构

\- 差速驱动



仓库资源：

\- 6 个取货点

\- 6 个交付工位

\- 2 个充电站

\- 货架/障碍物

\- 单向或窄通道

\- 临时禁行区





\## 3. AMR 状态



每台 AMR 至少包含：

\- amr\_id

\- position

\- heading

\- battery

\- load

\- task\_status

\- health\_status

\- connection\_status





\## 4. 订单模型



V1 订单只支持：



pickup → transport → dropoff



订单字段至少包含：

\- order\_id

\- material\_id

\- pickup

\- dropoff

\- priority

\- release\_time

\- deadline

\- dependencies





\## 5. V1 核心异常



支持：

\- AMR 离线

\- AMR 低电量

\- 通道封闭

\- 工位占用

\- 工具超时

\- 计划不可行

\- 仿真状态与预期不一致





\## 6. V1 明确不做



模型：

\- VLM / 视觉

\- 多 LLM Agent

\- MCP

\- LoRA / SFT / Agentic RL



机器人：

\- ROS 2

\- Gazebo

\- 真实底盘

\- SLAM

\- 感知

\- 定位

\- 电机控制



规划算法：

\- CBS / ECBS

\- MILP

\- MPC

\- CBF

\- STL

\- 连续轨迹优化



平台：

\- Redis

\- Celery

\- Kubernetes

\- 完整前端

\- 任意代码执行 Sandbox





\## 7. V1 停止条件



当以下能力全部可复现时，停止继续扩大范围：



自然语言订单

→ RAG

→ 结构化任务 DAG

→ 确定性验证

→ C++ 规划

→ Python 仿真

→ Verifier

→ 局部重规划 / 审批

→ 证据报告



同时满足：

\- 正常闭环连续成功 3 次

\- 低电量、离线、封路、超时、不可行均有确定处置

\- Checkpoint 恢复不重复副作用

\- 安全违规项为 0

\- 60 例评测可一条命令执行

\- README / API / 测试评测报告 / 演示材料齐全

