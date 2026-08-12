#!/usr/bin/env python3
"""验证App将要使用的同一份已验证Capability。

开发态和测试态绝不把Capability写入``products/app/capability``。平台构建器直接
接收本次构建的Skill目录，并把它复制到最终安装物；这避免同步盘生成树被再次读取。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from verify_skill import verify_skill


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


def validated_capability(skill_root: Path) -> Path:
    """Return one verified, conflict-free Capability without staging a copy."""
    skill_root = skill_root.resolve()
    if not skill_root.is_dir():
        raise AppBuildError(f"Capability目录不存在：{skill_root}")
    errors = verify_skill(skill_root)
    if errors:
        raise AppBuildError("Skill候选未通过验收：\n" + "\n".join(errors))
    conflicts = _finder_copies(skill_root)
    if conflicts:
        names = ", ".join(path.relative_to(skill_root).as_posix() for path in conflicts)
        raise AppBuildError(f"Skill候选含同步冲突副本，拒绝使用：{names}")
    return skill_root


def main() -> None:
    parser = argparse.ArgumentParser(description="验证供App直接使用的Capability，不写入App开发树")
    parser.add_argument("--skill-root", type=Path, required=True)
    args = parser.parse_args()
    print(validated_capability(args.skill_root))


if __name__ == "__main__":
    main()
