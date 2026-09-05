"""Isolated calculation execution owned by the App control plane.

The supervisor keeps capability execution out of the HTTP process.  Inputs are
already authorized and frozen by :class:`ToolGateway`; workers receive only a
JSON-safe request, immutable data bytes and a private draft result store.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import base64
import hashlib
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import tempfile
from threading import Condition, Event, Lock, Thread
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from ..errors import UserActionError, ValidationError


PROTOCOL = "optionhelper.compute-worker.v1"
MAX_FRAME_BYTES = 128 * 1024 * 1024
COMPUTE_MODULES = frozenset({"payoffer", "pricer", "backtester"})
RECOVERY_DELAYS_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0)
MAX_RECOVERY_ATTEMPTS = 5
MAX_RECOVERY_SECONDS = 90.0
MAX_IDENTICAL_RECOVERIES = 2
STARTUP_TIMEOUT_SECONDS = 10.0
HEARTBEAT_TIMEOUT_SECONDS = {
    "payoffer": 120.0,
    "pricer": 120.0,
    "backtester": 15.0 * 60.0,
}


class ComputeRecoveryRequired(RuntimeError):
    """A recoverable compute-infrastructure or engine boundary failure."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_id: str | None = None,
        error_type: str | None = None,
    ) -> None:
        self.diagnostic_id = diagnostic_id or f"diag_{uuid4().hex}"
        self.error_type = error_type or type(self).__name__
        super().__init__(message)


@dataclass(frozen=True)
class PreparedComputeExecution:
    execution_id: str
    task_id: str
    module: str
    tenant_id: str
    request: dict[str, Any]
    caller_context: dict[str, Any]
    host_context: dict[str, Any]
    data_snapshots: tuple[dict[str, Any], ...]
    scripts_root: str
    runtime_root: str | None
    content_hashes: dict[str, str]
    capability_hash: str
    execution_token: str

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        module: str,
        tenant_id: str,
        request: Mapping[str, Any],
        caller_context: Mapping[str, Any],
        host_context: Mapping[str, Any],
        data_snapshots: tuple[dict[str, Any], ...],
        scripts_root: str,
        runtime_root: str | None,
        content_hashes: Mapping[str, str],
    ) -> "PreparedComputeExecution":
        if module not in COMPUTE_MODULES or not task_id or not tenant_id:
            raise ValidationError("隔离计算执行范围无效")
        capability_hash = _canonical_hash(dict(content_hashes))
        return cls(
            execution_id=f"compute_{uuid4().hex}",
            task_id=task_id,
            module=module,
            tenant_id=tenant_id,
            request=dict(request),
            caller_context=dict(caller_context),
            host_context=dict(host_context),
            data_snapshots=data_snapshots,
            scripts_root=str(Path(scripts_root).resolve()),
            runtime_root=str(Path(runtime_root).resolve()) if runtime_root else None,
            content_hashes={str(key): str(value) for key, value in content_hashes.items()},
            capability_hash=capability_hash,
            execution_token=f"exec_{uuid4().hex}",
        )

    def to_payload(self) -> dict[str, Any]:
        if _canonical_hash(self.content_hashes) != self.capability_hash:
            raise ValidationError("Capability文件清单在入队前发生变化")
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "module": self.module,
            "tenant_id": self.tenant_id,
            "request": self.request,
            "caller_context": self.caller_context,
            "host_context": self.host_context,
            "data_snapshots": list(self.data_snapshots),
            "scripts_root": self.scripts_root,
            "runtime_root": self.runtime_root,
            "content_hashes": self.content_hashes,
            "capability_hash": self.capability_hash,
            "execution_token": self.execution_token,
        }


@dataclass
class _QueuedExecution:
    prepared: PreparedComputeExecution
    cancelled: Callable[[], bool]
    future: Future[dict[str, Any]]
    attempt_id: str


