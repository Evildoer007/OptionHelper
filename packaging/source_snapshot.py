"""Freeze every source input used by one build transaction."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from hashlib import sha256
import json
import importlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from typing import Iterable, Iterator

APP_PACKAGING = Path(__file__).resolve().parent / "app"
if str(APP_PACKAGING) not in sys.path:
    sys.path.insert(0, str(APP_PACKAGING))

from platform_payload import (  # noqa: E402
    MACOS_BUILD_INPUTS,
    MACOS_SOURCE_MAPPINGS,
    SOURCE_EXCLUDES,
    WINDOWS_BUILD_INPUTS,
    WINDOWS_SOURCE_MAPPINGS,
)


ALWAYS_EXCLUDED_PARTS = frozenset({
    ".cache",
    ".DS_Store",
    ".git",
    ".hypothesis",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".nox",
    ".optionhelper",
    ".optionhelper-agent-sessions",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
})
FINDER_COPY_PATTERN = re.compile(r"^.+ \d+(?:\.[^.]*)*$")


class SourceSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotSelector:
    path: str
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        value = PurePosixPath(self.path)
        if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
            raise SourceSnapshotError(f"源码快照路径无效：{self.path}")


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _excluded(relative: Path, patterns: tuple[str, ...]) -> bool:
    if (
        relative.suffix in {".pyc", ".pyo"}
        or any(part in ALWAYS_EXCLUDED_PARTS for part in relative.parts)
        or any(FINDER_COPY_PATTERN.fullmatch(part) for part in relative.parts)
    ):
        return True
    value = relative.as_posix()
    return any(fnmatch(value, pattern) or fnmatch(relative.name, pattern) for pattern in patterns)


def _selected_files(repository_root: Path, selectors: Iterable[SnapshotSelector]) -> dict[str, Path]:
    values: dict[str, Path] = {}
    for selector in selectors:
        if _excluded(Path(selector.path), selector.excludes):
            continue
        source = repository_root / selector.path
        if not source.exists():
            raise SourceSnapshotError(f"源码快照输入不存在：{selector.path}")
        candidates = (source,) if source.is_file() else tuple(sorted(source.rglob("*")))
        for path in candidates:
            relative_to_source = Path(path.name) if source.is_file() else path.relative_to(source)
            if _excluded(relative_to_source, selector.excludes):
                continue
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except OSError as error:
                    raise SourceSnapshotError(f"源码快照含断裂符号链接：{path}") from error
                if not resolved.is_file():
                    raise SourceSnapshotError(f"源码快照符号链接目标不是文件：{path}")
            elif not path.is_file():
                continue
            relative = path.relative_to(repository_root).as_posix()
            existing = values.get(relative)
            if existing is not None and _file_hash(existing) != _file_hash(path):
                raise SourceSnapshotError(f"源码快照来源冲突：{relative}")
            values[relative] = path
    if not values:
        raise SourceSnapshotError("源码快照没有任何输入文件")
    regular_targets = {
        path.resolve(strict=True)
        for path in values.values()
        if not path.is_symlink()
    }
    for relative, path in values.items():
        if not path.is_symlink():
            continue
        link_target = os.readlink(path)
        if Path(link_target).is_absolute() or path.resolve(strict=True) not in regular_targets:
            raise SourceSnapshotError(f"源码快照符号链接目标不在冻结闭包：{relative} -> {link_target}")
    return dict(sorted(values.items()))


def _records(files: dict[str, Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative, path in files.items():
        record: dict[str, object] = {
            "path": relative,
            "sha256": _file_hash(path),
            "size": path.stat().st_size,
            "executable": bool(path.stat().st_mode & stat.S_IXUSR),
        }
        if path.is_symlink():
            record["kind"] = "symlink"
            record["link_target"] = os.readlink(path)
        else:
            record["kind"] = "file"
        records.append(record)
    return records


def _map_selectors(path: Path) -> list[SnapshotSelector]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceSnapshotError(f"无法读取源码映射：{path}") from error
    selectors: list[SnapshotSelector] = []
    for item in payload.get("files", []):
        selectors.append(SnapshotSelector(str(item["source"])))
    for item in payload.get("trees", []):
        selectors.append(SnapshotSelector(str(item["source"]), tuple(item.get("exclude", ()))))
    return selectors


def default_snapshot_selectors(repository_root: Path) -> tuple[SnapshotSelector, ...]:
    selectors = [
        *_map_selectors(repository_root / "packaging" / "skill" / "package-source-map.json"),
        *_map_selectors(repository_root / "packaging" / "app" / "capability-source-map.json"),
    ]
    for source, _target in (*MACOS_SOURCE_MAPPINGS, *WINDOWS_SOURCE_MAPPINGS):
        selectors.append(SnapshotSelector(source, tuple(SOURCE_EXCLUDES.get(source, ()))))
    selectors.extend(SnapshotSelector(path) for path in (*MACOS_BUILD_INPUTS, *WINDOWS_BUILD_INPUTS))
    selectors.extend((
        SnapshotSelector(
            "packaging",
            ("tests/**", "**/tests/**", "**/node_modules/**", "**/dist/**"),
        ),
        SnapshotSelector("packaging/skill/package-source-map.json"),
        SnapshotSelector("packaging/app/capability-source-map.json"),
        SnapshotSelector("products/app/runtime/optionhelper_agent_runtime", ("dist/**", "test/**")),
        SnapshotSelector("packaging/app/agent_runtime"),
        SnapshotSelector("LICENSES"),
    ))
    unique: dict[tuple[str, tuple[str, ...]], SnapshotSelector] = {}
    for selector in selectors:
        unique[(selector.path, selector.excludes)] = selector
    return tuple(unique.values())


def _remove_write_permissions(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def _restore_write_permissions(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(root.stat().st_mode | stat.S_IWUSR)
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


@contextmanager
def snapshot_module_environment(snapshot_root: Path, live_root: Path) -> Iterator[None]:
    """Resolve downstream build modules only from the frozen source tree."""

    snapshot = snapshot_root.expanduser().resolve()
    live = live_root.expanduser().resolve()
    import_paths = (
        snapshot / "core" / "src",
        snapshot / "packaging",
        snapshot / "packaging" / "skill",
        snapshot / "packaging" / "app",
        snapshot / "packaging" / "app" / "agent_runtime",
        snapshot / "packaging" / "app" / "macos",
        snapshot / "packaging" / "app" / "windows",
    )
    protected = {
        (live / "packaging" / name).resolve()
        for name in ("build_current.py", "release.py", "source_snapshot.py")
    }
    displaced: dict[str, object] = {}
    for name, module in tuple(sys.modules.items()):
        value = getattr(module, "__file__", None)
        if not value:
            continue
        try:
            path = Path(value).resolve()
            path.relative_to(live)
        except (OSError, ValueError):
            continue
        if path in protected:
            continue
        if path.is_relative_to(live / "packaging") or path.is_relative_to(live / "core" / "src" / "runtime"):
            displaced[name] = module
            del sys.modules[name]
    previous_path = list(sys.path)
    sys.path[:0] = [str(path) for path in import_paths]
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name, module in tuple(sys.modules.items()):
            value = getattr(module, "__file__", None)
            if not value:
                continue
            try:
                Path(value).resolve().relative_to(snapshot)
            except (OSError, ValueError):
                continue
            del sys.modules[name]
        sys.modules.update(displaced)
        sys.path[:] = previous_path
        importlib.invalidate_caches()


@dataclass
class FrozenSourceSnapshot:
    live_root: Path
    root: Path
    selectors: tuple[SnapshotSelector, ...]
    source_records: list[dict[str, object]]

    @property
    def agent_runtime_source(self) -> Path:
        return self.root / "products" / "app" / "runtime" / "optionhelper_agent_runtime"

    @property
    def agent_runtime_build_tools(self) -> Path:
        return self.root / "packaging" / "app" / "agent_runtime"

    def verify_current(self) -> None:
        current = _records(_selected_files(self.live_root, self.selectors))
        if current != self.source_records:
            before = {str(item["path"]): item for item in self.source_records}
            after = {str(item["path"]): item for item in current}
            changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
            detail = ", ".join(changed[:12])
            if len(changed) > 12:
                detail += f"等{len(changed)}项"
            raise SourceSnapshotError(f"构建期间源码发生变化：{detail}")


@contextmanager
def frozen_source_snapshot(
    repository_root: Path,
    destination: Path,
    *,
    selectors: Iterable[SnapshotSelector] | None = None,
) -> Iterator[FrozenSourceSnapshot]:
    live_root = repository_root.expanduser().resolve()
    target = destination.expanduser().resolve()
    if target.exists():
        raise SourceSnapshotError(f"源码快照目录已存在：{target}")
    selected = tuple(selectors or default_snapshot_selectors(live_root))
    files = _selected_files(live_root, selected)
    source_records = _records(files)
    target.mkdir(parents=True, exist_ok=False)
    try:
        for relative, source in files.items():
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output, follow_symlinks=False)
        copied = {str(item["path"]): target / str(item["path"]) for item in source_records}
        if _records(copied) != source_records:
            raise SourceSnapshotError("源码在快照复制期间发生变化")
        if _records(_selected_files(live_root, selected)) != source_records:
            raise SourceSnapshotError("源码在快照复制期间发生变化")
        snapshot = FrozenSourceSnapshot(live_root, target, selected, source_records)
        _remove_write_permissions(target)
        yield snapshot
    finally:
        _restore_write_permissions(target)
