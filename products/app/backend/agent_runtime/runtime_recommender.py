"""Recommender AgentPort backed by the OptionHelper child runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
from runtime.research_policy import depth_policy, preset_definitions, role_tools, workflow_limits
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


def role_input_handoffs(role, payload, preset_id, completed_roles):
    """Describe only predecessor results actually present in the role input."""
    value = payload.get("input", {})
    if not isinstance(value, Mapping):
        return []
    sources = []
    if role == "Structurer" and value.get("phase") == "rework":
        if value.get("reviewer_feedback"): sources.append(("Reviewer", "return", "终审意见交回修订"))
        elif value.get("trader_feedback"): sources.append(("Trader", "return", "交易评估意见交回修订"))
    elif role == "Trader" and (value.get("round") or value.get("candidates") or
                              (value.get("comparison_only") and value.get("proposals"))):
        sources.append(("Structurer", "handoff", "交付候选方案"))
    elif role in {"Matcher", "Hedger"}:
        if value.get("discussion"): sources.append(("Moderator", "return", "交付定向复核问题"))
        elif value.get("phase") == "proposal" and value.get("framing"):
            sources.append(("Framer", "handoff", "交付研究约束"))
    elif role == "Moderator":
        for key, source in (("matcher", "Matcher"), ("hedger", "Hedger")):
            if isinstance(value.get(key), Mapping): sources.append((source, "handoff", "提交研究意见与证据"))
    elif role == "Generator" and value.get("ranking_spec"):
        sources.append(("Reviewer", "return", "补充候选") if value.get("reviewer_feedback") else ("Specifier", "handoff", "交付筛选与排序规则"))
    elif role == "Evaluator" and value.get("candidate"):
        sources.append(("Reviewer", "return", "交付补评意见") if value.get("reviewer_feedback") else ("Generator", "handoff", "交付待评估候选"))
    elif role == "Reviewer" and "candidates" in value:
        if preset_id == "product-trader-loop": sources.append(("Trader", "handoff", "提交候选与评估证据"))
        elif preset_id == "constraint-ranking" and "ranking_decisions" in value:
            sources.append(("Evaluator", "handoff", "提交证据与代码排序结果"))
    return [{"from_role":source,"to_role":role,"kind":kind,"label":label}
            for source,kind,label in sources if source in completed_roles]


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
        research_depth: str = "standard",
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
        self._research_depth = research_depth
        self._depth_policy = depth_policy(research_depth)
        if preset_id != "sequential-deliberation":
            # Covers role turns and targeted reviews, independently of compute count.
            role_budget = self._depth_policy["role_turns"]
            self._preset = replace(self._preset, max_agent_runs=role_budget, workflow_total_budget=role_budget)
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
        shared_tools = role_tools(self._preset.preset_id)
        selected_tools = shared_tools if allowed_tools_by_role is None else allowed_tools_by_role
        self._allowed_tools_by_role = {}
        for role, tools in selected_tools.items():
            canonical = canonical_role_name(role)
            permitted = shared_tools.get(canonical)
            if permitted is None or set(tools).difference(permitted):
                raise ValidationError("角色工具权限不能超出共享预设")
            self._allowed_tools_by_role[canonical] = tuple(tools)
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

    def bind_research_session(self, research):
        if self._tool_bridge is not None:
            self._tool_bridge.bind_research_session(research)
        self._research = research

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
        limits = workflow_limits(self._preset.preset_id, self._research_depth)
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
                "stages": preset_definitions()[self._preset.preset_id]["stages"],
            },
            max_parallel_agents=max_parallel,
            max_iterations=limits["rounds"],
            max_rounds=limits["rounds"],
            max_rework_rounds=limits["reworks"],
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
            self._request_record_start = len(self._run_records)

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

    def _publish_input_handoffs(self, role, payload):
        sink = self._event_sink
        if sink is None:
            return
        with self._lock:
            predecessors = {canonical_role_name(record["role"]) for record in self._run_records[getattr(self, "_request_record_start", 0):]
                            if record.get("status") == "completed"}
        for handoff in role_input_handoffs(role, payload, self._preset.preset_id, predecessors):
            identifier = f"handoff-{uuid4().hex}"
            try:
                sink(RuntimeEvent(
                    event_id=identifier, seq=0, task_id=self._task_id,
                    workflow_id=self._workflow_id, session_id=self._root_session_id,
                    agent_run_id=None, turn=None, step=None, type="workflow.handoff",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    payload={**handoff,"handoff_id":identifier,"status":"running","via":"OH","runtime_scope":"recommender"},
                ))
            except Exception:
                # Progress decoration must never alter a research decision.
                continue

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
        self._publish_input_handoffs(normalized_role, payload)
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
        return canonical_role_name(role) in preset_definitions()[self._preset.preset_id]['continuable_roles']

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

    def _research_checkpoint(self, request):
        """Rebuild only public, role-authorized state at a completed tool boundary."""
        from copy import deepcopy
        self._ensure_available()
        envelope = json.loads(str(request.get("prompt", "")))
        role = canonical_role_name(envelope.get("role", ""))
        if role not in self._preset.canonical_roles or envelope.get("protocol") != "optionhelper.recommender.step":
            raise ValidationError("研究检查点角色或协议无效")
        research = getattr(self, '_research', None) or getattr(self._tool_bridge, '_research', None)
        if research is None or research.case.task_id != self._task_id or research.case.tenant_id != self._identity.tenant_id:
            raise ValidationError("研究检查点不属于当前活动任务")
        state = research.continuation_snapshot(role)
        prior = envelope.get('input', {}).get('research_checkpoint', {})
        pages = list(prior.get('read_pages', [])) if isinstance(prior, Mapping) else []
        notes = list(prior.get('public_notes', [])) if isinstance(prior, Mapping) else []
        queries = list(prior.get('public_questions', [])) if isinstance(prior, Mapping) else []
        valid_refs = [(row.get('candidate_id'), ref) for row in state['evidence']
                      for ref in row.get('module_run_refs', [])]
        authorized_ids = {row['candidate_id'] for row in state['candidates']}
        calls = {}
        messages = request.get('publicMessages', [])
        start = max((i for i, row in enumerate(messages) if row.get('role') == 'user'), default=0)
        for message in messages[start + 1:]:
            for block in message.get('content', []):
                kind = str(block.get('type', '')).replace('_', '-')
                if kind == 'reasoning':
                    raise ValidationError('研究检查点不得接收私有推理')
                if kind == 'tool-call':
                    arguments = json.loads(block.get('arguments', '{}'))
                    if arguments.get('candidate_id') and arguments['candidate_id'] not in authorized_ids:
                        raise ValidationError('研究检查点含未授权候选')
                    calls[block['id']] = (block.get('name'), arguments)
                    if block.get('name') == 'evaluate_research_candidate':
                        queries.append(json.loads(block.get('arguments', '{}')).get('question', ''))
                elif kind == 'text' and message.get('role') == 'assistant' and block.get('text'):
                    notes.append({'source': 'public_assistant_statement', 'text': block['text']})
                elif kind == 'text' and message.get('role') == 'tool':
                    call_id = message.get('tool_call_id', message.get('toolCallId'))
                    if call_id not in calls:
                        raise ValidationError('研究检查点工具交换不完整')
                    tool, arguments = calls.pop(call_id)
                    wrapper = json.loads(block.get('text', '{}'))
                    page = wrapper.get('result', {}).get('result', {})
                    if tool == 'read_research_evidence' and page.get('source') == 'verified_frozen_result':
                        ref = page.get('module_run_ref')
                        locator = {'candidate_id': arguments.get('candidate_id'), 'module': arguments.get('module'),
                                   'result_path': page.get('result_path', []), 'offset': page.get('offset', 0),
                                   'limit': arguments.get('limit', 64), 'expected_run_ref': ref}
                        pages.append({'arguments': locator, 'status': page.get('status'),
                                      'page_metadata': {key: value for key, value in page.items() if key not in {'value','module_run_ref','deferred_fields','value_indices','request_id'}}})
                        def warnings(value):
                            if isinstance(value, list):
                                for row in value: warnings(row)
                            elif isinstance(value, dict):
                                for key, row in value.items():
                                    if key in {'warnings','warning','limitations','error','missing_information','messages'} and row:
                                        notes.append({'source': 'read_page', 'arguments': locator, key: deepcopy(row)})
                                    else: warnings(row)
                        warnings(page)
                    elif tool == 'read_research_evidence' and page.get('source') == 'frozen_stage_input':
                        notes.append({'source': 'read_stage_input', 'stage_ref': page.get('stage_ref'),
                                      'result_path': page.get('result_path', []), 'status': page.get('status')})
                    elif tool != 'evaluate_research_candidate' or wrapper.get('status') != 'completed':
                        notes.append({'source': 'tool_result', 'tool': tool, 'arguments': arguments, 'result': wrapper})
        if calls:
            raise ValidationError('研究检查点仍有未完成工具调用')
        def unique(values):
            return list({json.dumps(value, sort_keys=True, ensure_ascii=False): value for value in values}.values())
        retained = []
        for page in unique([{**page, 'page_metadata': {key: value for key, value in page.get('page_metadata', {}).items() if key != 'request_id'}} for page in pages]):
            args = page['arguments']
            if (args['candidate_id'], args['expected_run_ref']) in valid_refs:
                retained.append(page)
            else:
                notes.append({'source': 'stale_read_reference', 'candidate_id': args['candidate_id'],
                              'message': '候选或来源已改变，此旧页引用不能用于当前结论'})
        # Keep the latest completed tool exchange visible for the next model request.
        # Older exchange bodies remain in the original session and frozen ResultStore.
        latest_start = max((i for i, message in enumerate(messages[start + 1:], start + 1)
                            if message.get('role') == 'assistant' and any(
                                str(block.get('type', '')).replace('_', '-') == 'tool-call'
                                for block in message.get('content', []))), default=len(messages))
        latest_exchange = deepcopy(messages[latest_start:])
        # Store structured JSON once rather than recursively escaping tool JSON strings.
        for message in latest_exchange:
            if message.get('role') == 'tool':
                for block in message.get('content', []):
                    if block.get('type') == 'text':
                        try:
                            block['json_value'] = json.loads(block['text'])
                        except (TypeError, ValueError):
                            continue
                        block['type'] = 'json'
                        del block['text']
        envelope = deepcopy(envelope)
        phase_input = envelope['input'].get('input')
        if isinstance(phase_input, dict) and 'stage_history' not in phase_input and any(
                key in phase_input for key in ('own_proposals', 'registered_candidates')):
            reference = research.freeze_stage_input(role, phase_input)
            # Current contracts/evidence are in Host state. Preserve proposal reasoning,
            # suitability, and unresolved information explicitly because state lacks them.
            source_candidates = phase_input.get('registered_candidates', [])
            current = {row['candidate_id']: row for row in state['candidates']}
            contexts = []
            for row in source_candidates:
                if not isinstance(row, Mapping):
                    continue
                active = current.get(row.get('candidate_id'))
                if active is None or row.get('current_inputs') != active.get('current_inputs'):
                    continue
                contexts.append({key: deepcopy(value) for key, value in row.items() if key in {
                    'candidate_id', 'product_id', 'product_name', 'reason', 'main_risks',
                    'missing_inputs', 'suitable_for', 'not_suitable_for', 'library_status', 'key_terms'}})
            for key in ('own_proposals', 'registered_candidates', 'catalog_references', 'research'):
                phase_input.pop(key, None)
            phase_input['candidate_context'] = contexts
            phase_input['stage_history'] = {'stage_ref': reference,
                'tool': 'read_research_evidence',
                'read_paths': [['own_proposals'], ['registered_candidates'], ['catalog_references'], ['research']],
                'reading_rule': '按所需字段传stage_ref及result_path定向读取，不必重新读入全部历史阶段。',
                'contents': '本角色本阶段完整原始输入，含原提案、登记记录、目录定位。当前合同和有效事实以research_checkpoint.state为准。'}
        if (role == 'Moderator' and isinstance(phase_input, dict) and 'stage_history' not in phase_input
                and isinstance(phase_input.get('matcher'), Mapping) and isinstance(phase_input.get('hedger'), Mapping)):
            reference = research.freeze_stage_input(role, phase_input)
            # Keep both roles' complete conclusions, including disagreements and limitations.
            # Only remove evidence already present byte-for-value in current Host state.
            phase_input['research_evidence'] = [row for row in phase_input.get('research_evidence', [])
                                                if row not in state['evidence']]
            evidence = phase_input.get('evidence', [])
            archive_index = [{'product_id': row.get('product_id'), 'source': row.get('source'),
                              'evidence_id': row.get('evidence_id'), 'result_path': ['evidence', index]}
                             for index, row in enumerate(evidence)
                             if isinstance(row, Mapping) and row.get('product_id')]
            # Product catalogue text is frozen reference material, not current contract state.
            # Keep non-product material inline; every product entry remains exactly retrievable.
            phase_input['evidence'] = [row for row in evidence if not isinstance(row, Mapping)
                                      or not row.get('product_id')]
            phase_input['stage_history'] = {'stage_ref': reference, 'tool': 'read_research_evidence',
                'catalog_index': archive_index,
                'reading_rule': '共同约束、两角色完整结论和Host当前完整合同在正文；目录各来源原文用stage_ref与对应result_path按需读取，不必全部重复读取。',
                'contents': '本Moderator阶段完整冻结输入。已计算事实以research_checkpoint.state.evidence为准；历史原文不代替当前条款。'}
        # Initial independent-role inputs also contain full catalogue bodies.
        # Freeze them once, keeping constraints and role conclusions inline.
        if isinstance(phase_input, dict) and isinstance(phase_input.get('evidence'), list):
            evidence = phase_input['evidence']
            product_rows = [(index, row) for index, row in enumerate(evidence)
                            if isinstance(row, Mapping) and row.get('product_id')]
            if product_rows:
                reference = research.freeze_stage_input(role, {'evidence': evidence}, candidate_bound=False)
                phase_input['evidence'] = [row for row in evidence
                                          if not isinstance(row, Mapping) or not row.get('product_id')]
                phase_input['catalog_history'] = {
                    'stage_ref': reference, 'tool': 'read_research_evidence',
                    'catalog_index': [{**{key: deepcopy(value) for key, value in row.items()
                        if key in {'evidence_id', 'product_id', 'source', 'section', 'library_status', 'entry_status'}},
                        'result_path': ['evidence', index]} for index, row in product_rows],
                    'reading_rule': '目录原文完整保留，按stage_ref和result_path读取所需条目。不能根据索引猜测条款。',
                }
        # Catalogue searches are reference material. Keep their exact source in
        # the existing role-scoped store, not again inside every checkpoint.
        # The latest exchange above still carries the just-read complete text.
        indexed_notes = []
        for note in notes:
            wrapper = note.get('result', {}) if isinstance(note, Mapping) else {}
            result = wrapper.get('result', {}).get('result', {}) if isinstance(wrapper, Mapping) else {}
            if (note.get('source') == 'tool_result' and note.get('tool') == 'search_option_structures'
                    and wrapper.get('status') == 'completed' and result.get('ok') is True):
                reference = research.freeze_stage_input(role, wrapper, candidate_bound=False)
                indexed_notes.append({
                    'source': 'catalog_search', 'arguments': note.get('arguments', {}),
                    'status': 'completed', 'coverage': deepcopy(result.get('coverage', {})),
                    'stage_ref': reference, 'tool': 'read_research_evidence',
                    'catalog_index': [{**{key: deepcopy(value) for key, value in row.items()
                        if key in {'evidence_id', 'product_id', 'source', 'section', 'library_status', 'entry_status'}},
                        'result_path': ['result', 'result', 'evidence', index]}
                        for index, row in enumerate(result.get('evidence', [])) if isinstance(row, Mapping)],
                    'reading_rule': '目录原文完整保存。按stage_ref和目录条目的result_path读取；不必重复查询。',
                })
            else:
                indexed_notes.append(note)
        notes = indexed_notes
        envelope['input']['latest_complete_tool_exchange'] = latest_exchange
        envelope['input']['research_checkpoint'] = {
            'source': 'host_verified_research_state', 'state': state,
            'read_pages': retained, 'public_notes': unique(notes), 'public_questions': unique(queries),
            'continuation_rule': '继续同一研究角色与轮次；约束及当前候选以Host状态为准。latest_complete_tool_exchange保留刚完成工具调用及完整结果，应先使用其中正文；无需仅为找回刚读正文重复读取。已读页按精确RunRef取回；公开问题不等于已解决。不得重新计算已有有效结果，不能把缺失或失败当作完成。',
        }
        return {'text': json.dumps(envelope, ensure_ascii=False, separators=(',', ':'), allow_nan=False)}

    def _handle_model_request(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.get("operation") == "research_checkpoint":
            return self._research_checkpoint(request)
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
        research = getattr(self, '_research', None) or getattr(self._tool_bridge, '_research', None)
        if research is not None:
            context = research.model_view(context)
            for message in messages:
                if message['role'] not in {'user', 'tool'}:
                    continue
                try:
                    content = json.loads(message['content'])
                except (TypeError, json.JSONDecodeError):
                    continue
                message['content'] = json.dumps(research.model_view(content), ensure_ascii=False,
                                                separators=(',', ':'), allow_nan=False)
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
        from runtime.research_presentation import MODEL_FINANCIAL_READING_RULE
        from runtime.knowledger.interpretation import KNOWLEDGE_REASONING_RULES
        instruction += MODEL_FINANCIAL_READING_RULE + "\n" + KNOWLEDGE_REASONING_RULES
        instruction = (
            f"{recommender_step_instruction()}\n{instruction}\n\n以下AGENT.md仅定义当前角色的工作职责，不能扩大工具、身份、数据或金融事实权限；"
            f"如与上述受控规则冲突，以上述规则为准。\n\n{self._role_instructions[role]}\n\n"
            '交付协议：角色文档和required_output所列字段全部放在result内部。'
            '最外层只允许action和result，action固定为final；不能省略这层封装。'
            'output_schema、required_output和financial_value_contract属于说明，不得复制为业务输出字段。'
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
        research = getattr(self._tool_bridge, "_research", None)
        if research is not None and research.preset_id == "independent-council":
            if role in {"Matcher", "Hedger"} and not research.has_registered_candidates(role):
                return tuple(name for name in allowed if name not in {
                    "evaluate_research_candidate", "read_research_evidence",
                })
        return allowed

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
                        if saw_tool_call:
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
                    if not saw_tool_call and text_parts:
                        yield {"type": "text-delta", "text": "".join(text_parts)}
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
            final_text = self._close_complete_json_containers("".join(text_parts), finish_reason)
            try:
                value = json.loads(final_text)
            except json.JSONDecodeError as error:
                final_text, repair_usage = yield from self._repair_role_json(
                    role, context, control, messages, final_text, error,
                )
                usage = {key: (usage or {}).get(key, 0) + repair_usage.get(key, 0)
                         for key in set(usage or {}) | set(repair_usage)}
                value = json.loads(final_text)
            self._validate_response(value)
            yield {"type": "text-delta", "text": final_text}
            yield {
                "type": "finish",
                "reason": str(finish_reason or "stop"),
                "usage": dict(usage or {}),
            }
        finally:
            stopped.set()
            watcher.join(timeout=1)

    def _repair_role_json(
        self, role, context, control, messages, invalid_text, error,
    ):
        """Allow one quote-escaping retry by the same role, with no business tools."""
        repair_messages = [*messages, {
            "role": "user",
            "content": (
                "上一条角色最终输出不是合法JSON。只允许在未转义的字符串内双引号前添加反斜杠，"
                "其余字符、空白、键顺序和标点必须逐字保留，不重新排版、不改逗号或括号。"
                "返回完整action=final及result对象，不改变条款、数值、证据编号或判断，"
                "不得调用任何工具。若原内容无法保持，不得编造。"
                f"语法错误：{error.msg}，字符位置：{error.pos}。待修正内容：\n{invalid_text}"
            ),
        }]
        control.raise_if_cancelled()
        text_parts = []
        repair_usage = {}
        finished = False
        try:
            for chunk in self._role_model_stream(role, context, control, messages=repair_messages, tools=[]):
                control.refresh_deadline(self._preset.max_seconds_per_agent_run)
                control.raise_if_cancelled()
                kind = str(getattr(chunk, "type", None) if not isinstance(chunk, Mapping)
                           else chunk.get("type", "text_delta")).replace("-", "_")
                if kind in {"tool_call", "tool_call_delta"}:
                    raise ValidationError("JSON格式修复不得调用业务工具")
                if kind in {"text", "text_delta", "content_delta"}:
                    text_parts.append(str(getattr(chunk, "delta", "") if not isinstance(chunk, Mapping)
                                          else chunk.get("delta", chunk.get("content", chunk.get("text", "")))))
                elif kind == "usage":
                    usage = getattr(chunk, "usage", None) if not isinstance(chunk, Mapping) else chunk.get("usage")
                    repair_usage = dict(usage or {})
                    yield {"type": "usage", "usage": repair_usage}
                elif kind == "finish":
                    finished = True
        except Exception:
            yield self._json_failure_diagnostic(role, invalid_text, "".join(text_parts), error, "repair_stream_failed")
            raise
        if not finished:
            yield self._json_failure_diagnostic(role, invalid_text, "".join(text_parts), error, "repair_stream_incomplete")
            raise ValidationError("JSON格式修复流未正常结束")
        final_text = "".join(text_parts)
        if not self._only_added_quote_escapes(invalid_text, final_text):
            yield self._json_failure_diagnostic(role, invalid_text, final_text, error, "repair_changed_content")
            raise ValidationError("JSON修复改变了原始内容，拒绝采用")
        try:
            value = json.loads(final_text)
        except json.JSONDecodeError as repaired_error:
            yield self._json_failure_diagnostic(role, invalid_text, final_text, error, "repair_json_invalid")
            raise ValidationError("Agent角色一次格式修复后仍未返回严格JSON") from repaired_error
        try:
            self._validate_response(value)
        except Exception:
            yield self._json_failure_diagnostic(role, invalid_text, final_text, error, "repair_response_invalid")
            raise
        return final_text, repair_usage

    @staticmethod
    def _close_complete_json_containers(text: str, finish_reason: object) -> str:
        """Close containers at EOF only; never complete a string, value or stopped stream."""
        if finish_reason not in {'stop', 'end_turn'}:
            return text
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError as error:
            if error.pos != len(text.rstrip()) or error.msg != "Expecting ',' delimiter":
                return text
        stack = []
        quoted = escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == chr(92):
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in '{[':
                stack.append('}' if character == '{' else ']')
            elif character in '}]':
                if not stack or stack.pop() != character:
                    return text
        if quoted or not stack:
            return text
        closed = text + ''.join(reversed(stack))
        try:
            json.loads(closed)
        except json.JSONDecodeError:
            return text
        return closed

    @staticmethod
    def _json_failure_diagnostic(role, original, repaired, syntax_error, failure):
        # Only the two public text buffers are allowed here, never messages/context/reasoning.
        return {"type": "json-diagnostic", "diagnostic": {
            "source": "untrusted_model_draft", "role": str(role), "failure": failure,
            "original_public_output": original, "repaired_public_output": repaired,
            "syntax_position": syntax_error.pos, "syntax_line": syntax_error.lineno,
            "syntax_column": syntax_error.colno, "syntax_error": syntax_error.msg,
        }}

    @staticmethod
    def _only_added_quote_escapes(original: str, repaired: str) -> bool:
        """Accept only insertion of a backslash directly before an existing quote."""
        left = right = 0
        while left < len(original) and right < len(repaired):
            if original[left] == repaired[right]:
                left += 1
                right += 1
            elif original[left] == '"' and repaired[right:right + 2] == '\\"':
                right += 1
            else:
                return False
        return left == len(original) and right == len(repaired)

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
