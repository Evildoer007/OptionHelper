"""Private atomic-document support shared by formal App stores."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from ..errors import ValidationError


def _windows_extended_path(value: str) -> str:
    """Use Win32 extended paths without requiring a machine-wide registry edit."""
    import ntpath

    value = ntpath.normpath(value)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\.\\") or not ntpath.isabs(value):
        raise ValueError("Storage requires an absolute filesystem path")
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith("\\"):
        raise ValueError("Storage requires an absolute drive path")
    return "\\\\?\\" + value


class _LocalDocumentStore:
    """App-private JSON persistence; domain stores remain the public API."""

    _FILES = {
        "sessions", "tasks", "settings", "audit", "data_assets",
        "results", "result_recovery", "reports", "report_documents", "product_rule_revisions",
    }

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        if os.name == "nt":
            # Reports add task/run/candidate directories under this root.
            # Prefix it before any store creates or reads nested artifacts.
            self._root = Path(_windows_extended_path(str(self._root)))
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._migrate_task_model_current_input_v1()

    def _migrate_task_model_current_input_v1(self) -> None:
        """Reset retired task-bound state exactly once for this state root.

        The migration deliberately preserves global market assets, settings,
        authentication records and credential vaults.  A prepared marker is
        durable before deletion starts, so reopening after an interrupted
        migration safely repeats the same bounded cleanup.
        """

        marker_path = self._root / "task-model-current-input-v1.json"
        marker = _read_task_model_marker(marker_path)
        if marker == "complete":
            return
        if marker not in {None, "prepared"}:
            raise ValidationError("Task model migration marker is invalid")
        if marker is None:
            _write_atomic_json(marker_path, {"migration": "task-model-current-input-v1", "status": "prepared"})

        for name in (
            "sessions", "tasks", "results", "result_recovery", "reports", "report_documents",
        ):
            _write_atomic_json(self._root / f"{name}.json", {})

        for name in (
            "contracts.json", "session-events.sqlite3", "task-operations.sqlite3",
            "tool-idempotency.sqlite3", "calculation-jobs.sqlite3",
        ):
            for suffix in ("", "-shm", "-wal"):
                path = self._root / f"{name}{suffix}"
                if path.is_symlink() or not path.exists():
                    continue
                if not path.is_file():
                    raise ValidationError(f"Task model migration target is not a regular file: {path.name}")
                path.unlink()

        for relative in (
            Path("attachments"),
            Path("agent-runtime-sessions"),
            Path("module-runs"),
            Path("diagnostics/compute"),
            Path("runtime/result"),
        ):
            path = (self._root / relative).resolve()
            if path == self._root or self._root not in path.parents:
                raise ValidationError("Task model migration target escaped the App state root")
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if not path.is_dir():
                    raise ValidationError(f"Task model migration target is not a directory: {relative}")
                shutil.rmtree(path)

        _write_atomic_json(marker_path, {"migration": "task-model-current-input-v1", "status": "complete"})

    def read(self, name: str) -> dict[str, Any]:
        self._validate_name(name)
        with self._lock:
            path = self._path(name)
            if not path.exists():
                return {}
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValidationError(f"Local App state is invalid: {path.name}") from error
            if not isinstance(value, dict):
                raise ValidationError(f"Local App state must be an object: {path.name}")
            return value

    def update(self, name: str, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        with self._lock:
            value = self.read(name)
            replacement = mutator(value)
            updated = value if replacement is None else replacement
            if not isinstance(updated, dict):
                raise ValidationError("Local App state mutator must return an object")
            encoded = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            target = self._path(name)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=self._root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            return updated

    def _path(self, name: str) -> Path:
        self._validate_name(name)
        return self._root / f"{name}.json"

    def _validate_name(self, name: str) -> None:
        if name not in self._FILES:
            raise ValidationError(f"Unsupported local App state collection: {name}")


def _read_task_model_marker(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Task model migration marker is unreadable") from error
    if not isinstance(value, dict) or value.get("migration") != "task-model-current-input-v1":
        raise ValidationError("Task model migration marker is invalid")
    status = value.get("status")
    return str(status) if isinstance(status, str) else "invalid"


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
