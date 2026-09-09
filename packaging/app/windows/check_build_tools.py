#!/usr/bin/env python3
"""Bounded Windows build-tool preflight for the one-click entry."""

from __future__ import annotations

from pathlib import Path
import platform
import shutil
import subprocess
import sys
from windows_installer import find_installer_compiler


ROOT = Path(__file__).resolve().parents[3]
ICON = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
TIMEOUT_SECONDS = 60


def check() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("Windows候选必须在Windows机器构建")
    find_installer_compiler()
    if sys.version_info < (3, 12):
        raise RuntimeError("锁定依赖要求Python3.12及以上，建议使用64位Python3.12或3.13")
    if platform.machine().casefold() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise RuntimeError("Windows构建要求x64系统与64位Python")
    if not ICON.is_file():
        raise RuntimeError(f"缺少受控Windows图标：{ICON}")
    if shutil.which("powershell") is None:
        raise RuntimeError("缺少Windows PowerShell")
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise RuntimeError("缺少dotnet SDK 8")
    try:
        completed = subprocess.run(
            [dotnet, "--list-sdks"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("dotnet SDK检查超时") from error
    if completed.returncode or not any(line.lstrip().startswith("8.") for line in completed.stdout.splitlines()):
        raise RuntimeError("需要dotnet SDK 8")


def main() -> None:
    try:
        check()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    print("Windows打包工具检查通过。")


if __name__ == "__main__":
    main()
