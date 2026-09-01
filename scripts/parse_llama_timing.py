"""从 llama.cpp 日志提取 Prefill；不会把 progress=1.00 写成 TTFT。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from evals.perf.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["parse-log", *sys.argv[1:]]))
