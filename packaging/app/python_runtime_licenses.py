"""Package and verify licenses for distributions collected by PyInstaller."""

from __future__ import annotations

from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
from typing import Callable, Iterable


REQUIRED_FROZEN_DISTRIBUTIONS = frozenset({"certifi", "numpy", "pandas", "scipy", "numba", "llvmlite"})
LICENSE_TOKENS = ("license", "licence", "copying", "notice", "copyright")
FORBIDDEN_LICENSE_PATH_PARTS = frozenset({
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "example",
    "examples",
    "sample",
    "samples",
    "test",
    "tests",
})
FORBIDDEN_LICENSE_SUFFIXES = frozenset({
    ".a", ".c", ".cc", ".class", ".cpp", ".dll", ".dylib", ".exe",
    ".h", ".hpp", ".js", ".o", ".obj", ".py", ".pyc", ".pyi", ".pyo",
    ".so", ".ts",
})
MAX_LICENSE_DOCUMENT_BYTES = 4 * 1024 * 1024
FALLBACK_LICENSES = {
    "openpyxl": ("openpyxl-LICENSE.txt",),
}


class PythonRuntimeLicenseError(RuntimeError):
    pass


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^\s#]+)\s*", line)
        if match:
            versions[_canonical(match.group(1))] = match.group(2)
    if not versions:
        raise PythonRuntimeLicenseError(f"依赖锁没有固定分发版本：{path}")
    return versions


def _analysis_contains(analysis: str, path: Path) -> bool:
    value = str(path.resolve())
    return (
        repr(value) in analysis
        or json.dumps(value, ensure_ascii=False) in analysis
        or any(line.strip() == value for line in analysis.splitlines())
    )


def _is_license_document(relative: Path, path: Path) -> bool:
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or FORBIDDEN_LICENSE_PATH_PARTS.intersection(part.casefold() for part in relative.parts)
        or relative.suffix.casefold() in FORBIDDEN_LICENSE_SUFFIXES
        or not any(token in relative.name.casefold() for token in LICENSE_TOKENS)
        or path.is_symlink()
        or not path.is_file()
    ):
        return False
    size = path.stat().st_size
    if size <= 0 or size > MAX_LICENSE_DOCUMENT_BYTES:
        return False
    with path.open("rb") as handle:
        return b"\0" not in handle.read(min(size, 8192))


def _license_files(distribution: object) -> list[tuple[str, Path]]:
    values: dict[str, Path] = {}
    for relative in getattr(distribution, "files", ()) or ():
        relative_path = Path(str(relative))
        path = Path(distribution.locate_file(relative))
        if _is_license_document(relative_path, path):
            values[relative_path.as_posix()] = path
    return sorted(values.items())


def _distribution_is_collected(distribution: object, analysis: str) -> bool:
    for relative in getattr(distribution, "files", ()) or ():
        path = Path(distribution.locate_file(relative))
        if path.is_file() and _analysis_contains(analysis, path):
            return True
    return False


def _distribution_name(distribution: object) -> str:
    value = getattr(distribution, "metadata", None)
    if value is not None:
        try:
            name = value["Name"]
        except (KeyError, TypeError):
            name = None
        if name:
            return _canonical(str(name))
    name = getattr(distribution, "name", None)
    return _canonical(str(name)) if name else ""


def stage_pyinstaller_dependency_licenses(
    analysis_path: Path,
    destination: Path,
    *,
    requirements_lock: Path,
    repository_license_root: Path | None = None,
    distribution_lookup: Callable[[str], object] = metadata.distribution,
    distribution_inventory: Callable[[], Iterable[object]] = metadata.distributions,
    required_distributions: Iterable[str] = REQUIRED_FROZEN_DISTRIBUTIONS,
) -> dict[str, object]:
    if not analysis_path.is_file():
        raise PythonRuntimeLicenseError(f"缺少PyInstaller依赖分析：{analysis_path}")
    analysis = analysis_path.read_text(encoding="utf-8", errors="replace")
    locked = _locked_versions(requirements_lock)
    required = {_canonical(name) for name in required_distributions}
    candidates: dict[str, object] = {}
    for distribution in distribution_inventory():
        name = _distribution_name(distribution)
        if name:
            candidates.setdefault(name, distribution)
    for name in sorted(set(locked) | required):
        try:
            distribution = distribution_lookup(name)
        except (metadata.PackageNotFoundError, KeyError) as error:
            raise PythonRuntimeLicenseError(f"构建环境缺少冻结分发包：{name}") from error
        candidates[name] = distribution
        if name not in locked:
            locked[name] = str(getattr(distribution, "version", ""))
    records: list[dict[str, object]] = []
    destination.mkdir(parents=True, exist_ok=False)
    for name, distribution in sorted(candidates.items()):
        actual_version = str(getattr(distribution, "version", ""))
        expected_version = locked.get(name)
        if expected_version is not None and actual_version != expected_version:
            raise PythonRuntimeLicenseError(
                f"构建环境分发版本不一致：{name}，需要{expected_version}，当前{actual_version or 'missing'}"
            )
        if not _distribution_is_collected(distribution, analysis):
            continue
        files = _license_files(distribution)
        if not files and repository_license_root is not None:
            for filename in FALLBACK_LICENSES.get(name, ()):
                fallback = repository_license_root / filename
                if fallback.is_file():
                    files.append((f"controlled/{filename}", fallback))
        if not files:
            raise PythonRuntimeLicenseError(f"冻结分发包缺少可归档许可证或NOTICE：{name}")
        packaged_files: list[dict[str, str]] = []
        notice_files: list[str] = []
        target_root = destination / name
        for relative, source in files:
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            packaged_relative = target.relative_to(destination).as_posix()
            packaged_files.append({"path": packaged_relative, "sha256": _hash(target)})
            if "notice" in Path(relative).name.casefold() or "third-party" in Path(relative).name.casefold():
                notice_files.append(packaged_relative)
        records.append({
            "name": name,
            "version": actual_version,
            "files": packaged_files,
            "notice_files": sorted(notice_files),
        })
    included = {str(record["name"]) for record in records}
    missing = sorted(required - included)
    if missing:
        raise PythonRuntimeLicenseError("PyInstaller分析未包含必需冻结分发包：" + ", ".join(missing))
    inventory: dict[str, object] = {
        "schema": "optionhelper.python-runtime-license-inventory",
        "analysis": "PyInstaller/Analysis-00.toc",
        "distributions": records,
    }
    manifest = destination / "python-runtime-license-manifest.json"
    manifest.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return inventory


