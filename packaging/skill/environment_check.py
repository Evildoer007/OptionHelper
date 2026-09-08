#!/usr/bin/env python3
"""OptionHelper Skill安装前的依赖、iFind和外部Store检查。"""

from __future__ import annotations

import argparse
from datetime import datetime
from getpass import getpass
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
import sysconfig
import tempfile
from typing import Callable, Mapping


# Configuration is executed from the installed Skill as well.  Do not leave
# interpreter caches beside its signed content manifest.
sys.dont_write_bytecode = True


_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#]+)$")
_MEMORY_FILENAME = "memory.md"
_MEMORY_MAX_BYTES = 16 * 1024
_MEMORY_TOKEN_LINE = re.compile(r"^\s*-\s*IFIND_REFRESH_TOKEN\s*:\s*(\S+)\s*$", re.MULTILINE)
PYPI_MIRROR_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
_DEPENDENCY_CACHE_FILENAME = "dependency-readiness.json"
_DEPENDENCY_CACHE_SCHEMA = 1


class MemoryConfigurationError(ValueError):
    """Raised when the project-local memory file is not safe to consume."""


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


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat_fingerprint(path: Path) -> dict[str, object] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"path": str(path.resolve()), "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def _environment_fingerprint(requirements_path: Path) -> dict[str, object]:
    roots: list[Path] = []
    candidates = [Path(sys.executable), Path(sys.prefix)]
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        candidates.append(Path(purelib))
    candidates.extend((Path(sys.prefix) / "conda-meta", Path(sys.prefix) / "pyvenv.cfg"))
    for candidate in candidates:
        if str(candidate) and candidate not in roots:
            roots.append(candidate)
    return {
        "requirements_sha256": _sha256_file(requirements_path),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "environment_state": [item for root in roots if (item := _stat_fingerprint(root)) is not None],
    }


def _dependency_cache_path(cache_root: Path) -> Path:
    return cache_root / _DEPENDENCY_CACHE_FILENAME


