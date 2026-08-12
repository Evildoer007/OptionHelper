"""Private atomic-document support shared by formal App stores."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from ..errors import ValidationError


class _LocalDocumentStore:
    """App-private JSON persistence; domain stores remain the public API."""

    _FILES = {"sessions", "tasks", "settings", "audit", "contracts", "data_assets", "results", "result_recovery", "reports"}

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

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
            updated = mutator(value) or value
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
