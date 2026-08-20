# P0-08 C++ Hungarian 任务分配

P0-08 提供一个独立的 `task_allocator` 静态库和 `task_allocator_cli.exe`。库只接收内存中的结构化快照；CLI 通过标准输入读取一份 JSON、通过标准输出返回一份 JSON，不读取调用方传入的路径，也不执行 Shell。后续 Python 工具适配应调用固定工作目录中的可执行文件，并自行施加超时。

## 运行入口

在已初始化 MSVC 开发环境的 PowerShell 中：

```powershell
Get-Content request.json -Raw | .\build\cpp\services\planner_cpp\task_allocator_cli.exe --algorithm hungarian
Get-Content request.json -Raw | .\build\cpp\services\planner_cpp\task_allocator_cli.exe --algorithm nearest_idle
```

`hungarian` 是生产算法，默认行为也是它；`nearest_idle` 是独立 baseline，只按每个订单到 pickup 的最近距离选择尚未占用的空闲 AMR，不调用 Hungarian，也不使用生产总代价做选择。两个算法都复用同一份确定性可行性检查，保证 baseline 不会绕过电量、连接或健康边界。

CLI 退出码：

- `0`：请求合法，`complete`、`partial` 或 `no_feasible_assignment` 都是已处理的业务结果。
- `2`：JSON、字段、枚举、数值范围或命令参数错误；stdout 仍返回稳定的 `status=error` JSON。
- `3`：未预期的内部异常。

## 请求 JSON（schema_version=`"1.0"`）

顶层字段严格为：`schema_version`、`amrs`、`orders`、`location_positions`、`completed_order_ids`、`weights`、`config`。未知字段、重复 JSON 键和缺少必填字段都会拒绝；输入大小上限为 4 MiB。

`amrs` 中的对象直接复用 P0-04 `AMRState` 字段，`position` 必须是 `{ "x": integer, "y": integer }`；`orders` 中的对象直接复用 P0-04 `TransportOrder` 字段，不能把坐标塞回订单。因为 P0-04 订单只保存 pickup/dropoff ID，本模块用 `location_positions` 提供本次环境快照，例如 `{ "P1": {"x":2,"y":3}, "S1": {"x":27,"y":3} }`。

`completed_order_ids` 表示本次滚动周期外已经完成的订单；订单依赖只有出现在这里才会被视为满足。当前批次中未完成的依赖会让对应 AMR—订单组合不可行。

订单依赖还必须引用当前请求中的已知订单、不能重复或自依赖，并且整体必须无环；C++ 入口使用稳定 Kahn 拓扑门禁复现 P0-04 的合同校验。

`weights` 是显式代价配置，五个字段都必须是有限非负数：

| 字段 | 代价含义 |
|---|---|
| `distance` | 到 pickup 的 Manhattan 栅格距离 |
| `lateness_risk` | 预计完成时间超过 deadline 的迟到比例 |
| `battery_risk` | 预警电量与预计剩余电量风险 |
| `load_penalty` | 当前 `load / maximum_load_kg` |
| `priority_bonus` | `priority / 5`，作为奖励从总代价中减去 |

`config` 必须显式提供：`current_time`、`maximum_load_kg`、`travel_speed_cells_per_second`、`energy_per_cell_percent`，以及冻结的 `battery_warning_threshold_percent`、`new_task_battery_threshold_percent`、`critical_battery_threshold_percent`、`battery_safety_reserve_percent`。项目冻结值应保持 `30 / 20 / 10 / 15`；改变它们前必须同步更新电量 SOP、Validator 和测试。

预计时间按 `max(current_time, release_time) + ceil(route_distance / speed)` 计算，其中 `route_distance` 是 AMR→pickup 加 pickup→dropoff 的 Manhattan 距离。迟到风险为 `max(0, estimated_completion - deadline) / max(1, deadline - release_time)`。P0-08 计算迟到风险但不把迟到本身伪装成 INF；P0-10 Validator 仍需对时间窗做最终硬校验。

## 响应 JSON

响应包含：

- `algorithm`：`hungarian` 或 `nearest_idle_amr`。
- `status`：全部订单匹配为 `complete`；部分匹配为 `partial`；没有任何可行匹配为 `no_feasible_assignment`。
- `assignments`：按 `order_id` 稳定排序，包含 `amr_id`、`order_id` 和完整代价分解。
- `cost_matrix`：行按字典序 `amr_id`、列按字典序 `order_id`；可行组合是数字，不可行组合是 JSON 字符串 `"INF"`。JSON 不使用非标准的 `Infinity`。
- `pair_evaluations`：每个 AMR—订单组合的 `feasible/infeasible`、代价、分解或原因。
- `unassigned_orders`：未分配订单的稳定 `reason_code` 和每台候选 AMR 的原因。
- `unassigned_amrs`、`total_cost`：未使用车辆和已分配组合的业务总代价。

主要不可行原因码如下：

| reason_code | 含义 |
|---|---|
| `amr_not_idle` | AMR 不是 `IDLE` |
| `amr_not_healthy` | 健康状态不是 `HEALTHY` |
| `amr_not_online` | 连接状态不是 `ONLINE` |
| `battery_critical` | 电量不高于 10% 危险阈值 |
| `battery_below_new_task_threshold` | 电量不高于 20%，不能接受普通新订单 |
| `completion_below_safety_reserve` | 预计完成后低于 15% 安全余量 |
| `current_load_exceeds_limit` | 当前载荷已超过配置上限 |
| `order_dependency_pending` | 订单前置依赖未完成 |
| `pickup_location_missing` / `dropoff_location_missing` | 本次位置快照缺少订单引用的工位 |

Hungarian 使用 dummy 行/列表示未匹配车辆或订单，先最大化可行匹配数量，再在可行匹配中最小化代价；`INF` 组合不会被当作真实分配返回。所有 ID 先按字典序规范化，完全相同代价使用稳定 ID 破平。

## 安全与范围边界

- 编解码器只实现本模块需要的严格 JSON 子集，拒绝未知字段、重复键、非有限数字、越界坐标和不合法枚举；字符串支持 JSON 转义及 UTF-16 surrogate pair。
- P0-08 使用 Manhattan 距离，不读取障碍/单向边，也不生成路线；P0-09 的 A* 与时空预约、P0-10 的车队验证负责实际运动合法性和冲突。
- P0-04 没有订单数量字段，因此当前负载只进入 `load_penalty`，并阻断已超过 `maximum_load_kg` 的快照；新订单物料容量约束应在后续版本扩展带版本的契约后实现。
- baseline 只用于正确性和策略对照，不能作为生产 Hungarian 结果的隐式 fallback。