def _read_dependency_cache(cache_root: Path, requirements_path: Path, fingerprint: Mapping[str, object]) -> dict[str, object] | None:
    path = _dependency_cache_path(cache_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema") != _DEPENDENCY_CACHE_SCHEMA:
        return None
    key = _sha256_file(requirements_path)
    entries = payload.get("entries")
    entry = entries.get(key) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("fingerprint") != dict(fingerprint):
        return None
    report = entry.get("report")
    if not isinstance(report, Mapping) or not bool(report.get("ok")):
        return None
    return dict(report)


def _write_dependency_cache(
    cache_root: Path,
    requirements_path: Path,
    fingerprint: Mapping[str, object],
    report: Mapping[str, object],
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _dependency_cache_path(cache_root)
    payload: dict[str, object] = {"schema": _DEPENDENCY_CACHE_SCHEMA, "entries": {}}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, Mapping) and existing.get("schema") == _DEPENDENCY_CACHE_SCHEMA:
            payload["entries"] = dict(existing.get("entries") or {})
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    entries = payload["entries"]
    if not isinstance(entries, dict):
        entries = {}
        payload["entries"] = entries
    entries[_sha256_file(requirements_path)] = {
        "fingerprint": dict(fingerprint),
        "report": dict(report),
    }
    temporary_name: str | None = None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".dependency-readiness-", suffix=".tmp", dir=cache_root)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def check_dependencies(
    requirements_path: Path,
    *,
    version_lookup: Callable[[str], str] = installed_version,
    cache_root: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    requirements_path = requirements_path.expanduser().resolve()
    fingerprint = _environment_fingerprint(requirements_path) if cache_root is not None else None
    if cache_root is not None and not force and fingerprint is not None:
        cached = _read_dependency_cache(cache_root, requirements_path, fingerprint)
        if cached is not None:
            cached["cache"] = {"status": "hit"}
            return cached
    findings: list[dict[str, object]] = []
    for name, expected in read_requirements(requirements_path):
        try:
            actual = version_lookup(name)
            status = "ok" if actual == expected else "version_mismatch"
        except metadata.PackageNotFoundError:
            actual = None
            status = "missing"
        findings.append({"name": name, "expected": expected, "actual": actual, "status": status})
    python_ok = sys.version_info >= (3, 12)
    report: dict[str, object] = {
        "ok": python_ok and all(item["status"] == "ok" for item in findings),
        "python": {"required": ">=3.12", "actual": sys.version.split()[0], "status": "ok" if python_ok else "unsupported"},
        "requirements": findings,
        "data_provider": {"name": "iFind API", "credential": "IFIND_REFRESH_TOKEN"},
        "note": "Skill不会自动安装依赖或修改Python环境。",
    }
    report["cache"] = {"status": "bypass" if cache_root is None else "miss"}
    if cache_root is not None and bool(report["ok"]) and fingerprint is not None and not force:
        _write_dependency_cache(cache_root, requirements_path, fingerprint, report)
    return report


def _project_root(project_root: Path | None = None) -> Path:
    return (project_root or Path.cwd()).expanduser().resolve()


def _dependency_cache_root(project_root: Path | None, skill_root: Path | None) -> Path | None:
    if project_root is None:
        return None
    cache_root = (_project_root(project_root) / ".optionhelper" / "runtime").resolve()
    if skill_root is None:
        return cache_root
    try:
        cache_root.relative_to(skill_root.expanduser().resolve())
    except ValueError:
        return cache_root
    return None


def _assert_memory_outside_skill(project_root: Path, skill_root: Path | None) -> None:
    if skill_root is None:
        return
    installation = skill_root.expanduser().resolve()
    memory_root = project_root / ".optionhelper"
    try:
        memory_root.relative_to(installation)
    except ValueError:
        return
    raise MemoryConfigurationError("项目memory.md不得写入Skill安装目录")


def _memory_path(project_root: Path | None = None) -> Path:
    return _project_root(project_root) / ".optionhelper" / _MEMORY_FILENAME


def _read_memory_token(
    project_root: Path | None = None,
    *,
    skill_root: Path | None = None,
) -> str | None:
    project = _project_root(project_root)
    _assert_memory_outside_skill(project, skill_root)
    path = _memory_path(project)
    if path.parent.is_symlink():
        raise MemoryConfigurationError("本地配置目录不能是符号链接")
    if path.is_symlink():
        raise MemoryConfigurationError("本地memory.md不是普通文件")
    if not path.exists():
        return None
    if not path.is_file():
        raise MemoryConfigurationError("本地memory.md不是普通文件")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise MemoryConfigurationError("本地memory.md不可读取") from error
    if size > _MEMORY_MAX_BYTES:
        raise MemoryConfigurationError("本地memory.md超出允许大小")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise MemoryConfigurationError("本地memory.md编码或读取失败") from error
    matches = _MEMORY_TOKEN_LINE.findall(text)
    if len(matches) != 1 or any(character.isspace() for character in matches[0]):
        raise MemoryConfigurationError("本地memory.md缺少有效的IFIND_REFRESH_TOKEN配置")
    return matches[0]


def load_ifind_refresh_token(
    *,
    project_root: Path | None = None,
    skill_root: Path | None = None,
) -> str | None:
    """Read the user-confirmed project-local token without exposing it."""

    return _read_memory_token(project_root, skill_root=skill_root)


def check_data_api(
    *,
    project_root: Path | None = None,
    skill_root: Path | None = None,
) -> dict[str, object]:
    """Check iFind availability without exposing credentials or contacting a provider."""

    try:
        configured = bool(load_ifind_refresh_token(
            project_root=project_root,
            skill_root=skill_root,
        ))
        status = "configured" if configured else "missing"
    except MemoryConfigurationError:
        configured = False
        status = "invalid"
    return {
        "ok": configured,
        "data_provider": {
            "name": "iFind API",
            "credential": "IFIND_REFRESH_TOKEN",
            "status": status,
        },
        "note": "预检只检查iFind是否已安全配置，不发起网络或数据请求。",
    }


def save_ifind_refresh_token(
    token: str,
    project_root: Path | None = None,
    *,
    skill_root: Path | None = None,
) -> Path:
    """Atomically save a user-confirmed token in the project-local memory file."""

    if not isinstance(token, str) or not token.strip() or any(character.isspace() for character in token):
        raise ValueError("IFIND_REFRESH_TOKEN不能为空且不能包含空白字符")
    if len(token.encode("utf-8")) > _MEMORY_MAX_BYTES:
        raise ValueError("IFIND_REFRESH_TOKEN超出允许大小")
    project = _project_root(project_root)
    _assert_memory_outside_skill(project, skill_root)
    path = _memory_path(project)
    directory = path.parent
    if directory.exists() and directory.is_symlink():
        raise MemoryConfigurationError("本地配置目录不能是符号链接")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise MemoryConfigurationError("本地memory.md不是普通文件")
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    content = (
        "# OptionHelper本地配置\n\n"
        "## iFinD数据API\n\n"
        f"- IFIND_REFRESH_TOKEN: {token}\n"
        f"- updated_at: {timestamp}\n"
    )
    temporary_name: str | None = None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=directory)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path


