"""Persistent session history and model-visible context projections.

The append-only event log is the authority. Human transcripts, model context,
tool-result pruning and summaries are derived views; none may rewrite raw
events or controlled financial facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Mapping, NewType, Sequence
from uuid import uuid4

from ..errors import UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from .runtime_invariants import (
    CURRENT_EVENT_VERSION,
    CURRENT_SESSION_FORMAT_VERSION,
    RUNTIME_INVARIANTS,
)
from .session_migrations import initialize_or_migrate_session_database


SessionId = NewType("SessionId", str)
RunId = NewType("RunId", str)

_SURFACE_EVENT_TYPES = frozenset({
    "conversation.message", "conversation.summary", "tool.call", "tool.result",
})


@dataclass(frozen=True)
class SessionEvent:
    session_id: SessionId
    seq: int
    event_id: str
    event_type: str
    timestamp: str
    data: Mapping[str, Any]
    surface_operation: str | Mapping[str, int] | None = None
    source_event_seqs: tuple[int, ...] = ()
    event_version: int = CURRENT_EVENT_VERSION
    ignorable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": str(self.session_id),
            "seq": self.seq,
            "event_id": self.event_id,
            "event": self.event_type,
            "timestamp": self.timestamp,
            "data": dict(self.data),
            "surface_operation": self.surface_operation,
            "source_event_seqs": list(self.source_event_seqs),
            "event_version": self.event_version,
            "ignorable": self.ignorable,
        }


@dataclass(frozen=True)
class SessionHeader:
    session_id: SessionId
    kind: str
    created_at: str
    parent_session_id: SessionId | None = None
    workflow_run_id: RunId | None = None
    agent_run_id: RunId | None = None
    format_version: int = CURRENT_SESSION_FORMAT_VERSION
    revision: int = 0


class SessionEventLog:
    """SQLite-backed append-only session log with monotonic per-session seq."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path.expanduser().resolve() if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._memory_connection = (
            sqlite3.connect(":memory:", check_same_thread=False)
            if self._path is None else None
        )
        with self._connection() as connection:
            initialize_or_migrate_session_database(connection, database_path=self._path)

    def _connection(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        assert self._path is not None
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def ensure_session(
        self,
        session_id: SessionId,
        *,
        kind: str,
        parent_session_id: SessionId | None = None,
        workflow_run_id: RunId | None = None,
        agent_run_id: RunId | None = None,
    ) -> SessionHeader:
        if not str(session_id).strip() or not kind.strip():
            raise ValidationError("Session identity and kind are required")
        created_at = _now()
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO session_headers "
                    "(session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,format_version,revision) "
                    "VALUES(?,?,?,?,?,?,?,0)",
                    (
                        str(session_id), kind.strip(), created_at,
                        str(parent_session_id) if parent_session_id else None,
                        str(workflow_run_id) if workflow_run_id else None,
                        str(agent_run_id) if agent_run_id else None,
                        CURRENT_SESSION_FORMAT_VERSION,
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,format_version,revision "
                    "FROM session_headers WHERE session_id=?",
                    (str(session_id),),
                ).fetchone()
            finally:
                if close:
                    connection.close()
        assert row is not None
        header = _header_from_row(row)
        expected = (
            kind.strip(), str(parent_session_id) if parent_session_id else None,
            str(workflow_run_id) if workflow_run_id else None,
            str(agent_run_id) if agent_run_id else None,
        )
        actual = (
            header.kind, str(header.parent_session_id) if header.parent_session_id else None,
            str(header.workflow_run_id) if header.workflow_run_id else None,
            str(header.agent_run_id) if header.agent_run_id else None,
        )
        if actual != expected:
            raise ValidationError("Session identity is already bound to different metadata")
        return header

    def header(self, session_id: SessionId) -> SessionHeader:
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                row = connection.execute(
                    "SELECT session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,format_version,revision "
                    "FROM session_headers WHERE session_id=?",
                    (str(session_id),),
                ).fetchone()
            finally:
                if close:
                    connection.close()
        if row is None:
            raise KeyError(str(session_id))
        return _header_from_row(row)

    def headers(self, *, kind: str | None = None) -> tuple[SessionHeader, ...]:
        """List durable session identities for bounded startup recovery."""

        query = (
            "SELECT session_id,kind,created_at,parent_session_id,workflow_run_id,agent_run_id,format_version,revision "
            "FROM session_headers"
        )
        parameters: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE kind=?"
            parameters = (str(kind),)
        query += " ORDER BY created_at,session_id"
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                rows = connection.execute(query, parameters).fetchall()
            finally:
                if close:
                    connection.close()
        return tuple(_header_from_row(row) for row in rows)

    def append(
        self,
        session_id: SessionId,
        event_type: str,
        data: Mapping[str, Any],
        *,
        event_id: str | None = None,
        surface_operation: str | Mapping[str, int] | None = None,
        source_event_seqs: Sequence[int] = (),
        event_version: int = CURRENT_EVENT_VERSION,
        ignorable: bool = False,
    ) -> SessionEvent:
        name = str(event_type).strip()
        if not name or not isinstance(data, Mapping):
            raise ValidationError("Session event type and data are required")
        operation = _validate_surface_operation(name, surface_operation)
        sources = _validate_source_event_seqs(source_event_seqs)
        safe_data = _snapshot_json(data)
        RUNTIME_INVARIANTS.validate_event(
            event_type=name,
            event_version=event_version,
            ignorable=ignorable,
            data=safe_data,
        )
        identifier = str(event_id or uuid4())
        timestamp = _now()
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,event_version,ignorable "
                    "FROM session_events WHERE session_id=? AND event_id=?",
                    (str(session_id), identifier),
                ).fetchone()
                if existing is not None:
                    persisted = _event_from_row(existing)
                    if (
                        persisted.event_type != name
                        or _snapshot_json(persisted.data) != safe_data
                        or persisted.surface_operation != operation
                        or persisted.source_event_seqs != sources
                        or persisted.event_version != event_version
                        or persisted.ignorable != ignorable
                    ):
                        raise ValidationError("Session event idempotency conflict")
                    connection.commit()
                    return persisted
                if connection.execute(
                    "SELECT 1 FROM session_headers WHERE session_id=?", (str(session_id),),
                ).fetchone() is None:
                    raise KeyError(str(session_id))
                seq = int(connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM session_events WHERE session_id=?",
                    (str(session_id),),
                ).fetchone()[0])
                candidate = SessionEvent(
                    SessionId(str(session_id)), seq, identifier, name, timestamp,
                    safe_data, operation, sources, event_version, ignorable,
                )
                current_rows = connection.execute(
                    "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,event_version,ignorable "
                    "FROM session_events WHERE session_id=? ORDER BY seq",
                    (str(session_id),),
                ).fetchall()
                candidate_events = [*(_event_from_row(row) for row in current_rows), candidate]
                RUNTIME_INVARIANTS.validate_replay(candidate_events)
                if (name in _SURFACE_EVENT_TYPES or operation is not None) and not ignorable:
                    # Validation happens while the write lock is held. A stale
                    # replace therefore fails before the candidate reaches disk.
                    SurfaceProjector().project(
                        candidate_events,
                        allow_inflight_tool_calls=True,
                    )
                connection.execute(
                    "INSERT INTO session_events "
                    "(session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,event_version,ignorable) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(session_id), seq, identifier, name, timestamp,
                        json.dumps(safe_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        json.dumps(operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        if operation is not None else None,
                        json.dumps(list(sources), separators=(",", ":")),
                        event_version,
                        1 if ignorable else 0,
                    ),
                )
                updated = connection.execute(
                    "UPDATE session_headers SET revision=revision+1 WHERE session_id=? AND revision=?",
                    (str(session_id), seq - 1),
                )
                if updated.rowcount != 1:
                    raise ValidationError("Session revision is inconsistent with event sequence")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                if close:
                    connection.close()
        return SessionEvent(
            SessionId(str(session_id)), seq, identifier, name, timestamp,
            safe_data, operation, sources, event_version, ignorable,
        )

    def claim_dispatch_operation(
        self,
        session_id: SessionId,
        *,
        operation_id: str,
        operation_kind: str,
        metadata: Mapping[str, Any],
    ) -> tuple[str, SessionEvent | None]:
        """Atomically claim one external operation without replaying uncertainty."""

        checkpoint_id = f"dispatch:{operation_id}:checkpointed"
        terminal_ids = {
            "completed": f"dispatch:{operation_id}:dispatch.succeeded",
            "uncertain_failed": f"dispatch:{operation_id}:dispatch.failed",
            "uncertain_interrupted": f"dispatch:{operation_id}:dispatch.interrupted_unknown",
        }
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                terminal_rows = connection.execute(
                    "SELECT event_id FROM session_events WHERE session_id=? AND event_id IN (?,?,?)",
                    (str(session_id), *terminal_ids.values()),
                ).fetchall()
                terminal = {str(row[0]) for row in terminal_rows}
                if terminal_ids["completed"] in terminal:
                    connection.commit()
                    return "completed", None
                existing = connection.execute(
                    "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,"
                    "source_event_seqs_json,event_version,ignorable FROM session_events "
                    "WHERE session_id=? AND event_id=?",
                    (str(session_id), checkpoint_id),
                ).fetchone()
                if existing is not None or terminal:
                    connection.commit()
                    return "uncertain", _event_from_row(existing) if existing is not None else None
                header = connection.execute(
                    "SELECT revision FROM session_headers WHERE session_id=?", (str(session_id),),
                ).fetchone()
                if header is None:
                    raise KeyError(str(session_id))
                seq = int(header[0]) + 1
                timestamp = _now()
                data = _snapshot_json({
                    "operation_id": operation_id,
                    "operation_kind": operation_kind,
                    "metadata": dict(metadata),
                })
                candidate = SessionEvent(
                    session_id, seq, checkpoint_id, "dispatch_checkpointed", timestamp, data,
                )
                rows = connection.execute(
                    "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,"
                    "source_event_seqs_json,event_version,ignorable FROM session_events "
                    "WHERE session_id=? ORDER BY seq", (str(session_id),),
                ).fetchall()
                RUNTIME_INVARIANTS.validate_replay([*(_event_from_row(row) for row in rows), candidate])
                connection.execute(
                    "INSERT INTO session_events "
                    "(session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,"
                    "source_event_seqs_json,event_version,ignorable) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(session_id), seq, checkpoint_id, "dispatch_checkpointed", timestamp,
                        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        None, "[]", CURRENT_EVENT_VERSION, 0,
                    ),
                )
                updated = connection.execute(
                    "UPDATE session_headers SET revision=revision+1 WHERE session_id=? AND revision=?",
                    (str(session_id), seq - 1),
                )
                if updated.rowcount != 1:
                    raise ValidationError("Session revision changed while claiming dispatch")
                connection.commit()
                return "new", candidate
            except Exception:
                connection.rollback()
                raise
            finally:
                if close:
                    connection.close()

    def delete_session_tree(self, session_id: SessionId) -> tuple[SessionId, ...]:
        """Delete one session and every descendant in one database transaction."""

        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "WITH RECURSIVE descendants(session_id) AS ("
                    " SELECT session_id FROM session_headers WHERE session_id=?"
                    " UNION ALL"
                    " SELECT child.session_id FROM session_headers child"
                    " JOIN descendants parent ON child.parent_session_id=parent.session_id"
                    ") SELECT session_id FROM descendants",
                    (str(session_id),),
                ).fetchall()
                identifiers = tuple(SessionId(str(row[0])) for row in rows)
                if not identifiers:
                    connection.commit()
                    return ()
                placeholders = ",".join("?" for _ in identifiers)
                values = tuple(str(value) for value in identifiers)
                connection.execute(f"DELETE FROM session_events WHERE session_id IN ({placeholders})", values)
                connection.execute(f"DELETE FROM session_headers WHERE session_id IN ({placeholders})", values)
                connection.commit()
                return identifiers
            except Exception:
                connection.rollback()
                raise
            finally:
                if close:
                    connection.close()

    def replay(self, session_id: SessionId, *, after_seq: int = 0) -> list[SessionEvent]:
        if after_seq < 0:
            raise ValidationError("Session replay sequence must be non-negative")
        with self._lock:
            connection = self._connection()
            close = connection is not self._memory_connection
            try:
                rows = connection.execute(
                    "SELECT session_id,seq,event_id,event_type,timestamp,data_json,surface_operation_json,source_event_seqs_json,event_version,ignorable "
                    "FROM session_events WHERE session_id=? AND seq>? ORDER BY seq",
                    (str(session_id), after_seq),
                ).fetchall()
                revision_row = connection.execute(
                    "SELECT revision FROM session_headers WHERE session_id=?", (str(session_id),),
                ).fetchone()
            finally:
                if close:
                    connection.close()
        events = [_event_from_row(row) for row in rows]
        if after_seq == 0:
            if revision_row is None:
                if events:
                    raise ValidationError("Session events exist without a durable header")
                return []
            if int(revision_row[0]) != len(events):
                raise ValidationError("Session revision does not match durable event count")
            RUNTIME_INVARIANTS.validate_replay(events)
        else:
            for event in events:
                RUNTIME_INVARIANTS.validate_event(
                    event_type=event.event_type,
                    event_version=event.event_version,
                    ignorable=event.ignorable,
                    data=event.data,
                )
        return events


