"""使用真实本地模型重复验证 P0-03 结构化输出契约。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# 直接运行脚本时，Python 默认只把 scripts/ 加入 sys.path；这里补入项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# Windows 控制台编码可能不是 UTF-8，显式设置后中文取货点不会显示成乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from pydantic import BaseModel, ConfigDict, Field

from services.config import load_settings
from services.model_gateway import ChatMessage, ModelProvider


class TransportExtraction(BaseModel):
    """冒烟测试期望模型返回的最小运输订单结构。"""

    # extra='forbid' 保证模型多返回一个字段也算失败，而不是被静默忽略。
    model_config = ConfigDict(extra="forbid")

    pickup: str
    dropoff: str
    quantity: int = Field(gt=0)


def main() -> int:
    """启动一次 Provider，然后复用它完成 N 次结构化请求。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--profile", choices=("fast", "smart"))
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    environment = dict(os.environ)
    if args.profile:
        environment["LLM_PROFILE"] = args.profile
        environment.pop("LLM_MODEL", None)

    # startup 会先校验 /v1/models；alias 错误时不会进入生成循环。
    settings = load_settings(environ=environment)
    provider = ModelProvider(settings.model_gateway)
    version = provider.startup()
    print(
        f"Validated profile={version.profile} alias={version.served_alias} "
        f"sdk={version.openai_sdk_version}"
    )

    # 每次都验证字段值，而不只是“能解析成 JSON”。
    success = 0
    for index in range(args.iterations):
        try:
            result = provider.generate_structured(
                [
                    ChatMessage(
                        role="system",
                        content=(
                            "你负责从运输任务中抽取字段。输出 JSON，字段固定为 "
                            "pickup、dropoff、quantity。不要输出 Markdown 或其他字段。"
                        ),
                    ),
                    ChatMessage(role="user", content="把2箱物料从A区送到S3。"),
                ],
                TransportExtraction,
            )
            data = result.value
            assert data.pickup == "A区"
            assert data.dropoff == "S3"
            assert data.quantity == 2
            success += 1
            print(
                f"[{index + 1:02d}/{args.iterations}] PASS "
                f"{data.model_dump()} attempts={result.attempts}"
            )
        # 单次失败不提前中断，这样最终报告能展示 20 次中的真实成功率。
        except Exception as exc:
            print(
                f"[{index + 1:02d}/{args.iterations}] FAIL: "
                f"{type(exc).__name__}: {exc}"
            )

    print(f"\nResult: {success}/{args.iterations}")
    # 只有全部通过才返回 0，便于在自动化流程中作为硬门槛。
    return 0 if success == args.iterations else 1


if __name__ == "__main__":
    raise SystemExit(main())
