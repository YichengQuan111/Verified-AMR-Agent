# P0-09 C++ A* 与时空预约

`services/planner_cpp/` 现在提供独立的 `route_planner` 静态库和
`route_planner_cli.exe`。路径规划复用 P0-04 的 `AMRState`、`TransportOrder`、
`GridPosition` 与 P0-08 的 `Location` 语义，但不把 Manhattan 分配距离当作可执行路线。

## 算法边界

- 生产入口 `plan_routes_astar()` 使用状态 `(x, y, heading, t)`。动作是前进、左转、右转和等待；每个动作占用一个离散时间步，代价分别由 `move_cost`、`turn_cost` 和 `wait_cost` 提供。
- 启发式是 `Manhattan(cell, goal) * move_cost`，不估计转向和等待，因此不会高估代价。
- 多车按 `priority` 降序、`release_time` 升序、`order_id`、`amr_id` 排序。每完成一台车的完整 `pickup → dropoff` 路径，就把路径写入预约表后规划下一台车。
- `ReservationTable` 在 `(cell,t)` 上预约顶点，在 `(edge,t)` 上同时检查同向和反向边；已到达终点的单元保持预约至 `max_time`。因此发现冲突时只能等待、绕行或返回 `infeasible`。
- `plan_routes_dijkstra()` 是独立的时间扩展 Dijkstra 基线，使用 `h=0` 的独立开放表，不是 A* 的失败回退。相同请求可以用它比较最优代价和可行性。
- `blocked_cells`、`blocked_edges`、`one_way_edges` 和地图边界都是硬约束；起点、工位或路径非法时不会修正坐标，也不会输出穿障碍/逆行/冲突路线。订单 deadline 由 P0-10 车队验证器做最终硬校验，P0-09 返回 `pickup_time/dropoff_time` 供其检查。

## CLI JSON 契约

CLI 从标准输入读取 UTF-8 JSON，拒绝未知字段、重复键、非有限数和超过 4 MiB 的输入；不读取路径、不执行 Shell。请求顶层字段固定为：

`schema_version`、`environment_ref`、`map_width`、`map_height`、`blocked_cells`、
`blocked_edges`、`one_way_edges`、`amrs`、`orders`、`location_positions`、
`assignments`、`completed_order_ids`、`start_time`、`max_time`、`costs`。

`amrs` 和 `orders` 中的对象字段直接复用 P0-04；`location_positions` 与 P0-08 一致，
例如 `{ "P1": {"x": 2, "y": 3} }`；`assignments` 只包含
`{"amr_id":"AMR-1","order_id":"ORDER-1"}`，也可以直接携带 P0-08 输出中的
`components` 对象；该旧代价只作审计快照，路线规划会重新计算。`blocked_edges` 和 `one_way_edges`
的元素为 `{ "from": {"x":...,"y":...}, "to": {"x":...,"y":...} }`。

运行示例：

```powershell
Get-Content route_request.json -Raw |
  .\build\cpp\services\planner_cpp\route_planner_cli.exe --algorithm astar

Get-Content route_request.json -Raw |
  .\build\cpp\services\planner_cpp\route_planner_cli.exe --algorithm dijkstra
```

业务结果使用退出码 `0`：`status=complete` 表示所有 assignments 都有安全路线，
`status=infeasible` 表示至少一条路线无安全可行解，响应仍会给出每条失败路线的
`reason_code`，但不会给失败路线填充路径。JSON/参数/契约错误使用退出码 `2`，
未预期内部错误使用退出码 `3`。

响应的每条 `path` 都按时间顺序包含 `position`、`heading`、`time`、`action` 和
累计 `g_cost`；首个动作是 `start`。`planned` 路线会包含 `pickup_time`、
`dropoff_time`、总代价和扩展状态数，`infeasible` 路线只返回失败原因。

## 测试

CTest 独立覆盖障碍绕行、边界拒绝、等待让行、顶点预约、交换边预约、无解、
独立 Dijkstra、结果可复现性、30×20 四车性能和 JSON 契约；P0-08 原有 CTest
也继续保留。所有 C++ 中文注释目标都通过 MSVC `/utf-8` 编译选项。
