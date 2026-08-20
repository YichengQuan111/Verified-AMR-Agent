"""对当前本地 Qwen Profile 执行一次真实启动门禁。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# 允许直接执行 ``python scripts/check_model_gateway.py``，无需先安装 editable 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config import load_settings
from services.model_gateway.exceptions import ModelGatewayError
from services.model_gateway.provider import ModelProvider


def main() -> int:
    """校验配置、连接、单模型约束和 alias，并打印版本证据。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("fast", "smart"))
    parser.add_argument("--config")
    args = parser.parse_args()

    # 使用副本，避免修改当前 Python 进程的真实环境变量。
    environment = dict(os.environ)
    if args.profile:
        environment["LLM_PROFILE"] = args.profile
        # 命令行 --profile 是显式选择；移除旧 LLM_MODEL，避免残留 alias 与它冲突。
        environment.pop("LLM_MODEL", None)

    try:
        settings = load_settings(args.config, environ=environment)
        result = ModelProvider(settings.model_gateway).health_check()
    # 无论失败来自配置还是网关，都转换成稳定 JSON 和非零退出码。
    except (ModelGatewayError, ValueError, FileNotFoundError) as exc:
        code = getattr(exc, "code", "MODEL_GATEWAY_CONFIGURATION_ERROR")
        print(json.dumps({"status": "failed", "code": code, "message": str(exc)}))
        return 1

    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
