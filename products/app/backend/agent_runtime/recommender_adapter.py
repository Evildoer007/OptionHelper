"""In-process App adapters for the fixed Recommender workflow.

This module deliberately does not expose Recommender freeform to OptChat.  It
adapts the existing typed ports to App-owned identity, task, context and Tool
Gateway boundaries without a localhost HTTP hop.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from dataclasses import replace
from runpy import run_path
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Callable
from uuid import uuid4

_CORE_SRC = Path(__file__).resolve().parents[4] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from modules.recommender.models import (
    RECOMMENDATION_SET_SCHEMA,
    ModelCapability,
    RecommendationCandidate,
    RecommendationCase,
    RecommendationSet,
)
from modules.recommender.agent_steps import canonical_hash
from modules.recommender.ports import AgentPort, AgentRunReceipt, AgentStepResult, KnowledgePort, ToolPort
from modules.recommender.service import RecommendationInputRequired, RecommenderService
from modules.recommender.config import RecommenderConfig
from modules.recommender.interaction import (
    classify_term_change,
    merge_confirmed_constraints,
)
from runtime.protocol.models import ModuleRunRef

from ..errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from ..page_registry import PageRegistry
from ..stores.result_store import ResultStore
from ..task_runtime.task_service import PENDING_RECOMMENDATION_SCHEMA_ID, TaskService
from .tool_dispatcher import ToolDispatcher
from .redaction import redact_text
from .multi_agent import (
    AgentRunConcurrencyGate,
    AgentRunSettlementRegistry,
    LifecycleProjection,
    OneShotAgentRuntime,
    resolve_recommendation_preset,
)
from .runtime_recommender import RuntimeBackedAgentPort, ShadowRuntimeAgentPort
from .runtime_tool_bridge import RuntimeToolBridge


_REPORT_MODULES = ("payoff", "pricing", "backtest")
_REPORT_TO_COMPUTE_MODULE = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
_RUNTIME_ROLE_TOOLS = {
    "sequential-deliberation": {
        "Interpreter": (),
        "Selector": (),
        "Reviewer": (),
    },
    "product-trader-loop": {
        "Structurer": ("search_option_structures",),
        "Trader": (),
        "Reviewer": (),
    },
    "independent-council": {
        "Framer": (),
        "Matcher": ("search_option_structures",),
        "Hedger": ("search_option_structures",),
        "Moderator": (),
    },
    "constraint-ranking": {
        "Specifier": (),
        "Generator": ("search_option_structures",),
        "Evaluator": (),
        "Reviewer": (),
    },
}
_MODULE_RUN_REF_FIELDS = (
    "module", "tenant_id", "task_id", "run_id",
    "expected_result_file_hash", "expected_artifact_manifest_hash",
)

_REASONING_BATCH_CHUNKS = 128
_REASONING_BATCH_CHARACTERS = 4_096
_REASONING_FLUSH_INTERVAL_SECONDS = 0.4
_REASONING_REQUEST_MAX_CHARACTERS = 512_000
_REASONING_REQUEST_MAX_EVENTS = 4_096
_REASONING_EVENT_MAX_CHARACTERS = 64_000


class RuntimeEventPersistenceSink:
    """Persist provider reasoning in bounded, run-isolated batches.

    Runtime transport delivery remains per chunk. Only the durable projection
    is coalesced. The lock and run-specific key prevent parallel Mode3 roles
    from sharing text. Persistence failures are diagnostics-only and never
    alter the authoritative Recommender result.
    """

    _TERMINAL_TYPES = frozenset({
        "turn.completed", "turn.failed", "turn.cancelled",
        "agent.completed", "agent.failed", "agent.cancelled",
        "workflow.completed", "workflow.failed", "workflow.cancelled",
        "runtime.closed", "runtime.error",
    })

    def __init__(
        self,
        persist: Callable[[Any], object],
        *,
        batch_chunks: int = _REASONING_BATCH_CHUNKS,
        batch_characters: int = _REASONING_BATCH_CHARACTERS,
        max_characters: int = _REASONING_REQUEST_MAX_CHARACTERS,
        max_events: int = _REASONING_REQUEST_MAX_EVENTS,
        flush_interval_seconds: float = _REASONING_FLUSH_INTERVAL_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], Any] = Timer,
    ) -> None:
        if min(batch_chunks, batch_characters, max_characters, max_events) < 1:
            raise ValueError("Reasoning持久化边界必须是正整数")
        if not callable(timer_factory) or not math.isfinite(flush_interval_seconds) or flush_interval_seconds <= 0:
            raise ValueError("Reasoning持久化时间窗口必须是有限正数")
        self._persist = persist
        self._batch_chunks = batch_chunks
        self._batch_characters = batch_characters
        self._max_characters = max_characters
        self._max_events = max_events
        self._flush_interval_seconds = float(flush_interval_seconds)
        self._timer_factory = timer_factory
        self._lock = RLock()
        self._states: dict[tuple[object, ...], dict[str, Any]] = {}
        self._closed = False
        self.persistence_failures = 0

    def __call__(self, event: Any) -> None:
        with self._lock:
            if self._closed:
                return
            if event.type == "assistant.reasoning_delta":
                self._append_reasoning(event)
                return
            if event.type in self._TERMINAL_TYPES:
                if event.agent_run_id:
                    self._flush_run(event, terminal=True)
                else:
                    self._flush_all(terminal=True)
            else:
                self._flush_key(self._key(event), terminal=False)
            self._safe_persist(event)

    def flush(self) -> None:
        with self._lock:
            self._flush_all(terminal=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_all(terminal=True)
            self._closed = True

    @property
    def active_timer_count(self) -> int:
        with self._lock:
            return sum(1 for state in self._states.values() if state.get("timer") is not None)

    @staticmethod
    def _key(event: Any) -> tuple[object, ...]:
        return (
            event.workflow_id, event.session_id, event.agent_run_id,
            event.turn, event.step,
        )

    def _append_reasoning(self, event: Any) -> None:
        key = self._key(event)
        state = self._states.setdefault(key, {
            "template": event,
            "parts": [],
            "chunks": 0,
            "accepted_chars": 0,
            "provider_chars": 0,
            "persisted_events": 0,
            "available": False,
            "truncated": False,
            "timer": None,
        })
        state["template"] = event
        text = str(event.payload.get("delta", ""))
        state["provider_chars"] += len(text)
        state["available"] = state["available"] or event.payload.get("available") is True or bool(text)
        remaining = max(0, self._max_characters - state["accepted_chars"])
        accepted = text[:remaining]
        if accepted:
            state["parts"].append(accepted)
            state["accepted_chars"] += len(accepted)
        if len(accepted) < len(text):
            state["truncated"] = True
        state["chunks"] += 1
        buffered = sum(len(part) for part in state["parts"])
        if (
            state["persisted_events"] < self._max_events - 1
            and (
                state["chunks"] >= self._batch_chunks
                or buffered >= self._batch_characters
            )
        ):
            self._flush_key(key, terminal=False)
        elif state["persisted_events"] < self._max_events - 1:
            self._schedule_timer(key, state)

    def _schedule_timer(self, key: tuple[object, ...], state: dict[str, Any]) -> None:
        if self._closed or state.get("timer") is not None:
            return
        holder: dict[str, Any] = {}
        timer: Any = None

        def fire() -> None:
            self._timer_fired(key, holder["timer"])

        try:
            timer = self._timer_factory(self._flush_interval_seconds, fire)
            holder["timer"] = timer
            if hasattr(timer, "daemon"):
                timer.daemon = True
            state["timer"] = timer
            timer.start()
        except Exception:
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            state["timer"] = None
            self.persistence_failures += 1

    def _timer_fired(self, key: tuple[object, ...], timer: Any) -> None:
        with self._lock:
            state = self._states.get(key)
            if self._closed or state is None or state.get("timer") is not timer:
                return
            state["timer"] = None
            self._flush_key(key, terminal=False)

    def _cancel_timer(self, state: dict[str, Any]) -> None:
        timer = state.get("timer")
        state["timer"] = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                self.persistence_failures += 1

    def _flush_all(self, *, terminal: bool) -> None:
        for key in tuple(self._states):
            self._flush_key(key, terminal=terminal)

    def _flush_run(self, event: Any, *, terminal: bool) -> None:
        prefix = (event.workflow_id, event.session_id, event.agent_run_id)
        for key in tuple(self._states):
            if key[:3] == prefix:
                self._flush_key(key, terminal=terminal)

    def _flush_key(self, key: tuple[object, ...], *, terminal: bool) -> None:
        state = self._states.get(key)
        if state is None:
            return
        self._cancel_timer(state)
        content = "".join(state["parts"])
        state["parts"] = []
        state["chunks"] = 0
        reserved_terminal_slot = 0 if terminal else 1
        slots = max(0, self._max_events - state["persisted_events"] - reserved_terminal_slot)
        pieces = [
            content[index:index + _REASONING_EVENT_MAX_CHARACTERS]
            for index in range(0, len(content), _REASONING_EVENT_MAX_CHARACTERS)
        ]
        selected_pieces = pieces[:slots]
        deferred_content = "".join(pieces[slots:])
        if terminal and deferred_content:
            state["truncated"] = True
        elif deferred_content:
            state["parts"] = [deferred_content]
        for index, piece in enumerate(selected_pieces):
            is_last_slot = index == len(selected_pieces) - 1
            self._persist_batch(state, piece, terminal=terminal and is_last_slot)
        if (
            terminal
            and not pieces
            and state["available"]
            and (state["truncated"] or state["persisted_events"] == 0)
            and state["persisted_events"] < self._max_events
        ):
            self._persist_batch(state, "", terminal=True)
        if terminal:
            self._states.pop(key, None)
        elif state["parts"] and state["persisted_events"] < self._max_events - 1:
            self._schedule_timer(key, state)

    def _persist_batch(self, state: dict[str, Any], content: str, *, terminal: bool) -> None:
        template = state["template"]
        payload: dict[str, Any] = {
            "available": state["available"],
            "chars": len(content),
        }
        block_index = template.payload.get("index")
        if isinstance(block_index, int) and not isinstance(block_index, bool) and block_index >= 0:
            payload["index"] = block_index
        if content:
            payload["delta"] = content
        if terminal and state["truncated"]:
            payload["truncated"] = True
            payload["provider_chars"] = state["provider_chars"]
        state["persisted_events"] += 1
        self._safe_persist(replace(
            template,
            event_id=f"reasoning-batch-{uuid4().hex}",
            payload=payload,
        ))

    def _safe_persist(self, event: Any) -> None:
        try:
            self._persist(event)
        except Exception:
            self.persistence_failures += 1


class AppAgentPort(AgentPort):
    """Maps Recommender's typed step requests to the provider-neutral gateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        identity: SessionIdentity,
        task_id: str,
        selection: ModelSelection | None = None,
        *,
        preset_id: str = "sequential-deliberation",
        is_cancelled: Callable[[SessionIdentity, str], bool] | None = None,
        lifecycle_projection: LifecycleProjection | None = None,
        host_multi_agent: bool = True,
        task_service: TaskService | None = None,
        concurrency_gate: AgentRunConcurrencyGate | None = None,
        execution_ids: Mapping[str, str] | None = None,
        settlement_registry: AgentRunSettlementRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._identity = identity
        self._task_id = task_id
        self._selection = selection
        self._preset_id = preset_id
        self._is_cancelled = is_cancelled
        self._lifecycle_projection = lifecycle_projection
        self._host_multi_agent = host_multi_agent
        self._task_service = task_service
        self._execution_ids = dict(execution_ids or {})
        request_id = str(self._execution_ids.get("request_id") or "").strip() or None
        visible_event_sink = (
            lambda event_type, status, summary: task_service.append_visible_process_event(
                identity, task_id, request_id, event_type, status, summary,
            )
            if task_service is not None and request_id is not None
            else None
        )
        self._runtime = OneShotAgentRuntime(
            gateway,
            identity,
            task_id,
            preset=resolve_recommendation_preset(preset_id),
            selection=selection,
            is_cancelled=is_cancelled,
            lifecycle_projection=lifecycle_projection,
            host_multi_agent=host_multi_agent,
            role_model_defaults=(
                gateway.multi_agent_role_model_selections_for(identity, preset_id)
                if hasattr(gateway, "multi_agent_role_model_selections_for") else {}
            ),
            event_log=task_service.session_event_log if task_service is not None else None,
            parent_session_id=(
                task_service.conversation_session_id(identity, task_id)
                if task_service is not None else None
            ),
            concurrency_gate=concurrency_gate,
            workflow_run_id=(execution_ids or {}).get("workflow_run_id"),
            execution_id=(execution_ids or {}).get("execution_id"),
            step_id_namespace=(execution_ids or {}).get("step_id_namespace"),
            settlement_registry=settlement_registry,
            visible_event_sink=visible_event_sink if task_service is not None and request_id is not None else None,
        )

    def capability(self) -> ModelCapability:
        capability = (
            self._gateway.capability_for(self._identity)
            if self._selection is None
            else self._gateway.capability_for(self._identity, selection=self._selection)
        )
        # The App owns child orchestration. This is not a provider claim.
        return ModelCapability.from_mapping({
            **capability,
            "multi_agent": self._host_multi_agent,
            "max_parallel_agents": 3 if self._host_multi_agent else 1,
            "independent_child_sessions": self._host_multi_agent,
        })

    def fresh_single_agent_port(self) -> "AppAgentPort":
        """Create a distinct fallback executor with a fresh run budget."""

        return AppAgentPort(
            self._gateway,
            self._identity,
            self._task_id,
            self._selection,
            preset_id="sequential-deliberation",
            is_cancelled=self._is_cancelled,
            lifecycle_projection=self._lifecycle_projection,
            host_multi_agent=False,
            task_service=self._task_service,
            concurrency_gate=self._runtime.concurrency_gate,
            execution_ids={
                **self._execution_ids,
                "step_id_namespace": f"{self._execution_ids.get('step_id_namespace', 'step-fallback')}:fallback",
            },
            settlement_registry=self._runtime.settlement_registry,
        )

    def close(self) -> None:
        self._runtime.close()

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult:
        safe_payload = _safe_model_payload(payload)
        run = self._runtime.run_step_result(str(role), safe_payload)
        result = dict(run.result or {})
        if _contains_hidden_model_field(result):
            raise ValidationError("Recommender步骤不得返回推理或Secret字段")
        return AgentStepResult(
            receipt=AgentRunReceipt(
                agent_run_id=run.agent_run_id,
                child_session_id=run.child_session_id,
                role=str(role),
                input_hash=canonical_hash(safe_payload),
                output_hash=canonical_hash(result),
            ),
            result=result,
        )

    def run_steps(self, role: str, payloads: Sequence[Mapping[str, Any]]) -> list[AgentStepResult]:
        safe_payloads = [_safe_model_payload(payload) for payload in payloads]
        runs = self._runtime.run_steps_results(str(role), safe_payloads)
        results = [dict(run.result or {}) for run in runs]
        if any(_contains_hidden_model_field(result) for result in results):
            raise ValidationError("Recommender步骤不得返回推理或Secret字段")
        return [
            AgentStepResult(
                receipt=AgentRunReceipt(
                    agent_run_id=run.agent_run_id,
                    child_session_id=run.child_session_id,
                    role=str(role),
                    input_hash=canonical_hash(payload),
                    output_hash=canonical_hash(result),
                ),
                result=result,
            )
            for run, payload, result in zip(runs, safe_payloads, results, strict=True)
        ]

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]:
        """Publish one independent child run for every named Recommender role.

        ``OneShotAgentRuntime.run_steps`` is intentionally homogeneous: it
        batches siblings that share one role.  A council has different role
        prompts and role-model slots, so retaining an explicit call per role
        is the safe portable contract.  Every call below is still a fresh
        ChildSession/AgentRun; no role is collapsed into the parent model.
        """

        return {
            str(role): self.run_step(str(role), payload)
            for role, payload in requests.items()
        }