class ComputeProcessSupervisor:
    """Adaptive, task-fair pool of persistent single-operation workers."""

    def __init__(
        self,
        *,
        minimum_slots: int = 2,
        maximum_slots: int = 8,
        worker_factory: Callable[[list[str], int], Any] | None = None,
        diagnostics_root: Path | None = None,
    ) -> None:
        if minimum_slots < 1 or maximum_slots < minimum_slots:
            raise ValueError("compute slot bounds are invalid")
        self._minimum_slots = minimum_slots
        self._maximum_slots = maximum_slots
        self._slot_limit = adaptive_compute_slots(minimum_slots, maximum_slots)
        self._diagnostics = _ComputeDiagnostics(diagnostics_root)
        self._worker_factory = worker_factory or (
            lambda command, thread_limit: _ComputeWorker(
                command,
                thread_limit,
                diagnostics=self._diagnostics,
            )
        )
        self._condition = Condition()
        self._queues: dict[str, deque[_QueuedExecution]] = {}
        self._task_order: deque[str] = deque()
        self._workers: list[_ComputeWorker] = []
        self._idle: deque[_ComputeWorker] = deque()
        self._last_task_id: str | None = None
        self._closed = False
        self._dispatcher = Thread(target=self._dispatch_loop, name="optionhelper-compute-dispatch", daemon=True)
        self._dispatcher.start()

    @property
    def slot_limit(self) -> int:
        return self._slot_limit

    def execute(
        self,
        prepared: PreparedComputeExecution,
        *,
        cancelled: Callable[[], bool] | None = None,
        status_callback: Callable[[str, int, str | None], None] | None = None,
    ) -> dict[str, Any]:
        is_cancelled = cancelled or (lambda: False)
        recovery_count = 0
        recovery_started = time.monotonic()
        repeated_diagnostic: tuple[str, str] | None = None
        repeated_count = 0
        while True:
            if is_cancelled():
                raise _compute_cancelled()
            future: Future[dict[str, Any]] = Future()
            attempt_id = f"attempt_{uuid4().hex}"
            item = _QueuedExecution(prepared, is_cancelled, future, attempt_id)
            with self._condition:
                if self._closed:
                    raise _compute_cancelled()
                queue = self._queues.setdefault(prepared.task_id, deque())
                if not queue:
                    self._task_order.append(prepared.task_id)
                queue.append(item)
                self._condition.notify_all()
            try:
                result = future.result()
            except ComputeRecoveryRequired as error:
                recovery_count += 1
                diagnostic = (error.error_type, str(error))
                if diagnostic == repeated_diagnostic:
                    repeated_count += 1
                else:
                    repeated_diagnostic = diagnostic
                    repeated_count = 1
                if status_callback is not None:
                    status_callback("recovering", recovery_count, error.diagnostic_id)
                elapsed = time.monotonic() - recovery_started
                if (
                    recovery_count >= MAX_RECOVERY_ATTEMPTS
                    or elapsed >= MAX_RECOVERY_SECONDS
                    or repeated_count >= MAX_IDENTICAL_RECOVERIES
                ):
                    raise UserActionError(
                        "compute_recovery_exhausted",
                        "计算服务连续恢复失败，本次运行已停止。",
                        stage="compute",
                        next_step=f"请保留诊断编号{error.diagnostic_id}并重新运行；若重复出现，请检查Capability和运行依赖。",
                        retryable=True,
                    ) from error
                delay = RECOVERY_DELAYS_SECONDS[min(recovery_count - 1, len(RECOVERY_DELAYS_SECONDS) - 1)]
                if _wait_cancelled(delay, is_cancelled):
                    raise _compute_cancelled() from error
                if status_callback is not None:
                    status_callback("running", recovery_count, error.diagnostic_id)
                continue
            return result

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            for queue in self._queues.values():
                for item in queue:
                    item.future.cancel()
            self._queues.clear()
            self._task_order.clear()
            workers = tuple(self._workers)
            self._condition.notify_all()
        for worker in workers:
            worker.close()
        self._dispatcher.join(timeout=2)

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                self._refresh_admission_limit()
                while not self._closed and (not self._task_order or not self._worker_available()):
                    self._condition.wait(timeout=1.0)
                    self._refresh_admission_limit()
                if self._closed:
                    return
                item = self._next_item()
                try:
                    worker = self._take_worker()
                except BaseException as error:
                    diagnostic_id = self._diagnostics.record(
                        "worker_start_failed",
                        error_type=type(error).__name__,
                    )
                    item.future.set_exception(ComputeRecoveryRequired(
                        "计算服务启动未完成。",
                        diagnostic_id=diagnostic_id,
                        error_type=type(error).__name__,
                    ))
                    self._condition.notify_all()
                    continue
            Thread(
                target=self._run_item,
                args=(worker, item),
                name=f"optionhelper-compute-{item.prepared.module}",
                daemon=True,
            ).start()

    def _refresh_admission_limit(self) -> None:
        self._slot_limit = adaptive_compute_slots(self._minimum_slots, self._maximum_slots)

    def _worker_available(self) -> bool:
        active = len(self._workers) - len(self._idle)
        return active < self._slot_limit and (bool(self._idle) or len(self._workers) < self._slot_limit)

    def _take_worker(self) -> "_ComputeWorker":
        if self._idle:
            return self._idle.popleft()
        worker = self._worker_factory(worker_command(), threads_per_worker(self._slot_limit))
        self._workers.append(worker)
        return worker

    def _next_item(self) -> _QueuedExecution:
        if len(self._task_order) > 1 and self._task_order[0] == self._last_task_id:
            self._task_order.rotate(-1)
        task_id = self._task_order.popleft()
        self._last_task_id = task_id
        queue = self._queues[task_id]
        item = queue.popleft()
        if queue:
            self._task_order.append(task_id)
        else:
            self._queues.pop(task_id, None)
        return item

    def _run_item(self, worker: "_ComputeWorker", item: _QueuedExecution) -> None:
        reusable = True
        try:
            if item.cancelled():
                raise UserActionError(
                    "compute_cancelled",
                    "本次计算已取消。",
                    stage="compute",
                    next_step="如需结果，请重新运行。",
                )
            parameters = inspect.signature(worker.execute).parameters
            if "attempt_id" in parameters:
                result = worker.execute(item.prepared, item.cancelled, attempt_id=item.attempt_id)
            else:
                result = worker.execute(item.prepared, item.cancelled)
            item.future.set_result(result)
        except BaseException as error:
            reusable = worker.alive and not isinstance(error, ComputeRecoveryRequired)
            if not item.future.done():
                item.future.set_exception(error)
        finally:
            with self._condition:
                if reusable and worker.alive and not self._closed:
                    self._idle.append(worker)
                else:
                    worker.close()
                    if worker in self._workers:
                        self._workers.remove(worker)
                self._condition.notify_all()


