"""Task-owned background operations that outlive a browser page."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import Event, Lock
from typing import Any, Callable
from uuid import uuid4

from ..errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity


ACTIVE_STATES = frozenset({"queued", "running", "recovering", "cancel_requested"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_ALL_STATES = ACTIVE_STATES | TERMINAL_STATES
_ACTIVE_SQL = "'queued','running','recovering','cancel_requested'"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INPUT_HASH = re.compile(r"^[0-9a-f]{64}$")


class OperationCancelled(RuntimeError):
    """Raised by a cooperative operation after it has observed cancellation."""


class _OperationControl:
    def __init__(self, service: "TaskOperationService", operation_id: str, event: Event) -> None:
        self._service = service
        self._operation_id = operation_id
        self._event = event

    def __call__(self) -> bool:
        return self._event.is_set()

    def commit_if_active(self, commit: Callable[[], Any]) -> Any:
        """Linearize cancellation against the operation's durable commit."""

        with self._service._guard:
            if self._event.is_set():
                raise UserActionError(
                    "compute_cancelled",
                    "本次计算已取消。",
                    stage="compute",
                    next_step="如需结果，请重新运行。",
                )
            self._service._committing.add(self._operation_id)
            return commit()

    def compute_status(self, state: str, recovery_attempts: int, diagnostic_id: str | None) -> None:
        if self._event.is_set() or self._service._state(self._operation_id) in TERMINAL_STATES:
            return
        if state == "recovering":
            self._service._transition(
                self._operation_id,
                "recovering",
                "计算仍在后台运行，正在恢复计算服务。",
                recovery_attempts=recovery_attempts,
                diagnostic_id=diagnostic_id,
            )
        elif state == "running":
            self._service._transition(
                self._operation_id,
                "running",
                "计算服务已恢复，正在继续运行。",
                recovery_attempts=recovery_attempts,
                diagnostic_id=diagnostic_id,
            )


@dataclass(frozen=True)
class TaskOperation:
    operation_id: str
    tenant_id: str
    owner_id: str
    task_id: str
    module: str
    kind: str
    state: str
    created_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    message: str | None = None
    recovery_attempts: int = 0
    diagnostic_id: str | None = None

    def public(self, *, include_result: bool = False) -> dict[str, Any]:
        value = {
            "operation_id": self.operation_id,
            "task_id": self.task_id,
            "module": self.module,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message": self.message,
            "recovery_attempts": self.recovery_attempts,
        }
        if self.diagnostic_id:
            value["diagnostic_id"] = self.diagnostic_id
        if include_result and self.result is not None:
            value["result"] = self.result
        return value