class AppKnowledgePort(KnowledgePort):
    """Read-only Knowledger projection with bounded OptionList/OptionLib evidence."""

    def __init__(self, capability_root: Path, catalog_version: str) -> None:
        self._root = capability_root
        self._catalog_version = catalog_version

    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(payload.get("catalog_version", "")) != self._catalog_version:
            raise ValidationError("Knowledger查询版本与当前任务不一致")
        queries = payload.get("queries", ())
        if isinstance(queries, str):
            queries = (queries,)
        if not isinstance(queries, Sequence):
            raise ValidationError("Knowledger查询必须是字符串数组")
        query_text = " ".join(str(item).strip().lower() for item in queries if str(item).strip())
        registry = _load_capability_registry(self._root)
        rows = [
            (product_id, product)
            for product_id, product in registry["products"].items()
            if _matches(query_text, product_id, product)
        ][:3]
        evidence: list[dict[str, Any]] = []
        for product_id, product in rows:
            identity = dict(product.get("identity", {}))
            name = str(identity.get("name_zh", product_id))
            list_excerpt = _optionlist_excerpt(self._root / "references" / "optionlist.md", product_id, name)
            lib_excerpt = _optionlib_excerpt(self._root / "references" / "optionlib.md", product_id, name)
            status_excerpt = f"{product_id} {name} entry_status={bool(identity.get('entry_status'))}"
            evidence.extend((
                _evidence(product_id, "optionlist", list_excerpt or f"OptionList未找到{product_id}身份记录", identity,
                          bool(identity.get("entry_status")), self._catalog_version, "ready" if list_excerpt else "unavailable"),
                _evidence(product_id, "optionlib", lib_excerpt or f"OptionLib未找到{product_id}章节", identity,
                          bool(identity.get("entry_status")), self._catalog_version, "ready" if lib_excerpt else "unavailable"),
                _evidence(product_id, "optionreg_status", status_excerpt, identity, bool(identity.get("entry_status")), self._catalog_version),
            ))
        return {"ok": True, "catalog_version": self._catalog_version, "evidence": evidence}


