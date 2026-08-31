"""Local session persistence for the App runtime."""

from datetime import datetime, timedelta, timezone
from typing import Any

from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


_SESSION_FIELDS = frozenset({
    "session_id",
    "principal_id",
    "tenant_id",
    "role",
    "audience",
    "remember",
    "expires_at",
    "updated_at",
    "authentication_mode",
    "issuer",
    "account_generation",
})


class LocalSessionStore:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def load(
        self,
        session_id: str,
        *,
        authentication_mode: str,
        issuer: str,
        account_generation: str,
    ) -> dict[str, Any]:
        value = self._state.read("sessions").get(session_id)
        if not isinstance(value, dict) or set(value) != _SESSION_FIELDS:
            raise KeyError(session_id)
        try:
            expires_at = _aware_datetime(value["expires_at"])
            _aware_datetime(value["updated_at"])
        except (TypeError, ValueError) as error:
            raise KeyError(session_id) from error
        if (
            value["session_id"] != session_id
            or not _nonempty_string(value["principal_id"])
            or not _nonempty_string(value["tenant_id"])
            or value["role"] not in {"admin", "sales"}
            or value["audience"] != "option-helper-app"
            or not isinstance(value["remember"], bool)
            or value["authentication_mode"] != authentication_mode
            or value["issuer"] != issuer
            or value["account_generation"] != account_generation
        ):
            raise KeyError(session_id)
        if expires_at <= datetime.now(timezone.utc):
            self.delete(session_id)
            raise KeyError(session_id)
        return value

    def save(
        self,
        identity: SessionIdentity,
        *,
        remember: bool = False,
        authentication_mode: str,
        issuer: str,
        account_generation: str,
    ) -> None:
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
            "authentication_mode": authentication_mode,
            "issuer": issuer,
            "account_generation": account_generation,
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


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("session timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("session timestamp must contain a timezone")
    return parsed


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()
