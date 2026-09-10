"""Recommender AgentPort backed by the OptionHelper child runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from modules.recommender.models import ModelCapability
from modules.recommender.agent_steps import canonical_hash
from modules.recommender.ports import AgentPort, AgentRunReceipt, AgentStepResult

from ..errors import UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.request_control import ModelRequestControl
from ..model_gateway.step_instructions import recommender_step_instruction
from ..settings.settings_models import ModelSelection
from .runtime_controller import AgentRuntimeController
from .multi_agent import (
    MultiAgentRecommendationPreset,
    canonical_role_name,
    default_agent_context_policy,
    effective_agent_instructions,
    effective_preset_revision,
    resolve_recommendation_preset,
)
from .runtime_model_proxy import RuntimeModelProxy
from .runtime_protocol import (
    MAX_WORKFLOW_EVALUATORS,
    MAX_WORKFLOW_STEPS,
    MAX_WORKFLOW_TOOLS,
    RoleSpec,
    RuntimeEvent,
    WorkflowSpec,
)
from .runtime_subprocess import RuntimeSubprocessTransport, RuntimeSubprocessTimeout


TransportFactory = Callable[..., RuntimeSubprocessTransport]


class _OptionHelperRuntimeCapability(ModelCapability):
    """Capability view whose parallel count remains the selected preset value."""

    @property
    def supports_multi_agent_workflow(self) -> bool:
        # ``max_parallel_agents`` is a parallelism limit, not a minimum number
        # of children. Mode 1 and Mode 2 are valid multi-agent workflows even
        # though their fixed stages execute serially.
        return self.structured_output and self.tool_calling and self.multi_agent


class RuntimeBackedAgentPort(AgentPort):
    """Run fixed Recommender roles in independent child sessions.

    The existing RecommenderService remains the Parent Agent and retains all
    financial workflow authority. This port owns role-scoped model turns and
    exposes only the business tools explicitly granted by the frozen preset.
    """

    def __init__(
        self,
        gateway: object,
        identity: SessionIdentity,
        task_id: str,
        *,
        preset_id: str = "sequential-deliberation",
        selection: ModelSelection | None = None,
        role_model_defaults: Mapping[str, ModelSelection] | None = None,
        role_instructions: Mapping[str, str] | None = None,
        is_cancelled: Callable[[SessionIdentity, str], bool] | None = None,
        event_sink: Callable[[RuntimeEvent], object] | None = None,
        session_root: str | Path | None = None,
        root_session_id: str | None = None,
        workflow_id: str | None = None,
        transport_factory: TransportFactory = RuntimeSubprocessTransport,
        host_multi_agent: bool = True,
        tool_bridge: object | None = None,
        allowed_tools_by_role: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        if not isinstance(identity, SessionIdentity) or not str(task_id).strip():
            raise ValidationError("RuntimeBackedAgentPort需要有效身份和Task")
        self._gateway = gateway
        self._identity = identity
        self._task_id = str(task_id).strip()
        self._preset: MultiAgentRecommendationPreset = resolve_recommendation_preset(preset_id)
        self._selection = selection
        self._role_model_defaults = {
            canonical_role_name(role): model
            for role, model in (role_model_defaults or {}).items()
        }
        self._role_instructions = dict(effective_agent_instructions(self._preset, role_instructions))
        self._preset_revision = effective_preset_revision(self._preset, self._role_instructions)
        self._is_cancelled = is_cancelled or (lambda _identity, _task_id: False)
        self._event_sink = event_sink
        self._session_root = Path(session_root).resolve() if session_root is not None else None
        self._root_session_id = str(root_session_id or f"agent-root-{uuid4().hex}")
        self._workflow_id = str(workflow_id or f"workflow-{uuid4().hex}")
        self._transport_factory = transport_factory
        self._host_multi_agent = bool(host_multi_agent)
        self._tool_bridge = tool_bridge
        self._allowed_tools_by_role = {
            canonical_role_name(role): tuple(str(item) for item in tools)
            for role, tools in (allowed_tools_by_role or {}).items()
        }
        if self._preset.preset_id == "sequential-deliberation" and any(
            self._allowed_tools_by_role.values()
        ):
            raise ValidationError("结构推荐子Agent不得调用金融Tool")
        self._lock = RLock()
        self._closed = False
        self._call_count = 0
        self._role_runs: dict[str, dict[str, Any]] = {}
        self._run_records: list[dict[str, Any]] = []
        self._replay_results: dict[str, list[Mapping[str, Any]]] = {}

        self._model_proxy = RuntimeModelProxy(gateway, identity, self._task_id)
        self._role_routes = {
            canonical_role_name(role): self._model_proxy.issue_route(
                explicit_selection=selection,
                preset_role_selection=self._role_model_selection(role),
            )
            for role in self._preset.canonical_roles
        }
        self._route_selections = {
            role: ModelSelection(route.provider_id, route.model_id)
            for role, route in self._role_routes.items()
        }
        self._role_context_policies = {
            role: default_agent_context_policy(role)
            for role in self._preset.canonical_roles
        }
        self._workflow_spec = self._build_workflow_spec()
        scope = {"task_id": self._task_id, "root_session_id": self._root_session_id}
        kwargs: dict[str, Any] = {
            "scope": scope,
            # subagent.start(wait=True) waits for the role's model response,
            # not merely process activation. Leave time for terminal events.
            "request_timeout": self._preset.max_seconds_per_agent_run + 15.0,
            "model_handler": self._handle_model_request,
            "route_refs": {route.route_id: route for route in self._role_routes.values()},
        }
        if self._tool_bridge is not None:
            kwargs["tool_bridge"] = self._tool_bridge
            kwargs["identity"] = self._identity
        if self._session_root is not None:
            kwargs["session_root"] = self._session_root
        self._transport = transport_factory(**kwargs)
        self._bind_transport_workflow()
        initialized = self._transport.initialize()
        returned_root = str(initialized.get("rootSessionId", initialized.get("root_session_id", "")))
        if returned_root != self._root_session_id:
            self._transport.close()
            raise ValidationError("Agent运行时未绑定指定Root Session")
        self._controller = AgentRuntimeController(
            self._identity,
            self._task_id,
            transport=self._transport,
            root_session_id=self._root_session_id,
            event_sink=self._handle_runtime_event,
            register_scope=False,
        )
        self._controller.bind(self._workflow_spec)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def run_records(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._run_records)

    @property
    def process(self):
        return self._transport.process

    def _role_model_selection(self, role: str) -> ModelSelection | None:
        canonical = canonical_role_name(role)
        candidates = [canonical, str(role)]
        for candidate in candidates:
            selection = self._role_model_defaults.get(candidate)
            if selection is not None:
                return selection
        return None

    def _bind_transport_workflow(self) -> None:
        binder = getattr(self._transport, "bind_workflow", None)
        if not callable(binder):
            return
        workflow = self._workflow_spec.to_dict()
        try:
            parameters = inspect.signature(binder).parameters
        except (TypeError, ValueError):
            parameters = {}
        if len(parameters) >= 2 or "workflow" in parameters:
            binder(self._workflow_id, workflow)
        else:
            # Older fake transports accept only the workflow id. The central
            # WorkflowSpec remains authoritative whenever the second argument
            # is supported.
            binder(self._workflow_id)

    def _build_workflow_spec(self) -> WorkflowSpec:
        roles = tuple(
            RoleSpec(
                role_id=role,
                persona=self._role_instructions[role],
                model_route_ref=self._role_routes[role],
                allowed_tools=self._allowed_tools_by_role.get(role, ()),
                context_policy=dict(self._role_context_policies[role]),
                budget={"maxSteps": MAX_WORKFLOW_STEPS, "maxTools": MAX_WORKFLOW_TOOLS},
            )
            for role in self._preset.canonical_roles
        )
        max_parallel = min(self._preset.max_parallel_agent_runs, len(roles))
        return WorkflowSpec(
            workflow_id=self._workflow_id,
            task_id=self._task_id,
            preset_id=self._preset.preset_id,
            preset_revision=self._preset_revision,
            root_session_id=self._root_session_id,
            roles=roles,
            execution_graph={
                "strategy": self._preset.execution_strategy,
                "parent": "RecommenderService",
                "child_sessions": "independent",
            },
            max_parallel_agents=max_parallel,
            max_iterations=2 if self._preset.preset_id in {"product-trader-loop", "constraint-ranking"} else 1,
            max_rounds=2 if self._preset.preset_id in {"product-trader-loop", "constraint-ranking"} else 1,
            max_rework_rounds=2 if self._preset.preset_id in {"product-trader-loop", "constraint-ranking"} else 0,
            workflow_total_budget=self._preset.effective_workflow_total_budget,
            deadline_seconds=self._preset.max_seconds_per_agent_run * self._preset.effective_workflow_total_budget,
            output_contract={"financial_facts": "FactRef-only", "parent_authority": True},
            max_steps_per_run=MAX_WORKFLOW_STEPS,
            max_tools=MAX_WORKFLOW_TOOLS,
            max_evaluators=MAX_WORKFLOW_EVALUATORS,
        )

    @property
    def controller(self) -> AgentRuntimeController:
        return self._controller

    def begin_parent_request(self) -> None:
        """Reset only the per-request budget while preserving Child Sessions."""

        with self._lock:
            self._ensure_available()
            self._call_count = 0

    def capability(self) -> ModelCapability:
        capability = self._gateway.capability_for(
            self._identity,
            **({"selection": self._selection} if self._selection is not None else {}),
        )
        data = ModelCapability.from_mapping({
            **dict(capability),
            "multi_agent": self._host_multi_agent,
            "max_parallel_agents": (
                self._preset.max_parallel_agent_runs if self._host_multi_agent else 1
            ),
            "independent_child_sessions": self._host_multi_agent,
        })
        if not self._host_multi_agent:
            return data
        runtime = self.runtime_capabilities()
        runtime_state = runtime.get("runtime_state", {})
        optionhelper_runtime = runtime.get("optionhelper_runtime", {})
        if not isinstance(runtime_state, Mapping) or runtime_state.get("verified") is not True:
            raise ValidationError("Agent运行时未通过真实Capabilities握手")
        if not isinstance(optionhelper_runtime, Mapping) or optionhelper_runtime.get("one_shot_child_session") is not True:
            raise ValidationError("Agent运行时未声明独立Child Session能力")
        return _OptionHelperRuntimeCapability(
            data.model_id,
            structured_output=data.structured_output,
            tool_calling=data.tool_calling,
            multi_agent=True,
            max_parallel_agents=data.max_parallel_agents,
            independent_child_sessions=True,
        )

    def runtime_capabilities(self) -> Mapping[str, Any]:
        """Project the selected preset from the Runtime's real handshake."""

        handshake = self._controller.request("runtime.capabilities")
        supports = handshake.get("supports", {})
        if not isinstance(supports, Mapping):
            supports = {}
        runtime_state = handshake.get("runtime_state", {})
        if not isinstance(runtime_state, Mapping):
            runtime_state = {}
        continuable_by_preset = supports.get("continuableRolesByPreset", {})
        if not isinstance(continuable_by_preset, Mapping):
            continuable_by_preset = {}
        selected_continuable = continuable_by_preset.get(self._preset.preset_id, ())
        if not isinstance(selected_continuable, (list, tuple)):
            selected_continuable = ()
        if not selected_continuable and supports.get("continuableSubagent") is True:
            selected_continuable = tuple(
                role for role in self._preset.canonical_roles if self._continuable(role)
            )
        return {
            "runtime_state": dict(runtime_state),
            "legacy_one_shot": {
                "one_shot_child_session": True,
                "continuable_child_session": False,
                "max_parallel_agents": 1,
            },
            "optionhelper_runtime": {
                "enabled": self._host_multi_agent,
                "independent_child_sessions": supports.get("oneShotSubagent") is True,
                "one_shot_child_session": supports.get("oneShotSubagent") is True,
                "continuable_child_session": bool(selected_continuable),
                "continuable_roles": list(selected_continuable),
                "cold_resume": supports.get("coldResume") is True,
                "workflow_persistence": supports.get("workflowPersistence") is True,
                "workflow_scoped_cancel": supports.get("workflowScopedCancel") is True,
                "max_parallel_agents": (
                    self._preset.max_parallel_agent_runs if self._host_multi_agent else 1
                ),
                "preset_id": self._preset.preset_id,
                "preset_revision": self._preset_revision,
            },
        }

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult:
        wire_role = str(role).removeprefix("SingleAgent.")
        normalized_role = canonical_role_name(wire_role)
        if normalized_role not in self._preset.canonical_roles:
            raise ValidationError("AgentRun角色不属于当前预设")
        if not isinstance(payload, Mapping):
            raise ValidationError("AgentRun输入必须是对象")
        with self._lock:
            self._ensure_available()
            self._call_count += 1
            existing = self._role_runs.get(normalized_role)
        prompt = self._prompt(wire_role, payload)
        try:
            if existing is not None and self._continuable(normalized_role):
                raw = self._controller.followup(
                    {"run_id": existing["run_id"], "prompt": prompt},
                    request_id=f"followup-{uuid4().hex}",
                )
            else:
                raw = self._controller.start_subagent(
                    {
                        "role_id": normalized_role,
                        "run_id": f"run-{uuid4().hex}",
                        "mode": "continuable" if self._continuable(normalized_role) else "one-shot",
                        "label": normalized_role,
                        "tool_policy": {
                            "allowedTools": list(self._allowed_tools_by_role.get(normalized_role, ())),
                        },
                        "context_policy": dict(self._role_context_policies[normalized_role]),
                        "budget": {"maxSteps": MAX_WORKFLOW_STEPS, "maxTools": MAX_WORKFLOW_TOOLS},
                        "prompt": prompt,
                        "wait": True,
                    },
                    request_id=f"start-{uuid4().hex}",
                )
        except RuntimeSubprocessTimeout:
            self.close()
            raise
        snapshot = raw.get("result", raw) if isinstance(raw, Mapping) else raw
        if not isinstance(snapshot, Mapping):
            raise ValidationError("Agent运行时子Agent响应必须是对象")
        result = self._result(snapshot, normalized_role)
        local_run = raw.get("run", {}) if isinstance(raw, Mapping) else {}
        prior_run = existing or {}
        record = {
            "role": wire_role,
            "run_id": str(snapshot.get("runId", snapshot.get("run_id", local_run.get("run_id", local_run.get("runId", prior_run.get("run_id", "")))))),
            "session_id": str(snapshot.get("sessionId", snapshot.get("session_id", local_run.get("session_id", local_run.get("sessionId", prior_run.get("session_id", "")))))),
            "mode": str(snapshot.get("mode", local_run.get("mode", prior_run.get("mode", "one-shot")))),
            "status": str(snapshot.get("status", "completed")),
            "context_policy": dict(self._role_context_policies[normalized_role]),
        }
        with self._lock:
            self._run_records.append(record)
            if self._continuable(normalized_role):
                self._role_runs[normalized_role] = record
        return AgentStepResult(
            receipt=AgentRunReceipt(
                agent_run_id=record["run_id"],
                child_session_id=record["session_id"],
                role=str(role),
                input_hash=canonical_hash(payload),
                output_hash=canonical_hash(result),
            ),
            result=result,
        )

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]:
        items = [(str(role), payload) for role, payload in requests.items()]
        if not items:
            return {}
        maximum = min(len(items), self._preset.max_parallel_agent_runs)
        if maximum < 2:
            return {role: self.run_step(role, payload) for role, payload in items}
        with ThreadPoolExecutor(max_workers=maximum, thread_name_prefix="optionhelper-runtime-role") as executor:
            futures = {role: executor.submit(self.run_step, role, payload) for role, payload in items}
            return {role: future.result() for role, future in futures.items()}

    def replay_step(self, role: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> AgentStepResult:
        """Exercise the runtime with an already-settled authoritative result."""

        normalized_role = canonical_role_name(role)
        with self._lock:
            self._replay_results.setdefault(normalized_role, []).append(dict(result))
        return self.run_step(normalized_role, payload)

    def fresh_single_agent_port(self) -> "RuntimeBackedAgentPort":
        return RuntimeBackedAgentPort(
            self._gateway,
            self._identity,
            self._task_id,
            preset_id="sequential-deliberation",
            selection=self._selection,
            role_model_defaults=self._role_model_defaults,
            role_instructions=self._role_instructions,
            is_cancelled=self._is_cancelled,
            event_sink=self._event_sink,
            session_root=self._session_root,
            host_multi_agent=False,
            tool_bridge=self._tool_bridge,
            allowed_tools_by_role=self._allowed_tools_by_role,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._controller.close(reason="runtime_port_closed")

    def record_shadow_failure(self, role: str, failure_type: str) -> None:
        """Persist a non-authoritative shadow failure through the existing sink."""

        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(RuntimeEvent(
                event_id=f"shadow-error-{uuid4().hex}",
                seq=0,
                task_id=self._task_id,
                workflow_id=self._workflow_id,
                session_id=self._root_session_id,
                agent_run_id=None,
                turn=None,
                step=None,
                type="runtime.error",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={
                    "source": "shadow",
                    "role": str(role)[:80],
                    "failure_type": str(failure_type)[:80],
                    "authoritative_result_preserved": True,
                },
            ))
        except Exception:
            # Shadow diagnostics must never alter the authoritative result.
            return

    def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        # Role JSON is an internal protocol result. It remains in the child
        # session needed for validation, but must not become OptChat's ordinary
        # assistant answer. Real provider reasoning is a separate event type.
        if event.type in {"assistant.text_delta", "assistant.message"}:
            return
        if self._event_sink is not None:
            self._event_sink(event)

    def _continuable(self, role: str) -> bool:
        canonical = canonical_role_name(role)
        return (
            self._preset.preset_id == "product-trader-loop" and canonical in {"Structurer", "Trader"}
        ) or (
            self._preset.preset_id == "independent-council" and canonical in {"Matcher", "Hedger"}
        )

    def _ensure_available(self) -> None:
        if self._closed:
            raise RuntimeError("RuntimeBackedAgentPort已关闭")
        if self._is_cancelled(self._identity, self._task_id):
            self._transport.cancel("parent_task_cancelled")
            raise RuntimeError("Parent Task已取消")

    def _prompt(self, role: str, payload: Mapping[str, Any]) -> str:
        return json.dumps(
            {"protocol": "optionhelper.recommender.step", "role": role, "input": dict(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _handle_model_request(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        prompt = str(request.get("prompt", ""))
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = None
        first_step = isinstance(envelope, Mapping) and envelope.get("protocol") == "optionhelper.recommender.step"
        wire_role = str(envelope.get("role", "")) if first_step else str(
            request.get("roleId", request.get("role_id", ""))
        )
        role = canonical_role_name(wire_role)
        payload = envelope.get("input") if first_step else {"continuation": prompt}
        if role not in self._preset.canonical_roles or not isinstance(payload, Mapping):
            raise ValidationError("Agent运行时角色或输入无效")
        context = {
            "operation": "recommender_fixed_step",
            "child_context": {
                "child_session_id": str(request.get("sessionId", request.get("session_id", ""))),
                "workflow_run_id": self._workflow_id,
                "agent_run_id": str(request.get("agentRunId", request.get("agent_run_id", ""))),
                "role": role,
                "preset": {"id": self._preset.preset_id, "revision": self._preset.revision},
                "one_shot": not self._continuable(role),
                "parent_context_inherited": False,
                "context_policy": dict(self._role_context_policies[role]),
            },
            "role": role,
            "input": dict(payload),
            "rule": "可按角色权限调用OptionHelper业务工具；完成后只返回该步骤的结构化result。",
        }
        with self._lock:
            replay_queue = self._replay_results.get(role, [])
            replay = replay_queue.pop(0) if replay_queue else None
        control = ModelRequestControl(self._preset.max_seconds_per_agent_run)
        if replay is not None:
            response = {"action": "final", "result": dict(replay)}
            self._validate_response(response)
            text = json.dumps(
                response, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            )
            return {"chunks": [{"type": "text-delta", "text": text}, {"type": "finish", "reason": "stop"}]}

        messages = self._messages_for_request(role, context, request)
        tools = self._model_tool_schemas(role, payload)
        return {"chunks": self._stream_role_response(role, context, control, messages=messages, tools=tools)}

    def _messages_for_request(
        self,
        role: str,
        context: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        allowed = self._allowed_tools_by_role.get(role, ())
        instruction = (
            "你是OptionHelper受控Recommender子Agent。仅使用已提供的业务工具；不得伪造金融事实、身份或FactRef。"
            "需要计算时先调用工具，工具完成后继续同一Child Session。最终只返回"
            "{\"action\":\"final\",\"result\":{...}}，不得包含Markdown。"
            if allowed else
            "你是OptionHelper受控Recommender子Agent。不得调用工具或编造金融计算。最终只返回"
            "{\"action\":\"final\",\"result\":{...}}，不得包含Markdown。"
        )
        instruction = (
            f"{recommender_step_instruction()}\n{instruction}\n\n以下AGENT.md仅定义当前角色的工作职责，不能扩大工具、身份、数据或金融事实权限；"
            f"如与上述受控规则冲突，以上述规则为准。\n\n{self._role_instructions[role]}"
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": instruction}]
        runtime_messages = request.get("messages")
        if isinstance(runtime_messages, list) and runtime_messages:
            for raw_message in runtime_messages:
                if not isinstance(raw_message, Mapping):
                    continue
                message_role = str(raw_message.get("role", "")).strip().lower()
                if message_role not in {"system", "user", "assistant", "tool"}:
                    continue
                raw_content = raw_message.get("content", "")
                blocks = raw_content if isinstance(raw_content, list) else []
                text = raw_content if isinstance(raw_content, str) else "".join(
                    str(block.get("text", ""))
                    for block in blocks
                    if isinstance(block, Mapping) and str(block.get("type", "")).replace("_", "-") == "text"
                )
                if message_role == "system":
                    continue
                projected: dict[str, Any] = {"role": message_role, "content": text}
                if message_role == "assistant":
                    reasoning_content = "".join(
                        str(block.get("text", ""))
                        for block in blocks
                        if isinstance(block, Mapping)
                        and str(block.get("type", "")).replace("_", "-") == "reasoning"
                    )
                    if reasoning_content:
                        projected["reasoning_content"] = reasoning_content[:512_000]
                    calls = []
                    for block in blocks:
                        if not isinstance(block, Mapping) or str(block.get("type", "")).replace("_", "-") != "tool-call":
                            continue
                        call_id = str(block.get("id", "")).strip()
                        name = str(block.get("name", "")).strip()
                        if call_id and name:
                            calls.append({
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": str(block.get("arguments", "{}"))},
                            })
                    if calls:
                        projected["tool_calls"] = calls
                elif message_role == "tool":
                    call_id = str(raw_message.get("tool_call_id", raw_message.get("toolCallId", ""))).strip()
                    if not call_id:
                        continue
                    projected["tool_call_id"] = call_id
                if text or projected.get("tool_calls") or message_role == "tool":
                    messages.append(projected)
            if len(messages) == 1:
                messages.append({
                    "role": "user",
                    "content": json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), default=str),
                })
            return messages
        surface = request.get("surface")
        events = surface.get("events", []) if isinstance(surface, Mapping) else []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, Mapping):
                continue
            event_type = str(event.get("type", ""))
            data = event.get("data")
            if not isinstance(data, Mapping):
                continue
            if event_type == "user/message" and isinstance(data.get("text"), str):
                content = str(data["text"])
                try:
                    candidate = json.loads(content)
                except json.JSONDecodeError:
                    candidate = None
                if isinstance(candidate, Mapping) and candidate.get("protocol") == "optionhelper.recommender.step":
                    historical_context = {
                        "operation": "recommender_fixed_step",
                        "role": str(candidate.get("role", role)),
                        "input": dict(candidate.get("input", {})) if isinstance(candidate.get("input"), Mapping) else {},
                        "rule": "可按角色权限调用OptionHelper业务工具；完成后只返回该步骤的结构化result。",
                    }
                    content = json.dumps(historical_context, ensure_ascii=False, separators=(",", ":"), default=str)
                messages.append({"role": "user", "content": content})
            elif event_type == "assistant/message":
                text = str(data.get("text", ""))
                calls = data.get("toolCalls", [])
                message: dict[str, Any] = {"role": "assistant", "content": text}
                if isinstance(calls, list) and calls:
                    message["tool_calls"] = [
                        {
                            "id": str(call.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name", "")),
                                "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                        for call in calls if isinstance(call, Mapping) and call.get("id") and call.get("name")
                    ]
                if text or message.get("tool_calls"):
                    messages.append(message)
            elif event_type in {"tool/completed", "tool/failed", "tool/cancelled"}:
                call_id = data.get("toolCallId")
                if isinstance(call_id, str) and call_id:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(data.get("result"), ensure_ascii=False, separators=(",", ":"), default=str),
                    })
        if len(messages) == 1:
            messages.append({
                "role": "user",
                "content": json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), default=str),
            })
        return messages

    def _model_tool_schemas(
        self,
        role: str,
        payload: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        allowed = self._allowed_tools_for_request(role, payload)
        if not allowed or self._tool_bridge is None:
            return []
        factory = getattr(self._tool_bridge, "model_tool_schemas", None)
        if not callable(factory):
            raise ValidationError("RuntimeToolBridge缺少模型工具Schema投影")
        schemas = factory(list(allowed))
        if not isinstance(schemas, list):
            raise ValidationError("RuntimeToolBridge返回的模型工具Schema无效")
        return schemas

    def _allowed_tools_for_request(
        self,
        role: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, ...]:
        allowed = tuple(self._allowed_tools_by_role.get(role, ()))
        if self._preset.preset_id != "independent-council" or role not in {"Matcher", "Hedger"}:
            return allowed
        if isinstance(payload.get("candidate_versions"), list):
            return tuple(name for name in allowed if name != "search_option_structures")
        return tuple(name for name in allowed if name == "search_option_structures")

    def _stream_role_response(
        self,
        role: str,
        context: Mapping[str, Any],
        control: ModelRequestControl,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterable[Mapping[str, Any]]:
        """Stream a role response, then validate the complete JSON envelope."""

        stopped = Event()
        watcher = Thread(
            target=self._watch_model_request_cancellation,
            args=(control, stopped),
            name="optionhelper-runtime-model-cancel",
            daemon=True,
        )
        watcher.start()
        text_parts: list[str] = []
        finish_reason: str | None = None
        usage: Mapping[str, Any] | None = None
        saw_finish = False
        saw_tool_call = False
        try:
            for chunk in self._role_model_stream(role, context, control, messages=messages, tools=tools):
                control.refresh_deadline(self._preset.max_seconds_per_agent_run)
                control.raise_if_cancelled()
                kind = str(
                    getattr(chunk, "type", None)
                    if not isinstance(chunk, Mapping)
                    else chunk.get("type", "text_delta")
                ).replace("-", "_")
                if kind in {"text", "text_delta", "content_delta"}:
                    text = str(
                        getattr(chunk, "delta", "")
                        if not isinstance(chunk, Mapping)
                        else chunk.get("delta", chunk.get("content", chunk.get("text", "")))
                    )
                    if text:
                        text_parts.append(text)
                        yield {"type": "text-delta", "text": text}
                elif kind in {"reasoning", "reasoning_delta"}:
                    text = str(
                        getattr(chunk, "delta", "")
                        if not isinstance(chunk, Mapping)
                        else chunk.get("delta", chunk.get("content", chunk.get("text", "")))
                    )
                    metadata = getattr(chunk, "metadata", {}) if not isinstance(chunk, Mapping) else chunk
                    yield {
                        "type": "reasoning-delta",
                        "text": text,
                        "available": bool(text) or (
                            isinstance(metadata, Mapping) and metadata.get("available") is True
                        ),
                    }
                elif kind in {"tool_call", "tool_call_delta"}:
                    saw_tool_call = True
                    metadata = getattr(chunk, "metadata", {}) if not isinstance(chunk, Mapping) else chunk
                    if not isinstance(metadata, Mapping):
                        raise ValidationError("模型工具调用增量格式无效")
                    # An omitted name continues the existing call. Sending null
                    # would overwrite it with "undefined" in the Host assembler.
                    yield {
                        "type": "tool-call-delta",
                        **{key: metadata[key] for key in ("id", "index", "name")
                           if metadata.get(key) is not None},
                        "arguments": metadata.get("arguments", ""),
                    }
                elif kind == "usage":
                    usage = getattr(chunk, "usage", None) if not isinstance(chunk, Mapping) else chunk.get("usage")
                    yield {"type": "usage", "usage": dict(usage or {})}
                elif kind == "finish":
                    saw_finish = True
                    finish_reason = (
                        getattr(chunk, "finish_reason", None)
                        if not isinstance(chunk, Mapping)
                        else chunk.get("finish_reason", chunk.get("reason"))
                    )

            if not saw_finish:
                raise ValidationError("模型流未正常结束，不能采用部分JSON")
            if saw_tool_call:
                yield {
                    "type": "finish",
                    "reason": str(finish_reason or "tool_calls"),
                    "usage": dict(usage or {}),
                }
                return
            try:
                value = json.loads("".join(text_parts))
            except (json.JSONDecodeError, TypeError) as error:
                raise ValidationError("Agent角色未返回严格JSON") from error
            self._validate_response(value)
            yield {
                "type": "finish",
                "reason": str(finish_reason or "stop"),
                "usage": dict(usage or {}),
            }
        finally:
            stopped.set()
            watcher.join(timeout=1)

    def _role_model_stream(
        self,
        role: str,
        context: Mapping[str, Any],
        control: ModelRequestControl,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterable[object]:
        role = canonical_role_name(role)
        message_stream = getattr(self._gateway, "stream_messages_for", None)
        legacy_stream = getattr(self._gateway, "stream_for", None)
        if not callable(message_stream) and not callable(legacy_stream):
            if tools:
                raise UnavailableCapabilityError(
                    "模型工具调用",
                    "当前模型Gateway不支持保留消息边界的工具循环。",
                )
            yield from self._fallback_role_decision(role, context, control)
            return
        if tools and not callable(message_stream):
            raise UnavailableCapabilityError(
                "模型工具调用",
                "当前模型Gateway不支持保留消息边界的工具循环。",
            )
        emitted = False
        try:
            for chunk in self._model_proxy.stream(
                self._role_routes[role], messages, request_control=control, tools=tools,
            ):
                emitted = True
                yield chunk
        except UnavailableCapabilityError as error:
            # Only the explicit absence of a stream adapter may use the
            # existing strict JSON decision path. Network, auth and provider
            # failures remain failures and never fall back silently.
            if tools or emitted or getattr(error, "capability", "") != "模型流式输出":
                raise
            yield from self._fallback_role_decision(role, context, control)

    def _fallback_role_decision(
        self,
        role: str,
        context: Mapping[str, Any],
        control: ModelRequestControl,
    ) -> Iterable[Mapping[str, Any]]:
        role = canonical_role_name(role)
        response = self._gateway.decide_for(
            self._identity,
            self._task_id,
            context,
            selection=self._route_selections[role],
            request_control=control,
        )
        self._validate_response(response)
        yield {
            "type": "text-delta",
            "text": json.dumps(dict(response), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        }
        yield {"type": "finish", "reason": "stop"}

    def _watch_model_request_cancellation(self, control: ModelRequestControl, stopped: Event) -> None:
        while not stopped.wait(0.02):
            if self._closed:
                control.cancel("runtime_closed")
                return
            if self._is_cancelled(self._identity, self._task_id):
                control.cancel("parent_task_cancelled")
                return

    @staticmethod
    def _validate_response(value: object) -> None:
        if not isinstance(value, Mapping) or set(value) != {"action", "result"}:
            raise ValidationError("子Agent必须返回action与result")
        if value.get("action") != "final" or not isinstance(value.get("result"), Mapping):
            raise ValidationError("子Agent必须返回final结构化result")

    def _result(self, snapshot: Mapping[str, Any], role: str) -> dict[str, Any]:
        raw_result = snapshot.get("result")
        if isinstance(raw_result, Mapping) and isinstance(raw_result.get("text"), str):
            text = raw_result["text"]
        elif isinstance(snapshot.get("text"), str):
            # ``subagent.followup`` returns the runtime result directly while
            # ``subagent.start`` wraps it in Controller's ``result`` field.
            text = snapshot["text"]
        else:
            raise ValidationError(f"Agent角色{role}缺少最终文本")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValidationError(f"Agent角色{role}未返回严格JSON") from error
        self._validate_response(value)
        return dict(value["result"])


class ShadowRuntimeAgentPort(AgentPort):
    """Keep the current AgentPort authoritative and replay it asynchronously."""

    def __init__(
        self,
        authoritative: AgentPort,
        runtime: RuntimeBackedAgentPort,
        *,
        event_sink: Callable[[RuntimeEvent], object] | None = None,
    ) -> None:
        self._authoritative = authoritative
        self._runtime = runtime
        self._event_sink = event_sink
        self._shadow_failures: list[str] = []
        self._threads: set[Thread] = set()
        self._lock = RLock()
        self._closed = False

    @property
    def shadow_failures(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._shadow_failures)

    def capability(self) -> ModelCapability:
        return self._authoritative.capability()

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult:
        result = self._authoritative.run_step(role, payload)
        self._start_shadow(role, payload, result.result)
        return result

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]:
        results = self._authoritative.run_named_steps(requests)
        for role, result in results.items():
            self._start_shadow(role, requests[role], result.result)
        return results

    def fresh_single_agent_port(self) -> AgentPort:
        factory = getattr(self._authoritative, "fresh_single_agent_port", None)
        return factory() if callable(factory) else self._authoritative

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = tuple(self._threads)
            self._threads.clear()
        # No join: shadow work is diagnostic and must never delay the
        # authoritative Recommender response or teardown. Existing workers
        # finish in a daemon finalizer, which closes the runtime after a short
        # bounded grace period even if a replay provider is stuck.
        try:
            close = getattr(self._authoritative, "close", None)
            if callable(close):
                close()
        finally:
            if threads:
                Thread(
                    target=self._finish_shadow_close,
                    args=(threads,),
                    name="optionhelper-runtime-shadow-close",
                    daemon=True,
                ).start()
            else:
                self._runtime.close()

    def _finish_shadow_close(self, threads: tuple[Thread, ...]) -> None:
        deadline = monotonic() + 5.0
        for thread in threads:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        try:
            self._runtime.close()
        except Exception:
            return

    def _start_shadow(self, role: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed:
                self._record_failure(role, "closed_before_replay")
                return
            thread = Thread(
                target=self._run_shadow,
                args=(role, dict(payload), dict(result)),
                name="optionhelper-runtime-shadow",
                daemon=True,
            )
            self._threads.add(thread)
            try:
                # Start while holding the lock so close() cannot capture an
                # unstarted Thread and race its finalizer join.
                thread.start()
                return
            except Exception as error:
                self._threads.discard(thread)
                # Python clears the exception binding when this block ends.
                failure_type = type(error).__name__
        self._record_failure(role, failure_type)

    def _run_shadow(self, role: str, payload: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        current = current_thread()
        try:
            replayed = self._runtime.replay_step(role, payload, result)
            if dict(replayed.result) != dict(result):
                self._record_failure(role, "result_mismatch")
        except Exception as error:
            self._record_failure(role, type(error).__name__)
        finally:
            with self._lock:
                self._threads.discard(current)

    def _record_failure(self, role: str, failure_type: str) -> None:
        entry = f"{role}:{failure_type}"
        with self._lock:
            self._shadow_failures.append(entry)
        reporter = getattr(self._runtime, "record_shadow_failure", None)
        if callable(reporter):
            try:
                reporter(role, failure_type)
            except Exception:
                pass
            return
        if self._event_sink is not None:
            try:
                self._event_sink(RuntimeEvent(
                    event_id=f"shadow-error-{uuid4().hex}",
                    seq=0,
                    task_id="shadow-task",
                    workflow_id="shadow-workflow",
                    session_id="shadow-session",
                    agent_run_id=None,
                    turn=None,
                    step=None,
                    type="runtime.error",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    payload={
                        "source": "shadow",
                        "role": str(role)[:80],
                        "failure_type": str(failure_type)[:80],
                        "authoritative_result_preserved": True,
                    },
                ))
            except Exception:
                pass


__all__ = ["RuntimeBackedAgentPort", "ShadowRuntimeAgentPort"]