class AppToolPort(ToolPort):
    """Recommender executor port bound to one App identity and task."""

    def __init__(
        self,
        executor: "AppConversationToolExecutor",
        identity: SessionIdentity,
        task_id: str,
        *,
        visible_event_sink: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._executor = executor
        self._identity = identity
        self._task_id = task_id
        self._visible_event_sink = visible_event_sink

    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._executor.call(self._identity, self._task_id, f"{module}.run", payload)

    def evaluate_candidate(
        self,
        *,
        candidate: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
        modules: Sequence[str],
        term_overrides: Mapping[str, Any],
        candidate_id: str,
        round_no: int,
    ) -> Mapping[str, Any]:
        """Run a Host-owned pre-selection evaluation for Mode 2 or Mode 4.

        Child agents choose only the bounded module names and term proposals.
        Contract compilation, data acquisition, module execution and fact
        projection remain inside the authenticated App Host.
        """

        if str(candidate.get("candidate_id", "")).strip() != str(candidate_id).strip():
            raise ValidationError("候选评估candidate_id不一致")
        if round_no > 1:
            self._emit_visible_event("candidate_cycle", "reselecting", "候选未满足约束，正在重新筛选。")
        self._emit_visible_event("host_module", "started", "正在运行候选验证。")
        try:
            result = self._executor.evaluate_current_recommendation_candidate(
                self._identity,
                self._task_id,
                candidate=candidate,
                confirmed_constraints=confirmed_constraints,
                modules=modules,
                term_overrides=term_overrides,
                round_no=round_no,
            )
            if str(result.get("status", "")).strip().lower() == "needs_input":
                raise RecommendationInputRequired(
                    "path_count",
                    str(result.get("message") or "该候选需要明确Monte Carlo路径数后才能估值。"),
                )
        except RecommendationInputRequired:
            self._emit_visible_event("host_module", "needs_input", "请补充Monte Carlo路径数后继续候选验证。")
            raise
        except Exception:
            self._emit_visible_event("host_module", "failed", "候选验证模块未完成。")
            raise
        statuses = result.get("module_statuses") if isinstance(result, Mapping) else None
        completed_status = "failed" if isinstance(statuses, Mapping) and any(
            str(value).lower() in {"failed", "unsupported", "timed_out", "cancelled"}
            for value in statuses.values()
        ) else "completed"
        self._emit_visible_event("host_module", completed_status, (
            "候选验证结果已返回。" if completed_status == "completed" else "候选验证模块未全部完成。"
        ))
        return result

    def _emit_visible_event(self, event_type: str, status: str, summary: str) -> None:
        if self._visible_event_sink is not None:
            self._visible_event_sink(event_type, status, summary)


class RecommenderAdapter:
    """Run exactly one fixed Recommendation Workflow for an OptChat task."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        registry: PageRegistry,
        task_service: TaskService,
        tool_executor: "AppConversationToolExecutor",
        concurrency_gate: AgentRunConcurrencyGate | None = None,
        settlement_registry: AgentRunSettlementRegistry | None = None,
        agent_runtime_mode: str = "disabled",
        agent_runtime_session_root: Path | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._tasks = task_service
        self._tools = tool_executor
        self._concurrency_gate = concurrency_gate
        self._settlement_registry = settlement_registry
        runtime_mode = str(agent_runtime_mode or "disabled").strip().lower()
        if runtime_mode not in {"disabled", "shadow", "active"}:
            raise ValidationError("Agent Runtime模式必须是disabled、shadow或active")
        self._agent_runtime_mode = runtime_mode
        self._agent_runtime_session_root = agent_runtime_session_root
        self._runtime_lock = RLock()
        self._task_runtimes: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._runtime_idle_seconds = 15 * 60.0

    @staticmethod
    def _runtime_task_key(identity: SessionIdentity, task_id: str) -> tuple[str, str, str]:
        return (str(identity.tenant_id), str(identity.principal_id), str(task_id).strip())

    def _get_or_create_task_runtime(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        preset_id: str,
        selection: ModelSelection | None,
        execution_ids: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """Reuse one runtime scope for the authenticated Task.

        The conversation may call Recommender more than once while it is
        awaiting confirmation. The Child runtime must remain the same scope;
        a new legacy AppAgentPort is still created per authoritative request.
        """

        key = self._runtime_task_key(identity, task_id)
        with self._runtime_lock:
            existing = self._task_runtimes.get(key)
            if existing is not None and existing.get("preset_id") == preset_id:
                if int(existing.get("active_count", 0)) != 0:
                    raise ValidationError("同一Task已有Recommender运行，不能并发复用Child Session")
                timer = existing.get("idle_timer")
                if timer is not None:
                    timer.cancel()
                    existing["idle_timer"] = None
                begin_request = getattr(existing.get("runtime"), "begin_parent_request", None)
                if callable(begin_request):
                    begin_request()
                existing["active_count"] = int(existing.get("active_count", 0)) + 1
                return existing
            if existing is not None:
                self._close_runtime_entry(existing, reason="preset_changed")
                self._task_runtimes.pop(key, None)

            runtime_parent_session = self._tasks.conversation_session_id(identity, task_id)
            runtime_event_log = self._tasks.session_event_log
            runtime_event_log.ensure_session(runtime_parent_session, kind="conversation")
            runtime_mode = self._agent_runtime_mode

            def persist_runtime_event(event):
                projected = replace(
                    event,
                    payload={**dict(event.payload), "runtime_mode": runtime_mode},
                )
                runtime_event_log.append(
                    runtime_parent_session,
                    "runtime.event",
                    {"runtime_event": projected.to_dict()},
                    event_id=f"runtime:{projected.event_id}",
                    ignorable=True,
                )

            persistence_sink = RuntimeEventPersistenceSink(persist_runtime_event)
            runtime_workflow_id = str(
                (execution_ids or {}).get("workflow_run_id") or f"runtime-{uuid4().hex}"
            )
            try:
                bridge_factory = getattr(self._tools, "runtime_tool_bridge", None)
                runtime_tool_bridge = bridge_factory() if callable(bridge_factory) else None
                runtime_agent_port = RuntimeBackedAgentPort(
                    self._gateway,
                    identity,
                    task_id,
                    preset_id=preset_id,
                    selection=selection,
                    role_model_defaults=(
                        self._gateway.multi_agent_role_model_selections_for(identity, preset_id)
                        if hasattr(self._gateway, "multi_agent_role_model_selections_for") else {}
                    ),
                    role_instructions=(
                        self._gateway.multi_agent_role_instructions_for(identity, preset_id)
                        if hasattr(self._gateway, "multi_agent_role_instructions_for") else {}
                    ),
                    is_cancelled=self._tasks.is_cancelled,
                    event_sink=persistence_sink,
                    session_root=self._agent_runtime_session_root,
                    root_session_id=str(runtime_parent_session),
                    workflow_id=runtime_workflow_id,
                    tool_bridge=runtime_tool_bridge,
                    allowed_tools_by_role=_RUNTIME_ROLE_TOOLS.get(preset_id, {}),
                )
            except Exception:
                persistence_sink.close()
                raise
            entry = {
                "runtime": runtime_agent_port,
                "persistence_sink": persistence_sink,
                "preset_id": preset_id,
                "workflow_id": runtime_workflow_id,
                "active_count": 1,
                "idle_timer": None,
            }
            self._task_runtimes[key] = entry
            return entry

    @staticmethod
    def _close_runtime_entry(entry: Mapping[str, Any], *, reason: str) -> None:
        timer = entry.get("idle_timer")
        if timer is not None:
            timer.cancel()
        runtime = entry.get("runtime")
        sink = entry.get("persistence_sink")
        try:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
        finally:
            close_sink = getattr(sink, "close", None)
            if callable(close_sink):
                close_sink()

    def _release_task_runtime(self, identity: SessionIdentity, task_id: str, entry: Mapping[str, Any]) -> None:
        key = self._runtime_task_key(identity, task_id)
        with self._runtime_lock:
            current = self._task_runtimes.get(key)
            if current is not entry:
                return
            current["active_count"] = max(0, int(current.get("active_count", 1)) - 1)
            if current["active_count"]:
                return
            workflow_id = str(current.get("workflow_id", ""))
            timer = Timer(
                self._runtime_idle_seconds,
                self._expire_task_runtime,
                args=(key, workflow_id),
            )
            timer.daemon = True
            current["idle_timer"] = timer
            timer.start()

    def _expire_task_runtime(self, key: tuple[str, str, str], workflow_id: str) -> None:
        with self._runtime_lock:
            entry = self._task_runtimes.get(key)
            if (
                entry is None
                or str(entry.get("workflow_id", "")) != workflow_id
                or int(entry.get("active_count", 0)) != 0
            ):
                return
            self._task_runtimes.pop(key, None)
        self._close_runtime_entry(entry, reason="idle_timeout")

    def close_task_runtime(
        self,
        identity: SessionIdentity,
        task_id: str,
        reason: str = "task_closed",
    ) -> Mapping[str, Any]:
        key = self._runtime_task_key(identity, task_id)
        with self._runtime_lock:
            entry = self._task_runtimes.pop(key, None)
        if entry is None:
            return {"status": "not_found", "task_id": str(task_id)}
        self._close_runtime_entry(entry, reason=str(reason)[:120])
        return {"status": "closed", "task_id": str(task_id), "workflow_id": entry.get("workflow_id")}

    def cancel_task_runtime(
        self,
        identity: SessionIdentity,
        task_id: str,
        reason: str = "task_cancelled",
    ) -> Mapping[str, Any]:
        key = self._runtime_task_key(identity, task_id)
        with self._runtime_lock:
            entry = self._task_runtimes.get(key)
        if entry is None:
            return {"status": "not_found", "task_id": str(task_id)}
        runtime = entry.get("runtime")
        controller = getattr(runtime, "controller", None)
        cancel = getattr(controller, "cancel", None)
        if not callable(cancel):
            cancel = getattr(runtime, "cancel", None)
        if not callable(cancel):
            raise ValidationError("Agent Runtime缺少Task取消入口")
        result = cancel(str(reason)[:120])
        return dict(result) if isinstance(result, Mapping) else {"status": "cancelled"}

    def close_all_runtimes(self) -> Mapping[str, Any]:
        with self._runtime_lock:
            entries = tuple(self._task_runtimes.values())
            self._task_runtimes.clear()
        failures: list[str] = []
        for entry in entries:
            try:
                self._close_runtime_entry(entry, reason="all_runtimes_closed")
            except Exception as error:
                failures.append(f"{type(error).__name__}:{error}")
        return {"status": "closed" if not failures else "partial", "closed_count": len(entries), "failures": failures}

    def close(self) -> Mapping[str, Any]:
        """Close every Task-scoped runtime when the adapter itself is torn down."""

        return self.close_all_runtimes()

    def run_fixed(
        self, identity: SessionIdentity, task_id: str, prompt: str, arguments: Mapping[str, Any], *,
        selection: ModelSelection | None = None, execution_ids: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        result = dict(self._run_fixed_internal(
            identity, task_id, prompt, arguments, selection=selection, execution_ids=execution_ids,
        ))
        result["control"] = {
            "resume_main_agent": True,
            "candidate_owner": "recommender",
            "delivery_owner": "main_agent",
        }
        return result

    def _run_fixed_internal(
        self, identity: SessionIdentity, task_id: str, prompt: str, arguments: Mapping[str, Any], *,
        selection: ModelSelection | None = None, execution_ids: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        task = self._tasks.get(identity, task_id)
        catalog_version = str(self._registry.manifest["catalog_version"])
        messages = task.get("messages", [])
        history = list(messages) if isinstance(messages, list) else []
        if not history or not _same_pending_user_turn(history[-1], prompt):
            history.append({"role": "user", "content": str(prompt), "status": "pending_model"})
        confirmed_constraints = merge_confirmed_constraints({}, history)
        try:
            pending = self._tasks.pending_recommendation(identity, task_id)
        except ValidationError:
            return _confirmation_unavailable(task_id, catalog_version)
        approved_candidate_ids = _selected_candidate_ids(prompt, arguments, pending)
        if (
            isinstance(pending, Mapping)
            and pending.get("status") == "pending_approval"
            and not approved_candidate_ids
            and _has_candidate_selection_intent(prompt)
        ):
            return {
                "status": "needs_input",
                "message": "未能识别要批准的候选。请按候选序号、candidate_id或产品名称重新选择。",
            }
        if isinstance(pending, Mapping) and approved_candidate_ids:
            approved = self._tasks.approve_pending_recommendation(
                identity, task_id, approved_candidate_ids=list(approved_candidate_ids),
            )
            if approved is None:
                return _confirmation_unavailable(task_id, catalog_version, pending)
            execution = self._tools.execute_confirmed_recommendation(identity, task_id, approved)
            return {
                "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
                "recommendation_set": _continuation_recommendation_set(
                    task_id, catalog_version, approved,
                    status="completed" if execution.get("status") == "completed" else "candidate_ready",
                    candidate_status="approved",
                    analysis_status=str(execution.get("analysis_status", "not_started")),
                    delivery_status=str(execution.get("delivery_status", "not_requested")),
                ),
                "execution": execution,
                "next_step": str(execution.get("next_step") or "已按最新产品规则编译并执行已确认候选。"),
            }
        requested_outputs = _requested_outputs(arguments, confirmed_constraints)
        requested_candidate_count = _requested_candidate_count_argument(arguments)
        case_constraints = dict(confirmed_constraints)
        if requested_candidate_count is not None:
            case_constraints["_requested_candidate_count"] = requested_candidate_count
        case = RecommendationCase(
            analysis_case_id=f"chat-{task_id}",
            task_id=task_id,
            tenant_id=identity.tenant_id,
            prompt=str(prompt).strip(),
            catalog_version=catalog_version,
            run_id=f"recommend-{task_id}",
            requested_outputs=requested_outputs,
            audience=identity.audience,
            conversation_ref=f"task:{task_id}",
            confirmed_constraints=case_constraints,
        )
        if self._agent_runtime_mode == "disabled":
            raise UnavailableCapabilityError(
                "Recommender多智能体运行时",
                "当前App未启用Agent Runtime，不能以Legacy单Agent替代所选多智能体预设。",
            )
        preset_id = _saved_recommendation_preset(self._gateway, identity, arguments)
        preset = resolve_recommendation_preset(preset_id)
        lifecycle_projection = LifecycleProjection()
        request_id = str((execution_ids or {}).get("request_id") or "").strip() or None
        visible_event_sink = (
            lambda event_type, status, summary: self._tasks.append_visible_process_event(
                identity, task_id, request_id, event_type, status, summary,
            )
            if request_id is not None
            else None
        )
        legacy_agent_port = AppAgentPort(
            self._gateway,
            identity,
            task_id,
            selection,
            preset_id=preset_id,
            is_cancelled=self._tasks.is_cancelled,
            lifecycle_projection=lifecycle_projection,
            task_service=self._tasks,
            concurrency_gate=self._concurrency_gate,
            execution_ids=execution_ids,
            settlement_registry=self._settlement_registry,
        )
        agent_port: AgentPort = legacy_agent_port
        runtime_entry: Mapping[str, Any] | None = None
        runtime_agent_port: RuntimeBackedAgentPort | None = None
        legacy_closed = False
        if self._agent_runtime_mode in {"shadow", "active"}:
            try:
                runtime_entry = self._get_or_create_task_runtime(
                    identity,
                    task_id,
                    preset_id=preset_id,
                    selection=selection,
                    execution_ids=execution_ids,
                )
            except Exception as error:
                if self._agent_runtime_mode == "active":
                    legacy_agent_port.close()
                    raise
                parent_session = self._tasks.conversation_session_id(identity, task_id)
                self._tasks.session_event_log.ensure_session(parent_session, kind="conversation")
                self._tasks.session_event_log.append(
                    parent_session,
                    "runtime.shadow_failure",
                    {"error_type": type(error).__name__},
                    event_id=f"runtime-shadow-failure:{uuid4().hex}",
                    ignorable=True,
                )
                runtime_entry = None
            else:
                runtime_agent_port = runtime_entry["runtime"]
                if self._agent_runtime_mode == "active":
                    legacy_agent_port.close()
                    legacy_closed = True
                    agent_port = runtime_agent_port
                else:
                    agent_port = ShadowRuntimeAgentPort(legacy_agent_port, runtime_agent_port)
        review_policy_for = getattr(self._gateway, "multi_agent_review_policy_for", None)
        review_policy_id = (
            str(review_policy_for(identity)).strip().lower()
            if callable(review_policy_for) else "standard-review"
        )
        service = RecommenderService(
            agent_port=agent_port,
            knowledge_port=AppKnowledgePort(self._registry.capability_root, catalog_version),
            tool_port=AppToolPort(
                self._tools,
                identity,
                task_id,
                visible_event_sink=visible_event_sink,
            ),
            config=RecommenderConfig(
                agent_mode="multi",
                multi_agent_preset=preset_id,
            ),
            review_policy_id=review_policy_id,
        )
        workflow = str(arguments.get("workflow", "recommendation")).strip().lower()
        try:
            result = service.recommend_fixed(case, workflow=workflow)
        finally:
            # RuntimeBackedAgentPort is Task-scoped and is closed only by the
            # lifecycle methods below. The authoritative legacy port remains
            # request-scoped, including in shadow mode.
            if runtime_entry is None:
                agent_port.close()
            elif not legacy_closed:
                legacy_agent_port.close()
            if runtime_entry is not None:
                self._release_task_runtime(identity, task_id, runtime_entry)
        route = result.get("route", {})
        if not isinstance(route, Mapping) or route.get("fixed_recommendation_workflow") is not True:
            raise ValidationError("App内Recommender只允许固定推荐Workflow，不允许freeform")
        if "freeform" in result:
            raise ValidationError("App内Recommender不得返回freeform")
        recommendation = result.get("recommendation_set")
        continuation = _pending_recommendation_state(
            recommendation,
            identity=identity,
            analysis_case_id=case.analysis_case_id,
            confirmed_constraints=confirmed_constraints,
            preset_id=preset.preset_id,
        )
        if continuation is not None:
            continuation["workflow_mode"] = str(recommendation.get("workflow_mode", "single_agent"))
            continuation["confirmed_constraints"] = _persisted_constraints(continuation["confirmed_constraints"])
            self._tasks.save_pending_recommendation(identity, task_id, continuation)
            return {
                "route": dict(route),
                "recommendation_set": dict(recommendation),
                "next_step": "请确认要执行的候选。确认后，系统将按最新OptionReg重新编译当前输入并运行。",
            }
        if _requires_exact_pending_state(recommendation):
            return _confirmation_unavailable(task_id, catalog_version)
        return dict(result)


class AppConversationToolExecutor:
    """Authenticated Host executor for current inputs, ModuleRuns and reports."""

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        registry: PageRegistry,
        results: ResultStore | None = None,
        tasks: TaskService | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._results = results
        self._tasks = tasks
        self._recommender: RecommenderAdapter | None = None

    def bind_recommender(self, recommender: RecommenderAdapter) -> None:
        self._recommender = recommender

    def runtime_tool_bridge(self) -> RuntimeToolBridge:
        """Build the task-bound read-only bridge used by runtime Child Sessions.

        Child sessions may search the current product directory. Contract
        compilation, candidate evaluation and fact verification remain in the
        authenticated Host after the role returns its structured proposal.
        """

        def search_handler(
            identity: SessionIdentity,
            task_id: str,
            payload: Mapping[str, Any],
            _connection: object,
        ) -> Mapping[str, Any]:
            del identity, task_id
            query = payload.get("query", "")
            queries = payload.get("queries", [query])
            if isinstance(queries, (str, bytes)) or not isinstance(queries, Sequence):
                queries = [query]
            return AppKnowledgePort(
                self._registry.capability_root,
                str(self._registry.manifest["catalog_version"]),
            ).search({
                "catalog_version": str(self._registry.manifest["catalog_version"]),
                "queries": [str(item) for item in queries],
            })

        return RuntimeToolBridge(
            task_service=self._tasks,
            search_handler=search_handler,
        )

    def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if name == "recommendation_delivery.run":
            return self._run_pending_recommendation_delivery(identity, task_id, arguments)
        module, action = _tool_name(name)
        if any(key in arguments for key in ("action", "task_id", "tenant_id", "principal_id", "session_id", "host_context", "capability_token")):
            raise ValidationError("OptChat工具参数不得覆盖App路由或身份字段")
        if module == "knowledger" and action == "search":
            return AppKnowledgePort(self._registry.capability_root, str(self._registry.manifest["catalog_version"])).search({
                "catalog_version": str(self._registry.manifest["catalog_version"]),
                "queries": arguments.get("queries", [arguments.get("query", "")]),
            })
        if module == "recommender" and action == "run":
            if self._recommender is None:
                raise ValidationError("RecommenderAdapter未绑定")
            return self._recommender.run_fixed(identity, task_id, str(arguments.get("prompt", "")), arguments)
        if module == "reporter" and action == "run":
            prepared = self._prepare_report(
                identity,
                task_id,
                arguments,
            )
            if prepared.get("status") == "needs_input":
                return prepared
            response = self._dispatch(identity, task_id, name, module, prepared)
            return _with_report_delivery(response, prepared)
        else:
            payload = {**dict(arguments), "action": action, "task_id": task_id}
        return self._dispatch(identity, task_id, name, module, payload)

    def execute_confirmed_recommendation(
        self,
        identity: SessionIdentity,
        task_id: str,
        pending: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compile and run approved current inputs against the latest OptionReg."""

        approved_ids = _approved_candidate_ids(pending)
        candidates = pending.get("candidates")
        if not approved_ids or isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValidationError("已确认推荐缺少当前候选输入")
        candidate_index = {
            str(item.get("candidate_id", "")).strip(): item
            for item in candidates
            if isinstance(item, Mapping)
        }
        registry = _load_capability_registry(self._registry.capability_root)
        products = registry.get("products")
        if not isinstance(products, Mapping):
            raise ValidationError("最新OptionReg不可用")
        requested = {str(item).strip().lower() for item in pending.get("requested_outputs", ())}
        modules = _confirmed_execution_modules(requested)
        executions: list[dict[str, Any]] = []
        overall = "completed"
        analysis_case_id = f"recommendation-{uuid4().hex[:24]}"
        for candidate_id in approved_ids:
            candidate = candidate_index.get(candidate_id)
            if not isinstance(candidate, Mapping):
                raise ValidationError("已确认candidate_id不属于当前候选")
            product_id = _identifier(candidate.get("product_id"), "product_id")
            product = products.get(product_id)
            identity_row = product.get("identity") if isinstance(product, Mapping) else None
            current_revision = identity_row.get("rule_revision") if isinstance(identity_row, Mapping) else None
            if current_revision != candidate.get("rule_revision"):
                raise ValidationError("候选产品规则已更新，请重新推荐后确认")
            underlyings = [
                str(item).strip() for item in candidate.get("underlyings", ()) if str(item).strip()
            ]
            current_inputs = candidate.get("current_inputs")
            if not underlyings or not isinstance(current_inputs, Mapping):
                raise ValidationError("已确认候选缺少当前输入")
            term_overrides = current_inputs.get("term_overrides", {})
            module_inputs = current_inputs.get("module_inputs", {})
            if not isinstance(term_overrides, Mapping) or not isinstance(module_inputs, Mapping):
                raise ValidationError("已确认候选当前输入无效")
            module_results: dict[str, Any] = {}
            for module in modules:
                supplied = module_inputs.get(module, {})
                if not isinstance(supplied, Mapping):
                    raise ValidationError(f"{module}当前输入必须为对象")
                payload: dict[str, Any] = {
                    "action": "run",
                    "task_id": task_id,
                    "product_id": product_id,
                    "identity": {"underlyings": underlyings},
                    "term_overrides": dict(term_overrides),
                    **dict(supplied),
                }
                if module == "pricer":
                    methods = product.get("terms", {}).get("pricing_methods", ()) if isinstance(product, Mapping) else ()
                    pricing = payload.get("pricing_config")
                    if not isinstance(pricing, Mapping):
                        if "analytical" in methods:
                            pricing = {"model_method": "analytical"}
                        else:
                            path_count = pending.get("confirmed_constraints", {}).get("path_count")
                            if isinstance(path_count, bool) or not isinstance(path_count, int) or path_count <= 0:
                                return {
                                    "status": "needs_input",
                                    "analysis_status": "not_started",
                                    "delivery_status": "pending",
                                    "next_step": "该候选需要Monte Carlo估值，请先明确路径数。",
                                    "executions": executions,
                                }
                            pricing = {"model_method": "monte_carlo", "path_count": path_count}
                    payload["pricing_config"] = dict(pricing)
                elif module == "backtester":
                    payload.setdefault("backtest_config", {})
                try:
                    response = self._dispatch(
                        identity, task_id, f"{module}.run", module, payload,
                        binding={
                            "analysis_case_id": analysis_case_id,
                            "candidate_id": candidate_id,
                            "product_id": product_id,
                            "rule_revision": current_revision,
                        },
                    )
                except (AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError) as error:
                    overall = "partial"
                    module_results[module] = {"status": "failed", "message": str(error)}
                else:
                    module_results[module] = response
                    if _normalized_module_status(response) not in {"succeeded", "partial"}:
                        overall = "partial"
            executions.append({
                "candidate_id": candidate_id,
                "product_id": product_id,
                "rule_revision": current_revision,
                "modules": module_results,
            })
        return {
            "status": overall,
            "analysis_status": "completed" if overall == "completed" else "partial",
            "delivery_status": "not_requested",
            "executions": executions,
            "next_step": "已按最新OptionReg完成候选计算。" if overall == "completed" else "部分候选计算未完成，请查看模块结果。",
        }

    def evaluate_current_recommendation_candidate(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        candidate: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
        modules: Sequence[str],
        term_overrides: Mapping[str, Any],
        round_no: int,
    ) -> dict[str, Any]:
        """Evaluate one current candidate; every call compiles latest OptionReg."""

        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        product_id = _identifier(candidate.get("product_id"), "product_id")
        rule_revision = candidate.get("rule_revision")
        if isinstance(rule_revision, bool) or not isinstance(rule_revision, int) or rule_revision < 1:
            raise ValidationError("候选缺少rule_revision")
        requested_modules = tuple(str(item).strip().lower() for item in modules)
        if not requested_modules or len(requested_modules) != len(set(requested_modules)) or any(
            item not in {"payoffer", "pricer", "backtester"} for item in requested_modules
        ):
            raise ValidationError("候选评估模块无效")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
            raise ValidationError("候选评估轮次无效")
        current_inputs = candidate.get("current_inputs")
        if not isinstance(current_inputs, Mapping):
            current_inputs = {}
        merged_inputs = dict(current_inputs)
        merged_inputs["term_overrides"] = dict(term_overrides)
        snapshot = {
            "candidate_id": candidate_id,
            "product_id": product_id,
            "rule_revision": rule_revision,
            "product_name": str(candidate.get("product_name", "")),
            "underlyings": list(candidate.get("underlyings", ())),
            "library_status": "ready",
            "current_inputs": merged_inputs,
            "module_run_refs": [],
            "public_projection": {
                "reason": "当前候选计算",
                "suitable_for": [],
                "not_suitable_for": [],
                "main_risks": [],
                "key_terms": [],
            },
        }
        output_alias = {"payoffer": "payoff", "pricer": "pricing", "backtester": "backtest"}
        pending = {
            "approved_candidate_ids": [candidate_id],
            "candidates": [snapshot],
            "requested_outputs": [output_alias[item] for item in requested_modules],
            "confirmed_constraints": dict(confirmed_constraints),
        }
        execution = self.execute_confirmed_recommendation(identity, task_id, pending)
        rows = execution.get("executions", ())
        row = rows[0] if isinstance(rows, list) and rows else {}
        module_results = row.get("modules", {}) if isinstance(row, Mapping) else {}
        statuses: dict[str, str] = {}
        run_refs: list[dict[str, Any]] = []
        facts: dict[str, Mapping[str, Any]] = {}
        for module in requested_modules:
            response = module_results.get(module, {}) if isinstance(module_results, Mapping) else {}
            statuses[module] = _normalized_module_status(response) if isinstance(response, Mapping) else "failed"
            raw_ref = response.get("module_run_ref") if isinstance(response, Mapping) else None
            if isinstance(raw_ref, Mapping):
                reference = {field: raw_ref.get(field) for field in _MODULE_RUN_REF_FIELDS}
                run_refs.append(reference)
                if self._results is not None:
                    summary = self._results.verified_fact_summary(identity, reference)
                    for fact in summary.get("facts", ()):
                        if isinstance(fact, Mapping) and str(fact.get("metric", "")).strip():
                            facts[str(fact["metric"])] = {
                                "value": fact.get("value"), "unit": fact.get("unit"),
                                "fact_ref": fact.get("fact_ref"), "source": module,
                            }
        return {
            "status": execution.get("status", "partial"),
            "candidate_id": candidate_id,
            "product_id": product_id,
            "rule_revision": rule_revision,
            "module_run_refs": run_refs,
            "module_statuses": statuses,
            "verified_metrics": facts,
            "round_no": round_no,
        }

    def _run_pending_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Render an approved current-candidate set from newly frozen ModuleRuns."""

        if self._tasks is None:
            raise ValidationError("推荐交付状态机未配置")
        if set(arguments).difference({"kind", "format"}):
            raise ValidationError("推荐交付参数只允许kind或format")
        kind = str(arguments.get("kind", "")).strip().lower()
        output_format = str(arguments.get("format", "html")).strip().lower()
        if kind not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
            return {
                "status": "needs_input",
                "message": "请选择研究简报、参考报价或研究报告；默认生成HTML，需要PDF时请明确指定。",
            }
        pending = self._tasks.pending_recommendation(identity, task_id)
        if pending is None or pending.get("status") != "approved":
            return {"status": "needs_input", "message": "请先确认推荐候选与合同条款。"}
        requested = {"kind": kind, "format": output_format}
        if pending.get("delivery") is None:
            pending = self._tasks.choose_pending_recommendation_delivery(
                identity,
                task_id,
                requested,
            )
        if pending is None or pending.get("delivery") != requested:
            raise ValidationError("当前候选已选择其他交付类型")
        result = self.run_recommendation_candidate_set_delivery(
            identity,
            task_id,
            candidates=pending.get("candidates", ()),
            approved_candidate_ids=_approved_candidate_ids(pending),
            delivery=requested,
            confirmed_constraints=_pending_constraints(pending),
        )
        if result.get("status") == "completed":
            self._tasks.complete_pending_recommendation(identity, task_id)
        return result

    def run_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        candidate: Mapping[str, Any],
        delivery: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run and render one current candidate without a contract-version binding."""

        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        result = self.run_recommendation_candidate_set_delivery(
            identity,
            task_id,
            candidates=(candidate,),
            approved_candidate_ids=(candidate_id,),
            delivery=delivery,
            confirmed_constraints=confirmed_constraints or {},
        )
        deliveries = result.get("deliveries")
        if (
            result.get("status") == "completed"
            and isinstance(deliveries, list)
            and len(deliveries) == 1
            and isinstance(deliveries[0], Mapping)
        ):
            return {
                "status": "completed",
                "analysis_status": str(result.get("analysis_status", "completed")),
                "delivery_status": str(result.get("delivery_status", "completed")),
                **dict(deliveries[0]),
            }
        return result

    def run_recommendation_candidate_set_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        candidates: Sequence[Mapping[str, Any]],
        approved_candidate_ids: Sequence[str],
        delivery: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compile current candidates, run required modules, then render one delivery."""

        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValidationError("推荐交付缺少当前候选输入")
        candidate_index = {
            str(candidate.get("candidate_id", "")).strip(): dict(candidate)
            for candidate in candidates
            if isinstance(candidate, Mapping)
        }
        selected_ids = [_identifier(candidate_id, "candidate_id") for candidate_id in approved_candidate_ids]
        if (
            not selected_ids
            or len(set(selected_ids)) != len(selected_ids)
            or any(candidate_id not in candidate_index for candidate_id in selected_ids)
        ):
            raise ValidationError("推荐交付候选无效")
        kind = str(delivery.get("kind", "")).strip().lower()
        output_format = str(delivery.get("format", "")).strip().lower()
        if kind not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
            raise ValidationError("推荐交付类型无效")
        requested_outputs = ["pricing"] if kind == "quote" else list(_REPORT_MODULES)
        selected_candidates = [candidate_index[candidate_id] for candidate_id in selected_ids]
        execution = self.execute_confirmed_recommendation(
            identity,
            task_id,
            {
                "approved_candidate_ids": selected_ids,
                "candidates": selected_candidates,
                "requested_outputs": requested_outputs,
                "confirmed_constraints": dict(confirmed_constraints),
            },
        )
        execution_status = str(execution.get("status", "partial")).strip().lower()
        if execution_status == "needs_input":
            return dict(execution)
        if execution_status != "completed":
            return {
                "status": "partial",
                "analysis_status": str(execution.get("analysis_status", "partial")),
                "delivery_status": "not_requested",
                "deliveries": [],
                "next_step": str(execution.get("next_step") or "部分候选计算未完成，请查看模块结果。"),
            }
        title = str(selected_candidates[0].get("product_name") or "期权结构研究")
        if len(selected_ids) > 1:
            title = "多结构研究简报" if kind == "card" else "多结构完整报告" if kind == "report" else "多结构参考报价"
        prepared = self._prepare_report(
            identity,
            task_id,
            {
                "kind": kind,
                "format": output_format,
                "title": title,
                "delivery_mode": "comparison" if len(selected_ids) > 1 else "single",
                "candidate_ids": selected_ids,
            },
            report_run_id=f"chat-{kind}-{uuid4().hex[:16]}",
        )
        if prepared.get("status") == "needs_input":
            return {
                "status": "partial",
                "analysis_status": "completed",
                "delivery_status": "not_requested",
                "deliveries": [],
                "next_step": str(prepared.get("message") or "计算已完成，但尚未形成可验证的报告来源。"),
            }
        report = _with_report_delivery(
            self._dispatch(identity, task_id, "reporter.run", "reporter", prepared),
            prepared,
        )
        report_status = str(report.get("status", "")).strip().lower()
        receipt = dict(report.get("delivery", {}))
        complete = (
            report.get("ok") is True
            and report_status in {"succeeded", "completed"}
            and str(receipt.get("coverage_status", "complete")).lower() == "complete"
        )
        return {
            "status": "completed" if complete else "partial",
            "analysis_status": "completed",
            "delivery_status": "completed" if complete else "partial",
            "deliveries": [receipt] if receipt else [],
        }

    def _dispatch(
        self,
        identity: SessionIdentity,
        task_id: str,
        name: str,
        module: str,
        payload: Mapping[str, Any],
        *,
        binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # OptChat is server-side orchestration. It uses the same signed module
        # scope as Desk, but never grants the model direct module authority.
        binding = dict(binding or {})
        context = self._registry.service_context(
            identity,
            module,
            task_id=task_id,
            analysis_case_id=binding.get("analysis_case_id"),
            candidate_id=binding.get("candidate_id"),
            product_id=str(binding.get("product_id") or payload.get("product_id") or "").strip() or None,
            rule_revision=(
                binding.get("rule_revision")
                if isinstance(binding.get("rule_revision"), int) and not isinstance(binding.get("rule_revision"), bool)
                else None
            ),
        )
        return dict(self._dispatcher.dispatch_for_conversation(
            module,
            dict(payload),
            identity,
            module_context=context,
            request_id=f"conversation-{uuid4().hex}",
        ))

    def _prepare_report(
        self,
        identity: SessionIdentity,
        task_id: str,
        arguments: Mapping[str, Any],
        *,
        report_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Turn a semantic chat request into one server-owned Reporter selection."""

        if self._results is None:
            return _report_needs_input()
        default_kind = "report" if identity.role.value == "admin" else "card"
        requested_kind = str(arguments.get("kind") or arguments.get("output_type") or default_kind).strip().lower()
        if requested_kind not in {"card", "quote", "report"}:
            raise ValidationError("交付类型只能是card、quote或report")
        requested_format = str(arguments.get("format") or "html").strip().lower()
        if requested_format not in {"html", "pdf"}:
            raise ValidationError("交付格式只能是html或pdf")
        allowed = {"kind", "output_type", "format", "title", "delivery_mode", "candidate_ids"}
        if set(arguments).difference(allowed):
            raise ValidationError("OptChat报告请求只接受交付类型、格式、标题和对比模式")
        delivery_mode = str(arguments.get("delivery_mode") or "single").strip().lower()
        if delivery_mode not in {"single", "comparison"}:
            raise ValidationError("交付模式只能是single或comparison")
        requested_candidate_ids = arguments.get("candidate_ids")
        if requested_candidate_ids is not None:
            if isinstance(requested_candidate_ids, (str, bytes)) or not isinstance(requested_candidate_ids, Sequence):
                raise ValidationError("Reporter候选必须使用有序candidate_ids")
            requested_candidate_ids = [str(item).strip() for item in requested_candidate_ids]
            if (
                not requested_candidate_ids
                or any(not item for item in requested_candidate_ids)
                or len(set(requested_candidate_ids)) != len(requested_candidate_ids)
            ):
                raise ValidationError("Reporter候选candidate_ids不能为空或重复")
        catalog = self._results.list_owned_report_sources(identity, task_id=task_id)
        sources = catalog.get("sources") if isinstance(catalog, Mapping) else None
        if not isinstance(sources, list) or not sources:
            return _report_needs_input()
        selected_candidates: list[dict[str, Any]] = []
        if requested_candidate_ids is not None:
            source = None
            for raw_source in sources:
                if not isinstance(raw_source, Mapping):
                    continue
                index = {
                    str(item.get("candidate_id", "")): dict(item)
                    for item in raw_source.get("candidates", ())
                    if isinstance(item, Mapping)
                }
                if all(candidate_id in index for candidate_id in requested_candidate_ids):
                    source = raw_source
                    selected_candidates = [index[candidate_id] for candidate_id in requested_candidate_ids]
                    break
            if source is None or not selected_candidates:
                return _report_needs_input()
            candidate = selected_candidates[0]
            delivery_mode = "comparison" if len(selected_candidates) > 1 else "single"
        else:
            try:
                source, candidate = _best_report_candidate(sources)
            except ValidationError:
                return _report_needs_input()
            selected_candidates = [dict(candidate)]
        available = candidate.get("module_run_refs")
        available_modules = [
            name for name in _REPORT_MODULES
            if isinstance(available, Mapping) and isinstance(available.get(name), Mapping)
        ]
        if not available_modules:
            return _report_needs_input()
        if requested_kind == "quote":
            quote_items: list[dict[str, Any]] = []
            for selected_candidate in selected_candidates:
                selected_available = selected_candidate.get("module_run_refs")
                quote_module = "pricing"
                if not isinstance(selected_available, Mapping) or not isinstance(
                    selected_available.get(quote_module), Mapping,
                ):
                    return _report_needs_input()
                quote_refs = _current_verified_report_refs(
                    selected_available, [quote_module], identity=identity, task_id=task_id,
                )
                if quote_refs is None:
                    return _report_needs_input()
                quote_items.append({
                    "source_id": source["source_id"],
                    "candidate_id": selected_candidate["candidate_id"],
                    "module": quote_module,
                    "module_run_ref": quote_refs[quote_module],
                })
            title = str(arguments.get("title") or candidate.get("product_name") or "参考报价").strip()
            return {
                "action": "run",
                "task_id": task_id,
                "kind": "quote",
                "selection": {
                    "source_id": source["source_id"],
                    "quote_items": quote_items,
                    "output_type": "quote",
                    "format": requested_format,
                    "audience": identity.audience,
                    "report_run_id": report_run_id or f"chat-quote-{uuid4().hex[:16]}",
                    "metadata": {"title": title},
                },
            }
        modules = list(_REPORT_MODULES) if requested_kind == "report" else available_modules
        if delivery_mode == "comparison" and requested_candidate_ids is None:
            if requested_kind not in {"card", "report"}:
                raise ValidationError("横向对比只适用于研究简报或完整研究报告")
            selected_candidates = [
                dict(item)
                for item in source.get("candidates", [])
                if isinstance(item, Mapping)
            ]
            selected_candidates.sort(key=lambda item: (int(item.get("rank", 10_000)), str(item.get("candidate_id", ""))))
            if len(selected_candidates) < 2:
                return {
                    "status": "needs_input",
                    "message": "当前任务只有一个可验证合同版本，至少需要两个候选才能生成多结构对比交付。",
                }
        refs_by_candidate: dict[str, dict[str, dict[str, str] | None]] = {}
        for selected_candidate in selected_candidates:
            selected_id = str(selected_candidate.get("candidate_id", ""))
            selected_available = selected_candidate.get("module_run_refs")
            if not selected_id or not isinstance(selected_available, Mapping):
                return _report_needs_input()
            selected_refs = _current_verified_report_refs(
                selected_available,
                modules,
                identity=identity,
                task_id=task_id,
            )
            if selected_refs is None:
                return _report_needs_input()
            refs_by_candidate[selected_id] = selected_refs
        title = str(arguments.get("title") or candidate.get("product_name") or "期权结构研究").strip()
        if delivery_mode == "comparison" and "title" not in arguments:
            title = "多结构研究简报" if requested_kind == "card" else "多结构完整报告"
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [str(item["candidate_id"]) for item in selected_candidates],
            "selected_modules": modules,
            "module_run_refs": refs_by_candidate,
            "delivery_mode": delivery_mode,
            "output_type": requested_kind,
            "format": requested_format,
            "audience": identity.audience,
            "report_run_id": report_run_id or f"chat-report-{uuid4().hex[:16]}",
            "metadata": {"title": title},
        }
        return {"action": "run", "task_id": task_id, "kind": requested_kind, "selection": selection}


