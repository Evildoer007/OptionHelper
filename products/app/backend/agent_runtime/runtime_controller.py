"""Task-scoped controller for the OptionHelper Agent runtime.

This module is an adapter boundary. It does not implement another model loop
and it does not own OptionHelper tasks, contracts or module results. A runtime
process is supplied through ``RuntimeTransport`` and is controlled with the
small protocol defined here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import inspect
import json
from threading import RLock
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from .runtime_protocol import (
    AgentRun,
    MAX_WORKFLOW_STEPS,
    MAX_WORKFLOW_TOOLS,
    RuntimeEvent,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
    WorkflowSpec,
)
from .session_context import SessionEventLog, SessionId


class RuntimeTransport(Protocol):
    """Minimal transport required from an external or in-process runtime."""

    def start(self, scope: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def request(self, method: str, params: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...

    def cancel(self, reason: str = "cancelled") -> None: ...

    def close(self) -> None: ...


RuntimeTransportFactory = Callable[[Mapping[str, Any]], RuntimeTransport]
EventSink = Callable[[RuntimeEvent], None]
def _canonical_role_id(value: object) -> str:
    return str(value or "").strip().removeprefix("SingleAgent.")


class _UnavailableTransport:
    def start(self, scope: Mapping[str, Any]) -> Mapping[str, Any]:
        del scope
        raise UnavailableCapabilityError(
            "agent_runtime",
            "当前App未配置Agent运行时适配器；未发起外部模型或工具调用。",
        )

    def request(self, method: str, params: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        del method, params, kwargs
        raise UnavailableCapabilityError(
            "agent_runtime",
            "当前App未配置Agent运行时适配器；未发起外部模型或工具调用。",
        )

    def cancel(self, reason: str = "cancelled") -> None:
        del reason

    def close(self) -> None:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_value(value: object, *, depth: int = 0) -> Any:
    """Project transport output without reflecting credentials or raw prompts."""

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


class AgentRuntimeController:
    """Own exactly one runtime scope for one authenticated OptionHelper Task."""

    _registry_lock = RLock()
    _active_scopes: dict[tuple[str, str, str], "AgentRuntimeController"] = {}

    def __init__(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        runtime_factory: RuntimeTransportFactory | None = None,
        transport: RuntimeTransport | None = None,
        task_service: object | None = None,
        event_log: SessionEventLog | None = None,
        root_session_id: str | None = None,
        event_sink: EventSink | None = None,
        register_scope: bool = True,
    ) -> None:
        if not isinstance(identity, SessionIdentity):
            raise ValidationError("AgentRuntimeController需要已认证SessionIdentity")
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            raise ValidationError("task_id不能为空")
        self.identity = identity
        self.task_id = normalized_task
        self._task_service = task_service
        self._event_log = event_log
        self._event_sink = event_sink
        self._runtime_factory = runtime_factory
        self._transport = transport
        self._workflow: WorkflowSpec | None = None
        self._runs: dict[str, AgentRun] = {}
        self._history: list[RuntimeEvent] = []
        self._lock = RLock()
        self._status = "idle"
        self._closed = False
        self._close_error: Exception | None = None
        self._last_transport_result: dict[str, Any] = {}
        self._root_session_id = self._resolve_root_session(root_session_id)
        self._scope_key = (identity.tenant_id, identity.principal_id, self.task_id)
        if register_scope:
            with self._registry_lock:
                existing = self._active_scopes.get(self._scope_key)
                if existing is not None and not existing.closed:
                    raise ValidationError("同一Task只能存在一个AgentRuntimeController")
                self._active_scopes[self._scope_key] = self
        self._load_history()

    @classmethod
    def for_task(cls, identity: SessionIdentity, task_id: str, **kwargs: Any) -> "AgentRuntimeController":
        return cls(identity, task_id, **kwargs)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def root_session_id(self) -> str:
        return self._root_session_id

    @property
    def workflow(self) -> WorkflowSpec | None:
        return self._workflow

    @property
    def close_error(self) -> Exception | None:
        return self._close_error

    def _resolve_root_session(self, explicit: str | None) -> str:
        if self._task_service is not None:
            getter = getattr(self._task_service, "conversation_session_id", None)
            if callable(getter):
                resolved = getter(self.identity, self.task_id)
                return str(resolved)
        value = str(explicit or f"agent-root-{self.task_id}").strip()
        if not value:
            raise ValidationError("root_session_id不能为空")
        return value

    def _load_history(self) -> None:
        if self._event_log is None:
            return
        try:
            events = self._event_log.replay(SessionId(self._root_session_id))
        except KeyError:
            self._event_log.ensure_session(SessionId(self._root_session_id), kind="agent_runtime")
            return
        for row in events:
            if row.event_type != "runtime.event" or not isinstance(row.data, Mapping):
                continue
            raw = row.data.get("runtime_event")
            if not isinstance(raw, Mapping):
                raise ValidationError("持久化Runtime事件格式无效")
            event = RuntimeEvent.from_dict(raw)
            self._accept_event(event, persist=False, notify=False)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("AgentRuntimeController已关闭")

    def _ensure_task(self, workflow: WorkflowSpec) -> None:
        if workflow.task_id != self.task_id:
            raise AuthorizationError("agent_runtime.workflow", "WorkflowSpec不属于当前Task")
        if workflow.root_session_id != self._root_session_id:
            raise AuthorizationError("agent_runtime.session", "WorkflowSpec不属于当前Session")

    def start(self, workflow: WorkflowSpec) -> dict[str, Any]:
        if not isinstance(workflow, WorkflowSpec):
            raise ValidationError("start需要WorkflowSpec")
        with self._lock:
            self._ensure_open()
            self._ensure_task(workflow)
            if self._workflow is not None:
                if self._workflow.to_dict() == workflow.to_dict():
                    return self.status_snapshot()
                raise ValidationError("当前Task已经绑定另一份WorkflowSpec")
            self._workflow = workflow
            if self._transport is None:
                factory = self._runtime_factory
                self._transport = factory(self._transport_scope()) if factory is not None else _UnavailableTransport()
            self._attach_event_handler()
            self._status = "starting"
        try:
            # Runtime events are read on the transport thread. Do not retain
            # the controller lock while waiting for the RPC response, because
            # the runtime may publish workflow.started before that response.
            result = self._invoke_start(workflow.to_dict())
        except Exception:
            with self._lock:
                self._status = "failed"
                self._append_generated_event("runtime.error", {"stage": "start", "code": "runtime_start_failed"})
            raise
        with self._lock:
            self._last_transport_result = dict(_safe_value(result)) if isinstance(result, Mapping) else {}
            self._status = "running"
            self._append_generated_event("workflow.started", {"workflow": workflow.to_dict()})
            self._append_generated_event("runtime.ready", {"transport_ready": True})
            self._register_transport_runs(result)
            return self.status_snapshot()

    def bind(self, workflow: WorkflowSpec) -> dict[str, Any]:
        """Freeze a Host-orchestrated workflow on a pre-initialized transport.

        ``workflow.start`` persists the immutable preset and role boundary
        with ``auto_execute=false``. RecommenderService remains the only graph
        scheduler and sends the subsequent ``subagent.*`` requests.
        """

        if not isinstance(workflow, WorkflowSpec):
            raise ValidationError("bind需要WorkflowSpec")
        with self._lock:
            self._ensure_open()
            self._ensure_task(workflow)
            if self._workflow is not None:
                if self._workflow.to_dict() == workflow.to_dict():
                    return self.status_snapshot()
                raise ValidationError("当前Task已经绑定另一份WorkflowSpec")
            if self._transport is None:
                raise ValidationError("bind需要已初始化RuntimeTransport")
            self._workflow = workflow
            self._attach_event_handler()
            self._status = "starting"
        try:
            started = self._invoke_request(
                "workflow.start",
                {"spec": workflow.to_dict(), "auto_execute": False, "orchestration_owner": "optionhelper_host"},
                request_id=f"bind-{workflow.workflow_id}",
            )
        except Exception:
            with self._lock:
                self._status = "failed"
                self._append_generated_event("runtime.error", {"stage": "bind", "code": "runtime_bind_failed"})
            raise
        with self._lock:
            self._last_transport_result = dict(_safe_value(started))
            self._status = "running"
            self._append_generated_event("workflow.started", {"workflow": workflow.to_dict(), "bound": True})
            self._append_generated_event("runtime.ready", {"transport_ready": True, "bound": True})
            return self.status_snapshot()

    def start_from_dict(self, value: object) -> dict[str, Any]:
        return self.start(WorkflowSpec.from_dict(value))

    def _transport_scope(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tenant_id": self.identity.tenant_id,
            "principal_id": self.identity.principal_id,
            "root_session_id": self._root_session_id,
        }

    def _attach_event_handler(self) -> None:
        if self._transport is None:
            return
        setter = getattr(self._transport, "set_event_handler", None)
        if callable(setter):
            setter(self.ingest_event)

    def _invoke_start(self, workflow: Mapping[str, Any]) -> Mapping[str, Any]:
        assert self._transport is not None
        starter = getattr(self._transport, "start", None)
        if not callable(starter):
            raise ValidationError("RuntimeTransport缺少start")
        result = starter(workflow)
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise ValidationError("RuntimeTransport.start必须返回对象")
        return dict(result)

    def _register_transport_runs(self, result: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping):
            return
        raw_runs = result.get("runs")
        if not isinstance(raw_runs, list):
            return
        for raw in raw_runs:
            if not isinstance(raw, Mapping):
                continue
            try:
                run = AgentRun.from_dict(raw)
            except ValidationError:
                continue
            if run.workflow_id == (self._workflow.workflow_id if self._workflow else run.workflow_id):
                self._runs[run.run_id] = run

    def request(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        self._ensure_open()
        normalized = str(method or "").strip().replace("/", ".")
        if not normalized:
            raise ValidationError("RPC method不能为空")
        payload = dict(params or {})
        request_id = str(request_id or f"rpc-{uuid4().hex}")
        if normalized == "runtime.capabilities":
            raw = self._forward(normalized, payload, request_id=request_id)
            capabilities = raw.get("capabilities")
            if not isinstance(capabilities, Mapping):
                capabilities = {}
            runtime_state = capabilities.get("state")
            if not isinstance(runtime_state, Mapping):
                runtime_state = {}
            runtime_id = str(raw.get("runtimeId", raw.get("runtime_id", ""))).strip()
            protocol_version = str(raw.get("protocolVersion", raw.get("protocol_version", ""))).strip()
            initialized = bool(runtime_state.get("initialized", self._transport is not None))
            reported_verified = runtime_state.get("verified")
            verified = bool(
                runtime_id == "optionhelper-agent-runtime"
                and protocol_version
                and initialized
                and (reported_verified is True or (reported_verified is None and capabilities))
            )
            return {
                **dict(raw),
                "runtime": runtime_id or "unknown",
                "task_id": self.task_id,
                "status": self.status,
                "runtime_state": {
                    "detected": self._transport is not None,
                    "initialized": initialized,
                    "verified": verified,
                },
                "supports": dict(_safe_value(capabilities)),
            }
        if normalized in {"runtime.initialize", "workflow.start"}:
            raw_workflow = payload.get("workflow", payload)
            return self.start(WorkflowSpec.from_dict(raw_workflow))
        if normalized in {"workflow.status", "runtime.status"}:
            return self.status_snapshot()
        if normalized == "workflow.cancel":
            return self.cancel(str(payload.get("reason", "user_cancelled")))
        if normalized in {"session.history", "session.events"}:
            return {"events": self.history(after_seq=payload.get("after_seq", 0), limit=payload.get("limit", 500))}
        if normalized == "session.tree":
            return {"tree": self.session_tree()}
        if normalized == "session.resume":
            return self.resume(payload)
        if normalized == "subagent.start":
            return self.start_subagent(payload, request_id=request_id)
        if normalized == "subagent.followup":
            return self.followup(payload, request_id=request_id)
        if normalized == "subagent.interrupt":
            return self.interrupt(payload, request_id=request_id)
        if normalized == "runtime.shutdown":
            return self.close(reason=str(payload.get("reason", "shutdown")))
        if normalized.startswith("agent.") or normalized.startswith("workflow.") or normalized.startswith("session.") or normalized.startswith("subagent."):
            return self._forward(normalized, payload, request_id=request_id)
        raise ValidationError("RPC method未注册")

    def rpc(self, request: RuntimeRpcRequest | Mapping[str, Any]) -> RuntimeRpcResponse:
        parsed = request if isinstance(request, RuntimeRpcRequest) else RuntimeRpcRequest.from_dict(request)
        try:
            result = self.request(parsed.method, parsed.params, request_id=parsed.request_id)
            return RuntimeRpcResponse(parsed.request_id, True, result=result)
        except Exception as error:
            return RuntimeRpcResponse(
                parsed.request_id,
                False,
                error={"code": type(error).__name__, "message": str(error)[:500]},
            )

    def _forward(self, method: str, params: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        if self._transport is None:
            raise RuntimeError("AgentRuntime尚未启动")
        result = self._invoke_request(method, params, request_id=request_id)
        return dict(_safe_value(result))

    def _invoke_request(self, method: str, params: Mapping[str, Any], *, request_id: str) -> Mapping[str, Any]:
        fn = getattr(self._transport, "request", None)
        if not callable(fn):
            raise ValidationError("RuntimeTransport缺少request")
        signature = inspect.signature(fn)
        names = set(signature.parameters)
        if "request_id" in names:
            result = fn(method, dict(params), request_id=request_id)
        elif len(signature.parameters) == 1:
            result = fn(RuntimeRpcRequest(request_id, method, params).to_dict())
        else:
            result = fn(method, dict(params))
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise ValidationError("RuntimeTransport.request必须返回对象")
        return dict(result)

    def start_subagent(self, params: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        if self._workflow is None:
            raise ValidationError("Workflow尚未启动")
        role_id = _canonical_role_id(params.get("role_id", params.get("roleId", "")))
        role = self._workflow.role(role_id)
        run_id = str(params.get("run_id") or f"run-{uuid4().hex}")
        session_id = str(params.get("session_id") or f"agent-session-{uuid4().hex}")
        parent_run_id = params.get("parent_run_id", params.get("parentRunId"))
        if parent_run_id is not None and str(parent_run_id) not in self._runs:
            raise ValidationError("parent_run_id不属于当前Workflow")
        mode = str(params.get("mode", "one-shot")).strip() or "one-shot"
        if mode not in {"one-shot", "continuable"}:
            raise ValidationError("subagent.mode无效")
        parent_session_id = str(
            params.get("parent_session_id", params.get("parentSessionId", self._root_session_id))
            or self._root_session_id
        )
        prompt = params.get("prompt")
        if prompt is None:
            prompt = json.dumps(params.get("input", {}), ensure_ascii=False, sort_keys=True, default=str)
        budget = dict(role.budget)
        requested_budget = params.get("budget", {})
        if requested_budget is not None:
            if not isinstance(requested_budget, Mapping):
                raise ValidationError("subagent.budget必须是对象")
            budget.update(dict(requested_budget))
        requested_steps = budget.get("maxSteps", budget.get("max_steps", self._workflow.max_steps_per_run))
        requested_tools = budget.get("maxTools", budget.get("max_tools", self._workflow.max_tools))
        if isinstance(requested_steps, bool) or not isinstance(requested_steps, int):
            raise ValidationError("subagent.maxSteps必须是整数")
        if isinstance(requested_tools, bool) or not isinstance(requested_tools, int):
            raise ValidationError("subagent.maxTools必须是整数")
        budget["maxSteps"] = requested_steps
        budget["maxTools"] = requested_tools
        if budget["maxSteps"] < 0 or budget["maxTools"] < 0:
            raise ValidationError("subagent预算超出运行时上限")
        tool_policy = params.get("tool_policy", params.get("toolPolicy"))
        if tool_policy is not None and not isinstance(tool_policy, Mapping):
            raise ValidationError("subagent.toolPolicy必须是对象")
        requested_tools = (
            tool_policy.get("allowedTools", tool_policy.get("allowed_tools"))
            if isinstance(tool_policy, Mapping)
            else role.allowed_tools
        )
        if isinstance(requested_tools, str) or not isinstance(requested_tools, (list, tuple)):
            raise ValidationError("subagent.allowedTools必须是字符串数组")
        requested = tuple(str(item) for item in requested_tools)
        if any(not item.strip() for item in requested) or len(set(requested)) != len(requested):
            raise ValidationError("subagent.allowedTools包含空值或重复项")
        allowed = set(role.allowed_tools)
        effective_tools = [item for item in requested if item in allowed]
        tool_policy = {
            "allowedTools": effective_tools,
            "parallelTools": bool(tool_policy.get("parallelTools", tool_policy.get("parallel_tools", False)))
            if isinstance(tool_policy, Mapping) else False,
        }
        context_policy = params.get("context_policy", params.get("contextPolicy", role.context_policy))
        if not isinstance(context_policy, Mapping):
            raise ValidationError("subagent.contextPolicy必须是对象")
        run = AgentRun(
            run_id=run_id,
            workflow_id=self._workflow.workflow_id,
            parent_run_id=str(parent_run_id) if parent_run_id is not None else None,
            session_id=session_id,
            role_id=role.role_id,
            status="starting",
            model_route_ref=role.model_route_ref,
            started_at=_now(),
            parent_session_id=parent_session_id,
            label=str(params.get("label", role.role_id)),
            mode=mode,
        )
        self._runs[run.run_id] = run
        request = {
            "parentSessionId": parent_session_id,
            "workflowId": self._workflow.workflow_id,
            "runId": run.run_id,
            "parentRunId": run.parent_run_id,
            "roleId": role.role_id,
            "label": run.label,
            "mode": run.mode,
            "modelRouteRef": role.model_route_ref.to_dict() if role.model_route_ref else None,
            "toolPolicy": dict(tool_policy),
            "contextPolicy": dict(context_policy),
            "budget": budget,
            "prompt": str(prompt),
            "wait": bool(params.get("wait", True)),
        }
        result = self._forward("subagent.start", request, request_id=request_id)
        return {"run": run.to_dict(), "result": result}

    def followup(self, params: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        run_id = str(params.get("run_id", params.get("runId", ""))).strip()
        if run_id not in self._runs:
            raise ValidationError("run_id不属于当前Workflow")
        run = self._runs[run_id]
        if run.mode != "continuable":
            raise ValidationError("只有continuable Child Session可以followup")
        prompt = params.get("prompt")
        if prompt is None:
            raise ValidationError("followup.prompt不能为空")
        return self._forward(
            "subagent.followup",
            {
                "runId": run_id,
                "prompt": str(prompt),
                "modelRouteRef": run.model_route_ref.to_dict() if run.model_route_ref else None,
            },
            request_id=request_id,
        )

    def interrupt(self, params: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        run_id = str(params.get("run_id", params.get("runId", ""))).strip()
        if run_id not in self._runs:
            raise ValidationError("run_id不属于当前Workflow")
        return self._forward("subagent.interrupt", dict(params), request_id=request_id)

    def resume(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_open()
        with self._lock:
            created_transport = False
            if self._transport is None:
                factory = self._runtime_factory
                if factory is None:
                    self._transport = _UnavailableTransport()
                else:
                    self._transport = factory(self._transport_scope())
                created_transport = True
                self._attach_event_handler()
            if self._workflow is None:
                self._recover_workflow_from_history()
            if self._workflow is None:
                raise ValidationError("没有可恢复的Workflow")
            payload = dict(params or {})
            payload.setdefault("session_id", self._root_session_id)
            payload.setdefault("workflow", self._workflow.to_dict())
            transport = self._transport
            self._status = "starting"
        try:
            if created_transport and transport is not None:
                initializer = getattr(transport, "initialize", None)
                if callable(initializer):
                    initialized = initializer()
                    returned_root = str(initialized.get("rootSessionId", initialized.get("root_session_id", "")))
                    if returned_root and returned_root != self._root_session_id:
                        raise ValidationError("恢复Runtime未绑定指定Root Session")
            result = self._invoke_request("session.resume", payload, request_id=f"resume-{uuid4().hex}")
        except Exception:
            with self._lock:
                self._status = "failed"
                self._append_generated_event("runtime.error", {"stage": "resume", "code": "runtime_resume_failed"})
            raise
        with self._lock:
            self._register_transport_runs({"runs": result.get("restoredRuns", result.get("restored_runs", []))})
            self._status = "running"
            self._append_generated_event("session.recovered", {"session_id": self._root_session_id})
            return {"status": self._status, "result": dict(_safe_value(result))}

    def _recover_workflow_from_history(self) -> None:
        for event in self._history:
            if event.type == "workflow.started" and isinstance(event.payload.get("workflow"), Mapping):
                self._workflow = WorkflowSpec.from_dict(event.payload["workflow"])
                return

    def cancel(self, reason: str = "cancelled") -> dict[str, Any]:
        self._ensure_open()
        with self._lock:
            if self._status in {"cancelled", "closed"}:
                return {"status": self._status}
            self._status = "cancel_requested"
            transport = self._transport
        error: Exception | None = None
        if transport is not None:
            try:
                cancel = getattr(transport, "cancel", None)
                if callable(cancel):
                    signature = inspect.signature(cancel)
                    if signature.parameters:
                        cancel(str(reason)[:120])
                    else:
                        cancel()
            except Exception as caught:
                error = caught
        with self._lock:
            self._append_generated_event("session.interrupted", {"reason": str(reason)[:120]})
            self._status = "cancelled" if error is None else "failed"
            if error is not None:
                self._append_generated_event("runtime.error", {"stage": "cancel", "code": type(error).__name__})
                raise error
            return {"status": self._status}

    def history(self, *, after_seq: object = 0, limit: object = 500) -> list[dict[str, Any]]:
        try:
            start = int(after_seq)
            maximum = int(limit)
        except (TypeError, ValueError) as error:
            raise ValidationError("history分页参数无效") from error
        if start < 0 or maximum < 1 or maximum > 2_000:
            raise ValidationError("history分页范围无效")
        return [event.to_dict() for event in self._history if event.seq > start][:maximum]

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "root_session_id": self._root_session_id,
            "workflow": self._workflow.to_dict() if self._workflow else None,
            "runs": [run.to_dict() for run in self._runs.values()],
            "last_event_seq": self._history[-1].seq if self._history else 0,
        }

    def session_tree(self) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {
            self._root_session_id: {
                "session_id": self._root_session_id,
                "parent_session_id": None,
                "kind": "root",
                "run_id": None,
                "role_id": None,
                "status": self.status,
                "children": [],
            }
        }
        for run in self._runs.values():
            node = {
                "session_id": run.session_id,
                "parent_session_id": self._root_session_id,
                "kind": "agent",
                "run_id": run.run_id,
                "role_id": run.role_id,
                "status": run.status,
                "children": [],
            }
            nodes[run.session_id] = node
        for run in self._runs.values():
            parent_session = self._runs.get(run.parent_run_id).session_id if run.parent_run_id in self._runs else self._root_session_id
            if run.session_id in nodes and run.session_id != parent_session:
                nodes[parent_session]["children"].append(nodes[run.session_id])
        return nodes[self._root_session_id]

    def ingest_event(self, raw: RuntimeEvent | Mapping[str, Any]) -> RuntimeEvent:
        event = raw if isinstance(raw, RuntimeEvent) else RuntimeEvent.from_dict(raw)
        if event.task_id != self.task_id:
            raise AuthorizationError("agent_runtime.event", "Runtime事件不属于当前Task")
        if self._workflow is not None and event.workflow_id != self._workflow.workflow_id:
            raise AuthorizationError("agent_runtime.event", "Runtime事件不属于当前Workflow")
        with self._lock:
            existing = next((item for item in self._history if item.event_id == event.event_id), None)
            if existing is not None:
                previous = existing.to_dict()
                incoming = event.to_dict()
                previous.pop("seq", None)
                incoming.pop("seq", None)
                if previous != incoming:
                    raise ValidationError("Runtime事件idempotency冲突")
                return existing
            if self._history and event.seq <= self._history[-1].seq:
                # The transport sequence is local to the child process while
                # generated controller events share the persisted history.
                # Rebase only the new event at this boundary; source identity
                # and payload remain unchanged and duplicates stay idempotent.
                event = replace(event, seq=self._history[-1].seq + 1)
            self._accept_event(event, persist=True, notify=True)
        return event

    def _accept_event(self, event: RuntimeEvent, *, persist: bool, notify: bool) -> None:
        existing = next((item for item in self._history if item.event_id == event.event_id), None)
        if existing is not None:
            if existing.to_dict() != event.to_dict():
                raise ValidationError("Runtime事件idempotency冲突")
            return
        if self._history and event.seq <= self._history[-1].seq:
            raise ValidationError("Runtime事件seq必须递增")
        if persist and self._event_log is not None:
            try:
                self._event_log.append(
                    SessionId(self._root_session_id),
                    "runtime.event",
                    {"runtime_event": event.to_dict()},
                    event_id=f"runtime:{event.event_id}",
                    ignorable=True,
                )
            except KeyError:
                self._event_log.ensure_session(SessionId(self._root_session_id), kind="agent_runtime")
                self._event_log.append(
                    SessionId(self._root_session_id),
                    "runtime.event",
                    {"runtime_event": event.to_dict()},
                    event_id=f"runtime:{event.event_id}",
                    ignorable=True,
                )
        self._history.append(event)
        self._update_from_event(event)
        if notify and self._event_sink is not None:
            self._event_sink(event)

    def _append_generated_event(self, event_type: str, payload: Mapping[str, Any]) -> RuntimeEvent:
        next_seq = self._history[-1].seq + 1 if self._history else 1
        workflow_id = self._workflow.workflow_id if self._workflow else f"workflow-{self.task_id}"
        event = RuntimeEvent(
            event_id=f"event-{uuid4().hex}",
            seq=next_seq,
            task_id=self.task_id,
            workflow_id=workflow_id,
            session_id=self._root_session_id,
            agent_run_id=None,
            turn=None,
            step=None,
            type=event_type,
            timestamp=_now(),
            payload=payload,
        )
        self._accept_event(event, persist=True, notify=True)
        return event

    def _update_from_event(self, event: RuntimeEvent) -> None:
        if event.type == "workflow.completed":
            self._status = "completed"
        elif event.type == "workflow.failed":
            self._status = "failed"
        elif event.type == "workflow.cancelled":
            self._status = "cancelled"
        elif event.type == "runtime.closed":
            self._status = "closed"
        if not event.agent_run_id:
            return
        current = self._runs.get(event.agent_run_id)
        if current is None:
            role = str(event.payload.get("role_id", event.payload.get("role", "agent")))
            session_id = str(event.payload.get("session_id", f"agent-session-{event.agent_run_id}"))
            try:
                role_spec = self._workflow.role(role) if self._workflow else None
            except ValidationError:
                role_spec = None
            current = AgentRun(
                run_id=event.agent_run_id,
                workflow_id=event.workflow_id,
                parent_run_id=event.payload.get("parent_run_id"),
                session_id=session_id,
                role_id=role,
                status="queued",
                model_route_ref=role_spec.model_route_ref if role_spec else None,
            )
        status = current.status
        if event.type in {"agent.started", "agent.status"}:
            candidate = str(event.payload.get("status", "running"))
            status = candidate if candidate in {"starting", "running", "waiting_tool", "waiting_parent"} else "running"
        elif event.type == "agent.completed":
            status = "completed"
        elif event.type == "agent.failed":
            status = "failed"
        elif event.type == "agent.cancelled":
            status = "cancelled"
        elif event.type in {"tool.requested", "tool.started"}:
            status = "waiting_tool"
        elif event.type in {"tool.completed", "tool.failed", "tool.cancelled"}:
            status = "running"
        payload_session_id = str(event.payload.get("session_id", "") or "").strip()
        payload_role_id = str(event.payload.get("role_id", "") or "").strip()
        self._runs[event.agent_run_id] = replace(
            current,
            status=status,
            session_id=payload_session_id or current.session_id,
            role_id=payload_role_id or current.role_id,
        )

    def close(self, *, reason: str = "closed") -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {"status": "closed", "error": str(self._close_error) if self._close_error else None}
            should_cancel = self._status in {"running", "cancel_requested"}
            transport = self._transport
            self._closed = True
            self._status = "closing"
        error: Exception | None = None
        try:
            if should_cancel and transport is not None:
                cancel = getattr(transport, "cancel", None)
                if callable(cancel):
                    signature = inspect.signature(cancel)
                    if signature.parameters:
                        cancel(str(reason)[:120])
                    else:
                        cancel()
        except Exception as caught:
            error = caught
        finally:
            try:
                if transport is not None:
                    closer = getattr(transport, "close", None)
                    if callable(closer):
                        closer()
            except Exception as caught:
                error = error or caught
            with self._lock:
                self._status = "closed"
                self._close_error = error
                try:
                    self._append_generated_event("runtime.closed", {"reason": str(reason)[:120], "clean": error is None})
                except Exception as caught:
                    error = error or caught
                    self._close_error = error
                with self._registry_lock:
                    if self._active_scopes.get(self._scope_key) is self:
                        self._active_scopes.pop(self._scope_key, None)
        if error is not None:
            raise error
        return {"status": "closed", "clean": True}

    def __enter__(self) -> "AgentRuntimeController":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close(reason="context_exit")

    def __del__(self) -> None:
        try:
            if not getattr(self, "_closed", True):
                self.close(reason="garbage_collection")
        except Exception:
            pass


__all__ = ["AgentRuntimeController", "RuntimeTransport", "RuntimeTransportFactory"]
