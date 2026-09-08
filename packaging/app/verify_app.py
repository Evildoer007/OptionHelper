#!/usr/bin/env python3
"""App开发源与内置Capability边界验收。"""

from __future__ import annotations

import argparse
import ast
import posixpath
import re
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from verify_capability import verify_app_capability


APP_RUNTIME_SOURCE_DIRECTORIES = ("backend", "frontend", "desktop", "config", "runtime")


def verify_frontend_imports(app_root: Path) -> list[str]:
    """Check that every relative JS import is both shipped and served."""
    server = app_root / "backend" / "app_server.py"
    if not server.is_file():
        return []
    tree = ast.parse(server.read_text(encoding="utf-8"))
    assets = next((ast.literal_eval(node.value.args[0]) for node in tree.body
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "FRONTEND_ASSETS" for target in node.targets)), set())
    errors = []
    frontend = app_root / "frontend"
    for name in sorted(assets):
        path = frontend / name
        if not path.is_file():
            errors.append(f"App静态资源缺失：{name}")
            continue
        if path.suffix != ".js":
            continue
        for dependency in re.findall(r"(?:from\s*|import\s*\(?)['\"](\.[^'\"]+)['\"]", path.read_text(encoding="utf-8")):
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), dependency.split("?")[0]))
            if resolved not in assets:
                errors.append(f"App模块导入未开放：{name} -> {resolved}")
    return errors


def verify_app(app_root: Path, *, capability_root: Path | None) -> list[str]:
    errors: list[str] = verify_frontend_imports(app_root)
    errors.extend(
        f"App缺少{item}/"
        for item in APP_RUNTIME_SOURCE_DIRECTORIES
        if not (app_root / item).is_dir()
    )
    if capability_root is None:
        errors.append("App验收必须显式提供已验证Capability根目录")
    else:
        errors.extend(verify_app_capability(capability_root.resolve()))
    for relative in ("backend/reporter_adapter.py", "backend/stores/result_store.py"):
        adapter = app_root / relative
        if not adapter.is_file():
            errors.append(f"App缺少ModuleRunRef当前协议适配：{relative}")
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