def _with_report_delivery(response: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a public delivery receipt without exposing Reporter internals."""

    result = dict(response)
    selection = prepared.get("selection") if isinstance(prepared.get("selection"), Mapping) else {}
    kind = str(selection.get("output_type") or prepared.get("kind") or "").strip().lower()
    output_format = str(selection.get("format") or "").strip().lower()
    selected = selection.get("selected_modules")
    selected_modules = [
        name for name in _REPORT_MODULES
        if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)) and name in selected
    ]
    selection_refs = selection.get("module_run_refs")
    candidate_refs: dict[str, Mapping[str, Any]] = {}
    if isinstance(selection_refs, Mapping):
        candidate_ids = selection.get("candidate_ids")
        if isinstance(candidate_ids, Sequence) and not isinstance(candidate_ids, (str, bytes)):
            candidate_refs = {
                str(candidate_id): raw
                for candidate_id in candidate_ids
                if isinstance((raw := selection_refs.get(str(candidate_id))), Mapping)
            }
    missing_modules = [
        name for name in _REPORT_MODULES
        if name not in selected_modules or any(
            not isinstance(refs.get(name), Mapping)
            for refs in candidate_refs.values()
        )
    ] if kind == "report" and selection else []
    receipt: dict[str, Any] = {
        "kind": kind,
        "format": output_format,
    }
    if selection:
        receipt.update({
            "delivery_mode": str(selection.get("delivery_mode") or "single"),
            "coverage_status": "partial" if missing_modules else "complete",
            "included_modules": selected_modules,
            "missing_modules": missing_modules,
        })
    for field in ("preview_url", "download_url"):
        if isinstance(result.get(field), str) and result[field]:
            receipt[field] = result[field]
    output = result.get("output")
    if isinstance(output, Mapping) and isinstance(output.get("report"), str):
        receipt["artifact_name"] = output["report"]
    result["delivery"] = receipt
    return result


def _analysis_status(response: Mapping[str, Any]) -> str:
    """保留正式计算的终态，不以交付层partial覆盖它。"""

    status = str(response.get("status", "")).strip().lower()
    aliases = {"succeeded": "completed", "complete": "completed", "timeout": "timed_out", "unavailable": "unsupported"}
    normalized = aliases.get(status, status)
    return normalized if normalized in {"completed", "partial", "failed", "unsupported", "cancelled", "timed_out"} else "failed"


def _best_report_candidate(sources: list[object]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select the latest verified run group without inventing a task contract."""

    for raw_source in reversed(sources):
        if not isinstance(raw_source, Mapping):
            continue
        source = dict(raw_source)
        candidates: list[dict[str, Any]] = []
        for raw_candidate in source.get("candidates", []):
            if isinstance(raw_candidate, Mapping):
                candidates.append(dict(raw_candidate))
        candidates.sort(key=lambda candidate: (
            int(candidate.get("rank", 10_000)),
            str(candidate.get("candidate_id", "")),
        ))
        if candidates:
            return source, candidates[0]
    raise ValidationError("当前任务没有可用的报告候选")


def _current_verified_report_refs(
    available: Mapping[str, Any],
    modules: Sequence[str],
    *,
    identity: SessionIdentity,
    task_id: str,
) -> dict[str, dict[str, str] | None] | None:
    """仅把当前任务Store已验证的完整Core ModuleRunRef交给Reporter。"""

    selected: dict[str, dict[str, str] | None] = {}
    for display_module in modules:
        raw = available.get(display_module)
        if not isinstance(raw, Mapping):
            selected[display_module] = None
            continue
        try:
            reference = ModuleRunRef(**{field: raw.get(field) for field in _MODULE_RUN_REF_FIELDS})
        except (TypeError, ValueError):
            return None
        if (
            reference.module != _REPORT_TO_COMPUTE_MODULE[display_module]
            or reference.tenant_id != identity.tenant_id
            or reference.task_id != task_id
            or str(raw.get("status", "succeeded")).lower() != "succeeded"
        ):
            return None
        selected[display_module] = {field: str(raw[field]) for field in _MODULE_RUN_REF_FIELDS}
    return selected


def _report_needs_input() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "needs_input",
        "message": "当前任务尚无已完成分析。您可以直接发起结构推荐并生成交付，也可以先单独运行收益结构、估值或回测后再整理。",
    }


