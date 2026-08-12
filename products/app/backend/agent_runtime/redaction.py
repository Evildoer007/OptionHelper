"""One narrow redaction boundary for OptChat-visible text.

The App records the full authenticated identity only in private stores.  Model
context, model final text, tool observations, and provider failures all pass
through this module before they can re-enter a conversation response.
"""

from __future__ import annotations

import re

from ..identity.session_identity import SessionIdentity


_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]{20,}|[A-Fa-f0-9]{32,})(?![A-Za-z0-9_-])"
)
_NAMED_SECRET = re.compile(r"(?i)\b(?:bearer|token|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*(?::|=|\s)\s*[A-Za-z0-9._~+\-/=]{12,}")
_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9_])(?:~|/(?:Users|private|tmp|var|etc|opt|Library))(?:/[^\s,;，；<>]*)?")
_HIDDEN_REASONING = re.compile(r"(?is)<(?:think|analysis|reasoning)\b[^>]*>.*?</(?:think|analysis|reasoning)\s*>")


def redact_text(value: object, identity: SessionIdentity | None = None, *, limit: int | None = None) -> str:
    """Return a bounded public projection without local secrets or identity."""
    text = str(value or "")
    text = _HIDDEN_REASONING.sub("[REDACTED]", text)
    text = _TOKEN.sub("[REDACTED]", text)
    text = _NAMED_SECRET.sub("[REDACTED]", text)
    text = _LOCAL_PATH.sub("[REDACTED]", text)
    if identity is not None:
        for private_value in (identity.tenant_id, identity.principal_id, identity.session_id):
            if private_value:
                text = text.replace(private_value, "[REDACTED]")
    return text[:limit] if limit is not None else text


def has_hidden_reasoning(value: object) -> bool:
    return bool(_HIDDEN_REASONING.search(str(value or "")))


__all__ = ("has_hidden_reasoning", "redact_text")