@dataclass(frozen=True)
class ModelVisibleSurface:
    events: tuple[SessionEvent, ...]
    messages: tuple[Mapping[str, Any], ...]
    controlled_facts: Mapping[str, Any]
    replace_generation: int


class SurfaceProjector:
    """Fold surface operations without mutating the append-only log."""

    def project(
        self,
        events: Sequence[SessionEvent],
        *,
        controlled_facts: Mapping[str, Any] | None = None,
        allow_inflight_tool_calls: bool = False,
    ) -> ModelVisibleSurface:
        by_seq = {event.seq: event for event in events}
        nodes: list[int] = []
        generation = 0
        for event in events:
            if not RUNTIME_INVARIANTS.validate_event(
                event_type=event.event_type,
                event_version=event.event_version,
                ignorable=event.ignorable,
                data=event.data,
            ):
                continue
            if event.event_type not in _SURFACE_EVENT_TYPES:
                if event.surface_operation is not None:
                    raise ValidationError("Non-surface event cannot carry a surface operation")
                continue
            operation = event.surface_operation
            if operation == "append":
                nodes.append(event.seq)
            elif isinstance(operation, Mapping):
                start = int(operation["start"])
                end = int(operation["end"])
                try:
                    start_index = nodes.index(start)
                    end_index = nodes.index(end)
                except ValueError as error:
                    raise ValidationError("Surface replacement range is not current") from error
                if start_index > end_index:
                    raise ValidationError("Surface replacement range is reversed")
                shadowed = nodes[start_index:end_index + 1]
                if not set(shadowed).issubset(set(event.source_event_seqs)):
                    raise ValidationError("Surface replacement must cite every shadowed event")
                _validate_replacement_pair_boundary(
                    [by_seq[seq] for seq in nodes], shadowed, event,
                )
                _validate_tool_result_replacement(event, [by_seq[seq] for seq in shadowed])
                nodes[start_index:end_index + 1] = [event.seq]
                generation += 1
            else:
                raise ValidationError("Surface event requires append or replace semantics")
            _validate_current_tool_pairs([by_seq[seq] for seq in nodes])
        surface_events = tuple(by_seq[seq] for seq in nodes)
        if not allow_inflight_tool_calls:
            _validate_current_tool_pairs(surface_events, require_closed=True)
        return ModelVisibleSurface(
            surface_events,
            tuple(_message_from_event(event) for event in surface_events),
            _snapshot_json(controlled_facts or {}),
            generation,
        )