def _tool_name(name: str) -> tuple[str, str]:
    module, separator, action = str(name).strip().lower().partition(".")
    if not separator or not module or not action:
        raise ValidationError("Agent工具名称必须使用module.action")
    if module not in {"knowledger", "recommender", "datafetcher", "payoffer", "pricer", "backtester", "reporter"}:
        raise AuthorizationError("conversation.tool.run", "工具不在受限Agent目录中")
    permitted = {
        ("knowledger", "search"), ("recommender", "run"), ("datafetcher", "status"),
        ("datafetcher", "fetch"), ("datafetcher", "fetch_calendar"),
        ("payoffer", "run"), ("pricer", "run"), ("backtester", "run"), ("reporter", "run"),
    }
    if (module, action) not in permitted:
        raise AuthorizationError("conversation.tool.run", "工具动作不在受限Agent目录中")
    return module, action


def _matches(query: str, product_id: str, product: Mapping[str, Any]) -> bool:
    if not query:
        return False
    identity = product.get("identity", {})
    name = str(identity.get("name_zh", "")).lower() if isinstance(identity, Mapping) else ""
    if product_id.lower() in query or name in query:
        return True
    directional_hints = (
        (("上涨", "看涨", "上行", "走高"), "看涨"),
        (("下跌", "看跌", "下行", "走低"), "看跌"),
    )
    if any(any(term in query for term in terms) and target in name for terms, target in directional_hints):
        return True
    return any(token and token in name for token in re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]{2,}", query))


