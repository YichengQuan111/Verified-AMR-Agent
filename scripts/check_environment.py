"""用一条命令检查 P0 开发环境，并输出可机器读取的 JSON。

检查内容包括 Python 版本、锁文件中的直接依赖，以及固定的 CMake/Ninja/MSVC。
任一必需项不匹配时脚本返回非零退出码，供统一冒烟脚本立即停止。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any


# 所有路径都从脚本自身位置推导，避免依赖调用者当前所在目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CMAKE = Path(
    r"E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
)
DEFAULT_NINJA = Path(
    r"E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
)
DEFAULT_MSVC_ROOT = Path(r"E:\BuildingTools\VC\Tools\MSVC")
LOCK_FILES = (PROJECT_ROOT / "requirements.lock", PROJECT_ROOT / "requirements-dev.lock")


def parse_lock_files() -> dict[str, str]:
    """读取两个直接依赖锁文件，返回 ``包名 -> 精确版本``。"""

    packages: dict[str, str] = {}
    for lock_file in LOCK_FILES:
        for raw_line in lock_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # 锁文件只允许 name==version；发现范围版本或其他语法就立即失败。
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", line)
            if not match:
                raise ValueError(f"unsupported lock entry in {lock_file.name}: {line}")
            packages[match.group(1)] = match.group(2)
    return packages


def executable_version(path: Path, *arguments: str) -> dict[str, Any]:
    """安全运行一个版本命令，并把结果收集成字典而不是直接抛出异常。"""

    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        text = (completed.stdout or completed.stderr).strip()
        result.update(
            {
                "exit_code": completed.returncode,
                "version_output": text.splitlines()[0] if text else "",
            }
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def newest_msvc_compiler(msvc_root: Path) -> Path | None:
    """在显式 Build Tools 目录中选择版本号最高的 x64 MSVC 编译器。"""

    if not msvc_root.is_dir():
        return None
    candidates = sorted(
        msvc_root.glob(r"*\bin\Hostx64\x64\cl.exe"), reverse=True
    )
    return candidates[0] if candidates else None


def main() -> int:
    """构造完整环境报告，并用退出码表示是否满足 P0 锁定要求。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-native", action="store_true", help="skip C++ tool checks")
    parser.add_argument("--cmake", type=Path)
    parser.add_argument("--ninja", type=Path)
    parser.add_argument("--msvc-root", type=Path)
    args = parser.parse_args()

    # 对每个锁定包查询安装元数据；不导入重型库，因此检查速度很快。
    expected_packages = parse_lock_files()
    package_records: dict[str, dict[str, Any]] = {}
    mismatches: list[str] = []
    for package_name, expected_version in expected_packages.items():
        try:
            installed_version = metadata.version(package_name)
            matches = installed_version == expected_version
        except metadata.PackageNotFoundError:
            installed_version = None
            matches = False
        package_records[package_name] = {
            "expected": expected_version,
            "installed": installed_version,
            "matches": matches,
        }
        if not matches:
            mismatches.append(package_name)

    # 报告始终使用同一结构；失败原因统一收集在 errors 中。
    report: dict[str, Any] = {
        "status": "ok",
        "project_root": str(PROJECT_ROOT),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "packages": package_records,
        "native_tools": {},
        "errors": [],
    }

    if sys.version_info[:2] != (3, 12):
        report["errors"].append("P0 requires Python 3.12")
    if mismatches:
        report["errors"].append(
            "package lock mismatch: " + ", ".join(sorted(mismatches, key=str.lower))
        )

    # -SkipCpp 会让 PowerShell 入口传入 --no-native，只检查 Python 部分。
    if not args.no_native:
        cmake = args.cmake or Path(os.environ.get("AMR_CMAKE", str(DEFAULT_CMAKE)))
        ninja = args.ninja or Path(os.environ.get("AMR_NINJA", str(DEFAULT_NINJA)))
        msvc_root = args.msvc_root or Path(
            os.environ.get("AMR_MSVC_ROOT", str(DEFAULT_MSVC_ROOT))
        )
        compiler = newest_msvc_compiler(msvc_root)
        report["native_tools"] = {
            "cmake": executable_version(cmake, "--version"),
            "ninja": executable_version(ninja, "--version"),
            "msvc": {
                "path": str(compiler) if compiler else None,
                "exists": bool(compiler and compiler.is_file()),
            },
        }
        for tool_name, record in report["native_tools"].items():
            if not record.get("exists"):
                report["errors"].append(f"native tool unavailable: {tool_name}")

    # JSON 内容便于人阅读，也可被 CI/评测脚本解析；退出码供调用链判断。
    if report["errors"]:
        report["status"] = "failed"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
