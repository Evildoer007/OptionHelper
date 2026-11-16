"""Private atomic-document support shared by formal App stores."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
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
    _LARGE_DOCUMENT = 32 * 1024 * 1024
    _REPORT_COLLECTIONS = frozenset({"reports", "report_documents"})
    _REPORT_CACHE_LIMIT = 16 * 1024 * 1024

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        if os.name == "nt":
            # Reports add task/run/candidate directories under this root.
            # Prefix it before any store creates or reads nested artifacts.
            self._root = Path(_windows_extended_path(str(self._root)))
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks = {name: RLock() for name in self._FILES}
        self._cache_lock = RLock()
        self._record_cache = None
        self._report_reads = {}
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
        with self._locks[name]:
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

    def read_record(self, name: str, key: str) -> Any:
        """Read one detached record; reparse only when the source file changes.

        Keep at most one parsed collection. File identity, size, modification
        and change timestamps detect atomic replacements and in-place edits.
        Callers still authorize the returned record and verify Core artifacts.
        Full reads and writes never borrow this mutable private snapshot.
        """
        self._validate_name(name)
        with self._locks[name]:
            path = self._path(name)
            try:
                stat = path.stat()
            except FileNotFoundError:
                self._invalidate_record_cache(name)
                return None
            if name in self._REPORT_COLLECTIONS and stat.st_size >= self._LARGE_DOCUMENT:
                selected = self._read_large_reports(name, selector="record", value=key)
                if selected is not None:
                    return selected.get(key)
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            with self._cache_lock:
                cached = self._record_cache
            if cached is None or cached[0] != name or cached[1] != signature:
                records = self.read(name)
                if stat.st_size <= 256 * 1024 * 1024:
                    with self._cache_lock:
                        self._record_cache = (name, signature, records)
                else:
                    self._invalidate_record_cache(name)
            else:
                records = cached[2]
            return deepcopy(records.get(key))

    def read_task_reports(self, task_id: str) -> dict[str, Any]:
        """Read one task's report proofs and artifact metadata."""
        selected = self._read_large_reports("reports", selector="task", value=task_id)
        if selected is not None:
            return selected
        return {
            key: record for key, record in self.read("reports").items()
            if isinstance(record, dict) and record.get("task_id") == task_id
        }

    def read_report_edit_metadata(self, report_ids: tuple[str, ...]) -> dict[str, Any]:
        """Read the requested working-document titles and artifact metadata."""
        if not report_ids:
            return {}
        selected = self._read_large_reports(
            "report_documents", selector="edits", value=json.dumps(sorted(report_ids)),
        )
        if selected is not None:
            return selected
        records = self.read("report_documents")
        return {key: records[key] for key in report_ids if key in records}

    def _read_large_reports(self, name: str, *, selector: str, value: str) -> dict[str, Any] | None:
        if sqlite3.sqlite_version_info < (3, 51):
            return None
        predicates = {
            "record": "item.key = ?",
            "task": "json_extract(item.value, '$.task_id') = ?",
            "edits": "item.key IN (SELECT value FROM json_each(?))",
        }
        # Keep the report request and artifact proof fields. Only strip payloads
        # from listing responses, since opening the report still needs them.
        metadata = "jsonb_remove(item.value, '$.html', '$.chart_specs', '$.edit_requests')"
        projection = "json(item.value)" if selector == "record" else f"""
            CASE WHEN json_type(item.value, '$.artifacts') = 'array' THEN
                json(jsonb_set({metadata}, '$.artifacts', (
                    SELECT jsonb_group_array(jsonb_remove(artifact.value, '$.content', '$.content_base64'))
                    FROM jsonb_each(item.value, '$.artifacts') AS artifact
                )))
            ELSE json({metadata}) END
        """
        cache_key = (name, selector)
        with self._locks[name]:
            try:
                with self._path(name).open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    if stat.st_size < self._LARGE_DOCUMENT:
                        return None
                    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                    cached = self._report_reads.get(cache_key)
                    if cached is not None and cached[:2] == (signature, value):
                        return {key: json.loads(raw) for key, raw in cached[2]}
                    document = stream.read()
            except FileNotFoundError:
                self._report_reads.pop(cache_key, None)
                return {}
            try:
                with closing(sqlite3.connect(":memory:")) as connection:
                    rows = connection.execute(f"""
                        SELECT item.key, {projection}
                        FROM jsonb_each(jsonb(CAST(? AS TEXT))) AS item
                        WHERE item.type = 'object' AND {predicates[selector]}
                    """, (document, value)).fetchall()
                selected = {key: json.loads(raw) for key, raw in rows}
            except (sqlite3.Error, json.JSONDecodeError):
                return None
            if sum(len(raw) for _, raw in rows) <= self._REPORT_CACHE_LIMIT:
                self._report_reads[cache_key] = (signature, value, rows)
            else:
                self._report_reads.pop(cache_key, None)
            return selected

    def read_without_fields(self, name: str, fields: tuple[str, ...]) -> dict[str, Any]:
        """Read index metadata without materializing large embedded payloads."""
        self._validate_name(name)
        with self._locks[name]:
            path = self._path(name)
            try:
                large = path.stat().st_size >= self._LARGE_DOCUMENT
            except FileNotFoundError:
                return {}
            if large:
                records = self._read_large_metadata(path, fields)
                if records is not None:
                    return records
            return self.read(name)

    @staticmethod
    def _read_large_metadata(path: Path, omit: tuple[str, ...]) -> dict[str, Any] | None:
        if sqlite3.sqlite_version_info < (3, 51) or not omit:
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.lstrip().startswith("{"):
            return None
        paths = tuple(f"$.{field}" for field in omit)
        arguments = ", ".join("?" for _ in paths)
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                # Keep the atomic JSON file authoritative. Use the binary JSON
                # cursor to avoid decoding the payloads in every historical run.
                if not connection.execute("SELECT json_valid(?)", (raw,)).fetchone()[0]:
                    return None
                rows = connection.execute(
                    f"SELECT key, json_remove(value, {arguments}) FROM jsonb_each(jsonb(?)) WHERE type = 'object'",
                    (*paths, raw),
                )
                return {record_key: json.loads(value) for record_key, value in rows}
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def _invalidate_record_cache(self, name: str) -> None:
        with self._cache_lock:
            if self._record_cache is not None and self._record_cache[0] == name:
                self._record_cache = None

    def update(self, name: str, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        self._validate_name(name)
        with self._locks[name]:
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
                for selector in ("record", "task", "edits"):
                    self._report_reads.pop((name, selector), None)
                self._invalidate_record_cache(name)
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
