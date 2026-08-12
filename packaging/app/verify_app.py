#!/usr/bin/env python3
"""App开发源与内置Capability边界验收。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from verify_skill import verify_skill


def verify_app(app_root: Path, *, capability_root: Path | None) -> list[str]:
    errors: list[str] = []
    expected = ["backend", "frontend", "desktop", "config", "locks", "tests", "packaging"]
    errors.extend(f"App缺少{item}/" for item in expected if not (app_root / item).exists())
    if capability_root is None:
        errors.append("App验收必须显式提供已验证Capability根目录")
    else:
        errors.extend(verify_skill(capability_root.resolve()))
    for relative in ("backend/reporter_adapter.py", "backend/stores/result_store.py"):
        adapter = app_root / relative
        if not adapter.is_file():
            errors.append(f"App缺少ModuleRunRef v1.2适配：{relative}")
        elif "expected_artifact_manifest_hash" not in adapter.read_text(encoding="utf-8", errors="ignore"):
            errors.append(f"App适配未绑定ModuleRunRef外部锚：{relative}")
    for module in ("datafetcher", "payoffer", "pricer", "backtester", "reporter"):
        if any((app_root / "frontend").rglob(f"{module}.html")):
            errors.append(f"App frontend不得复制模块页面：{module}.html")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="验证OptionHelper App开发源和显式Capability")
    parser.add_argument("--app-root", type=Path, default=ROOT / "products" / "app")
    parser.add_argument("--capability-root", type=Path, required=True)
    args = parser.parse_args()
    errors = verify_app(args.app_root.resolve(), capability_root=args.capability_root)
    if errors:
        raise SystemExit("\n".join(errors))
    print("App source verified")


if __name__ == "__main__":
    main()
