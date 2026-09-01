"""Private length-prefixed compute worker entrypoint."""

from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Lock, Thread
import time
import traceback
from typing import Any, Mapping
from uuid import uuid4

from runtime.contracts.input_adapter import ContractResolutionError
from runtime.errors import ErrorCode, error_info

from .compute_process import MAX_FRAME_BYTES, PROTOCOL, _canonical_hash, _read_frame, _write_frame
from ..errors import UserActionError


class _DeterministicCapabilityRejection(Exception):
    """A verified Capability rejected the frozen business contract."""


class SnapshotDataStore:
    def __init__(self, snapshots: Any, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._content: dict[tuple[str, str], bytes] = {}
        if not isinstance(snapshots, list):
            raise ValueError("data_snapshots must be a list")
        for item in snapshots:
            if not isinstance(item, Mapping) or not isinstance(item.get("reference"), Mapping):
                raise ValueError("data snapshot is invalid")
            reference = dict(item["reference"])
            content = base64.b64decode(str(item.get("content", "")), validate=True)
            digest = hashlib.sha256(content).hexdigest()
            if digest != item.get("sha256") or digest != reference.get("content_hash"):
                raise ValueError("data snapshot hash mismatch")
            if reference.get("tenant_id") != tenant_id:
                raise ValueError("data snapshot tenant mismatch")
            self._content[(str(reference.get("data_asset_id")), digest)] = content

    def read_bytes(self, ref: Any, *, tenant_id: str) -> bytes:
        if tenant_id != self._tenant_id or getattr(ref, "tenant_id", None) != tenant_id:
            raise PermissionError("snapshot tenant mismatch")
        key = (str(getattr(ref, "data_asset_id", "")), str(getattr(ref, "content_hash", "")))
        if key not in self._content:
            raise PermissionError("snapshot is not part of the prepared execution")
        return self._content[key]


class DraftResultStore:
    def __init__(self, root: Path) -> None:
        from runtime.adapters.local_store import LocalResultStore

        self._delegate = LocalResultStore(root)
        self._draft: dict[str, Any] | None = None

    def commit_module_run(self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: Mapping[str, Any]) -> Any:
        if self._draft is not None:
            raise ValueError("worker can create only one ModuleRunDraft")
        self._draft = {
            "module": module,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "files": dict(files),
        }
        return self._delegate.commit_module_run(
            module=module, tenant_id=tenant_id, task_id=task_id, run_id=run_id, files=files,
        )

    def predict_module_run_ref(
        self,
        *,
        module: str,
        tenant_id: str,
        task_id: str,
        run_id: str,
        files: Mapping[str, Any],
    ) -> Any:
        return self._delegate.predict_module_run_ref(
            module=module,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            files=files,
        )

    def verify_module_run(self, ref: Any, *, tenant_id: str) -> None:
        self._delegate.verify_module_run(ref, tenant_id=tenant_id)

    def read_module_run_file(self, ref: Any, name: str, *, tenant_id: str) -> bytes:
        return self._delegate.read_module_run_file(ref, name, tenant_id=tenant_id)

    def read_module_run_bundle(self, ref: Any, *, tenant_id: str) -> dict[str, bytes]:
        return self._delegate.read_module_run_bundle(ref, tenant_id=tenant_id)

    def export(self) -> dict[str, Any]:
        if self._draft is None:
            raise ValueError("worker did not create a ModuleRunDraft")
        return {
            key: value for key, value in self._draft.items() if key != "files"
        } | {"files": {name: _encode_file(value) for name, value in self._draft["files"].items()}}


def main() -> int:
    _lower_process_priority()
    writer_lock = Lock()
    _send(sys.stdout.buffer, {"protocol": PROTOCOL, "type": "ready"}, writer_lock)
    while True:
        try:
            request = _read_frame(sys.stdin.buffer)
        except EOFError:
            return 0
        except BaseException:
            return 2
        if request.get("protocol") != PROTOCOL or request.get("type") != "execute":
            return 2
        execution = request.get("execution")
        execution_id = str(execution.get("execution_id", "")) if isinstance(execution, Mapping) else ""
        attempt_id = str(request.get("attempt_id", ""))
        stage = {"value": "loading_capability"}
        stopped = Event()
        heartbeat = Thread(
            target=_heartbeat_loop,
            args=(sys.stdout.buffer, writer_lock, stopped, execution_id, attempt_id, stage),
            name="optionhelper-compute-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            response = _execute(dict(execution or {}), stage)
            envelope = {
                "protocol": PROTOCOL,
                "type": "result",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "ok": True,
                **response,
            }
        except BaseException as error:
            failure = _safe_failure(error)
            if failure["failure_class"] != "domain_rejected":
                traceback.print_exception(error, file=sys.stderr)
            envelope = {
                "protocol": PROTOCOL,
                "type": "result",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "ok": False,
                "failure": failure,
            }
        finally:
            stopped.set()
            heartbeat.join(timeout=1.0)
        try:
            _send(sys.stdout.buffer, envelope, writer_lock)
        except BaseException:
            return 3


def _execute(execution: dict[str, Any], stage: dict[str, str]) -> dict[str, Any]:
    module = str(execution.get("module", ""))
    task_id = str(execution.get("task_id", ""))
    tenant_id = str(execution.get("tenant_id", ""))
    if module not in {"payoffer", "pricer", "backtester"} or not task_id or not tenant_id:
        raise ValueError("prepared compute scope is invalid")
    content_hashes = execution.get("content_hashes")
    if not isinstance(content_hashes, Mapping) or _canonical_hash(dict(content_hashes)) != execution.get("capability_hash"):
        raise ValueError("capability hash list is invalid")
    frozen = {
        "task_id": task_id,
        "module": module,
        "tenant_id": tenant_id,
        "request": execution.get("request"),
        "caller_context": execution.get("caller_context"),
        "host_context": execution.get("host_context"),
        "data_snapshots": execution.get("data_snapshots"),
        "capability_hash": execution.get("capability_hash"),
    }
    if _canonical_hash(frozen) != execution.get("execution_fingerprint"):
        raise ValueError("prepared compute execution fingerprint mismatch")
    scripts_root = str(Path(str(execution.get("scripts_root", ""))).resolve())
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
    from runtime.capability_import import load_verified_source_module, verified_capability_modules
    from runtime.protocol.models import CallerContext
    from runtime.protocol.module_host import ModuleHostContext, parse_capability_token
    from ..capability_service import capability_import_scope, load_verified_capability_service

    caller_payload = dict(execution["caller_context"])
    caller_payload["capabilities"] = tuple(caller_payload.get("capabilities", ()))
    caller = CallerContext(**caller_payload)
    token = parse_capability_token(execution["host_context"].get("capability_token"), now=0)
    context = ModuleHostContext.from_payload(execution["host_context"], now=max(0, token.expires_at - 1))
    data_store = SnapshotDataStore(execution.get("data_snapshots"), tenant_id)
    with tempfile.TemporaryDirectory(prefix="optionhelper-compute-draft-") as directory:
        private_root = Path(directory)
        worker_runtime = private_root / "runtime"
        (worker_runtime / "data").mkdir(parents=True)
        (worker_runtime / "result").mkdir()
        stage["value"] = "loading_capability"
        with capability_import_scope(scripts_root, runtime_root=worker_runtime):
            draft_store = DraftResultStore(private_root / "module-runs")
            with verified_capability_modules(scripts_root, content_hashes):
                entry = load_verified_source_module(
                    "option_helper_embedded_tool_entry", scripts_root, "tool_entry.py", content_hashes,
                )
                load_verified_capability_service(module, scripts_root)
                authorize = getattr(entry, "_authorize_verified_app_call")
                stage["value"] = "executing"
                try:
                    result = entry.call_tool(
                        module,
                        dict(execution["request"]),
                        authorization=authorize(caller, context),
                        result_store=draft_store,
                        data_store=data_store if module in {"pricer", "backtester"} else None,
                    )
                except BaseException as error:
                    if _is_payoff_domain_compile_error(error):
                        raise _DeterministicCapabilityRejection() from error
                    raise
        if not isinstance(result, Mapping):
            raise ValueError("compute capability result must be an object")
        stage["value"] = "serializing_result"
        return {
            "result": dict(result),
            "draft": draft_store.export(),
            "capability_hash": execution["capability_hash"],
            "execution_fingerprint": execution["execution_fingerprint"],
        }


def _encode_file(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        content = value
        return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii"), "sha256": hashlib.sha256(content).hexdigest()}
    if isinstance(value, str):
        content = value.encode("utf-8")
        return {"kind": "text", "value": value, "sha256": hashlib.sha256(content).hexdigest()}
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {"kind": "json", "value": value, "sha256": hashlib.sha256(content).hexdigest()}


def _safe_failure(error: BaseException) -> dict[str, Any]:
    if isinstance(error, UserActionError):
        return {
            "failure_class": "domain_rejected",
            "failure_code": error.code,
            "stage": error.stage,
            "message": error.message,
            "next_step": error.next_step,
            "retryable": False,
            "error_type": type(error).__name__,
        }
    if isinstance(error, ContractResolutionError):
        projection: Mapping[str, Any] = {}
        to_protocol = getattr(error, "to_protocol_dict", None)
        if callable(to_protocol):
            candidate = to_protocol()
            if isinstance(candidate, Mapping):
                projection = candidate
        return {
            "failure_class": "domain_rejected",
            "failure_code": str(projection.get("failure_code") or "compute_input_invalid"),
            "stage": str(projection.get("stage") or "input"),
            "message": str(projection.get("message") or error),
            "next_step": str(
                projection.get("next_step")
                or "请检查当前产品、参数、行情和交易日历后重新运行。"
            ),
            "retryable": False,
            "error_type": type(error).__name__,
        }
    if isinstance(error, _DeterministicCapabilityRejection):
        return {
            "failure_class": "domain_rejected",
            "failure_code": "compute_domain_rejected",
            "stage": "compute",
            "message": "当前合同无法完成确定性计算。",
            "next_step": "请检查合同条款、冻结日期和交易日历后重新运行。",
            "retryable": False,
            "error_type": type(error).__name__,
        }
    if isinstance(error, Exception):
        projection = error_info(error, stage="compute")
        if projection.error_code is ErrorCode.CONTRACT_INVALID:
            return {
                "failure_class": "domain_rejected",
                "failure_code": projection.error_code.value,
                "stage": projection.stage,
                "message": projection.message,
                "next_step": "请检查合同条款、冻结日期和交易日历后重新运行。",
                "retryable": False,
                "error_type": type(error).__name__,
            }
    return {
        "failure_class": "engine_recovering",
        "failure_code": "engine_recovering",
        "stage": "compute",
        "message": "计算服务内部运行未完成，正在自动恢复。",
        "next_step": "无需重复提交，OptionHelper将继续恢复本次计算。",
        "retryable": True,
        "error_type": type(error).__name__,
        "diagnostic_id": f"diag_{uuid4().hex}",
    }


def _is_payoff_domain_compile_error(error: BaseException) -> bool:
    """Recognize only Payoffer's formal deterministic compiler boundary."""

    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        error_type = type(current)
        if (
            error_type.__name__ == "DomainCompileError"
            and error_type.__module__ == "modules.payoffer.impl.path_sampler"
        ):
            return True
        current = current.__cause__
    return False


def _heartbeat_loop(
    stream: Any,
    writer_lock: Lock,
    stopped: Event,
    execution_id: str,
    attempt_id: str,
    stage: dict[str, str],
) -> None:
    while not stopped.wait(2.0):
        try:
            _send(stream, {
                "protocol": PROTOCOL,
                "type": "heartbeat",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "stage": stage.get("value", "executing"),
                "monotonic": time.monotonic(),
            }, writer_lock)
        except BaseException:
            return


def _send(stream: Any, value: Mapping[str, Any], writer_lock: Lock) -> None:
    with writer_lock:
        _write_frame(stream, value)


def _lower_process_priority() -> None:
    if os.name == "nt":
        try:
            import ctypes

            below_normal_priority_class = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                below_normal_priority_class,
            )
        except (AttributeError, OSError):
            pass
        return
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
