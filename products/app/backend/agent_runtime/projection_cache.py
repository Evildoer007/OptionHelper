"""Bounded revision-aware cache for model-visible session projections."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from threading import RLock
from typing import Any, Mapping

from .session_context import ModelVisibleSurface, SessionEventLog, SessionId, SurfaceProjector


class ProjectionCache:
    def __init__(self, event_log: SessionEventLog, *, maximum_entries: int = 128) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self._event_log = event_log
        self._maximum_entries = maximum_entries
        self._values: OrderedDict[tuple[str, int, str, str], ModelVisibleSurface] = OrderedDict()
        self._lock = RLock()

    def project(
        self,
        session_id: SessionId,
        *,
        controlled_facts: Mapping[str, Any] | None = None,
        mode: str = "model",
    ) -> ModelVisibleSurface:
        if mode not in {"model", "running"}:
            raise ValueError("Projection mode is invalid")
        header = self._event_log.header(session_id)
        facts = dict(controlled_facts or {})
        facts_hash = hashlib.sha256(
            json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key = (str(session_id), header.revision, facts_hash, mode)
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                self._values.move_to_end(key)
                return cached
        surface = SurfaceProjector().project(
            self._event_log.replay(session_id),
            controlled_facts=facts,
            allow_inflight_tool_calls=mode == "running",
        )
        with self._lock:
            self._values[key] = surface
            self._values.move_to_end(key)
            while len(self._values) > self._maximum_entries:
                self._values.popitem(last=False)
        return surface

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._values)


__all__ = ("ProjectionCache",)