def _readiness_guidance(
    next_action: str | None,
    requirements_path: Path,
    dependencies: Mapping[str, object],
) -> str:
    if next_action == "install_dependencies":
        python = dependencies.get("python")
        if isinstance(python, Mapping) and python.get("status") == "unsupported":
            return "当前Python不受支持。请先确认安装或恢复Python3.12或更高版本的环境，重新选择解释器后再检查。"
        return (
            "锁定依赖未就绪。请先确认安装，再使用当前解释器执行："
            f'"{sys.executable}" -m pip install -r "{requirements_path}" '
            f'--index-url "{PYPI_MIRROR_URL}"；安装后重新运行统一就绪检查。'
            "清华镜像不可用时保留原始错误，不自动切换其他环境或软件源。"
        )
    if next_action == "configure_data_api":
        return "iFind尚未配置。请在本机安全配置IFIND_REFRESH_TOKEN，或在项目目录运行environment_check.py --save-ifind-refresh-token后重新检查；无需提供Access Token。"
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
    force_dependencies: bool = False,
) -> dict[str, object]:
    """Return the single first-use gate for every OptionHelper workflow."""

    project = (project_root or Path.cwd()).expanduser().resolve()
    cache_root = _dependency_cache_root(project, skill_root)
    dependencies = check_dependencies(
        requirements_path,
        version_lookup=version_lookup,
        cache_root=cache_root,
        force=force_dependencies,
    )
    stores = check_external_stores(
        skill_root,
        data_root,
        result_root,
        runtime_root=runtime_root,
        project_root=project,
    )
    data_api = check_data_api(project_root=project, skill_root=skill_root)
    next_action = next(
        (
            action
            for action, item in (
                ("install_dependencies", dependencies),
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
    parser.add_argument(
        "--requirements",
        type=Path,
        action="append",
        help="锁定依赖文件；可重复传入多个文件",
    )
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-root")
    parser.add_argument("--result-root")
    parser.add_argument("--runtime-root")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--check-store", action="store_true")
    parser.add_argument("--check-data-api", action="store_true")
    parser.add_argument("--check-readiness", action="store_true")
    parser.add_argument("--force-dependencies", action="store_true", help="忽略本地就绪缓存并完整检查依赖")
    parser.add_argument("--save-ifind-refresh-token", action="store_true")
    args = parser.parse_args()
    requirement_paths = args.requirements or [Path(__file__).with_name("requirements.lock")]
    if args.check_readiness and len(requirement_paths) != 1:
        parser.error("--check-readiness一次只能检查一个依赖文件")
    if args.save_ifind_refresh_token:
        try:
            token = getpass("请输入iFinD Refresh Token（不会回显）：")
            path = save_ifind_refresh_token(token, args.project_root, skill_root=args.skill_root)
            print(json.dumps({"ok": True, "data_api": {"status": "configured", "storage": str(path)}}, ensure_ascii=False))
        except (OSError, ValueError, MemoryConfigurationError) as error:
            print(json.dumps({"ok": False, "data_api": {"status": "invalid", "reason": str(error)}}, ensure_ascii=False))
            raise SystemExit(1)
        return
    if args.check_readiness:
        try:
            report = check_readiness(
                requirement_paths[0],
                args.skill_root,
                args.data_root,
                args.result_root,
                runtime_root=args.runtime_root,
                project_root=args.project_root,
                force_dependencies=args.force_dependencies,
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
    explicit_check = args.check_dependencies or args.check_store or args.check_data_api
    check_dependencies_requested = args.check_dependencies or not explicit_check
    check_store_requested = args.check_store or not explicit_check
    report: dict[str, object] = {}
    ok = True
    if check_dependencies_requested:
        try:
            cache_root = _dependency_cache_root(args.project_root, args.skill_root)
            if len(requirement_paths) == 1:
                report["dependencies"] = check_dependencies(
                    requirement_paths[0],
                    cache_root=cache_root,
                    force=args.force_dependencies,
                )
            else:
                reports = {
                    str(path): check_dependencies(path, cache_root=cache_root, force=args.force_dependencies)
                    for path in requirement_paths
                }
                report["dependencies"] = reports
                report["cache"] = {
                    "status": "hit"
                    if all(item.get("cache", {}).get("status") == "hit" for item in reports.values())
                    else "miss"
                }
                ok = all(bool(item["ok"]) for item in reports.values())
        except (OSError, ValueError) as error:
            report["dependencies"] = {"ok": False, "status": "invalid_lock", "reason": str(error)}
            ok = False
        if len(requirement_paths) == 1:
            ok = ok and bool(report["dependencies"]["ok"])
    if check_store_requested:
        report["stores"] = check_external_stores(
            args.skill_root,
            args.data_root,
            args.result_root,
            runtime_root=args.runtime_root,
            project_root=args.project_root,
        )
        ok = ok and bool(report["stores"]["ok"])
    if args.check_data_api:
        report["data_api"] = check_data_api(project_root=args.project_root, skill_root=args.skill_root)
        ok = ok and bool(report["data_api"]["ok"])
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