def verify_python_runtime_licenses(
    destination: Path,
    inventory: object,
    *,
    required_distributions: Iterable[str] = REQUIRED_FROZEN_DISTRIBUTIONS,
) -> None:
    if not isinstance(inventory, dict) or inventory.get("schema") != "optionhelper.python-runtime-license-inventory":
        raise PythonRuntimeLicenseError("Python运行时许可证清单无效")
    distributions = inventory.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        raise PythonRuntimeLicenseError("Python运行时许可证清单为空")
    seen: set[str] = set()
    all_declared_paths: set[str] = set()
    notice_count = 0
    for record in distributions:
        if not isinstance(record, dict):
            raise PythonRuntimeLicenseError("Python运行时许可证记录无效")
        name = str(record.get("name", ""))
        if not name or name in seen:
            raise PythonRuntimeLicenseError("Python运行时许可证分发包重复或缺失")
        seen.add(name)
        files = record.get("files")
        notices = record.get("notice_files")
        if not isinstance(files, list) or not files or not isinstance(notices, list):
            raise PythonRuntimeLicenseError(f"Python运行时许可证文件无效：{name}")
        declared_paths = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise PythonRuntimeLicenseError(f"Python运行时许可证哈希记录无效：{name}")
            relative = str(item["path"])
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or len(relative_path.parts) < 2
                or relative_path.parts[0] != name
            ):
                raise PythonRuntimeLicenseError(f"Python运行时许可证路径越界：{relative}")
            path = destination / relative
            archived_relative = Path(*relative_path.parts[1:])
            if not _is_license_document(archived_relative, path):
                raise PythonRuntimeLicenseError(f"Python运行时许可证含禁止文件：{relative}")
            if _hash(path) != item["sha256"]:
                raise PythonRuntimeLicenseError(f"Python运行时许可证哈希不一致：{relative}")
            declared_paths.add(relative)
            all_declared_paths.add(relative)
        if not set(map(str, notices)).issubset(declared_paths):
            raise PythonRuntimeLicenseError(f"Python运行时NOTICE未绑定许可证文件：{name}")
        notice_count += len(notices)
    if notice_count == 0:
        raise PythonRuntimeLicenseError("Python运行时许可证闭包缺少第三方NOTICE记录")
    required = {_canonical(name) for name in required_distributions}
    missing = sorted(required - seen)
    if missing:
        raise PythonRuntimeLicenseError("Python运行时许可证清单缺少必需冻结分发包：" + ", ".join(missing))
    manifest = destination / "python-runtime-license-manifest.json"
    if not manifest.is_file():
        raise PythonRuntimeLicenseError("缺少Python运行时许可证Manifest")
    try:
        persisted = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PythonRuntimeLicenseError("Python运行时许可证Manifest不是有效JSON") from error
    if persisted != inventory:
        raise PythonRuntimeLicenseError("Python运行时许可证Manifest与App记录不一致")
    actual_paths: set[str] = set()
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise PythonRuntimeLicenseError(f"Python运行时许可证含禁止符号链接：{path.relative_to(destination)}")
        if path.is_file() and path != manifest:
            actual_paths.add(path.relative_to(destination).as_posix())
    if actual_paths != all_declared_paths:
        raise PythonRuntimeLicenseError("Python运行时许可证文件集合与Manifest不一致")
