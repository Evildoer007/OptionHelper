"""Audit record without secrets, request bodies or model prompts."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    action: str
    principal_id: str
    outcome: str
    reference: str | None = None
    tenant_id: str = "local-development"
    decision: str = "allow"
    reason: str | None = None
    request_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    timestamp: str = ""

    def serialized(self) -> dict[str, Any]:
        value = asdict(self)
        if not value["timestamp"]:
            value["timestamp"] = datetime.now(timezone.utc).isoformat()
        return value
