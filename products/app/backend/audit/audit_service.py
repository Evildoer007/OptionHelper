"""Local audit persistence for the App runtime."""

from .audit_models import AuditEvent
from ..stores import _LocalDocumentStore


class LocalAuditService:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def record(self, event: AuditEvent) -> None:
        serialized = event.serialized()

        def update(value: dict[str, object]) -> dict[str, object]:
            events = value.setdefault("events", [])
            if not isinstance(events, list):
                raise ValueError("audit events storage is invalid")
            events.append(serialized)
            return value

        self._state.update("audit", update)
