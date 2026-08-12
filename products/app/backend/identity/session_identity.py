"""Authenticated principal representation for the local App runtime."""

from dataclasses import dataclass

from ..authorization.roles import Role


@dataclass(frozen=True)
class SessionIdentity:
    principal_id: str
    tenant_id: str
    role: Role
    session_id: str
    audience: str = "option-helper-app"

    def public(self) -> dict[str, str]:
        """Return browser-safe identity fields without the session cookie token."""
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "role": self.role.value,
            "audience": self.audience,
        }
