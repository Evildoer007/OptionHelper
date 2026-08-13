"""开发仓库与Skill发行包共用的运行时定位器。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Iterator


class BootstrapError(RuntimeError):
    """运行根目录或必要资源无法定位。"""


_SCOPED_RELEASE_RUNTIME_ROOT: ContextVar[Path | None] = ContextVar(
    "optionhelper_scoped_release_runtime_root", default=None,
)
_SCOPED_DEVELOPMENT_STORE_ROOTS: ContextVar[tuple[Path, Path] | None] = ContextVar(
    "optionhelper_scoped_development_store_roots", default=None,
)


@contextmanager
def release_runtime_scope(runtime_root: str | Path) -> Iterator[Path]:
    """Bind one App-owned writable root to the current Capability call only.

    A verified Skill is read-only.  The desktop App therefore supplies its
    own controlled writable root through a ContextVar instead of mutating
    process environment variables that could leak to another caller.
    """
    root = Path(runtime_root).expanduser().resolve()
    if not root.is_dir():
        raise BootstrapError("App受控Capability运行目录不可用")
    token = _SCOPED_RELEASE_RUNTIME_ROOT.set(root)
    try:
        yield root
    finally:
        _SCOPED_RELEASE_RUNTIME_ROOT.reset(token)


@contextmanager
def local_runtime_scope(data_root: str | Path, result_root: str | Path) -> Iterator[tuple[Path, Path]]:
    """Bind one standalone Host's external writable stores for this call.

    Development source may live in the repository, but local user data never
    does.  The scope is process-local and avoids mutating environment
    variables, so nested module imports cannot silently select repository or
    system-default storage.
    """

    data = Path(data_root).expanduser().resolve()
    result = Path(result_root).expanduser().resolve()
    if not data.is_dir() or not result.is_dir() or data == result:
        raise BootstrapError("本机Host必须提供两个已预检的外部Store目录")
    token = _SCOPED_DEVELOPMENT_STORE_ROOTS.set((data, result))
    try:
        yield data, result
    finally:
        _SCOPED_DEVELOPMENT_STORE_ROOTS.reset(token)


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    knowledger_root: Path
    result_root: Path
    data_root: Path
    module_source_roots: tuple[Path, ...]
    mode: str

    def module_page_dir(self, module: str) -> Path:
        if self.mode == "release":
            return self.project_root / "assets" / "pages" / module
        return self.project_root / "modules" / module / "page"


def discover_project_root(start: str | Path | None = None) -> Path:
    """向上查找开发仓库根；不依赖当前工作目录。"""
    override = os.environ.get("OPTIONHELPER_PROJECT_ROOT")
    candidate = Path(override or start or __file__).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for root in (candidate, *candidate.parents):
        if (root / "SKILL.md").is_file() and (root / "references" / "optionreg.py").is_file():
            return root
    raise BootstrapError("无法定位OptionHelper开发仓库；可设置OPTIONHELPER_PROJECT_ROOT")


def _release_root(start: Path) -> Path | None:
    for root in (start, *start.parents):
        if (root / "SKILL.md").is_file() and (root / "scripts" / "knowledger" / "optionreg.py").is_file():
            return root
    return None


def _external_release_roots(release: Path) -> tuple[Path, Path]:
    """发行Skill只能使用显式且安装目录外的可写Store。"""
    scoped_runtime_root = _SCOPED_RELEASE_RUNTIME_ROOT.get()
    if scoped_runtime_root is not None:
        data_root = (scoped_runtime_root / "data").resolve()
        result_root = (scoped_runtime_root / "result").resolve()
    else:
        data_root, result_root = _environment_release_roots()
    for label, path in (("DataStore", data_root), ("ResultStore", result_root)):
        if path == release or release in path.parents:
            raise BootstrapError(f"发行Skill的{label}不得位于安装目录内")
    if data_root == result_root:
        raise BootstrapError("发行Skill的DataStore与ResultStore必须使用不同根目录")
    return data_root, result_root


def _environment_release_roots() -> tuple[Path, Path]:
    """Resolve only explicitly configured standalone-Skill stores.

    A released Capability must not silently turn its current working directory
    into a write authority.  The App injects a scoped runtime root; a standalone
    local Host must supply two Store roots or one explicit runtime root.
    """
    data_value = os.environ.get("OPTIONHELPER_DATA_ROOT")
    result_value = os.environ.get("OPTIONHELPER_RESULT_ROOT")
    runtime_value = os.environ.get("OPTIONHELPER_RUNTIME_ROOT")
    if bool(data_value) != bool(result_value):
        raise BootstrapError("发行Skill必须同时设置OPTIONHELPER_DATA_ROOT和OPTIONHELPER_RESULT_ROOT")
    if data_value and result_value:
        data_root = Path(data_value).expanduser().resolve()
        result_root = Path(result_value).expanduser().resolve()
    elif runtime_value:
        runtime_root = Path(runtime_value).expanduser().resolve()
        data_root = (runtime_root / "data").resolve()
        result_root = (runtime_root / "result").resolve()
    else:
        raise BootstrapError(
            "发行Skill必须由App注入运行根，或显式设置OPTIONHELPER_DATA_ROOT和OPTIONHELPER_RESULT_ROOT"
        )
    return data_root, result_root


def bootstrap_runtime(start: str | Path | None = None, *, mutate_sys_path: bool = True) -> RuntimePaths:
    """解析开发态或发行态资源，并按需注册唯一模块源码根。"""
    source = Path(start or __file__).expanduser().resolve()
    release = _release_root(source if source.is_dir() else source.parent)
    if release is not None:
        data_root, result_root = _external_release_roots(release)
        paths = RuntimePaths(
            project_root=release,
            knowledger_root=release / "scripts" / "knowledger",
            result_root=result_root,
            data_root=data_root,
            module_source_roots=(release / "scripts",),
            mode="release",
        )
    else:
        root = discover_project_root(source)
        scoped_roots = _SCOPED_DEVELOPMENT_STORE_ROOTS.get()
        if scoped_roots is not None:
            data_root, result_root = scoped_roots
        else:
            data_root = Path(os.environ.get("OPTIONHELPER_DATA_ROOT", root / "data")).expanduser().resolve()
            result_root = Path(os.environ.get("OPTIONHELPER_RESULT_ROOT", root / "result")).expanduser().resolve()
        module_roots = tuple(
            path
            for path in sorted((root / "modules").glob("*/src"))
            if path.is_dir()
        )
        paths = RuntimePaths(
            project_root=root,
            knowledger_root=root / "references",
            result_root=result_root,
            data_root=data_root,
            module_source_roots=module_roots,
            mode="development",
        )
    if mutate_sys_path:
        for module_root in reversed(paths.module_source_roots):
            value = str(module_root)
            if value not in sys.path:
                sys.path.insert(0, value)
    return paths
