# P1-1 STL 规约第二判定层（fleet_plan_validator）

P0-10 的 C++ 规则验证器是整个系统“LLM 不能绕过确定性 Validator”这一安全论证的落点，
但在 P1-1 之前它自己只有 CTest 正反例，没有独立 oracle。P1-1 把同一批安全约束改写成
**信号时序逻辑（STL）规约**，由一个独立实现的离散时间、有限轨迹**鲁棒度监控器**在派发前
重新判定，作为 `fleet_plan_validator` 的第二判定层：

- **布尔一致**：每条公式违反 ⟺ 规则层对应错误码出现。不一致即两层之一有 Bug。
- **定量鲁棒度**：除通过/失败外，给出带符号裕量与最薄弱时刻，可记录“险胜”，也是后续
  可验证奖励（P2-A）的稠密信号。
- **规约来自文件**：`config/stl/fleet_plan_stl_spec.json`，DSL 文本 + 规则码映射 + 险胜阈值，
  不硬编码在 C++ 里；请求 JSON 没有任何字段能指定或削弱规约。

范围边界：只做派发前离线验证；不做仿真流在线监控、不做 SMT、不做模型检验（这些在 P2-B）。

## 1. 代码位置

| 文件 | 职责 |
|---|---|
| `services/planner_cpp/include/fleet_plan_validator/stl_monitor.hpp` | DSL AST、规约结构、通用求值接口、报告结构 |
| `services/planner_cpp/src/stl_monitor.cpp` | 词法/递归下降解析、规范文本、规约加载与作用域目录校验、布尔 + 鲁棒度求值 |
| `services/planner_cpp/src/stl_fleet_monitor.cpp` | 从 `FleetPlanRequest` 独立提取信号轨迹、按作用域实例化公式、汇总报告 |
| `services/planner_cpp/src/fleet_plan_validator.cpp` | `validate_fleet_plan(request, spec)`：规则层 → STL 层 → gate 模式追加 `stl_specification_violated` |
| `services/planner_cpp/src/fleet_plan_validator_main.cpp` | CLI：`--validate [--stl-spec <path>]`、`--describe-stl-spec <path>` |
| `services/planner_cpp/tests/stl_monitor_tests.cpp` | 14 个 CTest（`stl_*`） |
| `config/stl/fleet_plan_stl_spec.json` | 发布规约 `amr-fleet-plan-stl` / `p1-1.v1`，8 条公式，`enforcement=gate` |
| `agent/tools/cpp_client.py`、`services/amr_simulator/validator.py` | 固定 argv 传入仓库内规约路径；缺失时 fail-closed |
| `agent/tools/schemas.py` | `ValidationResponse.stl`（`STLMonitorOutput`）契约 |
| `agent/tools/registry.py`、`agent/runtime/graph.py`、`agent/runtime/pevr.py` | 鲁棒度摘要进入审计元数据/Trace 与 `PEVRMetrics.stl_*` |
| `evals/stl_consistency/harness.py`、`scripts/run_stl_consistency.ps1` | 60 例派生计划 + 变异 + 合成场景的布尔一致性核对 |

## 2. 两层独立

STL 层刻意**不复用**规则层匿名命名空间里的栅格 helper（曼哈顿距离、前进格、边键等都在
`stl_fleet_monitor.cpp` 重新实现），也不读取规则层的 `normalized` 中间结果或 `errors`。
两层若共享一个带 Bug 的 helper，会同时给出同样错误的结论，一致性核对就失去了
“独立 oracle”的意义。信号语义与规则层逐条对齐（见第 5 节），任何差异都应通过
一致性核对暴露，而不是在监控器里“修一下让它一致”。

## 3. DSL 与语义

### 3.1 文法（优先级从低到高）

```text
implies := or ( "->" or )?                 右结合
or      := and ( "or" and )*
and     := until ( "and" until )*
until   := unary ( "U" interval? unary )?
unary   := "not" unary | "G" interval? unary | "F" interval? unary
         | "(" implies ")" | "true" | atom
atom    := identifier ( ">=" | "<=" | ">" | "<" ) ( number | identifier )
interval:= "[" bound "," bound "]"
bound   := integer | identifier (("+"|"-") integer)? | "inf"
```

- 区间相对当前求值时刻（标准 STL）；顶层在轨迹起点 `start_time` 求值，因此
  `G[0, release_time - 1]` 中的 `release_time` 是已换算成“相对 start_time 偏移”的命名参数。
  省略区间等价于 `[0, inf]`。解析后的负下界截断到 0（过去不在有限轨迹内）。
- 阈值可以是数字或命名参数（`battery_safety_reserve_percent` 等来自请求 `config`）。
- 保留字：`and or not G F U true inf`；信号名和参数名必须在该作用域的目录内，
  否则规约在加载阶段被拒绝（fail-closed）。

