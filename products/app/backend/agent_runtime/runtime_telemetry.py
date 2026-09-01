"""Content-free internal runtime counters derived from durable state."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..task_runtime.job_registry import JobRegistry
from .session_context import SessionEventLog


class RuntimeTelemetry:
    def __init__(self, event_log: SessionEventLog, jobs: JobRegistry) -> None:
        self._event_log = event_log
        self._jobs = jobs

    def snapshot(self) -> dict[str, Any]:
        """Return only structural counts; telemetry failure never reaches business paths."""

        try:
            session_kinds: Counter[str] = Counter()
            event_types: Counter[str] = Counter()
            for header in self._event_log.headers():
                session_kinds[header.kind] += 1
                event_types.update(event.event_type for event in self._event_log.replay(header.session_id))
            return {
                "available": True,
                "session_counts": dict(sorted(session_kinds.items())),
                "event_counts": dict(sorted(event_types.items())),
                "job_counts": self._jobs.state_counts(),
            }
        except Exception:
            return {"available": False, "session_counts": {}, "event_counts": {}, "job_counts": {}}


__all__ = ("RuntimeTelemetry",)
