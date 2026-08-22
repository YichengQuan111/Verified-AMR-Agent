"""离线导出演示仿真轨迹 JSON（生成物写入 tmp/，不登记为源码交付物）。

用途：
- 不启动 HTTP 服务即可验证「C++ 计划 → Validator → AMRSimulator」演示链路；
- 生成前端联调用的固定轨迹文件，便于核对 UI 折线与后端 amr.path_step 一致。

用法（默认导出 ORDER-001 到 tmp/demo_trajectory_order_001.json）::

    E:\\Anaconda\\envs\\torch128\\python.exe scripts\\export_demo_trajectory.py
    E:\\Anaconda\\envs\\torch128\\python.exe scripts\\export_demo_trajectory.py --order ORDER-002
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 直接执行时把仓库根目录加入模块搜索路径，与 scripts/export_schemas.py 同约定。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.demo import DemoSimulateRequest, WarehouseDemoService  # noqa: E402


def main() -> int:
    """命令行入口：跑真实 C++ 链路并把完整响应落盘到 tmp/。"""

    parser = argparse.ArgumentParser(description="导出演示仿真轨迹 JSON 到 tmp/")
    parser.add_argument("--order", default="ORDER-001", help="种子订单 ID")
    parser.add_argument(
        "--out",
        default=None,
        help="输出路径（默认 tmp/demo_trajectory_<order_id 小写>.json）",
    )
    args = parser.parse_args()

    output_path = Path(args.out) if args.out else (
        PROJECT_ROOT / "tmp" / f"demo_trajectory_{args.order.lower()}.json"
    )
    response = WarehouseDemoService().run_simulation(DemoSimulateRequest(order_id=args.order))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(response.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"order={args.order} status={response.summary.simulation_status} "
          f"path_steps={response.summary.path_step_count} -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
