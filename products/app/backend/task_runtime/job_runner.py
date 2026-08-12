"""Synchronous local job runner used until a managed worker is configured."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import TypeVar
from uuid import uuid4


T = TypeVar("T")


@dataclass(frozen=True)
class JobExecution:
    job_id: str
    started_at: str
    elapsed_ms: int


class JobRunner:
    """Executes one authorized local operation without claiming queue support."""

    def run(self, operation: Callable[[], T]) -> tuple[JobExecution, T]:
        started_at = datetime.now(timezone.utc).isoformat()
        started = monotonic()
        value = operation()
        execution = JobExecution(str(uuid4()), started_at, int((monotonic() - started) * 1000))
        return execution, value