def _optionlist_excerpt(path: Path, product_id: str, fallback: str) -> str:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if f"| {product_id} |" in line or f"|{product_id}|" in line.replace(" ", ""):
                return line[:180]
    return f"{product_id} {fallback}"


def _optionlib_excerpt(path: Path, product_id: str, fallback: str) -> str | None:
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        match = re.search(rf"^###\s+{re.escape(product_id)}\s+.*$", text, re.MULTILINE)
        if match:
            end = text.find("\n### ", match.end())
            return text[match.start(): len(text) if end < 0 else end][:1800]
    return None


def _evidence(
    product_id: str,
    source: str,
    excerpt: str,
    identity: Mapping[str, Any],
    entry_status: bool,
    catalog_version: str,
    library_status: str = "ready",
) -> dict[str, Any]:
    normalized = str(excerpt).strip() or product_id
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "evidence_id": hashlib.sha256(f"{source}:{product_id}:{digest}".encode("utf-8")).hexdigest()[:24],
        "product_id": product_id,
        "catalog_version": catalog_version,
        "source": source,
        "section": product_id,
        "library_status": library_status,
        "excerpt_hash": digest,
        "excerpt": normalized,
        "identity": dict(identity),
        "entry_status": entry_status,
    }


def _load_capability_registry(capability_root: Path) -> Mapping[str, Any]:
    """Load OptionReg from the same verified Capability as OptionList/Lib.

    ``runtime.knowledger.load_registry`` intentionally resolves a bootstrap
    root.  Using it here would mix development OptionReg with a temporary or
    embedded Capability during App tests.  This bounded loader only accepts
    the Capability's declared read-only reference file.
    """

    path = capability_root / "scripts" / "knowledger" / "optionreg.py"
    try:
        registry = run_path(str(path)).get("REGISTRY")
    except (OSError, SyntaxError) as error:
        raise ValidationError("已验证Capability中缺少可读取的OptionReg") from error
    if not isinstance(registry, Mapping) or set(registry) != {"term_catalog", "products"}:
        raise ValidationError("Capability OptionReg结构无效")
    if not isinstance(registry.get("products"), Mapping):
        raise ValidationError("Capability OptionReg.products无效")
    return registry


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("requested_outputs必须为字符串数组")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _requested_outputs(arguments: Mapping[str, Any], constraints: Mapping[str, Any]) -> tuple[str, ...]:
    """只保留用户显式要求的交付类型，避免把Card误当成管理员默认Report。"""

    output_type = str(constraints.get("output_type", "")).strip().lower()
    if output_type == "both":
        return ("card", "report")
    if output_type in {"card", "quote", "report"}:
        return (output_type,)
    return _text_tuple(arguments.get("requested_outputs", ()))


