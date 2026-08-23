# P0-10 C++ 车队计划验证器

`fleet_plan_validator` 是 P0-10 的确定性最终门禁。它接收完整的地图快照、P0-04
的 AMR/订单快照和 P0-09 输出的离散路径，重新计算并验证约束；不会读取
`environment_ref` 指向的文件，也不会信任 P0-08 的 Manhattan 代价、P0-09 的
`status` 或任何 LLM/Prompt 声明。

## 构建与入口

在已初始化 MSVC 开发环境的 PowerShell 中：

```powershell
cmake -S . -B build\cpp -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_MAKE_PROGRAM=E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe `
  -DBUILD_TESTING=ON
cmake --build build\cpp
ctest --test-dir build\cpp -R "^fleet_validator_" --output-on-failure
```

可执行文件为 `build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe`：

```powershell
Get-Content fleet_plan_request.json -Raw |
  .\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --validate
.\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --error-dictionary
```

CLI 的 stdin 上限为 4 MiB，使用 P0-08 的严格 JSON 子集：重复键、未知字段、非
有限数字、错误类型和缺失字段在解析阶段拒绝。业务上不合法的计划不是进程异常，
而是 stdout 返回 `status="invalid"` 的结构化结果；调用方必须检查 `valid=true`、
`status="valid"`、`errors=[]` 和 `ruleset_version="p0-10.v1"`。

退出码：

- `0`：请求已经确定性处理，计划可能 `valid`，也可能 `invalid`。
- `2`：JSON、字段、参数或契约错误；stdout 返回 `status="error"`。
- `3`：未预期内部异常。

## 请求契约（schema_version=`"1.0"`）

顶层必填字段为：

`schema_version`、`environment_ref`、`map_width`、`map_height`、`blocked_cells`、
`blocked_edges`、`one_way_edges`、`amrs`、`orders`、`location_positions`、
`completed_order_ids`、`routes`、`start_time`、`max_time`、`config`、
`workstation_capacities`。

`ruleset_version` 可选；缺省为 `p0-10.v1`，填写其他版本会使结果为
`invalid_config`。所有坐标使用 P0-04 的 `{ "x": integer, "y": integer }`，
所有路径时间为离散整数秒。

`amrs` 和 `orders` 的对象字段直接复用 P0-04。`location_positions` 是本次请求的
工位位置快照，例如 `{ "P1": {"x":2,"y":3} }`；`environment_ref` 只作审计
标识，不触发隐式文件读取。

`routes` 每个对象至少包含：

```json
{
  "amr_id": "AMR-01",
  "order_id": "ORDER-01",
  "payload_kg": 12.5,
  "pickup_time": 7,
  "dropoff_time": 31,
  "path": [
    {"position":{"x":1,"y":2},"heading":90,"time":0,"action":"start","g_cost":0.0}
  ]
}
```

`path` 的每个元素必须包含 `position`、`heading`、`time`、`action`、`g_cost`；
`action` 只能是 `start`、`move`、`turn_left`、`turn_right`、`wait`。路线首元素
必须等于 AMR 的初始位置/朝向和 `start_time`，之后每一步恰好前进一个离散时刻。
`payload_kg` 是 P0-04 `TransportOrder` 没有携带的执行期物料重量，不能省略或由
模型猜测。

路线可以带 P0-09 的 `status`、`reason_code`、`reason`、`priority`、`total_cost`、
`expanded_states` 审计字段。它们只验证类型并记录 `route_not_planned` 证据，不能
代替 Validator 的重新检查；任何 `llm_valid`、`skip_validation`、`approved` 或
任意未声明字段都会被拒绝。

`config` 必填字段为：

| 字段 | 语义 | 安全边界 |
|---|---|---|
| `maximum_load_kg` | AMR 最大载荷 | 有限正数 |
| `energy_per_cell_percent` | 每个 `move` 消耗的电量百分比 | 有限非负数 |
| `battery_safety_reserve_percent` | 路线结束最低安全余量 | 0～100 |
| `new_task_battery_threshold_percent` | 普通新任务电量门槛 | 0～100 |
| `critical_battery_threshold_percent` | 临界电量门槛 | 0～100，不能高于新任务门槛 |
| `minimum_safety_distance_cells` | 两车最小曼哈顿距离 | 非负整数；相同顶点另由 `vertex_conflict` 检查 |
| `default_workstation_capacity` | 未单独列出的工位容量 | 正整数 |

项目冻结的电量规则为 `30 / 20 / 10 / 15`；P0-10 使用其中的普通新任务门槛、
临界门槛和安全余量，即默认 `20 / 10 / 15`。`workstation_capacities` 是工位
ID 到正整数容量的覆盖表；同一工位同一离散服务时刻的 pickup/dropoff 事件数超过
容量时报告 `workstation_capacity_exceeded`。未覆盖的工位使用
`default_workstation_capacity`。

## 确定性检查

验证顺序固定为：配置/地图快照 → AMR/订单身份 → 依赖 DAG → 路线几何和时间窗 →
载荷/电量 → 工位容量 → 全车队时空冲突。所有错误最后按错误码、时间、AMR、任务
和坐标稳定排序，因此同一请求的 JSON 字节序列可复现。

- 任务依赖：未知依赖、重复/自依赖、环、未规划前置任务，以及前置订单
  `dropoff_time > 当前订单 pickup_time` 均拒绝。`completed_order_ids` 中的外部订单
 视为已完成。
- 时间窗：装货事件时刻为 ``route.pickup_time``，且该时刻路径必须停在 pickup
  工位。允许 ``t < release_time`` 先踏上 pickup 再等待（与 P0-09 A* 预定位一致）；
  装卸事件本身不得早于 `release_time`。路线终点必须是 dropoff，且 `dropoff_time`
  不得晚于 `deadline`。声明的 pickup/dropoff 时间必须与路径上对应事件时刻完全相等。
- 载荷：初始载荷不得超过上限，pickup 后按
  `amr.load + route.payload_kg` 再检查一次；dropoff 后的卸载不作为绕过 pickup
  硬检查的理由。
- 电量：只按路径中的 `move` 动作计算消耗：
  `remaining = initial_battery - move_count * energy_per_cell_percent`。
  路线结束低于安全余量拒绝；初始电量不高于临界或普通新任务门槛也会给出稳定
  证据，不能通过改写 Prompt 忽略。
- 禁行区和路径：检查地图边界、初始/工位/路径禁行格、禁行有向边、单向边、
  首状态、动作—位置—朝向一致性、连续时间和累计代价。
- 工位容量：按工位 ID 和离散事件时刻聚合 pickup/dropoff 服务事件，输出超限的
  两个任务、两个 AMR、工位坐标、时间、观察数量和容量上限。
- 安全距离：使用栅格曼哈顿距离；当 `distance < minimum_safety_distance_cells`
  时报告 `safety_distance_breached`。同格占用独立报告 `vertex_conflict`，不把
  两类约束合并成一个模糊错误。
- 路径冲突：每个时刻检查顶点冲突；相邻时刻检查交换边冲突。路线结束后 AMR
  继续占用最终单元直到 `max_time`，与 P0-09 `ReservationTable` 的终点保持语义
  一致；因此不能只验证 path 数组中显式的前缀。

## 响应与证据

响应固定包含 `schema_version`、`ruleset_version`、`status`、`valid`、`error_count`
和 `errors`。每条 `errors[*]` 都包含：

`code`、`constraint`、`message`、`task_id`、`related_task_id`、`order_id`、
`related_order_id`、`amr_id`、`related_amr_id`、`coordinate`、
`related_coordinate`、`time`、`related_time`、`observed`、`limit`、
`path_index`、`related_path_index`。

不适用的定位字段使用 `null` 或 `""`，不会被省略；冲突、时间窗、容量、电量等
规则会携带坐标/时间和观察值/上限，依赖错误会携带前后任务，路径错误会携带 path
索引。这样 Trace 和人工排障不需要解析中文 message 才能定位。

## 错误字典

机器可读的完整字典由以下命令从 C++ 单一实现导出：

```powershell
.\build\cpp\services\planner_cpp\fleet_plan_validator_cli.exe --error-dictionary
```

稳定错误码按约束分组如下（具体 `description` 和 `evidence_contract` 以 CLI 输出
为准）：

| 约束 | 错误码 |
|---|---|
| 任务依赖 | `duplicate_order_dependency`、`unknown_order_dependency`、`order_dependency_cycle`、`missing_route`、`task_dependency_unplanned`、`task_dependency_time_order`、`order_already_completed`、`invalid_completed_order_id` |
| 时间窗/路线时序 | `pickup_before_release`、`dropoff_after_deadline`、`pickup_time_mismatch`、`dropoff_time_mismatch`、`route_time_invalid` |
| 路线几何 | `route_empty`、`route_start_mismatch`、`route_out_of_bounds`、`route_heading_invalid`、`route_action_invalid`、`route_cost_invalid`、`route_not_planned`、`pickup_not_reached`、`dropoff_not_reached` |
| 载荷/电量 | `route_payload_invalid`、`load_capacity_exceeded`、`amr_battery_critical`、`amr_battery_below_new_task_threshold`、`battery_safety_reserve_breached` |
| 禁行区/边 | `forbidden_zone_occupied`、`forbidden_edge_traversed`、`one_way_violation`、`duplicate_blocked_cell`、`duplicate_blocked_edge`、`invalid_blocked_edge`、`duplicate_one_way_edge`、`invalid_one_way_edge` |
| 工位 | `workstation_capacity_config_missing`、`workstation_capacity_exceeded`、`pickup_location_missing`、`dropoff_location_missing` |
| 车队安全 | `vertex_conflict`、`swap_edge_conflict`、`safety_distance_breached` |
| 快照/身份/配置 | `environment_ref_empty`、`invalid_map`、`invalid_time_horizon`、`invalid_config`、`invalid_order`、`duplicate_amr_id`、`duplicate_order_id`、`duplicate_location_id`、`duplicate_completed_order_id`、`unknown_route_amr`、`unknown_route_order`、`duplicate_route_amr`、`duplicate_route_order`、`amr_unavailable` |

P0-10 的 CTest 为每个主要约束提供正反例，并额外验证错误证据字段、稳定序列化、
错误字典唯一/排序和 LLM 旁路字段拒绝。Validator 通过前，后续 Executor 不得派发
计划；P0-13/P0-12 只能把 `status="valid"` 当作可执行门禁。