class TokenMeter:
    """Conservative local estimate; it never impersonates provider usage."""

    def estimate_text(self, value: object) -> int:
        text = str(value or "")
        if not text:
            return 0
        ascii_count = sum(ord(character) < 128 for character in text)
        return max(1, (ascii_count + 3) // 4 + len(text) - ascii_count)

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        return sum(4 + self.estimate_text(json.dumps(_snapshot_json(message), ensure_ascii=False)) for message in messages)


class ToolResultPruner:
    def __init__(self, event_log: SessionEventLog, *, max_characters: int = 4_000) -> None:
        if max_characters < 64:
            raise ValueError("max_characters must be at least 64")
        self._event_log = event_log
        self._max_characters = max_characters

    def prune(self, session_id: SessionId) -> list[SessionEvent]:
        events = self._event_log.replay(session_id)
        surface = SurfaceProjector().project(events)
        appended: list[SessionEvent] = []
        for event in surface.events:
            if event.event_type != "tool.result":
                continue
            content = str(event.data.get("content", ""))
            if len(content) <= self._max_characters:
                continue
            omitted = len(content) - self._max_characters
            replacement = {**dict(event.data), "content": f"{content[:self._max_characters]}\n[已剪枝{omitted}个字符]"}
            appended.append(self._event_log.append(
                session_id,
                "tool.result",
                replacement,
                surface_operation={"start": event.seq, "end": event.seq},
                source_event_seqs=(event.seq,),
            ))
        return appended


class ContextCompactor:
    """Optional atomic summary compaction over a validated surface range."""

    def __init__(self, event_log: SessionEventLog, gateway: ModelGateway, token_meter: TokenMeter | None = None) -> None:
        self._event_log = event_log
        self._gateway = gateway
        self._token_meter = token_meter or TokenMeter()

    def compact(
        self,
        identity: SessionIdentity,
        task_id: str,
        session_id: SessionId,
        *,
        start_seq: int,
        end_seq: int,
        selection: ModelSelection | None = None,
    ) -> SessionEvent:
        events = self._event_log.replay(session_id)
        surface = SurfaceProjector().project(events)
        selected = [event for event in surface.events if start_seq <= event.seq <= end_seq]
        if not selected or selected[0].seq != start_seq or selected[-1].seq != end_seq:
            raise ValidationError("Compaction range must match current surface boundaries")
        _validate_compaction_pair_boundary(surface.events, start_seq, end_seq)
        messages = [_message_from_event(event) for event in selected]
        prompt = (
            "请将以下已完成对话压缩为忠实、简洁的上下文摘要。保留用户目标、约束、结论与未完成事项；"
            "不得新增金融事实、运行结果或凭据。\n" + json.dumps(_snapshot_json(messages), ensure_ascii=False)
        )
        route = self._gateway.resolve_agent_model_route(
            identity, explicit_selection=selection, preset_role_selection=None,
        )
        # No event is appended before the model returns and validation succeeds.
        metadata_completion = getattr(self._gateway, "complete_with_metadata_for", None)
        provider_usage: Mapping[str, Any] | None = None
        if callable(metadata_completion):
            response = metadata_completion(
                identity, task_id, prompt, selection=route.dispatch_selection,
            )
            if not isinstance(response, Mapping) or not isinstance(response.get("text"), str):
                raise ValidationError("Context summary response is invalid")
            summary = str(response["text"]).strip()
            raw_usage = response.get("usage")
            if isinstance(raw_usage, Mapping):
                provider_usage = _validated_provider_usage(raw_usage)
        else:
            summary = self._gateway.complete_for(
                identity, task_id, prompt, selection=route.dispatch_selection,
            ).strip()
        if not summary:
            raise ValidationError("Context summary is empty")
        usage = {
            "source": "local_estimate",
            "input_tokens": self._token_meter.estimate_text(prompt),
            "output_tokens": self._token_meter.estimate_text(summary),
        }
        summary_data: dict[str, Any] = {
            "content": summary,
            "model_selection": {
                "provider_id": route.model_selection.provider_id,
                "model_id": route.model_selection.model_id,
            },
            "model_selection_source": route.source,
            "usage": usage,
        }
        if provider_usage:
            summary_data["provider_usage"] = dict(provider_usage)
        return self._event_log.append(
            session_id,
            "conversation.summary",
            summary_data,
            surface_operation={"start": start_seq, "end": end_seq},
            source_event_seqs=tuple(event.seq for event in selected),
        )


def transcript_messages(events: Sequence[SessionEvent]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "conversation.message" or event.surface_operation != "append":
            continue
        message = event.data.get("message")
        if isinstance(message, Mapping):
            result.append(dict(message))
    return result


def _validate_surface_operation(event_type: str, value: object) -> str | Mapping[str, int] | None:
    return RUNTIME_INVARIANTS.validate_surface_operation(event_type, value, _SURFACE_EVENT_TYPES)


def _validate_source_event_seqs(values: Sequence[int]) -> tuple[int, ...]:
    return RUNTIME_INVARIANTS.validate_source_event_seqs(values)


def _validate_tool_result_replacement(event: SessionEvent, shadowed: Sequence[SessionEvent]) -> None:
    RUNTIME_INVARIANTS.validate_tool_result_replacement(event, shadowed)


def _validate_replacement_pair_boundary(
    events: Sequence[SessionEvent], shadowed: Sequence[int], replacement: SessionEvent,
) -> None:
    """Reject a replacement that removes only one side of a tool exchange."""

    RUNTIME_INVARIANTS.validate_pair_boundary(events, set(shadowed), replacement=replacement)


def _validate_current_tool_pairs(events: Sequence[SessionEvent], *, require_closed: bool = False) -> None:
    RUNTIME_INVARIANTS.validate_tool_pairs(events, require_closed=require_closed)


def _validate_compaction_pair_boundary(events: Sequence[SessionEvent], start: int, end: int) -> None:
    inside = {event.seq for event in events if start <= event.seq <= end}
    RUNTIME_INVARIANTS.validate_pair_boundary(events, inside)


def _message_from_event(event: SessionEvent) -> Mapping[str, Any]:
    if event.event_type == "conversation.message":
        message = event.data.get("message")
        if not isinstance(message, Mapping):
            raise ValidationError("Conversation event message is invalid")
        return dict(message)
    if event.event_type == "conversation.summary":
        return {"role": "system", "content": str(event.data.get("content", "")), "kind": "context_summary"}
    if event.event_type == "tool.call":
        return {
            "role": "assistant",
            "tool_call": {
                "call_id": str(event.data.get("call_id", "")),
                "name": str(event.data.get("name", "")),
                "arguments": _snapshot_json(event.data.get("arguments", {})),
            },
        }
    if event.event_type == "tool.result":
        return {
            "role": "tool",
            "call_id": str(event.data.get("call_id", "")),
            "name": str(event.data.get("name", "")),
            "content": str(event.data.get("content", "")),
            "is_error": bool(event.data.get("is_error", False)),
        }
    raise ValidationError("Event is not model-visible")


def _snapshot_json(value: object) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ValidationError("Session event data must be JSON-compatible") from error


def _validated_provider_usage(value: Mapping[str, Any]) -> dict[str, int] | None:
    allowed = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {
        key: int(value[key])
        for key in allowed
        if isinstance(value.get(key), int) and not isinstance(value.get(key), bool) and int(value[key]) >= 0
    }
    return result or None


def _header_from_row(row: Sequence[Any]) -> SessionHeader:
    header = SessionHeader(
        SessionId(str(row[0])), str(row[1]), str(row[2]),
        SessionId(str(row[3])) if row[3] is not None else None,
        RunId(str(row[4])) if row[4] is not None else None,
        RunId(str(row[5])) if row[5] is not None else None,
        int(row[6]), int(row[7]),
    )
    RUNTIME_INVARIANTS.validate_header(
        format_version=header.format_version, revision=header.revision,
    )
    return header


def _event_from_row(row: Sequence[Any]) -> SessionEvent:
    operation = json.loads(row[6]) if row[6] is not None else None
    return SessionEvent(
        SessionId(str(row[0])), int(row[1]), str(row[2]), str(row[3]), str(row[4]),
        json.loads(row[5]), operation, tuple(json.loads(row[7])), int(row[8]), bool(row[9]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
