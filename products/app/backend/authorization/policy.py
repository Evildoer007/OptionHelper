"""Server-side role-to-capability policy.

This policy is intentionally evaluated by HTTP and Tool Gateway entry points.
It is not a UI visibility hint.
"""

from ..errors import AuthorizationError
from .roles import Role


class AuthorizationPolicy:
    _ALLOW = {
        Role.SALES: frozenset({
            "optchat", "settings.read", "settings.preferences.write",
            "settings.storage.write", "task.create",
            "task.read", "conversation.write", "conversation.tool.run", "report.card.request",
        }),
        Role.ADMIN: frozenset({
            "optchat", "optdesk", "settings.read", "settings.preferences.write",
            "settings.model.write", "settings.storage.write", "settings.data.write",
            "task.create", "task.read", "conversation.write", "conversation.tool.run", "module.page",
            "module.catalog", "module.run", "module.host_context", "report.card.request", "report.full.request",
        }),
    }

    def allows(self, role: Role, capability: str) -> bool:
        return capability in self._ALLOW[role]

    def require(self, role: Role, capability: str) -> None:
        if not self.allows(role, capability):
            raise AuthorizationError(capability, f"role={role.value}")