class TaskOperationService:
    """Durable operation index plus an App-owned executor.

    The input and authority stay at the normal App endpoint.  This service
    stores only the owner-visible result projection, never request payloads or
    provider credentials.  A disconnected WKWebView therefore cannot cancel a
    calculation simply by disappearing.
    """

    def __init__(
        self,
        path: Path,
        *,
        maximum_running: int = 4,
        maximum_compute_orchestrators: int = 8,
        maximum_conversations: int = 6,
    ) -> None:
        if maximum_running < 1:
            raise ValueError("maximum_running must be positive")
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._executors = {
            "control": ThreadPoolExecutor(max_workers=maximum_running, thread_name_prefix="optionhelper-control"),
            "compute": ThreadPoolExecutor(max_workers=maximum_compute_orchestrators, thread_name_prefix="optionhelper-compute-operation"),
            "conversation": ThreadPoolExecutor(max_workers=maximum_conversations, thread_name_prefix="optionhelper-conversation"),
        }
        self._controls: dict[str, Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._committing: set[str] = set()
        self._guard = Lock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_operations ("
                "operation_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,"
                "task_id TEXT NOT NULL,module TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,result_json TEXT,message TEXT,"
                "request_id TEXT,input_hash TEXT,recovery_attempts INTEGER NOT NULL DEFAULT 0,diagnostic_id TEXT)"
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(task_operations)")}
            if "request_id" not in columns:
                connection.execute("ALTER TABLE task_operations ADD COLUMN request_id TEXT")
            if "input_hash" not in columns:
                connection.execute("ALTER TABLE task_operations ADD COLUMN input_hash TEXT")
            if "recovery_attempts" not in columns:
                connection.execute("ALTER TABLE task_operations ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0")
            if "diagnostic_id" not in columns:
                connection.execute("ALTER TABLE task_operations ADD COLUMN diagnostic_id TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_operations_owner_task_idx "
                "ON task_operations(tenant_id,owner_id,task_id,updated_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS task_operations_request_claim_idx "
                "ON task_operations(tenant_id,owner_id,task_id,module,kind,request_id) "
                "WHERE request_id IS NOT NULL"
            )
            connection.execute(
                "UPDATE task_operations SET state='interrupted',updated_at=?,message=? "
                f"WHERE state IN ({_ACTIVE_SQL})",
                (_now(), "OptionHelper已退出，未完成运行已中断。"),
            )

    def submit(
        self,
        identity: SessionIdentity,
        *,
        task_id: str,
        module: str,
        kind: str,
        operation: Callable[[Callable[[], bool]], dict[str, Any]],
        request_id: str | None = None,
        input_hash: str | None = None,
        lane: str = "control",
    ) -> TaskOperation:
        if not task_id or not module or not kind:
            raise ValidationError("Task operation identity is invalid")
        if lane not in self._executors:
            raise ValidationError("Task operation lane is invalid")
        normalized_request_id = _request_id(request_id)
        normalized_input_hash = _input_hash(input_hash)
        if (normalized_request_id is None) != (normalized_input_hash is None):
            raise ValidationError("Task operation request_id and input_hash must be supplied together")
        with self._connect() as connection:
            if normalized_request_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT operation_id,tenant_id,owner_id,task_id,module,kind,state,created_at,updated_at,"
                    "result_json,message,recovery_attempts,diagnostic_id,input_hash FROM task_operations "
                    "WHERE tenant_id=? AND owner_id=? AND task_id=? AND module=? AND kind=? AND request_id=?",
                    (identity.tenant_id, identity.principal_id, task_id, module, kind, normalized_request_id),
                ).fetchone()
                if existing is not None:
                    if str(existing[13] or "") != normalized_input_hash:
                        connection.rollback()
                        raise ValidationError("同一请求ID不能对应不同的后台操作输入")
                    connection.commit()
                    record = _row(existing)
                    self._require_owner(identity, record)
                    return record
                matching = connection.execute(
                    "SELECT operation_id,tenant_id,owner_id,task_id,module,kind,state,created_at,updated_at,"
                    "result_json,message,recovery_attempts,diagnostic_id FROM task_operations "
                    "WHERE tenant_id=? AND owner_id=? AND task_id=? AND module=? AND kind=? AND input_hash=? "
                    f"AND state IN ({_ACTIVE_SQL}) ORDER BY updated_at DESC LIMIT 1",
                    (identity.tenant_id, identity.principal_id, task_id, module, kind, normalized_input_hash),
                ).fetchone()
                if matching is not None:
                    connection.commit()
                    record = _row(matching)
                    self._require_owner(identity, record)
                    return record
            active = int(connection.execute(
                f"SELECT COUNT(*) FROM task_operations WHERE state IN ({_ACTIVE_SQL})"
            ).fetchone()[0])
            if active >= 32:
                if normalized_request_id is not None:
                    connection.rollback()
                raise UnavailableCapabilityError("task_operation_queue", "后台运行队列已满，请稍后重试。")
            identifier = str(uuid4())
            now = _now()
            connection.execute(
                "INSERT INTO task_operations (operation_id,tenant_id,owner_id,task_id,module,kind,state,"
                "created_at,updated_at,result_json,message,request_id,input_hash,recovery_attempts,diagnostic_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, identity.tenant_id, identity.principal_id, task_id, module, kind, "queued",
                    now, now, None, None, normalized_request_id, normalized_input_hash, 0, None,
                ),
            )
            if normalized_request_id is not None:
                connection.commit()
        control = Event()
        with self._guard:
            self._controls[identifier] = control
            self._futures[identifier] = self._executors[lane].submit(self._run, identifier, operation, control)
        return self.get(identity, identifier)

    def get(self, identity: SessionIdentity, operation_id: str) -> TaskOperation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT operation_id,tenant_id,owner_id,task_id,module,kind,state,created_at,updated_at,"
                "result_json,message,recovery_attempts,diagnostic_id "
                "FROM task_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        record = _row(row)
        self._require_owner(identity, record)
        return record

    def list(self, identity: SessionIdentity, task_id: str, *, active_only: bool = False) -> list[TaskOperation]:
        query = (
            "SELECT operation_id,tenant_id,owner_id,task_id,module,kind,state,created_at,updated_at,"
            "result_json,message,recovery_attempts,diagnostic_id "
            "FROM task_operations WHERE tenant_id=? AND owner_id=? AND task_id=?"
        )
        values: tuple[object, ...] = (identity.tenant_id, identity.principal_id, task_id)
        if active_only:
            query += f" AND state IN ({_ACTIVE_SQL})"
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_row(row) for row in rows]

    def active_summaries(self, identity: SessionIdentity, task_ids: list[str]) -> dict[str, list[dict[str, str]]]:
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id,task_id,module,kind,state,updated_at FROM task_operations "
                f"WHERE tenant_id=? AND owner_id=? AND task_id IN ({placeholders}) "
                f"AND state IN ({_ACTIVE_SQL}) ORDER BY updated_at DESC",
                (identity.tenant_id, identity.principal_id, *task_ids),
            ).fetchall()
        grouped: dict[str, list[dict[str, str]]] = {}
        for operation_id, task_id, module, kind, state, updated_at in rows:
            grouped.setdefault(str(task_id), []).append({
                "operation_id": str(operation_id), "module": str(module), "kind": str(kind),
                "state": str(state), "updated_at": str(updated_at),
            })
        return grouped

    def recover_succeeded(
        self,
        proof: Callable[[TaskOperation, str, str], dict[str, Any] | None],
    ) -> int:
        """Restore interrupted compute operations from an exact durable proof.

        The callback receives only operations that carry the request and input
        identities used by ToolDispatcher.  It must return the owner-visible
        result reconstructed from a verified immutable ModuleRun, never a
        request payload or a signal to rerun the calculation.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id,tenant_id,owner_id,task_id,module,kind,state,created_at,updated_at,"
                "result_json,message,recovery_attempts,diagnostic_id,request_id,input_hash "
                "FROM task_operations WHERE state='interrupted' AND module IN ('payoffer','pricer','backtester') "
                "AND kind='run' AND request_id IS NOT NULL AND input_hash IS NOT NULL"
            ).fetchall()
        recovered = 0
        for raw in rows:
            record = _row(raw[:13])
            request_id, input_hash = str(raw[13]), str(raw[14])
            result = proof(record, request_id, input_hash)
            if result is None:
                continue
            if not isinstance(result, dict):
                raise ValidationError("Recovered task operation result must be an object")
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE task_operations SET state='succeeded',updated_at=?,message=?,result_json=? "
                    "WHERE operation_id=? AND tenant_id=? AND owner_id=? AND task_id=? AND module=? AND kind='run' "
                    "AND state='interrupted' AND request_id=? AND input_hash=?",
                    (
                        _now(), "运行结果已从不可变提交恢复。", encoded,
                        record.operation_id, record.tenant_id, record.owner_id, record.task_id,
                        record.module, request_id, input_hash,
                    ),
                )
            recovered += int(cursor.rowcount)
        return recovered

    def cancel(self, identity: SessionIdentity, operation_id: str) -> TaskOperation:
        record = self.get(identity, operation_id)
        return self._cancel_owned(identity, record)

    def cancel_for_task(
        self, identity: SessionIdentity, task_id: str, operation_id: str,
    ) -> TaskOperation:
        """Cancel only after the immutable operation/task binding is verified."""

        record = self.get(identity, operation_id)
        if record.task_id != task_id:
            raise KeyError(operation_id)
        return self._cancel_owned(identity, record)

    def _cancel_owned(self, identity: SessionIdentity, record: TaskOperation) -> TaskOperation:
        with self._guard:
            record = self.get(identity, record.operation_id)
            if record.state in TERMINAL_STATES:
                return record
            commit_started = record.operation_id in self._committing
            control = self._controls.get(record.operation_id)
            future = self._futures.get(record.operation_id)
            if not commit_started and control is not None:
                control.set()
        if commit_started:
            return self.get(identity, record.operation_id)
        if record.state == "queued" and future is not None and future.cancel():
            self._transition(record.operation_id, "cancelled", "已取消。")
        else:
            self._transition(record.operation_id, "cancel_requested", "正在停止运行。")
        return self.get(identity, record.operation_id)

    def cancel_task(self, identity: SessionIdentity, task_id: str) -> list[TaskOperation]:
        return [self.cancel(identity, item.operation_id) for item in self.list(identity, task_id, active_only=True)]

    def interrupt_active(self, message: str) -> int:
        with self._guard:
            committing = tuple(self._committing)
            for operation_id, control in self._controls.items():
                if operation_id not in self._committing:
                    control.set()
            exclusion = ""
            parameters: list[Any] = [_now(), message]
            if committing:
                exclusion = f" AND operation_id NOT IN ({','.join('?' for _ in committing)})"
                parameters.extend(committing)
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE task_operations SET state='interrupted',updated_at=?,message=? "
                    f"WHERE state IN ({_ACTIVE_SQL}){exclusion}",
                    parameters,
                )
        return int(cursor.rowcount)

    def shutdown(self) -> None:
        self.interrupt_active("OptionHelper已退出，未完成运行已中断。")
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        operation_id: str,
        operation: Callable[[Callable[[], bool]], dict[str, Any]],
        control: Event,
    ) -> None:
        operation_control = _OperationControl(self, operation_id, control)
        try:
            if control.is_set():
                self._transition(operation_id, "cancelled", "已取消。")
                return
            self._transition(operation_id, "running", "正在运行。")
            result = operation(operation_control)
            # A cancellation request is advisory until the implementation
            # confirms it. Existing financial engines are synchronous, so a
            # completed durable result remains a success rather than a false
            # cancellation.
            state = self._state(operation_id)
            if state == "interrupted":
                return
            if control.is_set() and state == "cancel_requested" and result.get("cancelled") is True:
                self._transition(operation_id, "cancelled", "已取消。")
            elif result.get("ok") is False or result.get("status") in {
                "failed",
                "unsupported",
                "timed_out",
            }:
                self._transition(
                    operation_id,
                    "failed",
                    str(result.get("message") or "运行未完成，请按结果提示处理。"),
                    result=result,
                )
            else:
                self._transition(operation_id, "succeeded", "运行完成。", result=result)
        except OperationCancelled:
            self._transition(operation_id, "cancelled", "已取消。")
        except UserActionError as error:
            if self._state(operation_id) != "interrupted":
                self._transition(
                    operation_id,
                    "failed",
                    error.message,
                    result={
                        "error": error.code,
                        "failure_code": error.code,
                        "stage": error.stage,
                        "message": error.message,
                        "next_step": error.next_step,
                        **({"retryable": True} if error.retryable else {}),
                        **error.details,
                    },
                )
        except UnavailableCapabilityError as error:
            if self._state(operation_id) != "interrupted":
                message = error.message or "当前运行所需能力暂不可用。"
                self._transition(
                    operation_id,
                    "failed",
                    message,
                    result={
                        "error": error.failure_code,
                        "failure_code": error.failure_code,
                        "stage": error.stage,
                        "message": message,
                        "next_step": error.next_step,
                    },
                )
        except AuthorizationError:
            if self._state(operation_id) != "interrupted":
                self._transition(
                    operation_id,
                    "failed",
                    "当前任务权限或运行范围已失效。",
                    result={
                        "error": "domain_rejected",
                        "failure_code": "domain_rejected",
                        "stage": "authorization",
                        "message": "当前任务权限或运行范围已失效。",
                        "next_step": "请重新进入当前任务并确认登录状态。",
                    },
                )
        except ValidationError as error:
            if self._state(operation_id) != "interrupted":
                message = _safe_validation_message(error)
                self._transition(
                    operation_id,
                    "failed",
                    message,
                    result={
                        "error": "domain_rejected",
                        "failure_code": "domain_rejected",
                        "stage": "input",
                        "message": message,
                        "next_step": "请按提示检查当前产品、参数、行情和交易日历。",
                    },
                )
        except Exception:
            if self._state(operation_id) != "interrupted":
                diagnostic_id = f"diag_{uuid4().hex}"
                message = "后台运行内部状态异常。"
                self._transition(
                    operation_id,
                    "failed",
                    message,
                    result={
                        "error": "operation_internal_error",
                        "failure_code": "operation_internal_error",
                        "stage": "operation",
                        "message": message,
                        "next_step": "请保留诊断编号并重新打开当前任务。",
                        "diagnostic_id": diagnostic_id,
                    },
                    diagnostic_id=diagnostic_id,
                )
        finally:
            with self._guard:
                self._committing.discard(operation_id)
                self._controls.pop(operation_id, None)
                self._futures.pop(operation_id, None)

    def _state(self, operation_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT state FROM task_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return str(row[0]) if row else "interrupted"

    def _transition(
        self,
        operation_id: str,
        state: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        recovery_attempts: int | None = None,
        diagnostic_id: str | None = None,
    ) -> None:
        if state not in _ALL_STATES:
            raise ValidationError("Task operation state is invalid")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str) if result is not None else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE task_operations SET state=?,updated_at=?,message=?,result_json=COALESCE(?,result_json),"
                "recovery_attempts=COALESCE(?,recovery_attempts),diagnostic_id=COALESCE(?,diagnostic_id) "
                f"WHERE operation_id=? AND state IN ({_ACTIVE_SQL})",
                (state, _now(), message, encoded, recovery_attempts, diagnostic_id, operation_id),
            )

    def _require_owner(self, identity: SessionIdentity, record: TaskOperation) -> None:
        if record.tenant_id != identity.tenant_id or record.owner_id != identity.principal_id:
            raise AuthorizationError("task.read", "task operation is not owned by current caller")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()


def _row(row: tuple[Any, ...]) -> TaskOperation:
    result = None
    if isinstance(row[9], str):
        try:
            decoded = json.loads(row[9])
            result = decoded if isinstance(decoded, dict) else None
        except json.JSONDecodeError:
            result = None
    return TaskOperation(
        *[str(value) for value in row[:9]],
        result=result,
        message=str(row[10]) if row[10] is not None else None,
        recovery_attempts=int(row[11] or 0),
        diagnostic_id=str(row[12]) if row[12] is not None else None,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_id(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    request_id = str(value).strip()
    if not _REQUEST_ID.fullmatch(request_id):
        raise ValidationError("Task operation request_id is invalid")
    return request_id


def _input_hash(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    input_hash = str(value).strip()
    if not _INPUT_HASH.fullmatch(input_hash):
        raise ValidationError("Task operation input_hash is invalid")
    return input_hash


def _safe_validation_message(error: ValidationError) -> str:
    message = str(error).strip().replace("\r", " ").replace("\n", " ")[:240]
    if not message or "/" in message or "\\" in message:
        return "当前输入未通过运行校验。"
    return message