### 3.2 布尔语义与鲁棒度

原子谓词 `f op c` 的鲁棒度是带符号裕量：`>=`/`>` 为 `f - c`，`<=`/`<` 为 `c - f`。
布尔值按算子判定：非严格 `>=`/`<=` 在 `ρ >= -1e-9` 时满足，严格 `>`/`<` 在 `ρ > 1e-9`
时满足——与规则层对同一阈值的边界判定逐字对齐（例如“电量不高于阈值即拒绝”）。

复合式在每个时刻同时计算布尔值、鲁棒度和**决定该值的时刻（witness）**：

| 算子 | 布尔 | 鲁棒度 | witness |
|---|---|---|---|
| `not φ` | ¬ | −ρ | φ 的 |
| `φ and ψ` | ∧ | min | 取较弱一方（优先未满足者，再比鲁棒度） |
| `φ or ψ` / `φ -> ψ` | ∨ / ¬φ∨ψ | max | 取较强一方 |
| `G[a,b] φ` | ∀t'∈[t+a,t+b]∩T | min | argmin（违反时优先未满足时刻） |
| `F[a,b] φ` | ∃ | max | argmax |
| `φ U[a,b] ψ` | ∃t'：ψ(t') ∧ ∀t''∈[t,t')：φ(t'') | max_t' min(ρψ(t'), min_{t''<t'} ρφ(t'')) | 绑定项的 witness |

有限轨迹语义：窗口落在轨迹之外时，`G` 为空真（ρ=+inf）、`F`/`U` 为假（ρ=−inf），
报告中 `vacuous=true`、`robustness=null`。窗口超出轨迹末尾时截断到 `max_time`，因此
`deadline > max_time` 的订单其时间窗鲁棒度按 `max_time` 计算（布尔值不受影响）。
`weakest_time` 是 witness 换算的绝对时刻：对 `G` 是最小裕量处，对 `F` 是决定裕量的
时刻（时间窗公式里就是 `deadline` 本身）。

布尔型信号（是否合法边、是否交换边、是否在充电站）取值 ±1 并用 `> 0` 判定，因此
含布尔子式的公式鲁棒度**饱和在 1**（例如 `fleet_separation` 的取值范围是 −∞…1）。

复杂度：每个算子 O(N·w)（w 为窗口宽度，无界窗口为 O(N²)），N = `max_time − start_time + 1`
≤ 2001。CTest `stl_performance`（4 车、时间域 2000、34 个实例）实测 75 ms。

## 4. 规约文件

```json
{
  "schema_version": "1.0",
  "spec_id": "amr-fleet-plan-stl",
  "spec_version": "p1-1.v1",
  "enforcement": "gate",
  "charging_location_ids": ["C1", "C2"],
  "formulas": [
    {"id": "time_window", "scope": "order", "description": "...",
     "formula": "G[0, release_time - 1](loaded_margin <= -1) and F[release_time, deadline](delivered_margin >= 0)",
     "rule_codes": ["pickup_before_release", "dropoff_after_deadline", "dropoff_not_reached"],
     "warn_below": 5}
  ]
}
```

- `enforcement`：`gate` 时 STL 违反追加为 `stl_specification_violated` 错误并使计划 `invalid`；
  `shadow` 只记录报告。发布规约为 `gate`：任何计划必须同时通过规则层与 STL 层。
- `rule_codes`：该公式对应的规则层错误码，一致性核对据此逐条比对；空列表表示规则层没有对应规则。
- `warn_below`：鲁棒度低于该值记为险胜（`narrow_pass`）；`null` 表示不统计。
- 未知字段、未知作用域、未知信号/参数、重复 id、非法文法都会让 CLI 以退出码 2 和
  `invalid_stl_specification` 失败，不会退化成只跑规则层。

### 4.1 发布规约的 8 条公式

| id | 作用域 | 公式 | 覆盖规则错误码 | warn_below |
|---|---|---|---|---:|
| `time_window` | order | `G[0, release_time-1](loaded_margin <= -1) and F[release_time, deadline](delivered_margin >= 0)` | pickup_before_release, dropoff_after_deadline, dropoff_not_reached | 5 |
| `battery_safety` | amr | `G[0,0](battery > new_task_battery_threshold_percent) and G(battery >= battery_safety_reserve_percent)` | amr_battery_below_new_task_threshold, amr_battery_critical, battery_safety_reserve_breached | 5 |
| `traffic_rules` | amr | `G(blocked_cell_distance >= 1 and boundary_margin >= 0 and edge_legal > 0)` | forbidden_zone_occupied, route_out_of_bounds, forbidden_edge_traversed, one_way_violation | — |
| `load_capacity` | amr | `G(load <= maximum_load_kg)` | load_capacity_exceeded | 5 |
| `fleet_separation` | pair | `G(pair_distance >= 1 and pair_distance >= minimum_safety_distance_cells and no_edge_swap > 0)` | vertex_conflict, safety_distance_breached, swap_edge_conflict | 1 |
| `workstation_capacity` | station | `G(occupancy <= capacity)` | workstation_capacity_exceeded | — |
| `dependency_precedence` | dependency | `(dependent_loaded_margin <= -1) U (prerequisite_delivered_margin >= 0)` | task_dependency_time_order, task_dependency_unplanned | 2 |
| `low_battery_charging` | amr | `G(battery >= critical_battery_threshold_percent or F[0, 30](at_charging_station > 0))` | （无；临界门槛 ≤ 安全余量时由 battery_safety 蕴含） | 5 |

错误字典共 57 个错误码；其中 17 个安全/时序约束码全部被前 7 条公式覆盖，其余 40 个是
结构/契约类（重复 ID、未知引用、动作与朝向不一致、时间戳不连续、配置非法等），由规则层独占。

### 4.2 信号目录

| 作用域 | 信号 | 参数 |
|---|---|---|
| 全部 | `t`（相对起点的偏移） | `horizon`、`maximum_load_kg`、`energy_per_cell_percent`、`battery_safety_reserve_percent`、`new_task_battery_threshold_percent`、`critical_battery_threshold_percent`、`minimum_safety_distance_cells` |
| order | `loaded_margin = t − 装货事件时刻`、`delivered_margin = t − 交付时刻` | `release_time`、`deadline`（偏移）、`priority` |
| amr | `battery`、`load`、`blocked_cell_distance`、`boundary_margin`、`edge_legal`(±1)、`at_charging_station`(±1)、`moves` | `payload_kg`、`initial_battery` |
| pair | `pair_distance`、`no_edge_swap`(±1) | — |
| station | `occupancy` | `capacity` |
| dependency | `dependent_loaded_margin`、`prerequisite_delivered_margin` | — |

事件“从未发生”用 `max_time + 1` 表示，裕量在整个时间域内始终为负，`F`/`U` 形式的公式因此
必然违反而不会被空窗口误判为空真。

## 5. 与规则层对齐的信号语义

| 信号/事件 | 规则层来源 | STL 提取 |
|---|---|---|
| 位置轨迹 | `position_at`：路径结束后保持终点占用到 `max_time`；无路线 AMR 停在初始位置 | 同；路径缺失的时刻保持上一状态 |
| 装货事件 | 停在 pickup 且 `time == route.pickup_time`；载荷/工位/依赖用 `derived.pickup_index`（事件否则首次踏上） | `loaded_margin` 用事件时刻；`load`/`occupancy`/依赖用同一“事件否则首次”规则 |
| 交付 | 路径终点位于 dropoff 的终点时刻 | `delivered_margin` |
| 电量 | 只按合法 `move`（相邻、朝向一致、沿朝向前进）扣 `energy_per_cell_percent` | 同一判据 |
| 禁行格/边 | 只登记地图内的 blocked_cells；边必须相邻且在地图内 | 同 |
| 工位容量 | 非正容量属配置错误，不做容量比较 | 跳过该工位 |
| 依赖 | 已完成/未知依赖不比较；无路线前置用声明 `dropoff_time` | 同 |

## 6. 输出契约

`fleet_plan_validator_cli --validate --stl-spec <path>` 的响应在原有字段外增加 `stl`
（未传规约时为 `null`）：

```json
"stl": {
  "spec_id": "amr-fleet-plan-stl", "spec_version": "p1-1.v1", "enforcement": "gate",
  "status": "satisfied", "satisfied": true, "skip_reason": null,
  "formula_count": 8, "instance_count": 13, "violated_count": 0, "narrow_pass_count": 2,
  "min_robustness": 0, "min_robustness_formula_id": "fleet_separation",
  "min_robustness_scope": {"kind": "pair", "amr_id": "AMR-01", "related_amr_id": "AMR-02", ...},
  "results": [
    {"formula_id": "time_window", "scope": {"kind": "order", "order_id": "ORDER-001", "amr_id": "AMR-01", ...},
     "satisfied": true, "robustness": 84, "weakest_time": 120, "coordinate": {"x": 25, "y": 9},
     "related_coordinate": null, "vacuous": false, "narrow_pass": false}
  ]
}
```

`status` 为 `skipped` 表示请求连轨迹都无法构造（地图或时间域非法，规则层必然已报错）。
gate 模式下每条违反实例追加一条 `stl_specification_violated` 证据：`message` 含公式 id，
`observed` 为鲁棒度，`limit` 为 0，`time` 为最薄弱时刻，`coordinate` 为该时刻 AMR 栅格。
Python 契约 `ValidationResponse.stl`（`STLMonitorOutput`）会重算全部计数并拒绝
“gate 违反却 valid”的输出。

工具层 `validate_fleet_plan` 成功时把 `stl_status/stl_min_robustness/stl_narrow_pass_count/...`
写入 `ToolResult.audit_metadata`（进入 Trace `tool` 事件 metadata），证据引用增加
`stl://p1-1.v1/<status>`；`PEVRMetrics` 新增 `stl_status`、`stl_min_robustness`、
`stl_violated_count`、`stl_narrow_pass_count`。险胜**只记录**，不进入 LLM 上下文，
不改变终态或重规划行为。

## 7. 一致性核对（2026-09-03 实测）

```powershell
.\scripts\run_stl_consistency.ps1
```

harness 用正式 `ToolRegistry`（生产 Hungarian + A*，加难地图 + 每例 seed 障碍 +
电量/release 覆盖）为 P0-18 中 32 个会经过运输主链的用例生成真实计划，再对每个基础计划施加
13 种确定性变异（超载、三种低电、deadline 提前、release 推后、封路格/封路边/单向逆行、截断路径、
空闲 AMR 闯入路径、安全距离 2 + 相邻车、无路线前置订单），并加入 6 个合成多车场景
（合法、依赖时序、工位容量、安全距离、顶点冲突、交换边）。报告写入 `tmp/stl_consistency/`。

| 指标 | 结果 |
|---|---:|
| 计划数 | 453（32 基础 + 415 变异 + 6 合成） |
| 计划级布尔一致 | 453/453 |
| 公式级核对 / 不一致 | 3171 / 0 |
| 基础计划规则层 valid / STL satisfied | 32/32 / 32/32 |
| 变异计划规则层 invalid / STL violated | 415/415 / 415/415 |
| 安全约束错误码覆盖 | 17/17（全部 57 码中 40 个结构类由规则层独占） |
| 基础计划最小鲁棒度 | 32 例均为 0（贴边行驶/紧贴 release 装货/与停放车相邻） |
| 险胜计划数 | 31/32（`time_window` 20 例：A* 恰好在 release_time 装货，裕量 0；`fleet_separation` 17 例：与停放 AMR 曾相邻） |
| 单次 CLI 开销中位数 | 仅规则层 5.8 ms → 规则层+STL 7.0 ms（+1.2 ms，含进程启动） |
| 唯一跳过 | `p018-normal-010/safety_distance_two`：路径中段无空闲相邻格，变异不适用 |

“险胜”数字说明规约在定义什么算“恰好合法”：A* 会在 release_time 当刻装货、贴着货架与
停放车辆行驶，这些都是鲁棒度 0 的合法计划。`warn_below` 可按现场需要在规约文件里调整，
不需要改代码。

## 8. 面试口径（P1-1 后可用实现回答）

- **你怎么知道 Validator 没漏？** 两个独立实现（命令式规则 vs 声明式 STL）对 453 个计划、
  3171 次公式级判定布尔一致，且 17 个安全约束码都有对应公式；不一致会被 harness 直接判失败。
- **鲁棒度比通过/失败多给了什么？** 裕量（还能差多少）、最薄弱时刻（在哪一刻最接近违反）、
  险胜阈值（预防性重规划的触发条件）、稠密奖励（P2-A 的 GRPO 可直接用）。
- **G/F/U 在离散有限轨迹上怎么算？** 逐时刻 min/max 动态规划，见第 3.2 节；空窗口用 ±inf 与
  `vacuous` 显式标记，不把“没检查”当“通过”。
- **为什么规约放在 Validator 里而不是 Prompt？** 规约文件只能由固定 argv 加载，请求 JSON
  没有字段能替换它；LLM 只能生成计划，不能生成“什么算对”。
- **和 RA-L 的 STL/PrSTL 是什么关系？** 同一套规约语言：论文侧把 STL 编码进 MISOCP 做综合/概率
  证书，这里把 STL 做成工程验证层；P2-B 方向 1/3 就是把两者接回去。
- **两层不一致怎么定位？** 报告给出计划 id、规则码集合与逐公式一致性；先看信号语义表（第 5 节）
  哪一行漂移，再决定改规则层还是改规约，两边都要补 CTest。

## 9. 已知限制

- 时间窗鲁棒度按有限时间域截断；`deadline > max_time` 时裕量偏小，布尔值不受影响。
- 含布尔子式的公式鲁棒度饱和在 1（`traffic_rules`、`fleet_separation`）。
- `low_battery_charging` 在只含运输路线的 P0 计划里没有规则层对应，当前是 `battery_safety` 的推论；
  它为未来充电感知计划保留了 `G(p -> F[0,T] q)` 形式。
- 结构/契约类错误码（40 个）不在 STL 范围，由规则层独占；harness 只在“覆盖码集合”上比较。
- 只做派发前离线验证；仿真 Observation 流的在线监控、SMT 交叉核对与模型检验属于 P2-B。