def _confirmed_execution_modules(requested_outputs: set[str]) -> tuple[str, ...]:
    modules: set[str] = set()
    aliases = {
        "payoff": "payoffer", "payoffer": "payoffer",
        "pricing": "pricer", "pricer": "pricer",
        "backtest": "backtester", "backtester": "backtester",
    }
    modules.update(aliases[item] for item in requested_outputs if item in aliases)
    if requested_outputs.intersection({"card", "report"}):
        modules.update({"payoffer", "pricer", "backtester"})
    if "quote" in requested_outputs:
        modules.add("pricer")
    if not modules:
        modules.add("payoffer")
    return tuple(module for module in ("payoffer", "pricer", "backtester") if module in modules)


def _requested_candidate_count_argument(arguments: Mapping[str, Any]) -> int | None:
    values: list[object] = []
    for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
        if key in arguments:
            values.append(arguments[key])
    scalars: list[int] = []
    for raw in values:
        value = raw
        if isinstance(raw, Mapping):
            nested = [raw[key] for key in ("requested_candidate_count", "candidate_count") if key in raw]
            if not nested:
                continue
            if len(nested) > 1 and nested[0] != nested[1]:
                raise ValidationError("候选数量字段冲突")
            value = nested[0]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("requested_candidate_count必须为1至10的整数")
        scalars.append(value)
    if scalars and any(item != scalars[0] for item in scalars[1:]):
        raise ValidationError("候选数量字段冲突")
    if not scalars:
        return None
    if not 1 <= scalars[0] <= 10:
        raise ValidationError("requested_candidate_count必须位于1至10")
    return scalars[0]


def _same_pending_user_turn(value: object, prompt: object) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("role", "")).lower() == "user"
        and str(value.get("content", "")).strip() == str(prompt or "").strip()
    )


def _pending_recommendation_state(
    recommendation: object,
    *,
    identity: SessionIdentity,
    analysis_case_id: str,
    confirmed_constraints: Mapping[str, Any],
    preset_id: str,
) -> dict[str, Any] | None:
    """Project only current candidate inputs onto the TaskService schema."""

    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return None
    raw_candidates = recommendation.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping) or str(raw_candidate.get("library_status", "")) != "ready":
            continue
        missing_inputs = raw_candidate.get("missing_inputs", ())
        if (
            isinstance(missing_inputs, Sequence)
            and not isinstance(missing_inputs, str)
            and any(str(item).strip() for item in missing_inputs)
        ):
            continue
        underlyings = raw_candidate.get("underlyings")
        if isinstance(underlyings, str) or not isinstance(underlyings, Sequence):
            continue
        ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
        candidate_id = str(raw_candidate.get("candidate_id", "")).strip()
        product_id = str(raw_candidate.get("product_id", "")).strip()
        rule_revision = raw_candidate.get("rule_revision")
        module_run_refs = _pending_module_run_refs(raw_candidate.get("module_run_refs", ()))
        current_inputs = raw_candidate.get("current_inputs", {})
        if (
            not candidate_id
            or candidate_id in seen
            or not product_id
            or not ordered_underlyings
            or isinstance(rule_revision, bool)
            or not isinstance(rule_revision, int)
            or rule_revision < 1
            or module_run_refs is None
            or not isinstance(current_inputs, Mapping)
        ):
            continue
        seen.add(candidate_id)
        candidates.append({
            "candidate_id": candidate_id,
            "product_id": product_id,
            "rule_revision": rule_revision,
            "product_name": str(raw_candidate.get("product_name", "")).strip(),
            "underlyings": ordered_underlyings,
            "library_status": "ready",
            "current_inputs": dict(current_inputs),
            "module_run_refs": module_run_refs,
            "public_projection": _public_candidate_projection(raw_candidate, identity),
        })
    if not candidates:
        return None
    ranking_spec = recommendation.get("ranking_spec")
    return {
        "status": "pending_approval",
        "state_schema": PENDING_RECOMMENDATION_SCHEMA_ID,
        "analysis_case_id": analysis_case_id,
        "workflow_mode": str(recommendation.get("workflow_mode", "single_agent")),
        "preset_id": preset_id,
        "confirmed_constraints": dict(confirmed_constraints),
        "ranking_spec": dict(ranking_spec) if isinstance(ranking_spec, Mapping) else None,
        "delivery": None,
        "candidates": candidates,
        "approved_candidate_ids": [],
        "requested_outputs": [
            str(item).strip().lower()
            for item in recommendation.get("requested_outputs", ())
            if str(item).strip()
        ],
    }


