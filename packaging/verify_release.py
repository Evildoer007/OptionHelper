#!/usr/bin/env python3
"""Skill发行目录与App源的总验收入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_skill import verify_source_snapshot
from verify_skill import verify_skill
from verify_app import verify_app


def _published_capability_errors(skill_root: Path) -> list[str]:
    try:
        manifest = json.loads((skill_root / "capability-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"无法读取正式Capability Manifest：{error}"]
    if (
        manifest.get("package_status") != "published"
        or manifest.get("release_status") != "published"
        or manifest.get("formal_release") is not True
        or manifest.get("execution_scope") != "production"
    ):
        return ["verify_release只接受已签发的正式Capability，不接受技术或开发候选"]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="验收OptionHelper Skill发行目录与App源")
    parser.add_argument("--skill-root", type=Path, required=True, help="versions内已签发Capability的解压目录")
    parser.add_argument("--app-root", type=Path, default=ROOT / "products" / "app")
    args = parser.parse_args()
    skill_root = args.skill_root.resolve()
    app_root = args.app_root.resolve()
    errors = [
        *verify_skill(skill_root),
        *_published_capability_errors(skill_root),
        *verify_source_snapshot(skill_root),
        *verify_app(app_root, capability_root=skill_root),
    ]
    if errors:
        raise SystemExit("\n".join(sorted(set(errors))))
    print("OptionHelper Skill and App source verified")


if __name__ == "__main__":
    main()
