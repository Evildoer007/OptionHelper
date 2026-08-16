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
from typing import Callable, Mapping


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


def _configured(environ: Mapping[str, str], name: str) -> bool:
    return bool(environ.get(name, "").strip())


def check_model_api(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Check model configuration without exposing connection details or credentials."""

    values = os.environ if environ is None else environ
    if _configured(values, "OPTIONHELPER_HOST_URL"):
        return {"ok": True, "mode": "host", "status": "configured"}
    required = (
        "OPTIONHELPER_MODEL_BASE_URL",
        "OPTIONHELPER_MODEL",
        "OPTIONHELPER_MODEL_API_KEY",
    )
    configured = all(_configured(values, name) for name in required)
    return {"ok": configured, "mode": "direct", "status": "configured" if configured else "missing"}


def check_data_api(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Check only whether the Host configured iFind; never contact a provider."""

    values = os.environ if environ is None else environ
    configured = _configured(values, "IFIND_REFRESH_TOKEN")
    return {
        "ok": configured,
        "data_provider": {
            "name": "iFind API",
            "credential": "IFIND_REFRESH_TOKEN",
            "status": "configured" if configured else "missing",
        },
        "note": "预检只检查凭据是否由Host配置，不发起网络或数据请求。",
    }


def _readiness_guidance(
    next_action: str | None,
    requirements_path: Path,
    dependencies: Mapping[str, object],
) -> str:
    if next_action == "install_dependencies":
        python = dependencies.get("python")
        if isinstance(python, Mapping) and python.get("status") == "unsupported":
            return "当前Python不受支持。请先确认安装或恢复Python3.11或更高版本的环境，重新选择解释器后再检查。"
        return (
            "锁定依赖未就绪。请先确认安装，再使用当前解释器执行："
            f'"{sys.executable}" -m pip install -r "{requirements_path}"；安装后重新运行统一就绪检查。'
        )
    if next_action == "configure_model":
        return "模型尚未配置。请让Host配置模型，或完整设置兼容模型端点、模型名称和模型凭据后重新检查。"
    if next_action == "configure_data_api":
        return "iFind尚未配置。请让Host安全注入IFIND_REFRESH_TOKEN后重新检查；无需提供Access Token。"
    if next_action == "fix_store":
        return "项目Store不可用。请将data、result和.optionhelper/runtime设在Skill安装目录之外后重新检查。"
    return "统一就绪检查通过，可进入工作流。"


def check_readiness(
    requirements_path: Path,
    skill_root: Path,
    data_root: str | None = None,
    result_root: str | None = None,
    *,
    runtime_root: str | None = None,
    project_root: Path | None = None,
    version_lookup: Callable[[str], str] = installed_version,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the single first-use gate for every OptionHelper workflow."""

    dependencies = check_dependencies(requirements_path, version_lookup=version_lookup)
    stores = check_external_stores(
        skill_root,
        data_root,
        result_root,
        runtime_root=runtime_root,
        project_root=project_root,
    )
    model = check_model_api(environ)
    data_api = check_data_api(environ)
    next_action = next(
        (
            action
            for action, item in (
                ("install_dependencies", dependencies),
                ("configure_model", model),
                ("configure_data_api", data_api),
                ("fix_store", stores),
            )
            if not bool(item["ok"])
        ),
        None,
    )
    return {
        "ok": next_action is None,
        "dependencies": dependencies,
        "model": model,
        "data_api": data_api,
        "stores": stores,
        "next_action": next_action,
        "guidance": _readiness_guidance(next_action, requirements_path, dependencies),
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
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--check-store", action="store_true")
    parser.add_argument("--check-model-api", action="store_true")
    parser.add_argument("--check-data-api", action="store_true")
    parser.add_argument("--check-readiness", action="store_true")
    args = parser.parse_args()
    if args.check_readiness:
        try:
            report = check_readiness(
                args.requirements,
                args.skill_root,
                args.data_root,
                args.result_root,
                runtime_root=args.runtime_root,
                project_root=args.project_root,
            )
        except (OSError, ValueError) as error:
            report = {
                "ok": False,
                "dependencies": {"ok": False, "status": "invalid_lock", "reason": str(error)},
                "next_action": "fix_dependencies",
                "guidance": "依赖锁定文件不可读取。请确认使用了当前Skill的scripts/requirements.lock后重新检查。",
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not bool(report["ok"]):
            raise SystemExit(1)
        return
    explicit_check = args.check_dependencies or args.check_store or args.check_model_api or args.check_data_api
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
    if args.check_model_api:
        report["model"] = check_model_api()
        ok = ok and bool(report["model"]["ok"])
    if args.check_data_api:
        report["data_api"] = check_data_api()
        ok = ok and bool(report["data_api"]["ok"])
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
