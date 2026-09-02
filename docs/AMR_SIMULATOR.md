# P0-11 Python AMR 离散事件仿真器

`services/amr_simulator` 是 P0-11 的确定性执行层。它接收与 P0-10 相同的
计划 JSON envelope，先调用仓库内固定的
`build/cpp/services/planner_cpp/fleet_plan_validator_cli.exe --validate`，只有
响应同时满足 `status="valid"`、`valid=true`、`errors=[]` 才开始仿真。

## 执行语义

- 时间步固定为 1 秒，直接按 P0-09 `path[*].time` 索引位置、朝向和动作；不在
  Python 侧重算 A*、不接受 planner 的状态字段作为安全结论。
- `move` 每执行一步扣除 `config.energy_per_cell_percent`；`turn_left`、
  `turn_right`、`wait` 不扣电。路线结束后 AMR 保持 P0-09/P0-10 终点占用语义，
  不会被仿真器瞬移或释放到任意位置。
- 到达 pickup 的 tick 产生 `LOADING` 和 `order.pickup`；到达 dropoff 的 tick
  产生 `UNLOADING` 和 `order.dropoff`。装卸是零时长事件，不改变已验证的
  `pickup_time/dropoff_time`；下一 tick 才可回到 `IDLE` 或进入充电。
- 正常运输状态覆盖 `IDLE → TO_PICKUP → LOADING → TO_DROPOFF → UNLOADING`。
  电量不足但已在充电站时进入 `CHARGING`；不足但没有安全充电路径时进入
  `TO_CHARGE` 并保持原地等待，不伪造移动。故障安全停机时为 `OFFLINE`。
- 工位容量按同一离散 tick 的零时长 pickup/dropoff 事件重新检查；充电站按
  `SimulatorConfig.charging_stations` 的容量分配，候选 AMR 和站点均按稳定 ID
  排序。

## 公共 Python 入口

```python
from services.amr_simulator import AMRSimulator, FaultInjection, FaultType

result = AMRSimulator().run(
    fleet_plan_request,
    simulation_id="run-p011-001",
    seed=7,
    until_time=120,
    faults=[
        FaultInjection(
            at_time=35,
            amr_id="AMR-02",
            fault_type=FaultType.OFFLINE,
            reason="eval network fault",
        )
    ],
)
```

`SimulationResult` 包含最终 `AMRState`、订单/工位/充电站状态、每个 tick 一份
P0-04 `Observation` 和 `SimulationEvent` 日志。`Observation.observed_at` 使用
固定 Unix epoch 加离散秒，不读取墙上时钟；同一计划、配置、故障列表和 seed
会生成相同的 JSON 结构和事件顺序。

## 故障注入边界

`FaultInjection` 只属于仿真/Eval 接口，不属于 `agent.tools.ToolName`、
`ToolSpec` 或正常 Agent 工具清单。当前支持 `offline`、`battery_drain`、
`stuck` 三类故障，均在指定 tick 的路径动作前生效并安全停机；受影响未完成
订单变为 `blocked`，Observation 标记 `requires_replan=true`。当前不尝试恢复
并跳过失效路径，后续若加入恢复能力必须同步扩展时间戳和证据契约。

## 验证

```powershell
python -m pytest tests\unit\test_p011_simulator.py -q
```

专项测试覆盖正常运输、完整状态迁移、充电速率与站点状态、低电量待充、离线
与电量故障、P0-10 非法时间戳拒绝、同 seed 可复现，以及故障接口不进入正常
工具白名单。
