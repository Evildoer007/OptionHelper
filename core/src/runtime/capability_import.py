"""One process-wide lock and byte-pinned loader for verified Capabilities."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from hashlib import sha256
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import stat
import sys
from threading import RLock
from types import ModuleType
from typing import Iterator, Mapping


# Builtins is process-global even when the same App source is imported through
# both ``backend.*`` and ``products.app.backend.*``.  ``setdefault`` gives all
# aliases one RLock without retaining any import-path-specific module state.
CAPABILITY_IMPORT_LOCK = builtins.__dict__.setdefault("_optionhelper_capability_import_lock", RLock())


class CapabilityImportError(RuntimeError):
    """A Capability source does not match its authenticated manifest."""


def _read_verified_source(path: Path, expected_hash: str, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CapabilityImportError(f"{label}不可安全读取") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CapabilityImportError(f"{label}必须是普通文件")
            payload = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if sha256(payload).hexdigest() != expected_hash:
        raise CapabilityImportError(f"{label}与Capability清单哈希不一致")
    return payload


class _VerifiedModuleLoader(importlib.abc.Loader):
    def __init__(self, source: Path, expected_hash: str, manifest_name: str, is_package: bool) -> None:
        self._source = source
        self._expected_hash = expected_hash
        self._manifest_name = manifest_name
        self._is_package = is_package

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        payload = _read_verified_source(self._source, self._expected_hash, self._manifest_name)
        module.__file__ = str(self._source)
        if self._is_package:
            module.__path__ = [str(self._source.parent)]  # type: ignore[attr-defined]
        exec(compile(payload, str(self._source), "exec", dont_inherit=True), module.__dict__)


class _VerifiedCapabilityFinder(importlib.abc.MetaPathFinder):
    """Load ``modules.*`` from the exact bytes committed by a Capability."""

    def __init__(self, scripts_root: Path, hashes: Mapping[str, str]) -> None:
        self._root = scripts_root
        self._hashes = dict(hashes)

    def find_spec(
        self, fullname: str, path: object = None, target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        if fullname == "modules":
            directory = self._root / "modules"
            if not directory.is_dir() or directory.is_symlink():
                raise CapabilityImportError("Capability模块目录不可用")
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(directory)]
            return spec
        if not fullname.startswith("modules."):
            return None
        relative = Path(*fullname.split("."))
        candidates = (
            (relative / "__init__.py", True),
            (relative.with_suffix(".py"), False),
        )
        for candidate, is_package in candidates:
            source = self._root / candidate
            manifest_name = f"scripts/{candidate.as_posix()}"
            if not source.exists():
                continue
            expected = self._hashes.get(manifest_name)
            if not isinstance(expected, str) or len(expected) != 64:
                raise CapabilityImportError(f"Capability未声明模块源：{manifest_name}")
            return importlib.machinery.ModuleSpec(
                fullname,
                _VerifiedModuleLoader(source, expected, manifest_name, is_package),
                is_package=is_package,
            )
        raise CapabilityImportError(f"Capability模块源不存在：{fullname}")


@contextmanager
def verified_capability_modules(
    scripts_root: str | Path,
    content_hashes: Mapping[str, str],
) -> Iterator[None]:
    """Pin every dynamic Capability ``modules.*`` import to manifest bytes."""

    root = Path(scripts_root).expanduser().resolve()
    if not root.is_dir() or root.is_symlink() or not isinstance(content_hashes, Mapping):
        raise CapabilityImportError("Capability源码根或清单无效")
    finder = _VerifiedCapabilityFinder(root, content_hashes)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        try:
            sys.meta_path.remove(finder)
        except ValueError:
            pass


def load_verified_source_module(
    module_name: str,
    scripts_root: str | Path,
    relative_source: str,
    content_hashes: Mapping[str, str],
) -> ModuleType:
    """Compile and execute one declared source from its single verified read."""

    root = Path(scripts_root).expanduser().resolve()
    relative = Path(relative_source)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
        raise CapabilityImportError("Capability源码路径无效")
    manifest_name = f"scripts/{relative.as_posix()}"
    expected = content_hashes.get(manifest_name)
    if not isinstance(expected, str) or len(expected) != 64:
        raise CapabilityImportError(f"Capability未声明源码：{manifest_name}")
    source = root / relative
    payload = _read_verified_source(source, expected, manifest_name)
    module = ModuleType(module_name)
    module.__file__ = str(source)
    module.__package__ = ""
    exec(compile(payload, str(source), "exec", dont_inherit=True), module.__dict__)
    return module


__all__ = (
    "CAPABILITY_IMPORT_LOCK", "CapabilityImportError", "load_verified_source_module",
    "verified_capability_modules",
)
