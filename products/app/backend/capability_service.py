"""Controlled calls to a verified Capability module service.

The App uses this boundary only when a module exposes an App-safe service
entrypoint.  It never supplies filesystem paths or credential plaintext.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import hashlib
import importlib
from pathlib import Path
import sys
from typing import Any, Iterator

from runtime.bootstrap import release_runtime_scope
from runtime.capability_import import CAPABILITY_IMPORT_LOCK

from .errors import CapabilityIntegrityError, UnavailableCapabilityError, ValidationError
from .page_registry import PageRegistry


def _module_snapshot() -> dict[str, object]:
    """Return only the dynamic module namespace owned by a Capability.

    ``runtime`` intentionally stays outside this snapshot.  App Host contexts
    and Capability code must share the same Core runtime classes; evicting it
    would create incompatible type identities inside one process.
    """

    return {
        name: module
        for name, module in sys.modules.items()
        if name == "modules" or name.startswith("modules.")
    }


def _restore_modules(snapshot: dict[str, object]) -> None:
    for name in tuple(sys.modules):
        if name == "modules" or name.startswith("modules."):
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)


def verify_loaded_runtime_sources(scripts_root: str | Path) -> None:
    """Refuse a Capability call if shared Core runtime code drifted.

    The App deliberately retains ``runtime.*`` to preserve value-object type
    identity across the Host and the Capability.  That shared identity is safe
    only when every loaded file has byte-for-byte parity with the frozen copy
    under the verified Capability's ``scripts/runtime`` directory.
    """

    root = Path(scripts_root).expanduser().resolve()
    for name, module in tuple(sys.modules.items()):
        if name != "runtime" and not name.startswith("runtime."):
            continue
        source = getattr(module, "__file__", None)
        if not isinstance(source, str):
            raise CapabilityIntegrityError(f"Shared runtime source is not verifiable: {name}")
        source_path = Path(source).resolve()
        parts = name.split(".")[1:]
        expected = root / "runtime"
        if parts:
            expected = expected.joinpath(*parts)
        expected = expected / "__init__.py" if source_path.name == "__init__.py" else expected.with_suffix(".py")
        if not expected.is_file():
            raise CapabilityIntegrityError(f"Capability does not declare shared runtime source: {name}")
        if hashlib.sha256(source_path.read_bytes()).digest() != hashlib.sha256(expected.read_bytes()).digest():
            raise CapabilityIntegrityError(f"Shared runtime source differs from verified Capability: {name}")


@contextmanager
def capability_import_scope(
    scripts_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> Iterator[Path]:
    """Import one verified Capability without leaking ``modules.*`` globally.

    Evaluation and development code may import identically named module
    packages.  The App must never reuse them while executing an embedded,
    verified Capability.  The process-wide re-entrant lock serializes the
    short import-and-call scope, then restores the caller's namespace exactly.
    """

    root = Path(scripts_root).expanduser().resolve()
    if not root.is_dir():
        raise CapabilityIntegrityError(f"Capability scripts directory is unavailable: {root}")
    with CAPABILITY_IMPORT_LOCK:
        previous_path = list(sys.path)
        snapshot = _module_snapshot()
        previous_dont_write_bytecode = sys.dont_write_bytecode
        _restore_modules({})
        sys.path[:] = [str(root), *(item for item in previous_path if item != str(root))]
        sys.dont_write_bytecode = True
        try:
            with (
                release_runtime_scope(runtime_root)
                if runtime_root is not None
                else _null_runtime_scope()
            ):
                verify_loaded_runtime_sources(root)
                yield root
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
            _restore_modules(snapshot)
            sys.path[:] = previous_path


@contextmanager
def _null_runtime_scope() -> Iterator[None]:
    yield None


def load_verified_capability_service(module_name: str, scripts_root: str | Path) -> object:
    """Import a service and prove that its source belongs to this Capability."""

    root = Path(scripts_root).expanduser().resolve()
    service = importlib.import_module(f"modules.{module_name}.service")
    source = getattr(service, "__file__", None)
    if not isinstance(source, str):
        raise CapabilityIntegrityError(f"Capability service has no source path: modules.{module_name}.service")
    try:
        Path(source).resolve().relative_to(root)
    except ValueError as error:
        raise CapabilityIntegrityError(
            f"Capability service source is outside verified scripts: modules.{module_name}.service"
        ) from error
    return service


class CapabilityServiceCaller:
    """Loads one formal ``modules.<name>.service`` entrypoint at a time."""

    _import_lock = CAPABILITY_IMPORT_LOCK

    def __init__(self, registry: PageRegistry, *, runtime_root: str | Path | None = None) -> None:
        self._registry = registry
        self._runtime_root = Path(runtime_root).expanduser().resolve() if runtime_root is not None else None

    def call_app_datafetcher(
        self,
        request: dict[str, Any],
        caller_context: object,
        secret_ref: object | None,
        secret_port: object | None = None,
    ) -> dict[str, Any]:
        """Call only a Capability that declares the App-safe DataFetcher port.

        The legacy one-argument ``call_tool`` API cannot receive a trusted
        caller or an App-managed credential reference.  Falling back to it
        would silently use the module's local/environment defaults.
        """
        if not isinstance(request, dict):
            raise ValidationError("Capability service request must be an object")
        scripts_root = str(self._registry.capability_root / "scripts")
        self._registry.assert_execution_integrity()
        with capability_import_scope(scripts_root, runtime_root=self._runtime_root):
            try:
                service = load_verified_capability_service("datafetcher", scripts_root)
                handler = getattr(service, "call_tool_from_app", None)
                if not callable(handler):
                    raise UnavailableCapabilityError(
                        "datafetcher.app_secret_port",
                        "the verified DataFetcher Capability does not declare an App caller and SecretRef port",
                    )
                result = handler(
                    dict(request),
                    caller_context=caller_context,
                    secret_ref=secret_ref,
                    secret_port=secret_port,
                )
            except UnavailableCapabilityError:
                raise
            except Exception as error:
                raise UnavailableCapabilityError(
                    "datafetcher",
                    "the verified DataFetcher App port rejected or could not execute this request",
                ) from error
        if not isinstance(result, dict):
            raise ValidationError("Capability module service must return a JSON object")
        return result

    def read_app_datafetcher_asset(self, data_asset_id: str, caller_context: object) -> tuple[dict[str, Any], bytes]:
        """Read one indexed DataAsset through the verified Capability service."""

        if not isinstance(data_asset_id, str):
            raise ValidationError("DataAsset id must be a string")
        scripts_root = str(self._registry.capability_root / "scripts")
        self._registry.assert_execution_integrity()
        with capability_import_scope(scripts_root, runtime_root=self._runtime_root):
            try:
                service = load_verified_capability_service("datafetcher", scripts_root)
                handler = getattr(service, "read_data_asset", None)
                if not callable(handler):
                    raise UnavailableCapabilityError(
                        "datafetcher.download_port",
                        "the verified DataFetcher Capability does not declare an indexed asset read port",
                    )
                reference, content = handler(data_asset_id, caller=caller_context)
            except UnavailableCapabilityError:
                raise
            except (FileNotFoundError, PermissionError):
                raise KeyError(data_asset_id) from None
            except Exception as error:
                raise UnavailableCapabilityError(
                    "datafetcher.download",
                    "the verified DataFetcher download port rejected the request",
                ) from error
        if not is_dataclass(reference) or not isinstance(content, bytes):
            raise ValidationError("Capability DataFetcher download returned an invalid asset")
        return asdict(reference), content
