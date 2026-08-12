#!/usr/bin/env python3
"""OptionHelper Skill安装前的依赖和外部Store检查。"""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable


_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#]+)$")


def installed_version(name: str) -> str:
    """Return the loaded runtime version for packages with split metadata."""

    if name.casefold() == "pillow":
        from PIL import __version__

        return __version__
    return metadata.version(name)


def read_requirements(path: Path) -> list[tuple[str, str]]:
    requirements: list[tuple[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQUIREMENT.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{line_number}必须使用name==version锁定格式")
        requirements.append(match.groups())
    if not requirements:
        raise ValueError(f"{path}没有锁定依赖")
    return requirements


def check_dependencies(
    requirements_path: Path,
    *,
    version_lookup: Callable[[str], str] = installed_version,
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    for name, expected in read_requirements(requirements_path):
        try:
            actual = version_lookup(name)
            status = "ok" if actual == expected else "version_mismatch"
        except metadata.PackageNotFoundError:
            actual = None
            status = "missing"
        findings.append({"name": name, "expected": expected, "actual": actual, "status": status})
    python_ok = sys.version_info >= (3, 11)
    return {
        "ok": python_ok and all(item["status"] == "ok" for item in findings),
        "python": {"required": ">=3.11", "actual": sys.version.split()[0], "status": "ok" if python_ok else "unsupported"},
        "requirements": findings,
        "data_provider": {"name": "iFind API", "credential": "IFIND_REFRESH_TOKEN"},
        "note": "Skill不会自动安装依赖或修改Python环境。",
    }


def check_data_api() -> dict[str, object]:
    """Check only whether the Host configured iFind; never contact a provider."""

    configured = bool(os.environ.get("IFIND_REFRESH_TOKEN", "").strip())
    return {
        "ok": configured,
        "data_provider": {
            "name": "iFind API",
            "credential": "IFIND_REFRESH_TOKEN",
            "status": "configured" if configured else "missing",
        },
        "note": "预检只检查凭据是否由Host配置，不发起网络或数据请求。",
    }


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def check_external_stores(
    skill_root: Path,
    data_root: str | None,
    result_root: str | None,
    *,
    runtime_root: str | None = None,
    project_root: Path | None = None,
) -> dict[str, object]:
    root = skill_root.expanduser().resolve()
    project = (project_root or Path.cwd()).expanduser().resolve()
    default_runtime = project / ".optionhelper" / "runtime"
    values = {
        "data_root": data_root or os.environ.get("OPTIONHELPER_DATA_ROOT") or str(project / "data"),
        "result_root": result_root or os.environ.get("OPTIONHELPER_RESULT_ROOT") or str(project / "result"),
        "runtime_root": runtime_root or os.environ.get("OPTIONHELPER_RUNTIME_ROOT") or str(default_runtime),
    }
    stores: dict[str, dict[str, object]] = {}
    ok = True
    for name, raw in values.items():
        if not raw:
            stores[name] = {"status": "missing", "reason": f"请设置OPTIONHELPER_{name.upper()}"}
            ok = False
            continue
        resolved = Path(raw).expanduser().resolve()
        if resolved == root or _inside(resolved, root):
            stores[name] = {"path": str(resolved), "status": "inside_installation"}
            ok = False
            continue
        stores[name] = {"path": str(resolved), "status": "external"}
    if ok:
        paths = {str(store["path"]) for store in stores.values()}
        if len(paths) != len(stores):
            for name, store in stores.items():
                if sum(store["path"] == other["path"] for other in stores.values()) > 1:
                    store["status"] = "duplicate_store_root"
            ok = False
    return {
        "ok": ok,
        "skill_root": str(root),
        "project_root": str(project),
        "stores": stores,
        "note": "项目级data、result和.optionhelper/runtime可以位于宿主项目内，但不得位于Skill安装目录；检查不创建目录，也不写入凭据或会话。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="检查OptionHelper Skill依赖与外部Store")
    parser.add_argument("--requirements", type=Path, default=Path(__file__).with_name("requirements.lock"))
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-root")
    parser.add_argument("--result-root")
    parser.add_argument("--runtime-root")
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--check-store", action="store_true")
    parser.add_argument("--check-data-api", action="store_true")
    args = parser.parse_args()
    explicit_check = args.check_dependencies or args.check_store or args.check_data_api
    check_dependencies_requested = args.check_dependencies or not explicit_check
    check_store_requested = args.check_store or not explicit_check
    report: dict[str, object] = {}
    ok = True
    if check_dependencies_requested:
        try:
            report["dependencies"] = check_dependencies(args.requirements)
        except (OSError, ValueError) as error:
            report["dependencies"] = {"ok": False, "status": "invalid_lock", "reason": str(error)}
        ok = ok and bool(report["dependencies"]["ok"])
    if check_store_requested:
        report["stores"] = check_external_stores(
            args.skill_root,
            args.data_root,
            args.result_root,
            runtime_root=args.runtime_root,
        )
        ok = ok and bool(report["stores"]["ok"])
    if args.check_data_api:
        report["data_api"] = check_data_api()
        ok = ok and bool(report["data_api"]["ok"])
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
