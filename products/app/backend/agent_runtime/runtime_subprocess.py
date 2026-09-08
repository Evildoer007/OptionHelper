"""Subprocess transport for the OptionHelper Agent runtime.

The transport owns only the local process boundary. Model selection, secrets,
business authorization and module execution remain in the host callbacks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
import time
from typing import Any, Callable
from uuid import uuid4

from .runtime_model_proxy import ModelCompletion, ModelStreamDelta, RuntimeModelProxy
from .runtime_protocol import MAX_WORKFLOW_TOOLS, ModelRouteRef, RuntimeEvent


class RuntimeSubprocessError(RuntimeError):
    """The child process or its protocol failed before a valid response."""


class RuntimeSubprocessTimeout(TimeoutError):
    """A JSON-RPC request did not complete before its deadline."""


@dataclass
class _PendingRequest:
    event: Event
    result: Mapping[str, Any] | Any | None = None
    error: BaseException | None = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _safe_value(value: object, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    forbidden = {
        "secret", "secrets", "password", "credential", "credentials", "apikey",
        "api_key", "access_token", "authorization", "private_key", "system_prompt",
        "hidden_reasoning", "reasoning_content",
    }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in forbidden or any(marker in normalized for marker in ("secret", "password", "credential", "api_key")):
                continue
            result[str(key)] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value[:256]]
    if isinstance(value, str):
        return value[:64_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1_000]


def _pick(value: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return default


def _terminal_status(value: Mapping[str, Any]) -> str:
    """Normalize an explicit terminal status or reason without optimistic defaults."""

    aliases = {
        "completed": "completed", "complete": "completed", "success": "completed",
        "succeeded": "completed", "stop": "completed", "stopped": "completed", "done": "completed",
        "cancelled": "cancelled", "canceled": "cancelled", "interrupted": "cancelled",
        "abort": "cancelled", "aborted": "cancelled", "user_cancelled": "cancelled",
        "user-cancelled": "cancelled",
        "failed": "failed", "failure": "failed", "error": "failed", "exception": "failed",
        "timeout": "failed", "timed_out": "failed", "max_steps": "failed", "budget_exceeded": "failed",
    }
    for field in ("status", "reason"):
        raw = str(value.get(field, "") or "").strip().casefold().replace(" ", "_")
        if raw in aliases:
            return aliases[raw]
    return "failed"


def _identifier(value: object, fallback: str) -> str:
    candidate = str(value or "").strip()
    if candidate and all(char.isalnum() or char in "_.:-" for char in candidate):
        return candidate[:160]
    return fallback


def _reasoning_text(value: object) -> str:
    text = str(value or "")[:64_000]
    import re

    text = re.sub(
        r"(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password|authorization|private[_ -]?key|base[_ -]?url)\s*[:=]\s*[^\s,;]+",
        r"\1: [已过滤]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [已过滤]", text, flags=re.IGNORECASE)


class RuntimeSubprocessTransport:
    """Thread-safe line-delimited JSON-RPC transport with Host callbacks."""

    def __init__(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        runtime_path: str | os.PathLike[str] | None = None,
        node_binary: str | None = None,
        session_root: str | os.PathLike[str] | None = None,
        request_timeout: float = 30.0,
        host_callback_timeout: float = 60.0,
        stderr_limit: int = 8_192,
        line_limit: int = 1_000_000,
        model_proxy: RuntimeModelProxy | None = None,
        tool_bridge: object | None = None,
        identity: object | None = None,
        model_handler: Callable[..., object] | None = None,
        tool_handler: Callable[..., object] | None = None,
        route_resolver: Callable[[Mapping[str, Any]], ModelRouteRef] | None = None,
        route_refs: Mapping[str, ModelRouteRef] | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.scope = dict(scope or {})
        self.task_id = str(self.scope.get("task_id") or "").strip()
        if not self.task_id:
            raise ValueError("RuntimeSubprocessTransport需要task_id")
        self.root_session_id = str(self.scope.get("root_session_id") or f"agent-root-{self.task_id}").strip()
        runtime_root = Path(__file__).resolve().parents[2] / "runtime"
        default_runtime = runtime_root / "optionhelper_agent_runtime" / "dist" / "runtime.cjs"
        runtime_override = os.environ.get("OPTIONHELPER_AGENT_RUNTIME_PATH", "").strip()
        self._runtime_path_from_env = runtime_path is None and bool(runtime_override)
        runtime_value = runtime_path or (runtime_override or default_runtime)
        self.runtime_path = Path(runtime_value).expanduser().resolve()
        # Source-mode is intentionally limited to development source entries.
        # A staged artifact is always launched directly and never through Node.
        self._source_runtime = not self._runtime_path_from_env and self.runtime_path.suffix.casefold() in {".js", ".cjs", ".mjs", ".py"}
        self.node_binary = (
            str(node_binary or os.environ.get("OPTIONHELPER_NODE_BINARY") or shutil.which("node") or "node")
            if self._source_runtime else ""
        )
        self.session_root = Path(session_root or self.scope.get("session_root") or (Path.cwd() / ".optionhelper-agent-sessions")).expanduser().resolve()
        self.request_timeout = max(0.05, float(request_timeout))
        self.host_callback_timeout = max(0.05, float(host_callback_timeout))
        self.stderr_limit = max(256, int(stderr_limit))
        self.line_limit = max(1_024, int(line_limit))
        self.model_proxy = model_proxy
        self.tool_bridge = tool_bridge
        self.identity = identity
        self.model_handler = model_handler
        self.tool_handler = tool_handler
        self.route_resolver = route_resolver
        self.route_refs = dict(route_refs or {})
        self._popen_factory = popen_factory or subprocess.Popen

        self._process: subprocess.Popen[bytes] | None = None
        self._stdout_thread: Thread | None = None
        self._stderr_thread: Thread | None = None
        self._pending: dict[str, _PendingRequest] = {}
        self._pending_lock = RLock()
        self._write_lock = Lock()
        self._close_lock = Lock()
        self._event_handler: Callable[[RuntimeEvent], object] | None = None
        self._next_request = 0
        self._next_event_seq = 0
        self._started = False
        self._closed = False
        self._terminal_error: RuntimeSubprocessError | None = None
        self._stderr = ""
        self._workflow_id: str | None = None
        self._workflow: Mapping[str, Any] | None = None
        self._session_to_run: dict[str, str] = {}
        self._run_to_session: dict[str, str] = {}
        self._run_roles: dict[str, str] = {}
        self._run_connections: dict[str, object] = {}
        self._run_tool_counts: dict[str, int] = {}
        self._seen_source_events: set[str] = set()
        self._cancelled_model_requests: set[str] = set()

    @property
    def argv(self) -> tuple[str, ...]:
        if self._source_runtime:
            return (self.node_binary, str(self.runtime_path))
        return (str(self.runtime_path),)

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def stderr_text(self) -> str:
        return self._stderr

    @property
    def closed(self) -> bool:
        return self._closed

    def set_event_handler(self, handler: Callable[[RuntimeEvent], object] | None) -> None:
        self._event_handler = handler

    def initialize(self) -> Mapping[str, Any]:
        self._ensure_process()
        return self._rpc(
            "runtime.initialize",
            {
                "sessionRoot": str(self.session_root),
                "rootSessionId": self.root_session_id,
            },
        )

    def bind_workflow(self, workflow_id: str, workflow: Mapping[str, Any] | None = None) -> None:
        """Bind direct child runs to one Host-owned workflow identity."""

        value = str(workflow_id or "").strip()
        if not value or len(value) > 160 or any(not (char.isalnum() or char in "_.:-") for char in value):
            raise ValueError("workflow_id格式无效")
        if self._workflow_id is not None and self._workflow_id != value:
            raise RuntimeSubprocessError("运行时已经绑定另一Workflow")
        self._workflow_id = value
        if workflow is not None:
            if not isinstance(workflow, Mapping):
                raise ValueError("workflow必须是对象")
            self._workflow = dict(_safe_value(workflow))

    def start(self, workflow: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(workflow, Mapping):
            raise ValueError("workflow必须是对象")
        self._workflow = dict(_safe_value(workflow))
        self._workflow_id = str(_pick(workflow, "workflow_id", "workflowId", default="") or "").strip() or None
        self.initialize()
        return self._rpc("workflow.start", {"wait": False, "spec": dict(workflow)})

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        normalized = str(method or "").strip().replace("/", ".")
        if not normalized:
            raise ValueError("RPC method不能为空")
        result = self._rpc(normalized, params or {}, request_id=request_id, timeout=timeout)
        if normalized == "agent.activate":
            run_id = str(_pick(result, "runId", "run_id", default="") or "").strip()
            session_id = str(_pick(result, "sessionId", "session_id", default="") or "").strip()
            role_id = str(_pick(result, "roleId", "role_id", default="MainAgent") or "MainAgent")
            if run_id and session_id:
                self._run_to_session[run_id] = session_id
                self._session_to_run[session_id] = run_id
                self._run_roles[run_id] = role_id
        return result

    def cancel(self, reason: str = "cancelled") -> None:
        process = self._process
        if process is None or process.poll() is not None or not self._workflow_id:
            return
        try:
            self._rpc(
                "workflow.cancel",
                {"workflowId": self._workflow_id, "reason": str(reason)[:160]},
                timeout=min(self.request_timeout, 5.0),
            )
        except Exception as error:
            self._set_terminal(RuntimeSubprocessError(f"runtime cancel failed: {error}"))
            self._terminate_process()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                process = self._process
                if process is not None and process.poll() is None:
                    try:
                        self._rpc("runtime.shutdown", {}, timeout=min(self.request_timeout, 3.0))
                    except Exception:
                        pass
            finally:
                self._closed = True
                if self.tool_bridge is not None:
                    closer = getattr(self.tool_bridge, "close_connection", None)
                    if callable(closer):
                        for connection in tuple(self._run_connections.values()):
                            try:
                                closer(connection)
                            except Exception:
                                pass
                self._terminate_process()
                self._fail_pending(RuntimeSubprocessError("runtime transport closed"))

    def _ensure_process(self) -> None:
        with self._pending_lock:
            if self._closed:
                raise RuntimeSubprocessError("runtime transport closed")
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._process is not None:
                if self._process.poll() is not None:
                    raise RuntimeSubprocessError("runtime process is not running")
                return
            if not self.runtime_path.exists():
                raise RuntimeSubprocessError(f"runtime entrypoint not found: {self.runtime_path}")
            mode = os.environ.get("OPTIONHELPER_AGENT_RUNTIME_MODE", "disabled").strip().lower()
            if mode in {"active", "shadow"} and bool(getattr(sys, "frozen", False)) and not self._runtime_path_from_env:
                raise RuntimeSubprocessError("成品Agent运行时缺少Resources内置可执行文件")
            if not self._source_runtime and os.name != "nt" and not (self.runtime_path.stat().st_mode & 0o111):
                raise RuntimeSubprocessError(f"Agent运行时不可执行：{self.runtime_path}")
            argv = list(self.argv)
            environment = os.environ.copy()
            environment["OPTIONHELPER_AGENT_SESSION_ROOT"] = str(self.session_root)
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": str(self.runtime_path.parent),
                "env": environment,
                "shell": False,
                "close_fds": True,
                "bufsize": 0,
            }
            if os.name != "nt":
                kwargs["start_new_session"] = True
            try:
                process = self._popen_factory(argv, **kwargs)
            except Exception as error:
                raise RuntimeSubprocessError(f"runtime process start failed: {error}") from error
            self._process = process
            self._stdout_thread = Thread(target=self._read_stdout, name="optionhelper-runtime-stdout", daemon=True)
            self._stderr_thread = Thread(target=self._read_stderr, name="optionhelper-runtime-stderr", daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
            self._started = True

    def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        self._ensure_process()
        identifier = str(request_id or self._new_request_id())
        pending = _PendingRequest(Event())
        with self._pending_lock:
            if identifier in self._pending:
                raise RuntimeSubprocessError(f"duplicate request id: {identifier}")
            self._pending[identifier] = pending
        try:
            self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": _safe_value(params)})
        except Exception:
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise
        wait_for = self.request_timeout if timeout is None else (None if timeout == 0 else max(0.05, float(timeout)))
        if not pending.event.wait(wait_for):
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise RuntimeSubprocessTimeout(f"timeout waiting for {method}")
        if pending.error is not None:
            raise pending.error
        if pending.result is None:
            return {}
        if not isinstance(pending.result, Mapping):
            raise RuntimeSubprocessError(f"RPC {method} returned a non-object")
        return dict(_safe_value(pending.result))

    def _new_request_id(self) -> str:
        with self._pending_lock:
            self._next_request += 1
            return f"rpc_{self._next_request}_{uuid4().hex[:8]}"

    def _write(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeSubprocessError("runtime process is not writable")
        encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
            except Exception as error:
                self._set_terminal(RuntimeSubprocessError(f"runtime stdin failed: {error}"))
                raise self._terminal_error or error

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw_line in iter(process.stdout.readline, b""):
                if len(raw_line) > self.line_limit:
                    self._set_terminal(RuntimeSubprocessError("runtime stdout line exceeds limit"))
                    break
                line = raw_line.decode("utf-8", errors="strict").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._set_terminal(RuntimeSubprocessError(f"runtime emitted invalid JSON: {error}"))
                    break
                if not isinstance(message, Mapping):
                    self._set_terminal(RuntimeSubprocessError("runtime emitted a non-object JSON message"))
                    break
                self._dispatch_message(message)
        except Exception as error:
            self._set_terminal(RuntimeSubprocessError(f"runtime stdout reader failed: {error}"))
        finally:
            if self._closed:
                return
            process_error = self._terminal_error or RuntimeSubprocessError("runtime stdout reached EOF")
            self._set_terminal(process_error)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for raw_line in iter(process.stderr.readline, b""):
                text = raw_line.decode("utf-8", errors="replace")
                self._stderr = (self._stderr + text)[-self.stderr_limit:]
        except Exception:
            return

    def _dispatch_message(self, message: Mapping[str, Any]) -> None:
        identifier = message.get("id")
        method = message.get("method")
        if identifier is not None and not isinstance(method, str):
            key = str(identifier)
            with self._pending_lock:
                pending = self._pending.pop(key, None)
            if pending is None:
                return
            if isinstance(message.get("error"), Mapping):
                error = message["error"]
                pending.error = RuntimeSubprocessError(str(error.get("message") or "runtime RPC failed"))
            else:
                pending.result = message.get("result")
            pending.event.set()
            return
        if method == "runtime.event":
            try:
                event = self._normalize_event(message.get("params"))
            except Exception as error:
                self._set_terminal(RuntimeSubprocessError(f"runtime event rejected: {error}"))
                return
            if event is not None and self._event_handler is not None:
                try:
                    self._event_handler(event)
                except Exception as error:
                    self._set_terminal(RuntimeSubprocessError(f"runtime event sink failed: {error}"))
            return
        if method == "host.model.cancel":
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            request_id = str(_pick(params, "requestId", "request_id", default="") or "").strip()
            if request_id:
                with self._pending_lock:
                    self._cancelled_model_requests.add(request_id)
            return
        if isinstance(method, str) and method in {"host.model.stream", "host.model.complete", "host.tool.call"}:
            Thread(
                target=self._handle_host_request,
                args=(str(identifier), method, message.get("params") if isinstance(message.get("params"), Mapping) else {}),
                name="optionhelper-runtime-host-callback",
                daemon=True,
            ).start()

    def _handle_host_request(self, identifier: str, method: str, params: Mapping[str, Any]) -> None:
        try:
            if method == "host.model.stream":
                result = self._handle_model_stream_request(identifier, params)
            elif method == "host.model.complete":
                result = self._handle_model_request(params, complete=method.endswith("complete"))
            else:
                result = self._handle_tool_request(params)
            self._write({"jsonrpc": "2.0", "id": identifier, "result": _safe_value(result)})
        except Exception as error:
            try:
                self._write({
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": type(error).__name__, "message": str(error)[:500]},
                })
            except Exception:
                return

    def _bound_request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(_safe_value(params))
        for key in (
            "task_id", "taskId", "tenant_id", "tenantId", "principal_id", "principalId",
            "workflow_id", "workflowId",
        ):
            result.pop(key, None)
        result["task_id"] = self.task_id
        result["workflow_id"] = self._workflow_id or "workflow-unknown"
        run_id = str(_pick(params, "agentRunId", "agent_run_id", default="") or "").strip()
        if run_id:
            result["agent_run_id"] = run_id
            session_id = str(_pick(params, "sessionId", "session_id", default="") or "").strip()
            if session_id:
                self._run_to_session[run_id] = session_id
                self._session_to_run[session_id] = run_id
        return result

    def _model_raw(self, params: Mapping[str, Any]) -> object:
        bound = self._bound_request(params)
        if self.model_handler is not None:
            return self._invoke_callback(self.model_handler, bound)
        if self.model_proxy is None:
            raise RuntimeSubprocessError("Host未配置模型回调")
        route = self._resolve_route(params)
        prompt = str(_pick(params, "prompt", "message", default="") or "")
        return self.model_proxy.stream(route, [{"role": "user", "content": prompt}])

    def _handle_model_request(self, params: Mapping[str, Any], *, complete: bool) -> Mapping[str, Any]:
        return self._model_result(self._model_raw(params), complete=complete)

    def _handle_model_stream_request(self, identifier: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            for chunk in self._model_chunks(self._model_raw(params), complete=False):
                if self._is_model_request_cancelled(identifier):
                    raise RuntimeSubprocessError("模型流已取消")
                self._write({
                    "jsonrpc": "2.0",
                    "method": "host.model.chunk",
                    "params": {"requestId": identifier, "chunk": _safe_value(chunk)},
                })
            return {"streamed": True}
        finally:
            with self._pending_lock:
                self._cancelled_model_requests.discard(identifier)

    def _is_model_request_cancelled(self, identifier: str) -> bool:
        with self._pending_lock:
            return identifier in self._cancelled_model_requests

    def _model_result(self, raw: object, *, complete: bool) -> Mapping[str, Any]:
        return {"chunks": list(self._model_chunks(raw, complete=complete))}

    def _model_chunks(self, raw: object, *, complete: bool) -> Iterable[dict[str, Any]]:
        if isinstance(raw, ModelCompletion):
            yield {"type": "text-delta", "text": raw.text}
            yield {"type": "finish", "reason": raw.finish_reason or "stop", "usage": dict(raw.usage or {})}
            return
        if isinstance(raw, str):
            yield {"type": "text-delta", "text": raw}
            yield {"type": "finish", "reason": "stop"}
            return
        if isinstance(raw, Mapping):
            if "chunks" in raw and isinstance(raw["chunks"], Iterable) and not isinstance(raw["chunks"], (str, bytes)):
                source: object = raw["chunks"]
            elif "text" in raw:
                yield {"type": "text-delta", "text": str(raw["text"])}
                yield {
                    "type": "finish",
                    "reason": str(raw.get("finish_reason") or "stop"),
                    "usage": raw.get("usage") or {},
                }
                return
            else:
                raise RuntimeSubprocessError("模型回调必须返回chunks或text")
        else:
            source = raw
        try:
            iterator = iter(source)  # type: ignore[arg-type]
        except TypeError as error:
            raise RuntimeSubprocessError("模型回调返回值不可迭代") from error
        saw_finish = False
        for item in iterator:
            normalized = self._model_chunk(item)
            saw_finish = saw_finish or normalized.get("type") == "finish"
            yield normalized
        if complete and not saw_finish:
            yield {"type": "finish", "reason": "stop"}

    def _model_chunk(self, item: object) -> dict[str, Any]:
        if isinstance(item, ModelStreamDelta):
            kind = item.type.replace("_", "-")
            payload: dict[str, Any] = {"type": kind}
            if kind == "text-delta":
                payload["text"] = item.delta
            elif kind == "reasoning-delta":
                payload["text"] = _reasoning_text(item.delta)
                payload["available"] = bool(payload["text"]) or item.metadata.get("available") is True
                payload["chars"] = len(payload["text"])
            elif kind == "tool-call-delta":
                payload.update(_safe_value(item.metadata))
            elif kind == "usage":
                payload["usage"] = dict(item.usage or {})
            elif kind == "finish":
                payload["reason"] = item.finish_reason or "stop"
                if item.usage:
                    payload["usage"] = dict(item.usage)
            return payload
        if not isinstance(item, Mapping):
            raise RuntimeSubprocessError("模型流事件必须是对象")
        kind = str(_pick(item, "type", "event", default="text-delta")).replace("_", "-")
        payload = dict(_safe_value(item))
        payload["type"] = kind
        if kind == "reasoning-delta":
            text = _reasoning_text(_pick(item, "text", "delta", "content", default=""))
            payload["text"] = text
            payload.pop("reasoning_content", None)
            payload["chars"] = len(text)
            payload["available"] = bool(text) or item.get("available") is True
        return payload

    def _resolve_route(self, params: Mapping[str, Any]) -> ModelRouteRef:
        raw = _pick(params, "modelRouteRef", "model_route_ref", default={})
        route_id = str(_pick(raw, "route_id", "routeId", default="") or "") if isinstance(raw, Mapping) else ""
        route = self.route_refs.get(route_id) if route_id else None
        if route is None and self.route_resolver is not None:
            route = self.route_resolver(dict(raw) if isinstance(raw, Mapping) else {})
        if not isinstance(route, ModelRouteRef) or not route.trusted:
            raise RuntimeSubprocessError("ModelRouteRef必须由Host签发")
        return route

    def _handle_tool_request(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        bound = self._bound_request(params)
        run_id = str(bound.get("agent_run_id") or "").strip()
        if run_id:
            role_id = self._run_roles.get(run_id, str(params.get("roleId") or params.get("role_id") or "agent"))
            current = self._run_tool_counts.get(run_id, 0)
            if self._tool_limit(role_id) > 0 and current >= self._tool_limit(role_id):
                raise RuntimeSubprocessError("工具调用次数达到显式配置的上限")
            self._run_tool_counts[run_id] = current + 1
        if self.tool_handler is not None:
            return _safe_value(self._invoke_callback(self.tool_handler, bound))
        if self.tool_bridge is None:
            raise RuntimeSubprocessError("Host未配置工具回调")
        tool_name = str(_pick(params, "toolName", "tool_name", default="") or "").strip()
        call_id = str(_pick(params, "toolCallId", "tool_call_id", default="") or "").strip()
        if not run_id or not tool_name or not call_id:
            raise RuntimeSubprocessError("工具回调缺少运行身份或调用标识")
        connection = self._run_connections.get(run_id)
        if connection is None:
            if self.identity is None:
                raise RuntimeSubprocessError("工具回调缺少已认证Host身份")
            role_id = self._run_roles.get(run_id, str(params.get("roleId") or params.get("role_id") or "agent"))
            allowed = self._allowed_tools(role_id)
            connection = self.tool_bridge.open_connection(
                self.identity,
                task_id=self.task_id,
                workflow_id=self._workflow_id or "workflow-unknown",
                agent_run_id=run_id,
                role_id=role_id,
                allowed_tools=allowed,
                max_tools=self._tool_limit(role_id),
            )
            self._run_connections[run_id] = connection
        arguments = _pick(params, "arguments", default={})
        result = self.tool_bridge.call(connection, tool_name, arguments, request_id=call_id)
        return result.to_dict() if hasattr(result, "to_dict") else _safe_value(result)

    def _tool_limit(self, role_id: str) -> int:
        workflow = self._workflow or {}
        roles = workflow.get("roles", []) if isinstance(workflow, Mapping) else []
        for role in roles if isinstance(roles, list) else []:
            if not isinstance(role, Mapping):
                continue
            current = str(_pick(role, "role_id", "roleId", "id", default=""))
            if current != role_id:
                continue
            budget = _pick(role, "budget", default={})
            if isinstance(budget, Mapping):
                raw = _pick(budget, "maxTools", "max_tools", default=MAX_WORKFLOW_TOOLS)
                if isinstance(raw, int) and not isinstance(raw, bool):
                    return max(0, raw)
        return MAX_WORKFLOW_TOOLS

    def _allowed_tools(self, role_id: str) -> tuple[str, ...] | None:
        workflow = self._workflow or {}
        roles = workflow.get("roles", []) if isinstance(workflow, Mapping) else []
        for role in roles if isinstance(roles, list) else []:
            if not isinstance(role, Mapping):
                continue
            current = str(_pick(role, "role_id", "roleId", "id", default=""))
            if current == role_id:
                tools = _pick(role, "allowed_tools", "allowedTools", default=None)
                if isinstance(tools, list):
                    return tuple(str(item) for item in tools)
        return None

    def _invoke_callback(self, callback: Callable[..., object], payload: Mapping[str, Any]) -> object:
        signature = inspect.signature(callback)
        positional = [item for item in signature.parameters.values() if item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}]
        if len(positional) >= 2:
            return callback(dict(payload), dict(self.scope))
        return callback(dict(payload))

    def _normalize_event(self, envelope: object) -> RuntimeEvent | None:
        if not isinstance(envelope, Mapping):
            return None
        raw = envelope.get("event") if isinstance(envelope.get("event"), Mapping) else envelope
        if not isinstance(raw, Mapping):
            return None
        raw_id = str(_pick(raw, "eventId", "event_id", default="") or "")
        source_key = raw_id or json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        if source_key in self._seen_source_events:
            return None
        self._seen_source_events.add(source_key)
        raw_type = str(_pick(raw, "type", "event", default="") or "")
        data = _pick(raw, "data", "payload", default={})
        if not isinstance(data, Mapping):
            data = {}
        session_id = _identifier(_pick(raw, "sessionId", "session_id", default=self.root_session_id), self.root_session_id)
        run_id = str(_pick(raw, "agentRunId", "agent_run_id", default="") or "").strip() or self._session_to_run.get(session_id)
        if raw_type in {"agent/activated", "agent.activated"}:
            declared = str(_pick(data, "runId", "run_id", default="") or "").strip()
            declared_session = str(_pick(data, "sessionId", "session_id", default=session_id) or session_id).strip()
            if declared and declared_session:
                self._session_to_run[declared_session] = declared
                self._run_to_session[declared] = declared_session
                self._run_roles[declared] = str(_pick(data, "roleId", "role_id", default="MainAgent"))
            run_id = declared or run_id
        elif raw_type in {"subagent/start", "subagent.start"}:
            child = str(_pick(data, "childSessionId", "child_session_id", default="") or "").strip()
            declared = str(_pick(data, "runId", "run_id", default="") or "").strip()
            if child and declared:
                self._session_to_run[child] = declared
                self._run_to_session[declared] = child
                self._run_roles[declared] = str(_pick(data, "roleId", "role_id", default="agent"))
            run_id = declared or run_id
        elif raw_type in {"subagent/end", "subagent.end"}:
            run_id = str(_pick(data, "runId", "run_id", default=run_id) or run_id or "") or None
        elif raw_type in {"agent/status", "agent.status"}:
            run_id = str(_pick(data, "runId", "run_id", default=run_id) or run_id or "") or None
        if run_id:
            run_id = _identifier(run_id, "run-unknown")
        workflow_id = str(_pick(data, "workflowId", "workflow_id", default=self._workflow_id or "") or "").strip()
        if not workflow_id:
            workflow_id = self._workflow_id or f"workflow-{self.task_id}"
        if self._workflow_id and workflow_id != self._workflow_id:
            return None
        if raw_type in {"workflow/start", "workflow.start"}:
            self._workflow_id = workflow_id
        event_type = self._public_event_type(raw_type, data)
        if event_type is None:
            return None
        self._next_event_seq += 1
        payload = self._event_payload(event_type, data)
        turn = self._nonnegative_int(_pick(raw, "turn", default=_pick(data, "turn", default=None)))
        step = self._nonnegative_int(_pick(raw, "step", default=_pick(data, "step", default=None)))
        event_id = _identifier(raw_id, f"runtime-event-{self._next_event_seq}")
        return RuntimeEvent(
            event_id=event_id,
            seq=self._next_event_seq,
            task_id=self.task_id,
            workflow_id=_identifier(workflow_id, f"workflow-{self.task_id}"),
            session_id=session_id,
            agent_run_id=run_id,
            turn=turn,
            step=step,
            type=event_type,
            timestamp=str(_pick(raw, "timestamp", "time", default=_now())),
            payload=payload,
        )

    @staticmethod
    def _nonnegative_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @staticmethod
    def _public_event_type(raw_type: str, data: Mapping[str, Any]) -> str | None:
        normalized = raw_type.replace("/", ".")
        if normalized == "workflow.end":
            status = str(data.get("status", "completed"))
            return {"completed": "workflow.completed", "cancelled": "workflow.cancelled"}.get(status, "workflow.failed")
        if normalized == "subagent.start":
            return "agent.started"
        if normalized == "agent.activated":
            return "agent.started"
        if normalized == "subagent.end":
            status = str(data.get("status", "completed"))
            return {"completed": "agent.completed", "cancelled": "agent.cancelled"}.get(status, "agent.failed")
        if normalized == "turn.start":
            return "turn.started"
        if normalized == "turn.end":
            return f"turn.{_terminal_status(data)}"
        if normalized == "step.start":
            return "step.started"
        if normalized == "step.end":
            return "step.completed"
        if normalized == "usage.stream":
            return "usage.updated"
        if normalized == "model.route-bound":
            return "model.route_bound"
        if normalized == "compaction.start":
            return "compaction.started"
        if normalized == "compaction.end":
            return "compaction.completed" if data.get("status") == "completed" else "compaction.failed"
        if normalized in {"session.interrupted", "session.recovered", "runtime.error", "runtime.ready", "runtime.closed"}:
            return normalized
        public = {
            "workflow.start": "workflow.started",
            "agent.status": "agent.status",
            "assistant.block-start": "assistant.block_started",
            "assistant.block-end": "assistant.block_completed",
            "assistant.text_delta": "assistant.text_delta",
            "assistant.reasoning_delta": "assistant.reasoning_delta",
            "assistant.message": "assistant.message",
            "tool.requested": "tool.requested",
            "tool.started": "tool.started",
            "tool.completed": "tool.completed",
            "tool.failed": "tool.failed",
            "tool.cancelled": "tool.cancelled",
            "usage.updated": "usage.updated",
        }
        return public.get(normalized)

    @staticmethod
    def _event_payload(event_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
        if event_type == "assistant.reasoning_delta":
            text = _reasoning_text(_pick(data, "text", "delta", "reasoning", default=""))
            result = {"delta": text, "available": bool(text) or data.get("available") is True, "chars": len(text)}
            if isinstance(data.get("index"), int) and not isinstance(data.get("index"), bool):
                result["index"] = data["index"]
            return result
        if event_type == "assistant.text_delta":
            result = {"delta": str(_pick(data, "text", "delta", default=""))}
            if isinstance(data.get("index"), int) and not isinstance(data.get("index"), bool):
                result["index"] = data["index"]
            return result
        if event_type in {"assistant.block_started", "assistant.block_completed"}:
            raw = _safe_value(data)
            return raw if isinstance(raw, dict) else {}
        if event_type == "assistant.message":
            raw = _safe_value(data)
            if isinstance(raw, dict) and isinstance(raw.get("reasoning"), str):
                raw["reasoning"] = _reasoning_text(raw["reasoning"])
                raw["reasoning_chars"] = len(raw["reasoning"])
            return raw if isinstance(raw, dict) else {}
        if event_type.startswith("tool."):
            payload: dict[str, Any] = {
                "tool_call_id": str(_pick(data, "toolCallId", "tool_call_id", default="")),
                "tool_name": str(_pick(data, "toolName", "tool_name", default="")),
            }
            if event_type in {"tool.completed", "tool.failed", "tool.cancelled"}:
                payload["status"] = str(data.get("status", event_type.rsplit(".", 1)[-1]))
                payload["result"] = _safe_value(data.get("result"))
                if isinstance(data.get("error"), str):
                    payload["error"] = _safe_value(data["error"])
                if data.get("errorCode") is not None:
                    payload["error_code"] = str(data["errorCode"])
            return payload
        if event_type.startswith("agent."):
            return {
                "run_id": str(_pick(data, "runId", "run_id", default="")),
                "role_id": str(_pick(data, "roleId", "role_id", default="agent")),
                "status": str(data.get("status", "running")),
                "session_id": str(_pick(data, "sessionId", "session_id", "childSessionId", "child_session_id", default="")),
            }
        return _safe_value(data) if isinstance(_safe_value(data), dict) else {}

    def _set_terminal(self, error: RuntimeSubprocessError) -> None:
        with self._pending_lock:
            if self._terminal_error is None:
                self._terminal_error = error
        self._fail_pending(error)

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.event.set()

    def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    except Exception:
                        try:
                            process.terminate()
                        except Exception:
                            pass
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        try:
                            if os.name != "nt":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                        try:
                            process.wait(timeout=1.0)
                        except Exception:
                            pass
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            self._fail_pending(RuntimeSubprocessError("runtime process terminated"))


__all__ = ["RuntimeSubprocessError", "RuntimeSubprocessTimeout", "RuntimeSubprocessTransport"]
