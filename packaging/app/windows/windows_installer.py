"""Build and exercise the Windows installer without touching the user's install."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import subprocess


def find_installer_compiler() -> Path:
    explicit = os.environ.get("OPTIONHELPER_ISCC", "").strip().strip('"')
    if explicit:
        candidates = [Path(explicit)]
    else:
        candidates = [Path(found)] if (found := shutil.which("ISCC.exe")) else []
        for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            if root := os.environ.get(name):
                candidates.extend((Path(root) / "Inno Setup 6" / "ISCC.exe",
                                   Path(root) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("缺少Inno Setup 6安装包编译器。请安装Inno Setup 6.3或更新的6.x版本，或用OPTIONHELPER_ISCC指定ISCC.exe。")


def installer_command(compiler: Path, script: Path, app: Path, output: Path, version: str, icon: Path) -> list[str]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[.-][A-Za-z0-9.-]+)?", version)
    if not match or any(int(part) > 65535 for part in match.groups()):
        raise ValueError("安装包版本必须含有效的主、次、修订版本号")
    numeric_version = ".".join(str(int(part)) for part in match.groups()) + ".0"
    return [str(compiler), "/Qp", f"/DAppSource={app}", f"/DSetupOutput={output.parent}",
            f"/DSetupName={output.stem}", f"/DAppVersion={version}", f"/DAppNumericVersion={numeric_version}",
            f"/DAppIcon={icon}", str(script)]


def install_for_verification(installer: Path, destination: Path) -> None:
    """Exercise the installer in a fresh directory without shortcuts or registration."""
    if os.name != "nt":
        raise RuntimeError("Windows安装物验收必须在Windows运行")
    if destination.exists():
        raise RuntimeError("安装验收目标必须为全新目录")
    log = destination.parent / "installer-verification.log"
    command = [
        str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
        "/VERIFYINSTALL=1", "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
        f"/DIR={destination}", f"/LOG={log}",
    ]
    process = subprocess.Popen(command)
    try:
        returncode = process.wait(timeout=900)
    except BaseException:
        # The bootstrapper creates a second setup process. Stop both before
        # the caller removes its temporary installation directory.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        process.wait(timeout=30)
        raise
    if returncode:
        detail = log.read_text(encoding="utf-8", errors="replace")[-6000:] if log.is_file() else "未生成安装日志"
        raise RuntimeError(f"Windows安装物验收失败，退出码{returncode}\n{detail}")
