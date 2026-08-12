"""Persistent, process-safe idempotency index for calculation ModuleRuns."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from ..errors import ValidationError


class ToolIdempotencyStore:
    """Atomically claim one App calculation and retain its immutable RunRef."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_idempotency (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    run_ref_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, task_id, module, action, idempotency_key)
                )
                """
            )

    def claim(
        self, *, tenant_id: str, task_id: str, module: str, action: str,
        idempotency_key: str, input_hash: str,
    ) -> tuple[str, dict[str, str] | None]:
        scope = (tenant_id, task_id, module, action, idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT input_hash, state, run_ref_json FROM tool_idempotency WHERE tenant_id=? AND task_id=? AND module=? AND action=? AND idempotency_key=?",
                scope,
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO tool_idempotency (tenant_id,task_id,module,action,idempotency_key,input_hash,state,run_ref_json,updated_at) VALUES (?, ?, ?, ?, ?, ?, 'claimed', NULL, ?)",
                    (*scope, input_hash, _now()),
                )
                connection.commit()
                return "owner", None
            if row[0] != input_hash:
                raise ValidationError("同一幂等键不能对应不同租户、任务、模块、动作或输入")
            if row[1] == "uncertain":
                connection.commit()
                return "uncertain", None
            if row[1] != "completed" or not isinstance(row[2], str):
                connection.commit()
                return "busy", None
            try:
                reference = json.loads(row[2])
            except json.JSONDecodeError as error:
                raise ValidationError("持久幂等索引中的ModuleRunRef无效") from error
            if not isinstance(reference, dict):
                raise ValidationError("持久幂等索引中的ModuleRunRef无效")
            connection.commit()
            return "replay", {str(key): str(value) for key, value in reference.items()}

    def mark_uncertain(
        self, *, tenant_id: str, task_id: str, module: str, action: str,
        idempotency_key: str, input_hash: str,
    ) -> None:
        """Fail closed when a run may exist but its final RunRef was not indexed."""
        scope = (tenant_id, task_id, module, action, idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tool_idempotency SET state='uncertain', updated_at=? "
                "WHERE tenant_id=? AND task_id=? AND module=? AND action=? AND idempotency_key=? "
                "AND input_hash=? AND state='claimed'",
                (_now(), *scope, input_hash),
            )
            if cursor.rowcount != 1:
                raise ValidationError("无法将已提交但未完成索引的幂等claim标记为uncertain")
            connection.commit()

    def complete(
        self,
        *,
        tenant_id: str,
        task_id: str,
        module: str,
        action: str,
        idempotency_key: str,
        input_hash: str,
        reference: dict[str, str],
    ) -> None:
        scope = (tenant_id, task_id, module, action, idempotency_key)
        encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT input_hash, state, run_ref_json FROM tool_idempotency WHERE tenant_id=? AND task_id=? AND module=? AND action=? AND idempotency_key=?",
                scope,
            ).fetchone()
            if row is None or row[0] != input_hash:
                raise ValidationError("幂等claim与ModuleRun完成记录不一致")
            if row[1] == "completed":
                if row[2] != encoded:
                    raise ValidationError("同一幂等键不能对应不同ModuleRunRef")
                connection.commit()
                return
            connection.execute(
                "UPDATE tool_idempotency SET state='completed', run_ref_json=?, updated_at=? WHERE tenant_id=? AND task_id=? AND module=? AND action=? AND idempotency_key=?",
                (encoded, _now(), *scope),
            )
            connection.commit()

    def abandon(
        self, *, tenant_id: str, task_id: str, module: str, action: str,
        idempotency_key: str, input_hash: str,
    ) -> None:
        """Release only this unfinished claim; an already completed run is immutable."""
        scope = (tenant_id, task_id, module, action, idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM tool_idempotency WHERE tenant_id=? AND task_id=? AND module=? AND action=? AND idempotency_key=? AND input_hash=? AND state='claimed'",
                (*scope, input_hash),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10.0, isolation_level=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ("ToolIdempotencyStore",)
