#!/usr/bin/env python3
"""Preflight both locked Node projects without mutating the source tree."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_ROOTS = (
    ROOT / "packaging" / "app" / "agent_runtime",
    ROOT / "products" / "app" / "runtime" / "optionhelper_agent_runtime",
)


class DependencyRestoreError(RuntimeError):
    pass


def restore_locked_dependencies(*, npm: str | None = None) -> None:
    """Compatibility entrypoint used by one-click scripts.

    The actual ``npm ci`` calls now run only after source freezing, inside the
    disposable workspace owned by ``build_runtime.py``. This preflight checks
    that npm and both lockfile pairs are available without creating
    ``node_modules`` in either production source directory.
    """

    executable = npm or shutil.which("npm")
    if not executable:
        raise DependencyRestoreError("缺少Node.js/npm，无法恢复Agent Runtime锁定依赖")
    for root in DEPENDENCY_ROOTS:
        if not (root / "package.json").is_file() or not (root / "package-lock.json").is_file():
            raise DependencyRestoreError(f"缺少Agent Runtime锁文件：{root}")
    print("锁文件预检通过；npm ci将在冻结后的临时构建目录执行。", flush=True)


def main() -> None:
    try:
        restore_locked_dependencies()
    except DependencyRestoreError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