def _public_candidate_projection(
    candidate: Mapping[str, Any], identity: SessionIdentity,
) -> dict[str, Any]:
    """Keep only the reviewed customer-facing candidate explanation."""

    def text(value: object, fallback: str = "") -> str:
        cleaned = redact_text(value, identity, limit=800).strip()
        return cleaned if cleaned and "[REDACTED]" not in cleaned else fallback

    def items(value: object) -> list[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            return []
        return [cleaned for item in value[:16] if (cleaned := text(item))]

    return {
        "reason": text(candidate.get("reason"), "与已确认的市场观点和风险约束匹配。"),
        "suitable_for": items(candidate.get("suitable_for")),
        "not_suitable_for": items(candidate.get("not_suitable_for")),
        "main_risks": items(candidate.get("main_risks")),
        "key_terms": _public_display_terms(candidate.get("key_terms"), identity),
    }


def _public_display_terms(value: object, identity: SessionIdentity) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result: list[dict[str, str]] = []
    for row in value[:64]:
        if not isinstance(row, Mapping):
            continue
        label = redact_text(row.get("label"), identity, limit=120).strip()
        display = redact_text(row.get("value"), identity, limit=240).strip()
        if label and display and "[REDACTED]" not in label and "[REDACTED]" not in display:
            result.append({"label": label, "value": display})
    return result


def _pending_module_run_refs(value: object) -> list[dict[str, str]] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    rows: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        try:
            reference = ModuleRunRef(**{field: raw.get(field) for field in _MODULE_RUN_REF_FIELDS})
        except (TypeError, ValueError):
            return None
        rows.append({field: getattr(reference, field) for field in _MODULE_RUN_REF_FIELDS})
    return sorted(rows, key=lambda item: (item["module"], item["run_id"]))


def _requires_exact_pending_state(recommendation: object) -> bool:
    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return False
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return False
    return bool(candidates)


def _persisted_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Project user constraints onto the TaskService continuation schema.

    Product-owned term overrides are deliberately not duplicated into the
    shared constraint object; each CandidateContract owns its frozen copy.
    """

    return {key: value for key, value in constraints.items() if key != "term_overrides"}


def _pending_shared_constraints(pending: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recover the shared constraints from the authoritative pending state."""

    if not isinstance(pending, Mapping):
        return {}
    stored = pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        return {}
    return dict(stored)


def _pending_constraints(pending: Mapping[str, Any]) -> dict[str, Any]:
    """Return shared selection constraints; per-candidate terms stay in their contracts."""

    stored = pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        raise ValidationError("待确认候选缺少冻结条件")
    return dict(stored)


def _public_failure_metadata(response: Mapping[str, Any]) -> dict[str, str] | None:
    """Keep a direct dispatcher response in the App's existing public shape."""

    nested = response.get("error")
    source = nested if isinstance(nested, Mapping) else response
    failure_code = source.get("failure_code", source.get("code"))
    message = source.get("message", response.get("message", response.get("reason")))
    stage = source.get("stage", response.get("stage"))
    next_step = source.get("next_step", response.get("next_step"))
    if not all(isinstance(value, str) and value for value in (failure_code, message, stage, next_step)):
        return None
    return {
        "failure_code": failure_code,
        "message": message,
        "stage": stage,
        "next_step": next_step,
    }


def _pricing_config_for_resolved_contract(
    contract: Mapping[str, Any],
    confirmed_constraints: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the one formal Pricer configuration from a frozen contract.

    Analytical is selected when the frozen product supports it. A Monte Carlo-only contract cannot be dispatched
    until the user has provided a positive path count. This function is used
    by both candidate evaluation and report detail valuation.
    """

    terms = contract.get("terms")
    if not isinstance(terms, Mapping):
        raise ValidationError("冻结候选缺少正式合同条款")
    methods = tuple(str(value).strip() for value in terms.get("pricing_methods", ()) if str(value).strip())
    if "analytical" in methods:
        return {"model_method": "analytical"}
    if "monte_carlo" not in methods:
        raise ValidationError("冻结候选未声明可用定价方法")
    value = confirmed_constraints.get("path_count")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return {"model_method": "monte_carlo", "path_count": value}


def _selected_candidate_ids(
    prompt: object,
    arguments: Mapping[str, Any],
    pending: Mapping[str, Any] | None,
) -> list[str]:
    """Resolve an ordered user selection to authoritative candidate IDs."""

    if not isinstance(pending, Mapping):
        return []
    raw_candidates = pending.get("candidates")
    if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Sequence):
        return []
    candidates = [dict(item) for item in raw_candidates if isinstance(item, Mapping)]
    candidate_ids = [str(item.get("candidate_id", "")).strip() for item in candidates]
    if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
        return []
    supplied = arguments.get("approved_candidate_ids")
    if supplied is not None:
        if isinstance(supplied, (str, bytes)) or not isinstance(supplied, Sequence):
            raise ValidationError("approved_candidate_ids必须是有序数组")
        selected = [str(item).strip() for item in supplied]
        if not selected or any(not item for item in selected) or len(set(selected)) != len(selected):
            raise ValidationError("approved_candidate_ids不能为空或重复")
        if any(item not in candidate_ids for item in selected):
            raise ValidationError("approved_candidate_ids包含未知candidate_id")
        return selected
    text = str(prompt or "").strip().lower()
    direct = sorted(
        (
            (text.find(candidate_id.lower()), candidate_id)
            for candidate_id in candidate_ids
            if candidate_id.lower() in text
        ),
        key=lambda item: item[0],
    )
    if direct:
        return [candidate_id for _, candidate_id in direct]
    ranked: list[tuple[int, str]] = []
    for matched in re.finditer(r"(?:第|选择|选|候选)\s*(\d{1,2})(?:个|号|名|项)?", text):
        rank = int(matched.group(1))
        if 1 <= rank <= len(candidate_ids):
            ranked.append((matched.start(), candidate_ids[rank - 1]))
    if ranked:
        ordered = [candidate_id for _, candidate_id in sorted(ranked)]
        return list(dict.fromkeys(ordered))
    chinese_numbers = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    chinese_ranked: list[tuple[int, str]] = []
    for matched in re.finditer(r"(?:第|选择|选|候选)\s*([一二三四五六七八九十])(?:个|号|名|项)?", text):
        rank = chinese_numbers[matched.group(1)]
        if 1 <= rank <= len(candidate_ids):
            chinese_ranked.append((matched.start(), candidate_ids[rank - 1]))
    if chinese_ranked:
        ordered = [candidate_id for _, candidate_id in sorted(chinese_ranked)]
        return list(dict.fromkeys(ordered))
    named: list[tuple[int, str]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        for field in ("product_id", "product_name"):
            value = str(candidate.get(field, "")).strip().lower()
            position = text.find(value) if value else -1
            if position >= 0:
                named.append((position, candidate_id))
                break
    if named:
        return list(dict.fromkeys(candidate_id for _, candidate_id in sorted(named)))
    if len(candidate_ids) == 1 and _approval_text(prompt):
        return list(candidate_ids)
    return []


def _has_candidate_selection_intent(prompt: object) -> bool:
    text = str(prompt or "").strip().casefold()
    return bool(re.search(r"(?:选择|选|候选|第|全部|都选|前两个|多选)", text))


def _approved_candidate_ids(pending: Mapping[str, Any]) -> list[str]:
    value = pending.get("approved_candidate_ids")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result = [str(item).strip() for item in value if str(item).strip()]
    return result if len(result) == len(set(result)) else []


def _term_overrides_from_constraints(
    constraints: Mapping[str, Any], capability_root: Path, product_id: str,
) -> dict[str, Any]:
    """Map only typed customer terms that the selected OptionReg product owns."""

    registry = _load_capability_registry(capability_root)
    product = registry.get("products", {}).get(product_id)
    product_terms = product.get("terms") if isinstance(product, Mapping) else None
    if not isinstance(product_terms, Mapping):
        return {}
    result: dict[str, Any] = {}
    supplied = constraints.get("term_overrides")
    if isinstance(supplied, Mapping):
        for raw_key, value in supplied.items():
            key = str(raw_key)
            if key not in product_terms or isinstance(value, (Mapping, list, tuple, bool)):
                continue
            if not isinstance(value, (str, int, float)):
                continue
            result[key] = value
    horizon = str(constraints.get("horizon", "")).strip()
    if "T" not in product_terms or not horizon:
        return result
    months = re.fullmatch(r"(\d+)个月", horizon)
    years = re.fullmatch(r"(\d+)年", horizon)
    if months:
        result["T"] = int(months.group(1)) / 12
    if years:
        result["T"] = int(years.group(1))
    return result


def _display_terms(
    contract: Mapping[str, Any], registry: Mapping[str, Any], constraints: Mapping[str, Any],
) -> list[dict[str, str]]:
    """将Core合同的受控字段投影为确认前可见的专业条款摘要。"""

    identity = contract.get("identity") if isinstance(contract.get("identity"), Mapping) else {}
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    sources = contract.get("term_sources") if isinstance(contract.get("term_sources"), Mapping) else {}
    catalog = registry.get("term_catalog") if isinstance(registry.get("term_catalog"), Mapping) else {}
    result = [{
        "label": "标的",
        "value": "、".join(str(item) for item in identity.get("underlyings", ())),
        "source": "用户输入",
    }]
    if constraints.get("horizon"):
        result.append({"label": "研究期限", "value": str(constraints["horizon"]), "source": "用户输入"})
    labels = {
        "T": "合同期限",
        "K": "执行水平",
        "K1": "低执行水平",
        "K2": "高执行水平",
        "K3": "第三执行水平",
        "K4": "第四执行水平",
        "Pi_0": "权利金",
        "P_net": "净权利金",
        "c": "票息",
        "c_max": "最高票息",
        "alpha": "参与率",
        "H_KO": "敲出障碍",
        "H_KI": "敲入障碍",
        "B": "气囊障碍",
        "observation_price": "观察价格",
        "O_KO": "敲出观察日程",
        "O_KI": "敲入观察日程",
        "Oc": "票息观察日程",
        "exercise_style": "到期行权方式",
        "settlement": "结算方式",
    }
    preferred = (
        "T", "K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "c", "c_max", "alpha",
        "H_KO", "H_KI", "B", "observation_price", "O_KO", "O_KI", "Oc", "exercise_style", "settlement",
    )
    ignored = {"S0", "S0Vec", "N", "Nvar", "Nvega", "G", "pricing_methods", "constraints"}
    ordered_keys = (*preferred, *(key for key in terms if key not in preferred and key not in ignored))
    for key in ordered_keys:
        value = terms.get(key)
        if key not in terms or isinstance(value, (Mapping, list, tuple)):
            continue
        definition = catalog.get(key)
        label = str(definition.get("name_zh", labels.get(key, key))) if isinstance(definition, Mapping) else labels.get(key, key)
        source = "用户输入" if sources.get(key) == "override" else "拟采用参数"
        result.append({"label": label, "value": str(value), "source": source})
    return result


def _normalized_module_status(value: Mapping[str, Any]) -> str:
    raw = str(value.get("status", "")).strip().lower()
    raw = raw or ("succeeded" if value.get("ok") is True else "failed")
    status = {
        "completed": "succeeded", "complete": "succeeded", "unavailable": "unsupported", "timeout": "timed_out",
    }.get(raw, raw)
    if status not in {"succeeded", "partial", "failed", "unsupported", "cancelled", "timed_out"}:
        raise ValidationError("候选预选计算返回未知状态")
    return status


def _continuation_recommendation_set(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None,
    *,
    status: str,
    candidate_status: str = "candidate",
    analysis_status: str = "not_started",
    delivery_status: str = "not_requested",
) -> dict[str, Any]:
    """Restore a public RecommendationSet from current candidate snapshots."""

    state = dict(pending or {})
    constraints = state.get("confirmed_constraints")
    candidate_rows = state.get("candidates")
    approved_candidate_ids = _approved_candidate_ids(state)
    candidates: list[RecommendationCandidate] = []
    if (
        isinstance(constraints, Mapping)
        and isinstance(candidate_rows, Sequence)
        and not isinstance(candidate_rows, (str, bytes))
    ):
        for rank, candidate_data in enumerate(candidate_rows, start=1):
            public_projection = candidate_data.get("public_projection") if isinstance(candidate_data, Mapping) else None
            if not isinstance(candidate_data, Mapping) or not isinstance(public_projection, Mapping):
                continue
            try:
                candidate_id = _identifier(candidate_data.get("candidate_id"), "candidate_id")
                candidates.append(RecommendationCandidate(
                    candidate_id=candidate_id,
                    product_id=_identifier(candidate_data.get("product_id"), "product_id"),
                    rule_revision=int(candidate_data.get("rule_revision")),
                    underlyings=tuple(str(item).strip() for item in candidate_data.get("underlyings", ()) if str(item).strip()),
                    rank=rank,
                    reason=str(public_projection.get("reason", "")),
                    suitable_for=tuple(map(str, public_projection.get("suitable_for", ()))),
                    not_suitable_for=tuple(map(str, public_projection.get("not_suitable_for", ()))),
                    main_risks=tuple(map(str, public_projection.get("main_risks", ()))),
                    library_status="ready",
                    product_name=str(candidate_data.get("product_name", "")).strip() or None,
                    current_inputs=dict(candidate_data.get("current_inputs", {})),
                    key_terms=tuple(
                        dict(item) for item in public_projection.get("key_terms", ())
                        if isinstance(item, Mapping)
                    ),
                    candidate_status=(candidate_status if candidate_id in approved_candidate_ids else "candidate"),
                ))
            except (TypeError, ValueError, ValidationError):
                continue
    requested_outputs = tuple(str(item) for item in state.get("requested_outputs", ()))
    workflow_mode = str(state.get("workflow_mode", "single_agent"))
    if workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
        workflow_mode = "single_agent"
    result = RecommendationSet(
        schema=RECOMMENDATION_SET_SCHEMA,
        task_id=task_id,
        run_id=f"recommend-{task_id}",
        analysis_case_id=str(state.get("analysis_case_id") or f"chat-{task_id}"),
        catalog_version=catalog_version,
        route="recommendation",
        workflow_mode=workflow_mode,
        status=status,
        primary_candidate_id=(candidates[0].candidate_id if candidates else None),
        candidates=tuple(candidates),
        analysis_status=analysis_status,
        delivery_status=delivery_status,
        requested_outputs=requested_outputs,
    )
    public = result.to_dict()
    public["approved_candidate_ids"] = list(approved_candidate_ids)
    return public


def _confirmation_unavailable(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id, catalog_version, pending, status="unavailable", analysis_status="not_started", delivery_status="unavailable",
        ),
        "next_step": "当前候选条款暂无法受控校验。请稍后重试推荐。",
    }


def _approval_text(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text or re.search(
        r"(?:不|别|无需|不用|不要|禁止)[^，。！？,.!?]*(?:确认|同意|执行|批准)", text,
    ) or any(word in text for word in ("不同意", "取消", "先不要")):
        return False
    return any(word in text for word in ("确认", "同意", "按此", "按这个", "继续执行", "继续生成", "继续分析", "可以执行", "好的", "好，"))


def _host_term_change_status(value: object) -> str:
    """Fail closed when the formal parser sees an unparsed term-edit intent."""

    status = classify_term_change(value)
    if status != "none":
        return status
    text = str(value or "").strip().casefold()
    action = r"(?:改|调|调整|设|设置|变|替换|取消|删除|恢复|默认)"
    subject = r"(?:期限|观察(?:方式|频率|日程)?|气囊(?:水平|障碍)?|条款|参数)"
    clauses = (item for item in re.split(r"[，,。；;！？!?]", text) if item.strip())
    return "unresolved" if any(
        re.search(subject, clause) and re.search(action, clause)
        for clause in clauses
    ) else "none"


def _unparsed_term_change(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id,
            catalog_version,
            pending,
            status="pending_approval",
            analysis_status="not_started",
            delivery_status="pending",
        ),
        "next_step": "检测到条款修改意图，但未能解析出完整的新条款。请明确修改字段和数值后再确认。",
    }


def _rejected_term_change(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id,
            catalog_version,
            pending,
            status="pending_approval",
            analysis_status="not_started",
            delivery_status="pending",
        ),
        "next_step": "已保留当前候选的原条款，未写入任何修改。请单独确认后再继续。",
    }


def _saved_recommendation_preset(
    gateway: object,
    identity: SessionIdentity,
    arguments: Mapping[str, Any],
) -> str:
    """Use App settings as the production authority for recommendation topology."""

    resolver = getattr(gateway, "multi_agent_recommendation_preset_for", None)
    if callable(resolver):
        return str(resolver(identity)).strip().lower()
    # Narrow compatibility path for isolated adapters that do not own App
    # settings. Production ModelGateway always implements the resolver.
    return str(arguments.get("recommendation_preset", "sequential-deliberation")).strip().lower()


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text):
        raise ValidationError(f"{field}无效")
    return text


def _safe_model_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Bound long evidence while preserving the typed Recommender contract."""
    text = str(value)
    if len(text) > 80_000:
        raise ValidationError("Recommender步骤上下文超过安全上限")
    return dict(value)


def _contains_hidden_model_field(value: object) -> bool:
    forbidden = ("reasoning", "chain_of_thought", "analysis", "secret", "token", "password", "api_key")
    if isinstance(value, Mapping):
        return any(any(token in str(key).lower() for token in forbidden) or _contains_hidden_model_field(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_hidden_model_field(item) for item in value)
    return False


__all__ = ("AppAgentPort", "AppConversationToolExecutor", "AppKnowledgePort", "AppToolPort", "RecommenderAdapter")
