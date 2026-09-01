"""Recoverable and exact SQLite migration for the session event log."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from ..errors import ValidationError
from .runtime_invariants import CURRENT_EVENT_VERSION, CURRENT_SESSION_FORMAT_VERSION

SESSION_DATABASE_APPLICATION_ID = 0x4F485345
SESSION_DATABASE_USER_VERSION = 1

_LEGACY_HEADERS = (
    (0, "session_id", "TEXT", 0, None, 1), (1, "kind", "TEXT", 1, None, 0),
    (2, "created_at", "TEXT", 1, None, 0), (3, "parent_session_id", "TEXT", 0, None, 0),
    (4, "workflow_run_id", "TEXT", 0, None, 0), (5, "agent_run_id", "TEXT", 0, None, 0),
)
_CURRENT_HEADERS = _LEGACY_HEADERS + (
    (6, "format_version", "INTEGER", 1, str(CURRENT_SESSION_FORMAT_VERSION), 0),
    (7, "revision", "INTEGER", 1, "0", 0),
)
_LEGACY_EVENTS = (
    (0, "session_id", "TEXT", 1, None, 1), (1, "seq", "INTEGER", 1, None, 2),
    (2, "event_id", "TEXT", 1, None, 0), (3, "event_type", "TEXT", 1, None, 0),
    (4, "timestamp", "TEXT", 1, None, 0), (5, "data_json", "TEXT", 1, None, 0),
    (6, "surface_operation_json", "TEXT", 0, None, 0),
    (7, "source_event_seqs_json", "TEXT", 1, None, 0),
)
_CURRENT_EVENTS = _LEGACY_EVENTS + (
    (8, "event_version", "INTEGER", 1, str(CURRENT_EVENT_VERSION), 0),
    (9, "ignorable", "INTEGER", 1, "0", 0),
)
_EVENT_FOREIGN_KEYS = ((0, 0, "session_headers", "session_id", "session_id", "NO ACTION", "NO ACTION", "NONE"),)
_LEGACY_TABLE_SQL = {
    "session_headers": (
        "CREATE TABLE session_headers (session_id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
        "created_at TEXT NOT NULL,parent_session_id TEXT,workflow_run_id TEXT,agent_run_id TEXT)"
    ),
    "session_events": (
        "CREATE TABLE session_events (session_id TEXT NOT NULL,seq INTEGER NOT NULL,event_id TEXT NOT NULL,"
        "event_type TEXT NOT NULL,timestamp TEXT NOT NULL,data_json TEXT NOT NULL,surface_operation_json TEXT,"
        "source_event_seqs_json TEXT NOT NULL,PRIMARY KEY(session_id,seq),UNIQUE(session_id,event_id),"
        "FOREIGN KEY(session_id) REFERENCES session_headers(session_id))"
    ),
}
_CURRENT_TABLE_SQL = {
    "session_headers": _LEGACY_TABLE_SQL["session_headers"][:-1]
    + f",format_version INTEGER NOT NULL DEFAULT {CURRENT_SESSION_FORMAT_VERSION},revision INTEGER NOT NULL DEFAULT 0)",
    "session_events": (
        "CREATE TABLE session_events (session_id TEXT NOT NULL,seq INTEGER NOT NULL,event_id TEXT NOT NULL,"
        "event_type TEXT NOT NULL,timestamp TEXT NOT NULL,data_json TEXT NOT NULL,surface_operation_json TEXT,"
        f"source_event_seqs_json TEXT NOT NULL,event_version INTEGER NOT NULL DEFAULT {CURRENT_EVENT_VERSION},"
        "ignorable INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,seq),UNIQUE(session_id,event_id),"
        "FOREIGN KEY(session_id) REFERENCES session_headers(session_id))"
    ),
}


def initialize_or_migrate_session_database(
    connection: sqlite3.Connection,
    *,
    database_path: Path | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> Path | None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id not in {0, SESSION_DATABASE_APPLICATION_ID}:
        raise ValidationError("Session database application_id is not recognized")
    if user_version > SESSION_DATABASE_USER_VERSION:
        raise ValidationError("Session database version is newer than this App")
    objects = _schema_objects(connection)
    if not objects:
        _create_current(connection, fault_injector)
        return None
    if user_version == SESSION_DATABASE_USER_VERSION:
        if application_id != SESSION_DATABASE_APPLICATION_ID:
            raise ValidationError("Versioned session database is missing application_id")
        _validate_current_schema(connection)
        return None
    _validate_legacy_schema(connection)
    if database_path is None:
        raise ValidationError("Persistent Legacy0 migration requires a database path")
    backup_path = _create_verified_backup(connection, database_path)
    _migrate_legacy_zero(connection, fault_injector)
    return backup_path


def _create_current(connection: sqlite3.Connection, fault: Callable[[str], None] | None) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        _create_tables(connection)
        _inject(fault, "after_create_tables")
        connection.execute(f"PRAGMA application_id={SESSION_DATABASE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SESSION_DATABASE_USER_VERSION}")
        _validate_current_schema(connection)
        _inject(fault, "before_commit")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_legacy_zero(connection: sqlite3.Connection, fault: Callable[[str], None] | None) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE session_events RENAME TO session_events_legacy_zero")
        _inject(fault, "after_first_ddl")
        connection.execute("ALTER TABLE session_headers RENAME TO session_headers_legacy_zero")
        connection.execute("DROP INDEX session_events_type_idx")
        _create_tables(connection)
        _inject(fault, "after_schema_change")
        connection.execute(
            "INSERT INTO session_headers "
            "(session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,format_version,revision) "
            "SELECT session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,?,"
            "(SELECT COUNT(*) FROM session_events_legacy_zero "
            "WHERE session_events_legacy_zero.session_id=session_headers_legacy_zero.session_id) "
            "FROM session_headers_legacy_zero",
            (CURRENT_SESSION_FORMAT_VERSION,),
        )
        connection.execute(
            "INSERT INTO session_events "
            "(session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,event_version,ignorable) "
            "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,?,0 "
            "FROM session_events_legacy_zero",
            (CURRENT_EVENT_VERSION,),
        )
        _inject(fault, "after_revision_backfill")
        connection.execute("DROP TABLE session_events_legacy_zero")
        connection.execute("DROP TABLE session_headers_legacy_zero")
        connection.execute(f"PRAGMA application_id={SESSION_DATABASE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SESSION_DATABASE_USER_VERSION}")
        _validate_current_schema(connection)
        _inject(fault, "before_commit")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute(_CURRENT_TABLE_SQL["session_headers"])
    connection.execute(_CURRENT_TABLE_SQL["session_events"])
    connection.execute("CREATE INDEX session_events_type_idx ON session_events(session_id,event_type,seq)")


def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
    _validate_object_names(connection)
    _validate_table(connection, "session_headers", _LEGACY_HEADERS)
    _validate_table(connection, "session_events", _LEGACY_EVENTS)
    _validate_table_sql(connection, _LEGACY_TABLE_SQL)
    _validate_indexes(connection)
    _validate_foreign_keys(connection)


def _validate_current_schema(connection: sqlite3.Connection) -> None:
    _validate_object_names(connection)
    _validate_table(connection, "session_headers", _CURRENT_HEADERS)
    _validate_table(connection, "session_events", _CURRENT_EVENTS)
    _validate_table_sql(connection, _CURRENT_TABLE_SQL)
    _validate_indexes(connection)
    _validate_foreign_keys(connection)


def _validate_object_names(connection: sqlite3.Connection) -> None:
    expected = {
        ("table", "session_headers"), ("table", "session_events"),
        ("index", "sqlite_autoindex_session_headers_1"),
        ("index", "sqlite_autoindex_session_events_1"),
        ("index", "sqlite_autoindex_session_events_2"),
        ("index", "session_events_type_idx"),
    }
    if _schema_objects(connection) != expected:
        raise ValidationError("Session database contains an unknown or partial schema")


def _validate_table(connection: sqlite3.Connection, name: str, expected: tuple[tuple[object, ...], ...]) -> None:
    if tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({name})")) != expected:
        raise ValidationError(f"Session table {name} does not match the supported schema")


def _validate_table_sql(connection: sqlite3.Connection, expected: dict[str, str]) -> None:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('session_headers','session_events')"
    ).fetchall()
    definitions = {str(name): _normalized_sql(str(sql)) for name, sql in rows}
    normalized_expected = {name: _normalized_sql(sql) for name, sql in expected.items()}
    if definitions != normalized_expected:
        raise ValidationError("Session table DDL contains unsupported table-level semantics")


def _validate_indexes(connection: sqlite3.Connection) -> None:
    headers = {(str(row[1]), int(row[2]), str(row[3]), int(row[4])) for row in connection.execute("PRAGMA index_list(session_headers)")}
    events = {(str(row[1]), int(row[2]), str(row[3]), int(row[4])) for row in connection.execute("PRAGMA index_list(session_events)")}
    if headers != {("sqlite_autoindex_session_headers_1", 1, "pk", 0)}:
        raise ValidationError("Session header primary index is invalid")
    if events != {
        ("session_events_type_idx", 0, "c", 0),
        ("sqlite_autoindex_session_events_1", 1, "pk", 0),
        ("sqlite_autoindex_session_events_2", 1, "u", 0),
    }:
        raise ValidationError("Session event indexes are invalid")
    for name, columns in {
        "session_events_type_idx": ("session_id", "event_type", "seq"),
        "sqlite_autoindex_session_events_1": ("session_id", "seq"),
        "sqlite_autoindex_session_events_2": ("session_id", "event_id"),
    }.items():
        if tuple(str(row[2]) for row in connection.execute(f"PRAGMA index_info({name})")) != columns:
            raise ValidationError(f"Session index {name} has invalid columns")
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='session_events_type_idx'").fetchone()
    expected_sql = "CREATE INDEX session_events_type_idx ON session_events(session_id,event_type,seq)"
    if row is None or _normalized_sql(str(row[0])) != _normalized_sql(expected_sql):
        raise ValidationError("Session event index definition is invalid")


def _validate_foreign_keys(connection: sqlite3.Connection) -> None:
    if tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_list(session_events)")) != _EVENT_FOREIGN_KEYS:
        raise ValidationError("Session event foreign key definition is invalid")


def _schema_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(str(row[0]), str(row[1])) for row in connection.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' OR name LIKE 'sqlite_autoindex_%'")}


def _create_verified_backup(connection: sqlite3.Connection, database_path: Path) -> Path:
    source = database_path.expanduser().resolve()
    backup = source.with_name(f"{source.name}.pre-v{SESSION_DATABASE_USER_VERSION}-{uuid4().hex}.backup")
    destination = sqlite3.connect(backup)
    try:
        connection.backup(destination)
        if str(destination.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise ValidationError("Session migration backup failed integrity validation")
    finally:
        destination.close()
    verification = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        if str(verification.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise ValidationError("Session migration backup cannot be reopened")
    finally:
        verification.close()
    return backup


def _inject(fault: Callable[[str], None] | None, stage: str) -> None:
    if fault is not None:
        fault(stage)


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


__all__ = ("SESSION_DATABASE_APPLICATION_ID", "SESSION_DATABASE_USER_VERSION", "initialize_or_migrate_session_database")
