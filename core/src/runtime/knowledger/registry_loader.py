"""OptionReg唯一加载器。

业务模块不得直接导入或硬编码``references/optionreg.py``。开发态与发行态的
物理路径只在此处经Bootstrap解析。
"""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from threading import RLock
from typing import Any, Mapping

from runtime.bootstrap import bootstrap_runtime
from runtime.contracts.contract_types import deep_freeze, deep_thaw


class RegistryLoadError(ValueError):
    """OptionReg文件或顶层结构无效。"""


_REGISTRY_PIN_LOCK = RLock()
_REGISTRY_PINNED_HASHES: dict[str, str] = {}


def get_default_registry_path() -> Path:
    return bootstrap_runtime(mutate_sys_path=False).knowledger_root / "optionreg.py"


@lru_cache(maxsize=8)
def _load_registry_cached(path_text: str, content_hash: str, payload: bytes) -> Mapping[str, Any]:
    """Execute the exact authenticated OptionReg bytes once per content hash."""

    del content_hash
    source = Path(path_text)
    try:
        namespace: dict[str, Any] = {"__file__": str(source), "__name__": "__optionhelper_registry__"}
        exec(compile(payload, str(source), "exec", dont_inherit=True), namespace)
        registry = namespace.get("REGISTRY")
    except Exception as error:
        raise RegistryLoadError("OptionReg加载失败") from error
    if not isinstance(registry, Mapping) or set(registry) != {"term_catalog", "products"}:
        raise RegistryLoadError("OptionReg顶层只能包含term_catalog与products")
    if not isinstance(registry["term_catalog"], Mapping) or not isinstance(registry["products"], Mapping):
        raise RegistryLoadError("OptionReg的term_catalog与products必须为映射")
    if not registry["term_catalog"] or not registry["products"]:
        raise RegistryLoadError("OptionReg的term_catalog与products不能为空")
    if any(not isinstance(key, str) or not key or not isinstance(value, Mapping) for key, value in registry["products"].items()):
        raise RegistryLoadError("OptionReg产品必须使用非空字符串ID并映射到产品对象")
    return deep_freeze(registry)


def _read_regular_source(path: Path) -> bytes:
    """Read one non-symlink OptionReg snapshot without reopening its pathname."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise RegistryLoadError("OptionReg不存在") from error
    except OSError as error:
        raise RegistryLoadError("OptionReg必须是可安全读取的普通文件") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RegistryLoadError("OptionReg必须是普通文件")
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _registry_snapshot(path: str | Path | None) -> Mapping[str, Any]:
    configured = Path(path or get_default_registry_path()).expanduser()
    allowed_configured = get_default_registry_path().expanduser()
    if configured.is_symlink() or allowed_configured.is_symlink():
        raise RegistryLoadError("Registry Loader不接受符号链接OptionReg")
    source = configured.resolve()
    allowed = allowed_configured.resolve()
    if source != allowed:
        raise RegistryLoadError("Registry Loader只允许读取Bootstrap注入的OptionReg")
    payload = _read_regular_source(source)
    content_hash = sha256(payload).hexdigest()
    expected_hash = _expected_release_registry_hash()
    if expected_hash is not None and content_hash != expected_hash:
        raise RegistryLoadError("OptionReg与发行Capability清单哈希不一致")
    key = str(source)
    with _REGISTRY_PIN_LOCK:
        pinned = _REGISTRY_PINNED_HASHES.setdefault(key, content_hash)
    if content_hash != pinned:
        raise RegistryLoadError("OptionReg在Runtime启动后发生变化；请重启受控Host并重新验证Registry")
    return _load_registry_cached(key, content_hash, payload)


def _expected_release_registry_hash() -> str | None:
    """Return the installed Capability's immutable OptionReg digest, if any."""

    paths = bootstrap_runtime(mutate_sys_path=False)
    if paths.mode != "release":
        return None
    manifest_path = paths.project_root / "capability-manifest.json"
    try:
        manifest = json.loads(_read_regular_source(manifest_path))
        expected = manifest["content_hashes"]["scripts/knowledger/optionreg.py"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, RegistryLoadError) as error:
        raise RegistryLoadError("发行Capability清单缺少OptionReg哈希") from error
    if not isinstance(expected, str) or len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise RegistryLoadError("发行Capability清单中的OptionReg哈希无效")
    return expected


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    """返回可由调用方修改的隔离副本，不暴露进程内验证缓存。"""
    return deep_thaw(_registry_snapshot(path))


def load_term_catalog(path: str | Path | None = None) -> Mapping[str, Any]:
    """返回同一Registry快照中的深度只读term_catalog，供公式热路径复用。"""
    return _registry_snapshot(path)["term_catalog"]
