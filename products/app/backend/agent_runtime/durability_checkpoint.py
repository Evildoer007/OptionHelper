"""Durable pre-dispatch records for non-replayable external operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import Any, Callable

from ..errors import ValidationError
from .session_context import SessionEvent, SessionEventLog, SessionId


_TERMINAL_EVENTS = frozenset({
    "dispatch.succeeded", "dispatch.failed", "dispatch.interrupted_unknown",
})


@dataclass(frozen=True)
class DispatchCheckpoint:
    session_id: SessionId
    operation_id: str
    operation_kind: str
    claim_state: str = "new"


class DurabilityCheckpointStore:
    """Persists dispatch intent without prompts, arguments, responses or secrets."""

    def __init__(
        self,
        event_log: SessionEventLog,
        event_sink: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        self._event_log = event_log
        self._event_sink = event_sink

    def checkpoint(
        self,
        session_id: SessionId,
        *,
        operation_id: str,
        operation_kind: str,
        safe_metadata: Mapping[str, Any] | None = None,
    ) -> DispatchCheckpoint:
        identifier = _identifier(operation_id)
        kind = str(operation_kind).strip()
        if not kind:
            raise ValidationError("Dispatch operation kind is required")
        metadata = dict(safe_metadata or {})
        forbidden = {"prompt", "arguments", "payload", "response", "result", "secret"}.intersection(metadata)
        if forbidden:
            raise ValidationError("Dispatch checkpoint metadata contains forbidden content")
        claim_state, event = self._event_log.claim_dispatch_operation(
            session_id,
            operation_id=identifier,
            operation_kind=kind,
            metadata=metadata,
        )
        if event is not None:
            self._publish(event)
        checkpoint = DispatchCheckpoint(session_id, identifier, kind, claim_state)
        if claim_state == "completed":
            raise ValidationError("External operation already completed; automatic replay is forbidden")
        if claim_state == "uncertain":
            raise ValidationError("External operation outcome is uncertain; automatic replay is forbidden")
        return checkpoint

    def succeeded(self, checkpoint: DispatchCheckpoint) -> None:
        self._terminal(checkpoint, "dispatch.succeeded")

    def failed(self, checkpoint: DispatchCheckpoint) -> None:
        self._terminal(checkpoint, "dispatch.failed")

    def interrupted_unknown(self, checkpoint: DispatchCheckpoint) -> None:
        self._terminal(checkpoint, "dispatch.interrupted_unknown")

    def recover_incomplete(
        self, settled_state: Callable[[str], str | None] | None = None,
    ) -> int:
        """Mark incomplete dispatches uncertain; never replay an external call."""

        repaired = 0
        for header in self._event_log.headers():
            events = self._event_log.replay(header.session_id)
            terminal = {
                str(event.data.get("operation_id", ""))
                for event in events if event.event_type in _TERMINAL_EVENTS
            }
            for event in events:
                if event.event_type != "dispatch_checkpointed":
                    continue
                operation_id = str(event.data.get("operation_id", ""))
                if not operation_id or operation_id in terminal:
                    continue
                checkpoint = DispatchCheckpoint(
                    header.session_id,
                    operation_id,
                    str(event.data.get("operation_kind", "unknown")),
                )
                proven = settled_state(operation_id) if settled_state is not None else None
                if proven == "succeeded":
                    self.succeeded(checkpoint)
                elif proven == "failed":
                    self.failed(checkpoint)
                else:
                    self.interrupted_unknown(checkpoint)
                repaired += 1
        return repaired

    def reconcile_succeeded(self, operation_ids: set[str] | frozenset[str]) -> int:
        """Converge a previously uncertain checkpoint after durable proof validation."""

        pending = set(operation_ids)
        repaired = 0
        for header in self._event_log.headers():
            events = self._event_log.replay(header.session_id)
            by_operation: dict[str, list[SessionEvent]] = {}
            for event in events:
                operation_id = str(event.data.get("operation_id", ""))
                if operation_id in pending and (
                    event.event_type == "dispatch_checkpointed" or event.event_type in _TERMINAL_EVENTS
                ):
                    by_operation.setdefault(operation_id, []).append(event)
            for operation_id, operation_events in by_operation.items():
                types = [event.event_type for event in operation_events]
                if "dispatch.succeeded" in types:
                    pending.discard(operation_id)
                    continue
                if "dispatch.failed" in types:
                    raise ValidationError("Verified job conflicts with a failed dispatch checkpoint")
                checkpoint_event = next(
                    (event for event in operation_events if event.event_type == "dispatch_checkpointed"), None,
                )
                if checkpoint_event is None:
                    raise ValidationError("Verified job has no dispatch checkpoint")
                self.succeeded(DispatchCheckpoint(
                    header.session_id,
                    operation_id,
                    str(checkpoint_event.data.get("operation_kind", "side_effect_tool")),
                ))
                pending.discard(operation_id)
                repaired += 1
        return repaired

    def _terminal(self, checkpoint: DispatchCheckpoint, event_type: str) -> None:
        event = self._event_log.append(
            checkpoint.session_id,
            event_type,
            {
                "operation_id": checkpoint.operation_id,
                "operation_kind": checkpoint.operation_kind,
            },
            event_id=f"dispatch:{checkpoint.operation_id}:{event_type}",
        )
        self._publish(event)

    def _publish(self, event: SessionEvent) -> None:
        if self._event_sink is not None:
            self._event_sink(event)


def stable_operation_id(*parts: object) -> str:
    normalized = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _identifier(value: object) -> str:
    result = str(value).strip()
    if not result or len(result) > 160:
        raise ValidationError("Dispatch operation id is invalid")
    return result


__all__ = (
    "DispatchCheckpoint",
    "DurabilityCheckpointStore",
    "stable_operation_id",
)
