"""Local session persistence for the App runtime."""

from datetime import datetime, timedelta, timezone
from typing import Any

from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


class LocalSessionStore:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def load(self, session_id: str) -> dict[str, Any]:
        value = self._state.read("sessions").get(session_id)
        if not isinstance(value, dict):
            raise KeyError(session_id)
        expires_at = value.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expired = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
            except ValueError as error:
                raise KeyError(session_id) from error
            if expired:
                self.delete(session_id)
                raise KeyError(session_id)
        return value

    def save(self, identity: SessionIdentity, *, remember: bool = False) -> None:
        session_id = identity.session_id
        now = datetime.now(timezone.utc)
        stored = {
            "session_id": session_id,
            "principal_id": identity.principal_id,
            "tenant_id": identity.tenant_id,
            "role": identity.role.value,
            "audience": identity.audience,
            "remember": remember,
            "expires_at": (now + timedelta(days=30 if remember else 1)).isoformat(),
        }
        stored["updated_at"] = now.isoformat()

        def update(value: dict[str, object]) -> dict[str, object]:
            value[session_id] = stored
            return value

        self._state.update("sessions", update)

    def delete(self, session_id: str) -> None:
        def update(value: dict[str, object]) -> dict[str, object]:
            value.pop(session_id, None)
            return value

        self._state.update("sessions", update)
