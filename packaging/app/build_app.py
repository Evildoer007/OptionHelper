#!/usr/bin/env python3
"""验证App将要使用的独立Capability。"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from verify_capability import verify_app_capability


class AppBuildError(RuntimeError):
    pass


def _is_finder_copy(path: Path) -> bool:
    """识别存在无编号同名兄弟的Finder同步冲突副本。"""
    match = re.fullmatch(r"(?P<base>.+) (?P<copy>\d+)(?P<suffix>(?:\.[^.]+)*)", path.name)
    if not match:
        return False
    canonical = path.with_name(f"{match.group('base')}{match.group('suffix')}")
    return canonical.exists() or canonical.is_symlink()


def _finder_copies(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if _is_finder_copy(path))


def validated_capability(capability_root: Path) -> Path:
    """Return one verified, conflict-free App Capability."""
    capability_root = capability_root.resolve()
    if not capability_root.is_dir():
        raise AppBuildError(f"Capability目录不存在：{capability_root}")
    errors = verify_app_capability(capability_root)
    if errors:
        raise AppBuildError("App Capability未通过验收：\n" + "\n".join(errors))
    conflicts = _finder_copies(capability_root)
    if conflicts:
        names = ", ".join(path.relative_to(capability_root).as_posix() for path in conflicts)
        raise AppBuildError(f"App Capability含同步冲突副本，拒绝使用：{names}")
    return capability_root


def main() -> None:
    parser = argparse.ArgumentParser(description="验证供App直接使用的Capability，不写入App开发树")
    parser.add_argument("--skill-root", type=Path, required=True)
    args = parser.parse_args()
    print(validated_capability(args.skill_root))


if __name__ == "__main__":
    main()
