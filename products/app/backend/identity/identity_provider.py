"""Formal identity boundary for local and future managed App identity."""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..authorization.roles import Role
from ..errors import ValidationError
from .session_identity import SessionIdentity


class IdentityProvider(ABC):
    """Issues a server-verified caller context from provider-specific input."""

    @abstractmethod
    def authenticate(self, credentials: object) -> SessionIdentity:
        """Return an authenticated identity without exposing a session secret."""
        raise NotImplementedError


@dataclass(frozen=True)
class LocalAuthenticationRequest:
    role: Role
    principal_label: str | None = None


class LocalAuthProvider(IdentityProvider):
    """Explicit development-only identity provider without passwords or remote claims."""

    tenant_id = "local-development"

    def authenticate(self, credentials: object) -> SessionIdentity:
        if not isinstance(credentials, LocalAuthenticationRequest):
            raise ValidationError("LocalAuthProvider requires LocalAuthenticationRequest")
        label = (credentials.principal_label or credentials.role.value).strip()
        if not label or len(label) > 64:
            raise ValidationError("principal_label must contain 1 to 64 visible characters")
        normalized = "".join(character if character.isalnum() or character in "-_." else "-" for character in label)
        return SessionIdentity(
            principal_id=f"local:{normalized}",
            tenant_id=self.tenant_id,
            role=credentials.role,
            session_id=secrets.token_urlsafe(32),
        )
