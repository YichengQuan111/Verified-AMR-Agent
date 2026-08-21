"""校验 Qwen3.6 Fast manifest 与宿主文件，并输出不含密钥的 JSON 证据。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config import load_settings
from services.model_gateway.artifacts import load_and_verify_fast_artifact


def parse_args() -> argparse.Namespace:
    """路径参数只改变本机定位，manifest 的 size/hash 契约仍不可绕过。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--model")
    parser.add_argument("--runtime")
    return parser.parse_args()


def main() -> int:
    """任何参数、文件或哈希不一致都以非零退出，供启动器作为前置门禁。"""

    args = parse_args()
    settings = load_settings().model_gateway
    updates: dict[str, object] = {"artifact_verification_required": True}
    if args.manifest:
        updates["artifact_manifest_path"] = args.manifest
    if args.model:
        updates["artifact_model_path_override"] = args.model
    if args.runtime:
        updates["artifact_runtime_path_override"] = args.runtime
    settings = settings.model_copy(update=updates)
    record = load_and_verify_fast_artifact(settings)
    print(
        json.dumps(
            {"status": "ok", **record.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
