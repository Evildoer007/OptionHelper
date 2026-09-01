#!/usr/bin/env python3
"""Restore both locked Node build roots with bounded npm processes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_ROOTS = (
    ROOT / "packaging" / "app" / "agent_runtime",
    ROOT / "products" / "app" / "runtime" / "optionhelper_agent_runtime",
)
NPM_TIMEOUT_SECONDS = 600


class DependencyRestoreError(RuntimeError):
    pass


def npm_ci_command(executable: str, root: Path, *, windows: bool | None = None) -> list[str]:
    command = [executable, "ci", "--prefix", str(root), "--ignore-scripts"]
    use_cmd = os.name == "nt" if windows is None else windows
    if use_cmd and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", *command]
    return command


def restore_locked_dependencies(*, npm: str | None = None) -> None:
    executable = npm or shutil.which("npm")
    if not executable:
        raise DependencyRestoreError("缺少Node.js/npm，无法恢复Agent Runtime锁定依赖")
    for root in DEPENDENCY_ROOTS:
        if not (root / "package.json").is_file() or not (root / "package-lock.json").is_file():
            raise DependencyRestoreError(f"缺少Agent Runtime锁文件：{root}")
        print(f"正在恢复锁定Node依赖：{root.relative_to(ROOT)}", flush=True)
        command = npm_ci_command(executable, root)
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=NPM_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise DependencyRestoreError(f"npm ci超时：{root.relative_to(ROOT)}") from error
        if completed.returncode:
            raise DependencyRestoreError(
                f"npm ci失败：{root.relative_to(ROOT)}\n{completed.stdout}"
            )
        print(completed.stdout.rstrip(), flush=True)


def main() -> None:
    try:
        restore_locked_dependencies()
    except DependencyRestoreError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
