"""Persistent bounded registry for calculation jobs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import BoundedSemaphore
from typing import Any, Callable

from ..agent_runtime.runtime_invariants import RUNTIME_INVARIANTS
from ..errors import UnavailableCapabilityError, ValidationError


CALCULATION_MODULES = frozenset({"payoffer", "pricer", "backtester"})


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    tenant_id: str
    owner_id: str
    task_id: str
    module: str
    operation_id: str
    state: str
    created_at: str
    updated_at: str
    module_run_ref: dict[str, str] | None = None


class JobRegistry:
    def __init__(self, path: Path, *, global_limit: int = 32, per_task_limit: int = 8) -> None:
        if global_limit < 1 or per_task_limit < 1 or per_task_limit > global_limit:
            raise ValueError("Job queue limits are invalid")
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.global_limit = global_limit
        self.per_task_limit = per_task_limit
        with self._connect() as connection:
            self._initialize_schema(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS calculation_jobs_task_state_idx "
                "ON calculation_jobs(task_id,state)"
            )

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(calculation_jobs)"))
        if not columns:
            connection.execute(
                "CREATE TABLE calculation_jobs (job_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,"
                "owner_id TEXT NOT NULL,task_id TEXT NOT NULL,module TEXT NOT NULL,operation_id TEXT NOT NULL,"
                "state TEXT NOT NULL,module_run_ref_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            return
        current = (
            "job_id", "tenant_id", "owner_id", "task_id", "module", "operation_id",
            "state", "module_run_ref_json", "created_at", "updated_at",
        )
        legacy = ("job_id", "task_id", "module", "state", "module_run_ref_json", "created_at", "updated_at")
        if columns == current:
            return
        if columns != legacy:
            raise ValidationError("Calculation job registry schema is unknown or partial")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE calculation_jobs_v2 (job_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,"
                "owner_id TEXT NOT NULL,task_id TEXT NOT NULL,module TEXT NOT NULL,operation_id TEXT NOT NULL,"
                "state TEXT NOT NULL,module_run_ref_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO calculation_jobs_v2 "
                "SELECT job_id,'legacy-unverified','legacy-unverified',task_id,module,'legacy:'||job_id,"
                "CASE WHEN state IN ('queued','running','cancel_requested') THEN 'interrupted' ELSE state END,"
                "module_run_ref_json,created_at,updated_at FROM calculation_jobs"
            )
            connection.execute("DROP TABLE calculation_jobs")
            connection.execute("ALTER TABLE calculation_jobs_v2 RENAME TO calculation_jobs")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def enqueue(
        self, *, job_id: str, tenant_id: str, owner_id: str, task_id: str,
        module: str, operation_id: str,
    ) -> JobRecord:
        if module not in CALCULATION_MODULES or not all((job_id, tenant_id, owner_id, task_id, operation_id)):
            raise ValidationError("Calculation job identity is invalid")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if existing is not None:
                record = _row(existing)
                if (
                    record.tenant_id != tenant_id or record.owner_id != owner_id
                    or record.task_id != task_id or record.module != module
                    or record.operation_id != operation_id
                ):
                    raise ValidationError("Job identity is already bound to different metadata")
                connection.commit()
                return record
            active = ("queued", "running", "cancel_requested")
            placeholders = ",".join("?" for _ in active)
            global_count = int(connection.execute(
                f"SELECT COUNT(*) FROM calculation_jobs WHERE state IN ({placeholders})", active,
            ).fetchone()[0])
            task_count = int(connection.execute(
                f"SELECT COUNT(*) FROM calculation_jobs WHERE task_id=? AND state IN ({placeholders})",
                (task_id, *active),
            ).fetchone()[0])
            if global_count >= self.global_limit or task_count >= self.per_task_limit:
                raise UnavailableCapabilityError("calculation_job_queue", "计算队列已满，请稍后重试。")
            RUNTIME_INVARIANTS.validate_job_transition(None, "queued")
            connection.execute(
                "INSERT INTO calculation_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, tenant_id, owner_id, task_id, module, operation_id, "queued", None, now, now),
            )
            connection.commit()
        return JobRecord(job_id, tenant_id, owner_id, task_id, module, operation_id, "queued", now, now)

    def transition(
        self, job_id: str, target: str, *, module_run_ref: dict[str, str] | None = None,
    ) -> JobRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = _row(row)
            if current.state == target:
                if module_run_ref is not None and current.module_run_ref != module_run_ref:
                    raise ValidationError("Completed job reference is immutable")
                connection.commit()
                return current
            if (
                module_run_ref is not None
                and current.module_run_ref is not None
                and current.module_run_ref != module_run_ref
            ):
                raise ValidationError("Calculation job proof is immutable")
            RUNTIME_INVARIANTS.validate_job_transition(current.state, target)
            encoded = (
                json.dumps(module_run_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if module_run_ref is not None else None
            )
            now = _now()
            connection.execute(
                "UPDATE calculation_jobs SET state=?,module_run_ref_json=COALESCE(?,module_run_ref_json),updated_at=? "
                "WHERE job_id=?", (target, encoded, now, job_id),
            )
            connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row(row)

    def get_by_operation(self, operation_id: str) -> JobRecord | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE operation_id=?", (operation_id,),
            ).fetchall()
        if len(rows) > 1:
            raise ValidationError("Operation identity is bound to multiple jobs")
        return _row(rows[0]) if rows else None

    def bind_module_run_ref(self, job_id: str, reference: dict[str, str]) -> JobRecord:
        encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,module_run_ref_json FROM calculation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] != "succeeded":
                raise ValidationError("Only a succeeded calculation job can bind a ModuleRunRef")
            if row[1] is not None and row[1] != encoded:
                raise ValidationError("Completed job reference is immutable")
            connection.execute(
                "UPDATE calculation_jobs SET module_run_ref_json=?,updated_at=? WHERE job_id=?",
                (encoded, _now(), job_id),
            )
            connection.commit()
        return self.get(job_id)

    def record_module_run_proof(self, job_id: str, reference: dict[str, str]) -> JobRecord:
        """Persist an immutable proof before publishing the succeeded state."""

        encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,module_run_ref_json FROM calculation_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] not in {"running", "interrupted"}:
                raise ValidationError("ModuleRunRef proof requires a running or interrupted job")
            if row[1] is not None and row[1] != encoded:
                raise ValidationError("Calculation job proof is immutable")
            connection.execute(
                "UPDATE calculation_jobs SET module_run_ref_json=?,updated_at=? WHERE job_id=?",
                (encoded, _now(), job_id),
            )
            connection.commit()
        return self.get(job_id)

    def recover_interrupted(
        self,
        proof: Callable[[JobRecord], dict[str, str] | None] | None = None,
    ) -> int:
        recovered = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE state IN ('queued','running','cancel_requested') "
                "OR (state='interrupted' AND module_run_ref_json IS NOT NULL)"
            ).fetchall()
        for raw in rows:
            record = _row(raw)
            if record.state != "interrupted":
                self.transition(record.job_id, "interrupted")
                record = self.get(record.job_id)
            try:
                reference = proof(record) if proof is not None else record.module_run_ref
            except Exception:
                reference = None
            if reference is not None:
                if reference != record.module_run_ref:
                    raise ValidationError("Verified ModuleRunRef differs from persisted job proof")
                self.transition(record.job_id, "succeeded", module_run_ref=reference)
            recovered += 1
        return recovered

    def succeeded_operation_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM calculation_jobs WHERE state='succeeded' ORDER BY operation_id"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def validate_succeeded_proofs(
        self,
        proof: Callable[[tuple[JobRecord, ...]], tuple[dict[str, str], ...]],
    ) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,tenant_id,owner_id,task_id,module,operation_id,state,created_at,updated_at,module_run_ref_json "
                "FROM calculation_jobs WHERE state='succeeded'"
            ).fetchall()
        records = tuple(_row(raw) for raw in rows)
        if any(record.module_run_ref is None for record in records):
            raise ValidationError("Succeeded calculation job has no verified Core proof")
        verified = proof(records)
        if len(verified) != len(records):
            raise ValidationError("Succeeded calculation job proof count is invalid")
        for record, reference in zip(records, verified, strict=True):
            if reference != record.module_run_ref:
                raise ValidationError("Succeeded calculation job has no verified Core proof")

    def state_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state,COUNT(*) FROM calculation_jobs GROUP BY state ORDER BY state"
            ).fetchall()
        return {str(state): int(count) for state, count in rows}

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()


class AppJobWorker:
    """App-owned execution gate; JobRunner retains its synchronous result protocol."""

    def __init__(self, maximum_running: int = 4) -> None:
        if maximum_running < 1:
            raise ValueError("maximum_running must be positive")
        self._gate = BoundedSemaphore(maximum_running)

    def execute(self, operation: Callable[[], Any]) -> Any:
        with self._gate:
            return operation()


def _row(row: tuple[Any, ...]) -> JobRecord:
    try:
        reference = json.loads(row[9]) if isinstance(row[9], str) else None
    except json.JSONDecodeError as error:
        raise ValidationError("Persisted job ModuleRunRef is invalid") from error
    record = JobRecord(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]),
        str(row[6]), str(row[7]), str(row[8]), reference,
    )
    RUNTIME_INVARIANTS.validate_job_record(record)
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ("AppJobWorker", "CALCULATION_MODULES", "JobRecord", "JobRegistry")
