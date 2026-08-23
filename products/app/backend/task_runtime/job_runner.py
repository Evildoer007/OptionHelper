"""Synchronous protocol backed by the optional App job registry and worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import TypeVar
from uuid import uuid4

from .job_registry import AppJobWorker, JobRegistry


T = TypeVar("T")


@dataclass(frozen=True)
class JobExecution:
    job_id: str
    started_at: str
    elapsed_ms: int


class JobRunner:
    """Execute one authorized operation while preserving the synchronous API."""

    def __init__(
        self, registry: JobRegistry | None = None, worker: AppJobWorker | None = None,
    ) -> None:
        self._registry = registry
        self._worker = worker

    def run(
        self,
        operation: Callable[[], T],
        *,
        task_id: str | None = None,
        module: str | None = None,
        job_id: str | None = None,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        operation_id: str | None = None,
        defer_success: bool = False,
    ) -> tuple[JobExecution, T]:
        started_at = datetime.now(timezone.utc).isoformat()
        started = monotonic()
        identifier = str(job_id or uuid4())
        registered = self._registry is not None and task_id is not None and module is not None
        if registered:
            if not all((tenant_id, owner_id, operation_id)):
                raise ValueError("Registered jobs require tenant, owner and operation identity")
            self._registry.enqueue(
                job_id=identifier, tenant_id=str(tenant_id), owner_id=str(owner_id),
                task_id=task_id, module=module, operation_id=str(operation_id),
            )
            self._registry.transition(identifier, "running")
        try:
            value = self._worker.execute(operation) if self._worker is not None else operation()
        except Exception:
            if registered:
                self._registry.transition(identifier, "failed")
            raise
        if registered and not defer_success:
            self._registry.transition(identifier, "succeeded")
        execution = JobExecution(identifier, started_at, int((monotonic() - started) * 1000))
        return execution, value

    def attach_module_run_ref(self, job_id: str, reference: dict[str, str]) -> None:
        if self._registry is None:
            return
        current = self._registry.get(job_id)
        if current.state == "running":
            self._registry.record_module_run_proof(job_id, reference)
            self._registry.transition(job_id, "succeeded", module_run_ref=reference)
            return
        if current.state == "succeeded":
            self._registry.bind_module_run_ref(job_id, reference)
            return
        raise RuntimeError("Only a running or succeeded calculation job can bind a ModuleRunRef")

    def record_module_run_proof(self, job_id: str, reference: dict[str, str]) -> None:
        if self._registry is not None:
            self._registry.record_module_run_proof(job_id, reference)

    def succeed_job(self, job_id: str, reference: dict[str, str]) -> None:
        if self._registry is not None:
            self._registry.transition(job_id, "succeeded", module_run_ref=reference)

    def fail_job(self, job_id: str) -> None:
        if self._registry is None:
            return
        current = self._registry.get(job_id)
        if current.state in {"queued", "running", "cancel_requested"}:
            self._registry.transition(job_id, "failed")

    def interrupt_job(self, job_id: str) -> None:
        if self._registry is None:
            return
        current = self._registry.get(job_id)
        if current.state in {"queued", "running", "cancel_requested"}:
            self._registry.transition(job_id, "interrupted")
