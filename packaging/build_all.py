#!/usr/bin/env python3
"""在系统临时目录构建并验证开发态Skill与App边界。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_skill import build_skill, verify_source_snapshot, write_zip
from verify_skill import verify_skill
from build_app import validated_capability
from verify_app import verify_app


def main() -> None:
    parser = argparse.ArgumentParser(description="临时构建并验证OptionHelper Skill和当前App边界")
    parser.add_argument("--target", choices=("skill", "all"), default="all")
    parser.add_argument("--platform", choices=("current", "macos", "windows"), default="current")
    catalog_mode = parser.add_mutually_exclusive_group()
    catalog_mode.add_argument("--catalog-version", help="构建Skill时使用当前发行版本的已签发知识源清单")
    catalog_mode.add_argument("--candidate", action="store_true", help="从当前references构建仅开发环境可执行的技术候选")
    args = parser.parse_args()
    if args.target in {"skill", "all"} and not args.catalog_version and not args.candidate:
        raise SystemExit("--target skill或all必须显式提供--catalog-version或--candidate")
    with tempfile.TemporaryDirectory(prefix="optionhelper-build-all-") as temporary_name:
        candidate_root = Path(temporary_name) / "skill"
        skill_root = candidate_root / "option-helper"
        skill_root = build_skill(candidate_root, catalog_version=args.catalog_version, candidate=args.candidate)
        errors = verify_skill(skill_root)
        if errors:
            raise SystemExit("\n".join(errors))
        errors = verify_source_snapshot(skill_root)
        if errors:
            raise SystemExit("\n".join(errors))
        archive = write_zip(skill_root)
        capability = validated_capability(skill_root)
        if args.target == "all":
            errors = verify_app(ROOT / "products" / "app", capability_root=capability)
            if errors:
                raise SystemExit("\n".join(errors))
        print(f"Ephemeral verified Capability: {capability}")
        print(f"Ephemeral Skill ZIP (removed when verification exits): {archive}")
        if args.target == "all":
            print(f"App source verified against the same Capability; platform={args.platform}")


if __name__ == "__main__":
    main()