class _ComputeWorker:
    def __init__(
        self,
        command: list[str],
        thread_limit: int,
        *,
        diagnostics: "_ComputeDiagnostics | None" = None,
    ) -> None:
        self.generation = f"worker_{uuid4().hex}"
        self._diagnostics = diagnostics or _ComputeDiagnostics(None)
        self._workspace = tempfile.TemporaryDirectory(prefix="optionhelper-compute-worker-")
        environment = _worker_environment(thread_limit, Path(self._workspace.name))
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                cwd=self._workspace.name,
                bufsize=0,
            )
        except BaseException:
            self._workspace.cleanup()
            raise
        self._lock = Lock()
        self._stderr_thread = Thread(
            target=self._drain_stderr,
            name="optionhelper-compute-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._await_ready()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def execute(
        self,
        prepared: PreparedComputeExecution,
        cancelled: Callable[[], bool],
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.alive or self._process.stdin is None or self._process.stdout is None:
                raise self._recovery("worker_not_running", "计算服务进程未启动。")
            request = {
                "protocol": PROTOCOL,
                "type": "execute",
                "attempt_id": attempt_id,
                "execution": prepared.to_payload(),
            }
            try:
                _write_frame(self._process.stdin, request)
            except BaseException as error:
                raise self._recovery(
                    "worker_request_write_failed",
                    "计算服务请求传输中断。",
                    error=error,
                    attempt_id=attempt_id,
                ) from error
            frames: Queue[tuple[str, Any]] = Queue()

            def receive() -> None:
                try:
                    while True:
                        frame = _read_frame(self._process.stdout)
                        frames.put(("frame", frame))
                        if frame.get("type") == "result":
                            return
                except BaseException as error:
                    frames.put(("error", error))

            Thread(target=receive, name="optionhelper-compute-ipc", daemon=True).start()
            last_heartbeat = time.monotonic()
            last_stage: str | None = None
            timeout = HEARTBEAT_TIMEOUT_SECONDS[prepared.module]
            response: dict[str, Any] | None = None
            while response is None:
                if cancelled():
                    self.close()
                    raise _compute_cancelled()
                if not self.alive:
                    raise self._recovery(
                        "worker_exited",
                        "计算服务进程意外退出。",
                        attempt_id=attempt_id,
                        exit_code=self._process.poll(),
                    )
                try:
                    kind, value = frames.get(timeout=0.1)
                except Empty:
                    if time.monotonic() - last_heartbeat >= timeout:
                        raise self._recovery(
                            "worker_heartbeat_timeout",
                            "计算服务长时间没有响应。",
                            attempt_id=attempt_id,
                        )
                    continue
                if kind == "error":
                    raise self._recovery(
                        "worker_response_read_failed",
                        "计算服务响应传输中断。",
                        error=value,
                        attempt_id=attempt_id,
                    ) from value
                frame = value
                if not isinstance(frame, dict) or frame.get("protocol") != PROTOCOL:
                    raise self._recovery(
                        "worker_protocol_invalid",
                        "计算服务返回了无效协议。",
                        attempt_id=attempt_id,
                    )
                if frame.get("execution_id") != prepared.execution_id or frame.get("attempt_id") != attempt_id:
                    raise self._recovery(
                        "worker_execution_mismatch",
                        "计算服务返回了错误的执行标识。",
                        attempt_id=attempt_id,
                    )
                if frame.get("type") == "heartbeat":
                    last_heartbeat = time.monotonic()
                    stage = str(frame.get("stage", "")).strip()
                    if stage and stage != last_stage:
                        last_stage = stage
                        self._diagnostics.record(
                            "worker_stage",
                            worker_generation=self.generation,
                            attempt_id=attempt_id,
                            stage=stage,
                        )
                    continue
                if frame.get("type") != "result":
                    raise self._recovery(
                        "worker_frame_invalid",
                        "计算服务返回了未知响应。",
                        attempt_id=attempt_id,
                    )
                response = frame
            if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
                raise self._recovery("worker_protocol_invalid", "计算服务返回了无效协议。", attempt_id=attempt_id)
            if response.get("ok") is not True:
                failure = response.get("failure") if isinstance(response.get("failure"), Mapping) else {}
                if failure.get("failure_class") != "domain_rejected":
                    raise self._recovery(
                        str(failure.get("failure_code") or "engine_recovering"),
                        "计算服务内部运行未完成。",
                        diagnostic_id=str(failure.get("diagnostic_id") or "") or None,
                        error_type=str(failure.get("error_type") or "") or None,
                        attempt_id=attempt_id,
                    )
                raise UserActionError(
                    str(failure.get("failure_code") or "domain_rejected"),
                    str(failure.get("message") or "当前输入未通过计算校验。"),
                    stage=str(failure.get("stage") or "input"),
                    next_step=str(failure.get("next_step") or "请按提示调整当前输入后重试。"),
                    retryable=False,
                )
            return dict(response)

    def _await_ready(self) -> None:
        if self._process.stdout is None:
            raise self._recovery("worker_stdout_missing", "计算服务启动通道不可用。")
        outcome: Queue[tuple[str, Any]] = Queue(maxsize=1)

        def receive() -> None:
            try:
                outcome.put(("frame", _read_frame(self._process.stdout)))
            except BaseException as error:
                outcome.put(("error", error))

        Thread(target=receive, name="optionhelper-compute-ready", daemon=True).start()
        try:
            kind, value = outcome.get(timeout=STARTUP_TIMEOUT_SECONDS)
        except Empty as error:
            self.close()
            raise self._recovery("worker_ready_timeout", "计算服务启动超时。") from error
        if kind == "error":
            self.close()
            raise self._recovery(
                "worker_ready_read_failed",
                "计算服务启动响应不可用。",
                error=value,
            ) from value
        if not isinstance(value, dict) or value.get("protocol") != PROTOCOL or value.get("type") != "ready":
            self.close()
            raise self._recovery("worker_ready_invalid", "计算服务启动协议无效。")
        self._diagnostics.record("worker_ready", worker_generation=self.generation)

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not line:
                return
            self._diagnostics.record(
                "worker_stderr",
                worker_generation=self.generation,
                detail=line.decode("utf-8", errors="replace"),
            )

    def _recovery(
        self,
        event: str,
        message: str,
        *,
        error: BaseException | None = None,
        diagnostic_id: str | None = None,
        error_type: str | None = None,
        attempt_id: str | None = None,
        exit_code: int | None = None,
    ) -> ComputeRecoveryRequired:
        identifier = diagnostic_id or f"diag_{uuid4().hex}"
        self._diagnostics.record(
            event,
            diagnostic_id=identifier,
            worker_generation=self.generation,
            attempt_id=attempt_id,
            error_type=error_type or (type(error).__name__ if error is not None else None),
            exit_code=exit_code,
        )
        return ComputeRecoveryRequired(
            message,
            diagnostic_id=identifier,
            error_type=error_type or (type(error).__name__ if error is not None else None),
        )

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        self._diagnostics.record(
            "worker_closed",
            worker_generation=self.generation,
            exit_code=process.poll(),
        )
        self._workspace.cleanup()


class _ComputeDiagnostics:
    """Small, local-only and sanitized compute diagnostic log."""

    def __init__(self, root: Path | None) -> None:
        self._logger: logging.Logger | None = None
        if root is None:
            return
        directory = root.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"optionhelper.compute.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(
            directory / "compute.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        self._logger = logger

    def record(self, event: str, **fields: Any) -> str:
        diagnostic_id = str(fields.pop("diagnostic_id", "") or f"diag_{uuid4().hex}")
        if self._logger is not None:
            payload = {
                "timestamp": time.time(),
                "diagnostic_id": diagnostic_id,
                "event": _sanitize_diagnostic(event),
            }
            for key, value in fields.items():
                if value is not None:
                    payload[str(key)] = _sanitize_diagnostic(value)
            self._logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return diagnostic_id


def adaptive_compute_slots(minimum: int = 2, maximum: int = 8) -> int:
    cpu_count = os.cpu_count()
    preferred_minimum = max(2, minimum)
    cpu_slots = preferred_minimum if not cpu_count else max(preferred_minimum, (cpu_count - 2) // 2)
    available = _available_memory_bytes()
    memory_slots = preferred_minimum if available is None else max(
        minimum,
        int(max(0, available - 2 * 1024**3) // (1536 * 1024**2)),
    )
    return max(minimum, min(maximum, cpu_slots, memory_slots))


def threads_per_worker(slot_count: int) -> int:
    cpu_count = os.cpu_count() or 4
    return max(1, min(2, (max(2, cpu_count - 2) // max(1, slot_count))))


def worker_command() -> list[str]:
    if bool(getattr(sys, "frozen", False)):
        command = [sys.executable, "--compute-worker"]
        resource_root = os.environ.get("OPTIONHELPER_RESOURCE_ROOT", "").strip()
        if resource_root:
            command.extend(("--resource-dir", resource_root))
        return command
    return [sys.executable, "-m", f"{__package__}.compute_worker"]


def probe_compute_worker() -> dict[str, Any]:
    diagnostics_path = os.environ.get("OPTIONHELPER_COMPUTE_PROBE_DIAGNOSTICS", "").strip()
    diagnostics = _ComputeDiagnostics(Path(diagnostics_path) if diagnostics_path else None)
    worker = _ComputeWorker(worker_command(), 1, diagnostics=diagnostics)
    try:
        return {"status": "ready", "protocol": PROTOCOL}
    finally:
        worker.close()


def encode_snapshot(reference: Mapping[str, Any], content: bytes) -> dict[str, Any]:
    expected = str(reference.get("content_hash", ""))
    actual = hashlib.sha256(content).hexdigest()
    if expected != actual:
        raise ValidationError("冻结DataAssetRef与实际数据哈希不一致")
    return {"reference": dict(reference), "content": base64.b64encode(content).decode("ascii"), "sha256": actual}


def decode_draft_files(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise ValidationError("计算结果的ModuleRunDraft无效")
    decoded: dict[str, Any] = {}
    total = 0
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, Mapping):
            raise ValidationError("ModuleRunDraft文件清单无效")
        kind = item.get("kind")
        if kind == "bytes":
            try:
                content: Any = base64.b64decode(str(item.get("value", "")), validate=True)
            except ValueError as error:
                raise ValidationError("ModuleRunDraft二进制文件无效") from error
            payload = content
        elif kind == "text":
            payload = str(item.get("value", ""))
            content = payload.encode("utf-8")
        elif kind == "json":
            payload = item.get("value")
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        else:
            raise ValidationError("ModuleRunDraft文件类型无效")
        total += len(content)
        if total > MAX_FRAME_BYTES:
            raise ValidationError("ModuleRunDraft超过App内部限制")
        if hashlib.sha256(content).hexdigest() != item.get("sha256"):
            raise ValidationError("ModuleRunDraft文件哈希不匹配")
        decoded[name] = payload
    return decoded


def _worker_environment(thread_limit: int, private_home: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not any(token in key.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "REFRESH"))
    }
    value = str(max(1, thread_limit))
    for key in ("NUMBA_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        environment[key] = value
    python_paths = []
    for item in sys.path:
        try:
            path = Path(item or os.getcwd()).resolve()
        except (OSError, RuntimeError):
            continue
        if path.is_dir() and str(path) not in python_paths:
            python_paths.append(str(path))
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["HOME"] = str(private_home)
    environment["USERPROFILE"] = str(private_home)
    environment["TMPDIR"] = str(private_home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _available_memory_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.virtual_memory().available)
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return pages * page_size
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _write_frame(stream: Any, value: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValidationError("冻结计算请求超过App内部限制")
    stream.write(len(payload).to_bytes(8, "big") + payload)
    stream.flush()


def _read_frame(stream: Any) -> dict[str, Any]:
    header = _read_exact(stream, 8)
    size = int.from_bytes(header, "big")
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ValueError("invalid compute worker frame size")
    value = json.loads(_read_exact(stream, size))
    if not isinstance(value, dict):
        raise ValueError("compute worker frame must be an object")
    return value


def _read_exact(stream: Any, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError("compute worker pipe closed")
        chunks.extend(block)
    return bytes(chunks)


def _compute_cancelled() -> UserActionError:
    return UserActionError(
        "compute_cancelled",
        "本次计算已取消。",
        stage="compute",
        next_step="如需结果，请重新运行。",
    )


def _wait_cancelled(delay: float, cancelled: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + max(0.0, delay)
    while time.monotonic() < deadline:
        if cancelled():
            return True
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return cancelled()


_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+")
_SECRET_PATTERN = re.compile(r"(?i)(token|secret|password|api[_-]?key|refresh)[=:]\s*[^\s,;]+")


def _sanitize_diagnostic(value: Any) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).replace("\r", " ").replace("\n", " ")[:2000]
    text = _PATH_PATTERN.sub("<path>", text)
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)


__all__ = (
    "COMPUTE_MODULES",
    "ComputeRecoveryRequired",
    "ComputeProcessSupervisor",
    "PreparedComputeExecution",
    "adaptive_compute_slots",
    "decode_draft_files",
    "encode_snapshot",
    "probe_compute_worker",
    "threads_per_worker",
)
