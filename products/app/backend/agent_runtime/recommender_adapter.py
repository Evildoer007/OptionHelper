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
from datetime import date, timedelta
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
    EvaluationRecord,
    RECOMMENDATION_SET_SCHEMA,
    ModelCapability,
    RecommendationCandidate,
    RecommendationCase,
    RecommendationSet,
)
from modules.recommender.agent_steps import canonical_hash
from modules.recommender.ports import AgentPort, KnowledgePort, ToolPort
from modules.recommender.service import RecommendationInputRequired, RecommenderService
from modules.recommender.config import RecommenderConfig
from modules.recommender.interaction import constraints_fingerprint, merge_confirmed_constraints
from runtime.protocol.models import ModuleRunRef

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from ..page_registry import PageRegistry
from ..stores.contract_store import ContractStore
from ..stores.result_store import ResultStore
from ..task_runtime.task_service import TaskService
from .tool_dispatcher import ToolDispatcher
from .multi_agent import (
    AgentRunConcurrencyGate,
    AgentRunSettlementRegistry,
    LifecycleProjection,
    OneShotAgentRuntime,
    resolve_recommendation_preset,
)
from .runtime_recommender import RuntimeBackedAgentPort, ShadowRuntimeAgentPort


_REPORT_MODULES = ("payoff", "pricing", "backtest")
_REPORT_TO_COMPUTE_MODULE = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
_MODULE_RUN_REF_FIELDS = (
    "module", "tenant_id", "task_id", "run_id",
    "expected_semantic_result_hash", "expected_artifact_manifest_hash",
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

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._runtime.run_step(str(role), _safe_model_payload(payload))
        if _contains_hidden_model_field(result):
            raise ValidationError("Recommender步骤不得返回推理或Secret字段")
        return dict(result)

    def run_steps(self, role: str, payloads: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        results = self._runtime.run_steps(str(role), [_safe_model_payload(payload) for payload in payloads])
        if any(_contains_hidden_model_field(result) for result in results):
            raise ValidationError("Recommender步骤不得返回推理或Secret字段")
        return results

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
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
        self._market_data_refs: dict[tuple[str, ...], Mapping[str, Any]] = {}
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
        candidate_version_id: str,
        round_no: int,
        input_fingerprints: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        """Run a Host-owned pre-selection evaluation for Mode 2 or Mode 4.

        Child agents choose only the bounded module names and term proposals.
        Contract compilation, data acquisition, module execution and fact
        projection remain inside the authenticated App Host.
        """

        underlyings = candidate.get("underlyings", ())
        if round_no > 1:
            self._emit_visible_event("candidate_cycle", "reselecting", "候选未满足约束，正在重新筛选。")
        self._emit_visible_event("host_module", "started", "Host正在运行候选验证模块。")
        ordered_underlyings = tuple(str(item).strip() for item in underlyings if str(item).strip()) if isinstance(underlyings, Sequence) and not isinstance(underlyings, (str, bytes)) else ()
        market_data_ref = None
        if {str(item).strip().lower() for item in modules}.intersection({"pricer", "backtester"}):
            if not ordered_underlyings:
                raise ValidationError("候选预选计算缺少有序标的")
            market_data_ref = self._market_data_refs.get(ordered_underlyings)
            if market_data_ref is None:
                market_data_ref = self._executor.prepare_recommendation_market_data(
                    self._identity, self._task_id, ordered_underlyings,
                )
                self._market_data_refs[ordered_underlyings] = market_data_ref
        try:
            result = self._executor.evaluate_recommendation_candidate(
                self._identity,
                self._task_id,
                candidate=candidate,
                confirmed_constraints=confirmed_constraints,
                modules=modules,
                term_overrides=term_overrides,
                candidate_version_id=candidate_version_id,
                round_no=round_no,
                input_fingerprints=input_fingerprints,
                market_data_ref=market_data_ref,
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
            "Host已返回候选验证结果。" if completed_status == "completed" else "候选验证模块未全部完成。"
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
        pending = self._tasks.pending_recommendation(identity, task_id)
        if isinstance(pending, Mapping) and pending.get("status") == "invalid":
            return _confirmation_unavailable(task_id, catalog_version, pending)
        # TaskService intentionally keeps a compact, non-financial continuation
        # record. Product-owned term overrides remain in the authenticated task
        # conversation and are deterministically recovered from the turns that
        # preceded the current reply. This keeps one source of truth without
        # weakening TaskService's persisted-state allow-list.
        stored_constraints = _pending_constraints_from_history(pending, history)
        if isinstance(pending, Mapping) and pending.get("status") == "approval_prepared":
            return self._resume_prepared_recommendation_approval(
                identity, task_id, catalog_version, pending, stored_constraints,
            )
        if _may_resume_pending(
            pending,
            confirmed_constraints,
            catalog_version,
            explicit_candidate_confirmation=_approval_text(prompt),
            stored_constraints=stored_constraints,
        ):
            assert pending is not None
            expected = _pending_candidate_binding(
                self._tools,
                identity,
                task_id,
                pending,
                stored_constraints,
                self._registry.capability_root,
            )
            if expected is None:
                return _confirmation_unavailable(task_id, catalog_version, pending)
            if not _binding_matches_pending(expected, pending, constraints=stored_constraints):
                return _confirmation_invalidated(task_id, catalog_version, pending)
            prepared = self._tasks.prepare_pending_recommendation_approval(identity, task_id)
            if prepared is None:
                return _confirmation_unavailable(task_id, catalog_version, pending)
            return self._resume_prepared_recommendation_approval(
                identity, task_id, catalog_version, prepared, stored_constraints,
            )
        active_fingerprint_at_start = _recommendation_start_active_fingerprint(self._tools, identity, task_id)
        requested_outputs = _requested_outputs(arguments, confirmed_constraints)
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
            confirmed_constraints=confirmed_constraints,
            approved_candidate_ids=(),
            candidate_contracts={},
        )
        preset_id = str(arguments.get("recommendation_preset", "sequential-deliberation")).strip().lower()
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
        runtime_persistence_sink: RuntimeEventPersistenceSink | None = None
        if self._agent_runtime_mode in {"shadow", "active"}:
            runtime_parent_session = self._tasks.conversation_session_id(identity, task_id)
            runtime_event_log = self._tasks.session_event_log
            runtime_event_log.ensure_session(runtime_parent_session, kind="conversation")

            def persist_runtime_event(event):
                projected = replace(
                    event,
                    payload={**dict(event.payload), "runtime_mode": self._agent_runtime_mode},
                )
                runtime_event_log.append(
                    runtime_parent_session,
                    "runtime.event",
                    {"runtime_event": projected.to_dict()},
                    event_id=f"runtime:{projected.event_id}",
                    ignorable=True,
                )

            runtime_persistence_sink = RuntimeEventPersistenceSink(persist_runtime_event)

            runtime_workflow_id = str((execution_ids or {}).get("workflow_run_id") or f"runtime-{uuid4().hex}")
            try:
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
                    is_cancelled=self._tasks.is_cancelled,
                    event_sink=runtime_persistence_sink,
                    session_root=self._agent_runtime_session_root,
                    root_session_id=str(runtime_parent_session),
                    workflow_id=runtime_workflow_id,
                )
            except Exception as error:
                if self._agent_runtime_mode == "active":
                    raise
                runtime_event_log.append(
                    runtime_parent_session,
                    "runtime.shadow_failure",
                    {"workflow_id": runtime_workflow_id, "error_type": type(error).__name__},
                    ignorable=True,
                )
            else:
                if self._agent_runtime_mode == "active":
                    legacy_agent_port.close()
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
            tool_port=AppToolPort(self._tools, identity, task_id, visible_event_sink=visible_event_sink),
            config=RecommenderConfig(agent_mode="multi", multi_agent_preset=preset_id),
            review_policy_id=review_policy_id,
        )
        workflow = str(arguments.get("workflow", "recommendation")).strip().lower()
        try:
            result = service.recommend_fixed(case, workflow=workflow)
        finally:
            try:
                agent_port.close()
            finally:
                if runtime_persistence_sink is not None:
                    runtime_persistence_sink.close()
        route = result.get("route", {})
        if not isinstance(route, Mapping) or route.get("fixed_recommendation_workflow") is not True:
            raise ValidationError("App内Recommender只允许固定推荐Workflow，不允许freeform")
        if "freeform" in result:
            raise ValidationError("App内Recommender不得返回freeform")
        recommendation = result.get("recommendation_set")
        selected_candidate_id = _selected_candidate_id(prompt, recommendation)
        continuation = _pending_recommendation_state(
            recommendation,
            analysis_case_id=case.analysis_case_id,
            catalog_version=catalog_version,
            confirmed_constraints=confirmed_constraints,
            preset_id=preset.preset_id,
            preset_version=preset.version,
            selected_candidate_id=selected_candidate_id,
        )
        if continuation is not None:
            binding = _evaluated_candidate_binding(
                recommendation,
                continuation,
                continuation["confirmed_constraints"],
            ) or _current_candidate_binding(
                continuation["candidate"],
                continuation["confirmed_constraints"],
                self._registry.capability_root,
            )
            if binding is None:
                return _confirmation_unavailable(task_id, catalog_version)
            continuation["analysis_case_id"] = _confirmation_analysis_case_id(binding["contract_fingerprint"])
            continuation["workflow_mode"] = str(recommendation.get("workflow_mode", "single_agent"))
            continuation["confirmed_constraints"] = _persisted_constraints(continuation["confirmed_constraints"])
            raw_binding_overrides = binding.get("term_overrides")
            if isinstance(raw_binding_overrides, Mapping):
                frozen_overrides = dict(raw_binding_overrides)
            else:
                frozen_overrides = _term_overrides_from_constraints(
                    confirmed_constraints, self._registry.capability_root, continuation["candidate"]["product_id"],
                )
            continuation["contract_fingerprint"] = binding["contract_fingerprint"]
            continuation["term_overrides"] = frozen_overrides
            continuation["term_overrides_fingerprint"] = _term_overrides_fingerprint(frozen_overrides)
            continuation["expected_active_contract_fingerprint"] = active_fingerprint_at_start
            freeze = getattr(self._tools, "freeze_recommendation_candidate", None)
            if callable(freeze):
                try:
                    freeze(identity, task_id, continuation, binding)
                except (AuthorizationError, ValidationError):
                    return _confirmation_unavailable(task_id, catalog_version)
            self._tasks.save_pending_recommendation(identity, task_id, continuation)
            return {
                "route": dict(route),
                "recommendation_set": _with_confirmation_terms(recommendation, continuation["candidate"], binding),
                "next_step": _confirmation_question(continuation["candidate"], binding),
            }
        if _requires_exact_pending_state(recommendation, selected_candidate_id):
            return _confirmation_unavailable(task_id, catalog_version)
        return dict(result)

    def _resume_prepared_recommendation_approval(
        self,
        identity: SessionIdentity,
        task_id: str,
        catalog_version: str,
        pending: Mapping[str, Any],
        stored_constraints: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Finish a prepared cross-store confirmation, including crash recovery."""

        operation_id = pending.get("approval_operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return _confirmation_unavailable(task_id, catalog_version, pending)
        expected = _pending_candidate_binding(
            self._tools, identity, task_id, pending, stored_constraints, self._registry.capability_root,
        )
        if expected is None or not _binding_matches_pending(expected, pending, constraints=stored_constraints):
            self._tasks.invalidate_prepared_recommendation_approval(identity, task_id, operation_id)
            return _confirmation_invalidated(task_id, catalog_version, pending)
        activate = getattr(self._tools, "activate_recommendation_candidate", None)
        if not callable(activate):
            return _confirmation_unavailable(task_id, catalog_version, pending)
        try:
            # ContractStore accepts the already active exact variant as an
            # idempotent success. This is the recovery point after a crash
            # between contract activation and TaskService commit.
            activate(identity, task_id, pending)
        except (AuthorizationError, ValidationError):
            self._tasks.invalidate_prepared_recommendation_approval(identity, task_id, operation_id)
            return _confirmation_invalidated(task_id, catalog_version, pending)
        approved = self._tasks.commit_prepared_recommendation_approval(identity, task_id, operation_id)
        if approved is None or not _binding_matches_pending(
            expected, approved, constraints=_pending_constraints(approved),
        ):
            return _confirmation_unavailable(task_id, catalog_version, pending)
        finalize = getattr(self._tools, "commit_recommendation_candidate_activation", None)
        if not callable(finalize):
            return _confirmation_unavailable(task_id, catalog_version, pending)
        try:
            finalize(identity, task_id, operation_id)
        except (AuthorizationError, ValidationError):
            # The Task commit is durable but the contract remains deliberately
            # invisible. A request/startup recovery will retry this idempotent
            # visibility commit; never expose it prematurely.
            return _confirmation_unavailable(task_id, catalog_version, approved)
        return {
            "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
            "recommendation_set": _continuation_recommendation_set(
                task_id, catalog_version, approved, status="candidate_ready", candidate_status="approved",
                analysis_status="not_started", delivery_status="pending" if approved.get("delivery") else "not_requested",
            ),
            "next_step": "候选与合同条款已确认，控制权已交回主Agent。",
        }


class AppConversationToolExecutor:
    """App-owned tool adapter used only by AgentLoop and Recommender AppToolPort."""

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        registry: PageRegistry,
        results: ResultStore | None = None,
        contracts: ContractStore | None = None,
        tasks: TaskService | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._results = results
        self._contracts = contracts
        self._tasks = tasks
        self._recommender: RecommenderAdapter | None = None

    def bind_recommender(self, recommender: RecommenderAdapter) -> None:
        self._recommender = recommender

    def reconcile_prepared_recommendation_approvals(self) -> int:
        """Idempotently close prepared cross-Store candidate confirmations.

        A candidate activation is hidden from ordinary module access until the
        matching Task approval commits.  This startup/request recovery finishes
        that short durable sequence without re-running a recommendation,
        rebuilding a contract, or giving any child agent Store authority.
        """

        if self._tasks is None or self._contracts is None:
            return 0
        settled = 0
        for identity, task_id, pending in self._tasks.prepared_recommendation_approvals():
            operation_id = pending.get("approval_operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                continue
            try:
                if pending.get("status") == "approval_prepared":
                    self.activate_recommendation_candidate(identity, task_id, pending)
                    committed = self._tasks.commit_prepared_recommendation_approval(identity, task_id, operation_id)
                    if committed is None or committed.get("status") != "approved":
                        continue
                self.commit_recommendation_candidate_activation(identity, task_id, operation_id)
                settled += 1
            except (AuthorizationError, ValidationError):
                # No compensating activation rollback: the activation is
                # invisible until Task commit. A stale prepared record is
                # closed rather than being silently re-targeted.
                try:
                    self._tasks.invalidate_prepared_recommendation_approval(identity, task_id, operation_id)
                except (AuthorizationError, ValidationError):
                    pass
        return settled

    def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        # A request is also a recovery boundary.  If a previous process died
        # mid-confirmation, reconcile before any ordinary module can resolve a
        # contract for this task.
        self.reconcile_prepared_recommendation_approvals()
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
                expected_binding=self._report_binding(identity, task_id),
            )
            if prepared.get("status") == "needs_input":
                return prepared
            response = self._dispatch(identity, task_id, name, module, prepared)
            return _with_report_delivery(response, prepared)
        else:
            payload = {**dict(arguments), "action": action, "task_id": task_id}
        return self._dispatch(identity, task_id, name, module, payload)

    def prepare_recommendation_market_data(
        self,
        identity: SessionIdentity,
        task_id: str,
        underlyings: Sequence[str],
    ) -> Mapping[str, Any]:
        """Acquire one task-bound market snapshot reused across a recommendation batch."""

        ordered = [str(item).strip() for item in underlyings if str(item).strip()]
        if not ordered:
            raise ValidationError("候选预选计算缺少有序标的")
        return self._fetch_report_market_data(identity, task_id, ordered)

    def recommendation_active_contract_fingerprint(
        self, identity: SessionIdentity, task_id: str,
    ) -> str | None:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        active = self._contracts.get(identity, task_id)
        fingerprint = active.get("contract_fingerprint") if isinstance(active, Mapping) else None
        return str(fingerprint) if isinstance(fingerprint, str) else None

    def recommendation_candidate_binding(
        self,
        identity: SessionIdentity,
        task_id: str,
        pending: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        candidate = pending.get("candidate")
        version = pending.get("candidate_version")
        if not isinstance(candidate, Mapping) or not isinstance(version, Mapping):
            return None
        record = self._contracts.get_candidate_variant(
            identity,
            task_id,
            str(version.get("candidate_key", "")),
            str(version.get("candidate_version_id", "")),
        )
        if not isinstance(record, Mapping):
            return None
        if (
            record.get("contract_fingerprint") != pending.get("contract_fingerprint")
            or record.get("catalog_version") != pending.get("catalog_version")
        ):
            return None
        ranking_spec = pending.get("ranking_spec")
        if ranking_spec is not None:
            if not isinstance(ranking_spec, Mapping) or record.get("ranking_spec_anchor") != dict(ranking_spec):
                return None
        resolved_contract = record.get("resolved_contract")
        if not isinstance(resolved_contract, Mapping):
            return None
        constraints = _pending_constraints(pending)
        return {
            "candidate_id": str(candidate.get("candidate_id", "")),
            "constraints_fingerprint": constraints_fingerprint(constraints),
            "contract_fingerprint": str(record["contract_fingerprint"]),
            "display_terms": _display_terms(
                resolved_contract,
                _load_capability_registry(self._registry.capability_root),
                constraints,
            ),
            "term_overrides": dict(pending.get("term_overrides", {})),
            "resolved_contract": dict(resolved_contract),
            "candidate_variant": {
                "candidate_key": str(version.get("candidate_key", "")),
                "candidate_version_id": str(version.get("candidate_version_id", "")),
                "parent_version_id": version.get("parent_candidate_version_id"),
                "revision": version.get("revision"),
            },
            **({"ranking_spec_anchor": dict(ranking_spec)} if isinstance(ranking_spec, Mapping) else {}),
        }

    def freeze_recommendation_candidate(
        self,
        identity: SessionIdentity,
        task_id: str,
        pending: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Ensure every exact continuation has one immutable CandidateVariant."""

        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        version = pending.get("candidate_version")
        if not isinstance(version, Mapping):
            raise ValidationError("待确认候选缺少精确版本")
        candidate_key = str(version.get("candidate_key", ""))
        candidate_version_id = str(version.get("candidate_version_id", ""))
        stored = self._contracts.get_candidate_variant(identity, task_id, candidate_key, candidate_version_id)
        if stored is None:
            resolved_contract = binding.get("resolved_contract")
            if not isinstance(resolved_contract, Mapping):
                raise ValidationError("候选合同版本缺少冻结ResolvedContract")
            stored = self._contracts.put_candidate_variant(
                identity,
                task_id,
                candidate_key,
                candidate_version_id,
                {
                    "contract_fingerprint": binding.get("contract_fingerprint"),
                    "resolved_contract": dict(resolved_contract),
                },
                catalog_version=str(pending.get("catalog_version", "")),
                parent_version_id=version.get("parent_candidate_version_id"),
                revision=int(version.get("revision", 0)),
            )
        if (
            stored.get("contract_fingerprint") != pending.get("contract_fingerprint")
            or stored.get("catalog_version") != pending.get("catalog_version")
        ):
            raise ValidationError("候选合同版本与待确认绑定不一致")
        ranking_spec = pending.get("ranking_spec")
        if ranking_spec is not None:
            if not isinstance(ranking_spec, Mapping):
                raise ValidationError("待确认候选缺少规范化RankingSpec")
            stored = self._contracts.anchor_candidate_ranking_spec(
                identity, task_id, candidate_key, candidate_version_id, ranking_spec,
            )
        return stored

    def activate_recommendation_candidate(
        self,
        identity: SessionIdentity,
        task_id: str,
        pending: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        version = pending.get("candidate_version")
        if not isinstance(version, Mapping):
            raise ValidationError("待确认候选缺少精确版本")
        activated = self._contracts.activate_candidate_variant(
            identity,
            task_id,
            str(version.get("candidate_key", "")),
            str(version.get("candidate_version_id", "")),
            expected_active_contract_fingerprint=pending.get("expected_active_contract_fingerprint"),
            approval_operation_id=str(pending.get("approval_operation_id", "")).strip() or None,
        )
        if activated.get("contract_fingerprint") != pending.get("contract_fingerprint"):
            raise ValidationError("激活合同与待确认候选不一致")
        return activated

    def commit_recommendation_candidate_activation(
        self,
        identity: SessionIdentity,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, Any] | None:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        return self._contracts.commit_candidate_variant_activation(identity, task_id, operation_id)

    def evaluate_recommendation_candidate(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        candidate: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
        modules: Sequence[str],
        term_overrides: Mapping[str, Any],
        candidate_version_id: str,
        round_no: int,
        input_fingerprints: Mapping[str, str] | None = None,
        market_data_ref: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate one recommendation version without giving tools to a model.

        The method intentionally returns only immutable RunRefs and the small
        verified public fact vocabulary from ResultStore. Raw module payloads,
        private ledgers and contract-store records never enter child context.
        """

        if self._contracts is None or self._results is None:
            raise ValidationError("候选预选计算需要ContractStore与ResultStore")
        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        candidate_key = _identifier(candidate.get("candidate_key"), "candidate_key")
        product_id = _identifier(candidate.get("product_id"), "product_id")
        catalog_version = _identifier(candidate.get("catalog_version"), "catalog_version")
        version_id = _identifier(candidate_version_id, "candidate_version_id")
        version = candidate.get("candidate_version")
        if not isinstance(version, Mapping) or str(version.get("version_id", "")) != version_id:
            raise ValidationError("候选预选计算缺少精确CandidateVersion")
        if str(version.get("candidate_key", "")) != candidate_key:
            raise ValidationError("CandidateVersion与candidate_key不一致")
        parent_version_id = version.get("parent")
        if parent_version_id is not None:
            parent_version_id = _identifier(parent_version_id, "parent_candidate_version_id")
        revision = version.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValidationError("CandidateVersion.revision无效")
        underlyings = candidate.get("underlyings")
        if isinstance(underlyings, (str, bytes)) or not isinstance(underlyings, Sequence):
            raise ValidationError("候选预选计算缺少有序标的")
        ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
        if not ordered_underlyings:
            raise ValidationError("候选预选计算缺少有序标的")
        requested_modules = tuple(str(item).strip().lower() for item in modules)
        if (
            len(set(requested_modules)) != len(requested_modules)
            or any(item not in {"payoffer", "pricer", "backtester"} for item in requested_modules)
        ):
            raise ValidationError("候选预选计算模块无效")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or not 1 <= round_no <= 2:
            raise ValidationError("候选预选计算轮次必须为1或2")
        if not isinstance(term_overrides, Mapping):
            raise ValidationError("候选预选条款调整必须为对象")
        supplied_fingerprints = dict(input_fingerprints or {})
        if supplied_fingerprints and set(supplied_fingerprints) != set(requested_modules):
            raise ValidationError("候选预选输入指纹必须覆盖全部计划模块")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in supplied_fingerprints.values()):
            raise ValidationError("候选预选输入指纹无效")
        requested_modules = tuple(sorted(
            requested_modules,
            key=lambda module: (module == "payoffer", requested_modules.index(module)),
        ))

        merged_constraints = dict(confirmed_constraints)
        merged_constraints["term_overrides"] = dict(term_overrides)
        controlled_overrides = _term_overrides_from_constraints(
            merged_constraints, self._registry.capability_root, product_id,
        )
        needs_market_data = bool({"pricer", "backtester"}.intersection(requested_modules))
        data_ref = dict(market_data_ref) if isinstance(market_data_ref, Mapping) else None
        if needs_market_data and data_ref is None:
            raise ValidationError("候选批次缺少共享行情快照")
        # Create the immutable CandidateVariant before dispatching modules. Its
        # fingerprint is the stable identity for both preselection and any
        # later approved delivery, so ResultStore can keep its strict four-part
        # binding while a verified preselection RunRef is genuinely reusable.
        stored_variant = self._contracts.get_candidate_variant(identity, task_id, candidate_key, version_id)
        if not isinstance(stored_variant, Mapping):
            resolved_contract = _preview_candidate_contract(
                self._registry.capability_root,
                product_id=product_id,
                underlyings=ordered_underlyings,
                term_overrides=controlled_overrides,
            )
            contract_fingerprint = str(resolved_contract.get("contract_fingerprint", ""))
            stored_variant = self._contracts.put_candidate_variant(
                identity,
                task_id,
                candidate_key,
                version_id,
                {"contract_fingerprint": contract_fingerprint, "resolved_contract": resolved_contract},
                catalog_version=catalog_version,
                parent_version_id=parent_version_id,
                revision=revision,
            )
        resolved_contract = stored_variant.get("resolved_contract")
        contract_fingerprint = str(stored_variant.get("contract_fingerprint", ""))
        if not isinstance(resolved_contract, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint):
            raise ValidationError("候选预选计算缺少有效ResolvedContract")
        pricing_config = (
            _pricing_config_for_resolved_contract(resolved_contract, confirmed_constraints)
            if "pricer" in requested_modules else None
        )
        if "pricer" in requested_modules and pricing_config is None:
            return {
                "status": "needs_input",
                "message": "该候选需要Monte Carlo估值。请明确提供路径数，例如“MC路径数10000”。",
            }
        binding = {
            "analysis_case_id": _confirmation_analysis_case_id(contract_fingerprint),
            "candidate_id": _candidate_variant_module_identity(candidate_key, version_id),
            "catalog_version": catalog_version,
            "contract_fingerprint": contract_fingerprint,
            "candidate_key": candidate_key,
            "candidate_version_id": version_id,
        }
        candidate_variant = {
            "candidate_key": candidate_key,
            "candidate_version_id": version_id,
            "parent_version_id": parent_version_id,
            "revision": revision,
        }
        records: list[EvaluationRecord] = []
        statuses: dict[str, str] = {}
        run_refs: list[dict[str, str]] = []
        verified_metrics: dict[str, dict[str, Any]] = {}

        for module in requested_modules:
            payload: dict[str, Any] = {
                "action": "run",
                "task_id": task_id,
                "product_id": product_id,
                "identity": {"underlyings": ordered_underlyings},
                "term_overrides": controlled_overrides,
            }
            if module == "pricer":
                if data_ref is None:
                    records.append(_unavailable_evaluation(
                        version_id, module, payload, round_no, "缺少正式行情数据",
                        input_fingerprint=supplied_fingerprints.get(module),
                    ))
                    statuses[module] = "unsupported"
                    continue
                payload.update({"pricing_config": pricing_config, "market_data_refs": [data_ref]})
            elif module == "backtester":
                if data_ref is None:
                    records.append(_unavailable_evaluation(
                        version_id, module, payload, round_no, "缺少正式历史行情数据",
                        input_fingerprint=supplied_fingerprints.get(module),
                    ))
                    statuses[module] = "unsupported"
                    continue
                payload.update({"backtest_config": {}, "historical_data": data_ref})
            try:
                response = self._dispatch(
                    identity, task_id, f"{module}.run", module, payload, binding=binding,
                    candidate_variant=candidate_variant,
                )
                status = _normalized_module_status(response)
                raw_ref = response.get("module_run_ref")
                reference = ModuleRunRef(**dict(raw_ref)) if isinstance(raw_ref, Mapping) else None
                if status in {"succeeded", "partial"} and reference is None:
                    raise ValidationError(f"{module}成功结果缺少ModuleRunRef")
                limitation = None if reference is not None else str(
                    response.get("message") or response.get("reason") or f"{module}未完成预选计算"
                )
                record = EvaluationRecord(
                    evaluation_id=f"eval_{canonical_hash({'version': version_id, 'module': module, 'input': payload})[:24]}",
                    version_id=version_id,
                    module=module,
                    input_fingerprint=supplied_fingerprints.get(module) or canonical_hash(payload),
                    status=status,
                    module_run_ref=reference,
                    limitation=limitation,
                    idempotency_state="completed",
                    round_no=round_no,
                    source_mode="reused" if response.get("idempotent_replay") else "live" if reference is not None else "unavailable",
                )
                if reference is not None:
                    reference_payload = {field: getattr(reference, field) for field in _MODULE_RUN_REF_FIELDS}
                    stored_variant = self._contracts.get_candidate_variant(identity, task_id, candidate_key, version_id)
                    if not isinstance(stored_variant, Mapping):
                        raise ValidationError("ModuleRun缺少候选合同版本绑定")
                    expected_binding = {
                        **binding,
                        "contract_fingerprint": str(stored_variant.get("contract_fingerprint", "")),
                    }
                    summary = self._results.verified_fact_summary(
                        identity, reference_payload, expected_binding=expected_binding,
                    )
                    for fact in summary.get("facts", ()):  # verified bounded projection only
                        if not isinstance(fact, Mapping):
                            continue
                        metric = str(fact.get("metric", "")).strip()
                        value = fact.get("value")
                        if (
                            metric
                            and isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(float(value))
                        ):
                            verified_metrics[metric] = {
                                "value": value,
                                "unit": fact.get("unit"),
                                "fact_ref": fact.get("fact_ref"),
                                "source": module,
                            }
                    run_refs.append(reference_payload)
            except (AuthorizationError, UnavailableCapabilityError, ValidationError) as error:
                status = "failed"
                record = _unavailable_evaluation(
                    version_id, module, payload, round_no, str(error), status=status,
                    input_fingerprint=supplied_fingerprints.get(module),
                )
            records.append(record)
            statuses[module] = status

        terms = resolved_contract.get("terms")
        if isinstance(terms, Mapping):
            premium = terms.get("P_net", terms.get("Pi_0"))
            if (
                isinstance(premium, (int, float))
                and not isinstance(premium, bool)
                and math.isfinite(float(premium))
            ):
                verified_metrics["premium"] = {
                    "value": premium,
                    "unit": "contract",
                    "fact_ref": f"fact_{canonical_hash({'contract': contract_fingerprint, 'metric': 'premium', 'value': premium})[:24]}",
                    "source": "contract_terms",
                }
        return {
            "candidate_version_id": version_id,
            "contract_fingerprint": contract_fingerprint,
            "display_terms": _display_terms(resolved_contract, _load_capability_registry(self._registry.capability_root), merged_constraints),
            "evaluation_records": [record.to_dict() for record in records],
            "module_run_refs": run_refs,
            "module_statuses": statuses,
            "verified_metrics": verified_metrics,
            "term_overrides": controlled_overrides,
        }

    def _run_pending_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Independent main-Agent-owned delivery transition after approval."""

        if self._tasks is None:
            raise ValidationError("推荐交付状态机未配置")
        if set(arguments).difference({"kind", "format"}):
            raise ValidationError("推荐交付参数只允许kind或format")
        kind = str(arguments.get("kind", "")).strip().lower()
        output_format = str(arguments.get("format", "html")).strip().lower()
        if kind not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
            return {
                "status": "needs_input",
                "message": "请选择Card、Quote或Report；默认生成HTML，需要PDF时请明确指定。",
            }
        pending = self._tasks.pending_recommendation(identity, task_id)
        if pending is None or pending.get("status") != "approved":
            return {"status": "needs_input", "message": "请先确认推荐候选与合同条款。"}
        requested = {"kind": kind, "format": output_format}
        frozen_constraints = _pending_constraints(pending)
        expected = _pending_candidate_binding(
            self,
            identity,
            task_id,
            pending,
            frozen_constraints,
            self._registry.capability_root,
        )
        if expected is None or not _binding_matches_pending(expected, pending, constraints=frozen_constraints):
            return {"status": "needs_input", "message": "候选精确版本或合同绑定已失效，请重新推荐。"}
        if pending.get("delivery") is None:
            pending = self._tasks.choose_pending_recommendation_delivery(
                identity,
                task_id,
                requested,
            )
        if pending is None or pending.get("delivery") != requested:
            raise ValidationError("当前候选已绑定不同交付类型")
        result = self.run_recommendation_delivery(
            identity,
            task_id,
            analysis_case_id=str(pending["analysis_case_id"]),
            catalog_version=str(pending["catalog_version"]),
            candidate=pending["candidate"],
            delivery=requested,
            confirmed_constraints=frozen_constraints,
            frozen_binding=expected,
            frozen_candidate_variant=expected.get("candidate_variant") if isinstance(expected, Mapping) else None,
            module_run_refs=pending.get("module_run_refs"),
        )
        if result.get("status") == "completed":
            self._tasks.complete_pending_recommendation(identity, task_id)
        return result

    def _report_binding(self, identity: SessionIdentity, task_id: str) -> dict[str, str] | None:
        """Derive Reporter matching fields from the task's immutable contract binding."""

        if self._contracts is None:
            return None
        binding = self._contracts.get(identity, task_id)
        if not isinstance(binding, Mapping):
            return None
        fingerprint = binding.get("contract_fingerprint")
        catalog_version = binding.get("catalog_version")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(catalog_version, str) or not catalog_version:
            return None
        return {
            "analysis_case_id": f"case-{fingerprint[:24]}",
            "candidate_id": f"candidate-{fingerprint[:24]}",
            "catalog_version": catalog_version,
            "contract_fingerprint": fingerprint,
        }

    def run_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
        delivery: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any] | None = None,
        frozen_binding: Mapping[str, Any] | None = None,
        frozen_candidate_variant: Mapping[str, Any] | None = None,
        module_run_refs: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run one approved delivery while preserving the existing public shape."""

        result = self.run_recommendation_deliveries(
            identity,
            task_id,
            analysis_case_id=analysis_case_id,
            catalog_version=catalog_version,
            candidate=candidate,
            deliveries=(delivery,),
            confirmed_constraints=confirmed_constraints,
            frozen_binding=frozen_binding,
            frozen_candidate_variant=frozen_candidate_variant,
            module_run_refs=module_run_refs,
        )
        if result.get("status") == "completed":
            completed = result.get("deliveries")
            if isinstance(completed, list) and len(completed) == 1 and isinstance(completed[0], Mapping):
                return {
                    "status": "completed",
                    "analysis_status": str(result.get("analysis_status", "completed")),
                    "delivery_status": str(result.get("delivery_status", "completed")),
                    **dict(completed[0]),
                }
        return result

    def run_recommendation_deliveries(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]],
        confirmed_constraints: Mapping[str, Any] | None = None,
        frozen_binding: Mapping[str, Any] | None = None,
        frozen_candidate_variant: Mapping[str, Any] | None = None,
        module_run_refs: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run the exact analysis coverage required by the requested delivery."""

        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        product_id = _identifier(candidate.get("product_id"), "product_id")
        underlyings = candidate.get("underlyings")
        if isinstance(underlyings, str) or not isinstance(underlyings, Sequence):
            raise ValidationError("推荐候选缺少有序标的")
        ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
        if not ordered_underlyings:
            raise ValidationError("推荐候选缺少有序标的")
        binding = {
            "analysis_case_id": _identifier(analysis_case_id, "analysis_case_id"),
            "candidate_id": candidate_id,
            "catalog_version": _identifier(catalog_version, "catalog_version"),
        }
        expected = dict(frozen_binding) if isinstance(frozen_binding, Mapping) else _current_candidate_binding(
            candidate, confirmed_constraints or {}, self._registry.capability_root,
        )
        if expected is None:
            return {
                "status": "partial",
                "next_step": "当前候选条款无法完成受控校验。请重新确认候选和条款后再试。",
            }
        if (
            expected.get("candidate_id") != candidate_id
            or not isinstance(expected.get("contract_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("contract_fingerprint")))
        ):
            return {
                "status": "partial",
                "next_step": "冻结候选合同绑定无效，已停止交付。请重新推荐。",
            }
        binding["contract_fingerprint"] = str(expected["contract_fingerprint"])
        if frozen_binding is not None and not isinstance(frozen_candidate_variant, Mapping):
            return {
                "status": "partial",
                "next_step": "冻结候选版本不可用，已停止交付。请重新推荐。",
            }
        if isinstance(frozen_candidate_variant, Mapping):
            try:
                candidate_key = _identifier(frozen_candidate_variant.get("candidate_key"), "candidate_key")
                candidate_version_id = _identifier(
                    frozen_candidate_variant.get("candidate_version_id"), "candidate_version_id",
                )
            except ValidationError:
                return {
                    "status": "partial",
                    "next_step": "冻结候选版本不可用，已停止交付。请重新推荐。",
                }
            binding.update({
                "candidate_id": _candidate_variant_module_identity(candidate_key, candidate_version_id),
                "candidate_key": candidate_key,
                "candidate_version_id": candidate_version_id,
            })
        requested_kinds = {
            str(delivery.get("kind", "")).strip().lower()
            for delivery in deliveries
            if isinstance(delivery, Mapping)
        }
        if not requested_kinds or not requested_kinds.issubset({"card", "quote", "report"}):
            raise ValidationError("推荐交付类型无效")
        pricing_config: dict[str, Any] | None = None
        if "report" in requested_kinds:
            resolved_contract = expected.get("resolved_contract")
            if not isinstance(resolved_contract, Mapping):
                return {
                    "status": "partial",
                    "next_step": "冻结候选缺少正式合同快照，已停止交付。请重新推荐。",
                }
            pricing_config = _pricing_config_for_resolved_contract(resolved_contract, confirmed_constraints or {})
            if pricing_config is None:
                return {
                    "status": "needs_input",
                    "message": "该候选需要Monte Carlo估值。请明确提供路径数，例如“MC路径数10000”。",
                }
        term_overrides = (
            dict(expected.get("term_overrides", {}))
            if isinstance(expected.get("term_overrides"), Mapping)
            else _term_overrides_from_constraints(confirmed_constraints or {}, self._registry.capability_root, product_id)
        )
        compute_base = {
            "action": "run", "task_id": task_id, "product_id": product_id,
            "identity": {"underlyings": ordered_underlyings}, "term_overrides": term_overrides,
        }
        reusable = self._verified_frozen_module_runs(identity, task_id, module_run_refs, binding)
        analysis = reusable.get("payoff")
        if analysis is None:
            try:
                analysis = self._dispatch(
                    identity, task_id, "payoffer.run", "payoffer", compute_base, binding=binding,
                    candidate_variant=frozen_candidate_variant,
                )
            except (AuthorizationError, UnavailableCapabilityError, ValidationError):
                analysis = {"ok": False, "status": "failed"}
        analysis_status = _analysis_status(analysis)
        if analysis.get("ok") is not True or str(analysis.get("status", "")).lower() not in {"succeeded", "completed", "partial"}:
            return {
                "status": "partial",
                "analysis_status": analysis_status,
                "delivery_status": "not_requested",
                "next_step": "当前未能完成正式收益分析。请稍后重试，或调整标的和市场观点后重新筛选。",
            }
        if not _analysis_binding_matches(analysis, binding, tenant_id=identity.tenant_id, task_id=task_id):
            return {
                "status": "partial",
                "analysis_status": "failed",
                "delivery_status": "not_requested",
                "next_step": "当前分析结果未能与已确认候选条款一致绑定，已停止生成交付材料。请重新确认后再试。",
            }
        analysis_runs: dict[str, Mapping[str, Any]] = {"payoff": analysis}
        missing_analysis_modules: list[str] = []
        if "report" in requested_kinds:
            data_ref = self._fetch_report_market_data(identity, task_id, ordered_underlyings)
            if data_ref is None:
                missing_analysis_modules.extend(("pricing", "backtest"))
            else:
                detail_payloads = {
                    "pricing": {
                        **compute_base,
                        "pricing_config": pricing_config,
                        "market_data_refs": [data_ref],
                    },
                    "backtest": {
                        **compute_base,
                        "backtest_config": {},
                        "historical_data": data_ref,
                    },
                }
                for display_module, compute_module in _REPORT_TO_COMPUTE_MODULE.items():
                    if display_module == "payoff":
                        continue
                    response = reusable.get(display_module)
                    if response is None:
                        try:
                            response = self._dispatch(
                                identity,
                                task_id,
                                f"{compute_module}.run",
                                compute_module,
                                detail_payloads[display_module],
                                binding=binding,
                                candidate_variant=frozen_candidate_variant,
                            )
                        except (AuthorizationError, UnavailableCapabilityError, ValidationError):
                            response = {"ok": False, "status": "failed"}
                    analysis_runs[display_module] = response
                    if (
                        response.get("ok") is not True
                        or _analysis_status(response) != "completed"
                        or not _analysis_binding_matches(
                            response, binding, tenant_id=identity.tenant_id, task_id=task_id,
                            module=compute_module,
                        )
                    ):
                        missing_analysis_modules.append(display_module)
        aggregate_analysis_status = "partial" if missing_analysis_modules else analysis_status
        # A full report is only meaningful when its requested analysis coverage
        # is complete. Do not pass an incomplete selection to Reporter: that
        # would make an apparently formal report out of partial evidence.
        if "report" in requested_kinds and missing_analysis_modules:
            return {
                "status": "partial",
                "analysis_status": aggregate_analysis_status,
                "delivery_status": "not_requested",
                "missing_modules": list(missing_analysis_modules),
                "next_step": _incomplete_report_next_step(missing_analysis_modules),
                "deliveries": [],
            }
        completed: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        reporter_partial = False
        for delivery in deliveries:
            kind = str(delivery.get("kind", "")).strip().lower()
            output_format = str(delivery.get("format", "")).strip().lower()
            if kind not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
                raise ValidationError("推荐交付类型无效")
            # Designer内部固定Card和连续Report版式，交付请求没有版式参数。
            signature = (kind, output_format)
            if signature in seen:
                continue
            seen.add(signature)
            delivery_key = f"{task_id}:{candidate_id}:{kind}:{output_format}"
            report_run_id = f"chat-{kind}-{hashlib.sha256(delivery_key.encode()).hexdigest()[:16]}"
            report_request: dict[str, Any] = {
                "kind": kind,
                "format": output_format,
                "title": str(candidate.get("product_name") or "期权结构研究"),
            }
            prepared = self._prepare_report(
                identity,
                task_id,
                report_request,
                report_run_id=report_run_id,
                expected_binding=binding,
            )
            if prepared.get("status") == "needs_input":
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "partial",
                    "next_step": _incomplete_report_next_step(missing_analysis_modules),
                    "completed": completed,
                }
            try:
                report = _with_report_delivery(
                    self._dispatch(identity, task_id, "reporter.run", "reporter", prepared),
                    prepared,
                )
            except (AuthorizationError, UnavailableCapabilityError, ValidationError):
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "not_requested",
                    "next_step": _undelivered_card_next_step(kind),
                    "completed": completed,
                }
            report_status = str(report.get("status", "")).lower()
            if report.get("ok") is not True or report_status not in {"succeeded", "completed", "partial"}:
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "partial",
                    "next_step": _incomplete_report_next_step(missing_analysis_modules),
                    "completed": completed,
                }
            reporter_partial = reporter_partial or report_status == "partial"
            receipt = report.get("delivery")
            completed_receipt = dict(receipt) if isinstance(receipt, Mapping) else {"kind": kind, "format": output_format}
            if kind == "report" and missing_analysis_modules:
                completed_receipt.update({
                    "coverage_status": "partial",
                    "missing_modules": list(missing_analysis_modules),
                })
            completed.append(completed_receipt)
        coverage_partial = any(
            isinstance(receipt, Mapping) and receipt.get("coverage_status") == "partial"
            for receipt in completed
        )
        delivery_status = "partial" if coverage_partial or reporter_partial or missing_analysis_modules else "completed"
        return {
            "status": "completed" if aggregate_analysis_status == "completed" and delivery_status == "completed" else "partial",
            "analysis_status": aggregate_analysis_status,
            "delivery_status": delivery_status,
            "deliveries": completed,
        }

    def _verified_frozen_module_runs(
        self,
        identity: SessionIdentity,
        task_id: str,
        references: Sequence[Mapping[str, Any]] | None,
        binding: Mapping[str, str],
    ) -> dict[str, Mapping[str, Any]]:
        """Reuse only pending refs that still prove the exact frozen facts."""

        if getattr(self, "_results", None) is None or references is None:
            return {}
        reusable: dict[str, Mapping[str, Any]] = {}
        for raw in references:
            if not isinstance(raw, Mapping):
                continue
            try:
                reference = ModuleRunRef(**{field: raw.get(field) for field in _MODULE_RUN_REF_FIELDS})
            except (TypeError, ValueError):
                continue
            display_module = next(
                (name for name, compute in _REPORT_TO_COMPUTE_MODULE.items() if compute == reference.module), None,
            )
            if display_module is None or reference.tenant_id != identity.tenant_id or reference.task_id != task_id:
                continue
            try:
                self._results.verified_fact_summary(
                    identity,
                    {field: getattr(reference, field) for field in _MODULE_RUN_REF_FIELDS},
                    expected_binding=binding,
                )
            except (AuthorizationError, KeyError, ValidationError):
                continue
            reusable[display_module] = {
                "ok": True,
                "status": "completed",
                **dict(binding),
                "module_run_ref": {field: getattr(reference, field) for field in _MODULE_RUN_REF_FIELDS},
            }
        return reusable

    def _fetch_report_market_data(
        self, identity: SessionIdentity, task_id: str, underlyings: Sequence[str],
    ) -> dict[str, str] | None:
        """Fetch one task-owned daily asset before detailed pricing and backtest."""

        today = date.today()
        request = {
            "action": "fetch", "task_id": task_id, "asset_ids": list(underlyings),
            "start_date": (today - timedelta(days=730)).isoformat(), "end_date": today.isoformat(),
            "fields": ["close", "adj_close"], "provider": "ifind_http", "frequency": "1d",
            "adjustment": "forward", "cache_policy": "force_refresh", "offline": False,
        }
        try:
            response = self._dispatch(identity, task_id, "datafetcher.fetch", "datafetcher", request)
        except (AuthorizationError, UnavailableCapabilityError, ValidationError):
            return None
        raw_ref = response.get("data_asset_ref") if isinstance(response, Mapping) else None
        if response.get("ok") is not True or not isinstance(raw_ref, Mapping):
            return None
        asset_id = raw_ref.get("data_asset_id")
        content_hash = raw_ref.get("content_hash")
        if not isinstance(asset_id, str) or not asset_id or not isinstance(content_hash, str) or len(content_hash) != 64:
            return None
        return {"data_asset_id": asset_id, "content_hash": content_hash}

    def run_recommendation_card(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compatibility wrapper for the explicit Card-only internal entrypoint."""

        return self.run_recommendation_delivery(
            identity,
            task_id,
            analysis_case_id=analysis_case_id,
            catalog_version=catalog_version,
            candidate=candidate,
            delivery={"kind": "card", "format": "html"},
            confirmed_constraints=None,
        )

    def _dispatch(
        self,
        identity: SessionIdentity,
        task_id: str,
        name: str,
        module: str,
        payload: Mapping[str, Any],
        *,
        binding: Mapping[str, str] | None = None,
        candidate_variant: Mapping[str, Any] | None = None,
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
            catalog_version=binding.get("catalog_version") or str(self._registry.manifest["catalog_version"]),
            contract_fingerprint=binding.get("contract_fingerprint"),
        )
        request_material = json.dumps(
            {
                "task_id": task_id,
                "tool": name,
                "payload": payload,
                "binding": binding,
                "candidate_variant": candidate_variant,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        return dict(self._dispatcher.dispatch_for_conversation(
            module,
            dict(payload),
            identity,
            module_context=context,
            request_id=f"conversation-{hashlib.sha256(request_material.encode()).hexdigest()[:24]}",
            candidate_variant=candidate_variant,
        ))

    def _prepare_report(
        self,
        identity: SessionIdentity,
        task_id: str,
        arguments: Mapping[str, Any],
        *,
        report_run_id: str | None = None,
        expected_binding: Mapping[str, str] | None = None,
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
        allowed = {"kind", "output_type", "format", "title"}
        if set(arguments).difference(allowed):
            raise ValidationError("OptChat报告请求只接受交付类型、格式和标题")
        catalog = self._results.list_owned_report_sources(identity, task_id=task_id)
        sources = catalog.get("sources") if isinstance(catalog, Mapping) else None
        if not isinstance(sources, list) or not sources:
            return _report_needs_input()
        try:
            source, candidate = _best_report_candidate(sources, expected_binding=expected_binding)
        except ValidationError:
            return _report_needs_input()
        available = candidate.get("module_run_refs")
        available_modules = [
            name for name in _REPORT_MODULES
            if isinstance(available, Mapping) and isinstance(available.get(name), Mapping)
        ]
        if not available_modules:
            return _report_needs_input()
        if requested_kind == "quote":
            quote_module = next((name for name in _REPORT_MODULES if name in available_modules), None)
            if quote_module is None:
                return _report_needs_input()
            quote_refs = _current_verified_report_refs(
                available, [quote_module], identity=identity, task_id=task_id,
            )
            if quote_refs is None:
                return _report_needs_input()
            title = str(arguments.get("title") or candidate.get("product_name") or "参考报价").strip()
            return {
                "action": "run",
                "task_id": task_id,
                "kind": "quote",
                "selection": {
                    "source_id": source["source_id"],
                    "quote_items": [{
                        "source_id": source["source_id"],
                        "candidate_id": candidate["candidate_id"],
                        "module": quote_module,
                        "module_run_ref": quote_refs[quote_module],
                    }],
                    "output_type": "quote",
                    "format": requested_format,
                    "audience": identity.audience,
                    "report_run_id": report_run_id or f"chat-quote-{uuid4().hex[:16]}",
                    "metadata": {"title": title},
                },
            }
        modules = list(_REPORT_MODULES) if requested_kind == "report" else available_modules
        module_run_refs = _current_verified_report_refs(
            available,
            modules,
            identity=identity,
            task_id=task_id,
        )
        if module_run_refs is None:
            return _report_needs_input()
        title = str(arguments.get("title") or candidate.get("product_name") or "期权结构研究").strip()
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": modules,
            "module_run_refs": {candidate["candidate_id"]: module_run_refs},
            "delivery_mode": "single",
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
    candidate_refs = {}
    if isinstance(selection_refs, Mapping):
        candidate_ids = selection.get("candidate_ids")
        if isinstance(candidate_ids, Sequence) and not isinstance(candidate_ids, (str, bytes)) and len(candidate_ids) == 1:
            raw = selection_refs.get(str(candidate_ids[0]))
            if isinstance(raw, Mapping):
                candidate_refs = raw
    missing_modules = [
        name for name in _REPORT_MODULES
        if name not in selected_modules or not isinstance(candidate_refs.get(name), Mapping)
    ] if kind == "report" and selection else []
    receipt: dict[str, Any] = {
        "kind": kind,
        "format": output_format,
    }
    if selection:
        receipt.update({
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


def _best_report_candidate(
    sources: list[object], *, expected_binding: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if expected_binding is None:
        raise ValidationError("生成报告前必须确认候选和合同条款")
    for raw_source in reversed(sources):
        if not isinstance(raw_source, Mapping):
            continue
        source = dict(raw_source)
        candidates: list[dict[str, Any]] = []
        for raw_candidate in source.get("candidates", []):
            if isinstance(raw_candidate, Mapping):
                candidates.append(dict(raw_candidate))
        matched = next(
            (
                candidate for candidate in candidates
                if _report_candidate_matches_binding(source, candidate, expected_binding)
            ),
            None,
        )
        if matched is not None:
            return source, matched
    raise ValidationError("当前任务没有可用的报告候选")


def _report_candidate_matches_binding(
    source: Mapping[str, Any], candidate: Mapping[str, Any], expected: Mapping[str, str],
) -> bool:
    """Reporter只能消费本次候选、版本与合同共同绑定的正式结果。"""

    return (
        str(source.get("analysis_case_id", "")) == expected.get("analysis_case_id")
        and str(source.get("catalog_version", "")) == expected.get("catalog_version")
        and str(candidate.get("candidate_id", "")) == expected.get("candidate_id")
        and str(candidate.get("contract_fingerprint", "")) == expected.get("contract_fingerprint")
    )


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


def _same_pending_user_turn(value: object, prompt: object) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("role", "")).lower() == "user"
        and str(value.get("content", "")).strip() == str(prompt or "").strip()
    )


def _pending_recommendation_state(
    recommendation: object,
    *,
    analysis_case_id: str,
    catalog_version: str,
    confirmed_constraints: Mapping[str, Any],
    preset_id: str,
    preset_version: str,
    selected_candidate_id: str | None = None,
) -> dict[str, Any] | None:
    """Project one approved-later selection; financial facts stay out of TaskService."""

    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return None
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return None
    if len(candidates) != 1 and not selected_candidate_id:
        # 多候选必须由客户明确选择，不能把“确认”默认为第一名。
        return None
    primary_id = selected_candidate_id or str(recommendation.get("primary_candidate_id", "")).strip()
    raw_candidate = next(
        (item for item in candidates if isinstance(item, Mapping) and str(item.get("candidate_id", "")).strip() == primary_id),
        None,
    )
    if not isinstance(raw_candidate, Mapping) or str(raw_candidate.get("library_status", "")) != "ready":
        return None
    missing_inputs = raw_candidate.get("missing_inputs", ())
    if (
        isinstance(missing_inputs, Sequence)
        and not isinstance(missing_inputs, str)
        and any(str(item).strip() for item in missing_inputs)
    ):
        return None
    underlyings = raw_candidate.get("underlyings")
    if isinstance(underlyings, str) or not isinstance(underlyings, Sequence):
        return None
    ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
    candidate_id = str(raw_candidate.get("candidate_id", "")).strip()
    product_id = str(raw_candidate.get("product_id", "")).strip()
    if not candidate_id or not product_id or not ordered_underlyings:
        return None
    version = _pending_candidate_version(raw_candidate)
    if version is None:
        return None
    ranking_spec = _pending_ranking_spec(recommendation, preset_id)
    if preset_id == "constraint-ranking" and ranking_spec is None:
        return None
    module_run_refs = _pending_module_run_refs(raw_candidate.get("module_run_refs", ()))
    if module_run_refs is None:
        return None
    contract_fingerprint = str(raw_candidate.get("contract_fingerprint", "")).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint):
        contract_fingerprint = "0" * 64
    raw_overrides = raw_candidate.get("term_overrides", {})
    if not isinstance(raw_overrides, Mapping):
        return None
    term_overrides = {str(key): value for key, value in raw_overrides.items()}
    return {
        "status": "pending_approval",
        "state_schema": "exact-candidate-v1",
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        "workflow_mode": str(recommendation.get("workflow_mode", "single_agent")),
        "preset": {"preset_id": preset_id, "preset_version": preset_version},
        "confirmed_constraints": dict(confirmed_constraints),
        "contract_fingerprint": contract_fingerprint,
        "term_overrides": term_overrides,
        "term_overrides_fingerprint": _term_overrides_fingerprint(term_overrides),
        "expected_active_contract_fingerprint": None,
        "ranking_spec_id": ranking_spec["ranking_spec_id"] if ranking_spec is not None else None,
        "ranking_spec_fingerprint": ranking_spec["ranking_spec_fingerprint"] if ranking_spec is not None else None,
        "ranking_spec": ranking_spec,
        "module_run_refs": module_run_refs,
        "delivery": None,
        "candidate": {
            "candidate_id": candidate_id,
            "product_id": product_id,
            "product_name": str(raw_candidate.get("product_name", "")).strip(),
            "underlyings": ordered_underlyings,
            "library_status": "ready",
        },
        "candidate_version": version,
    }


def _pending_candidate_version(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project only the immutable candidate identity, never evidence or financial output."""

    raw = candidate.get("candidate_version")
    if not isinstance(raw, Mapping):
        return None
    candidate_key = str(raw.get("candidate_key", candidate.get("candidate_key", ""))).strip()
    version_id = str(raw.get("version_id", candidate.get("candidate_version_id", ""))).strip()
    candidate_key_field = str(candidate.get("candidate_key", candidate_key)).strip()
    version_id_field = str(candidate.get("candidate_version_id", version_id)).strip()
    parent = raw.get("parent")
    if parent is not None:
        parent = str(parent).strip()
    revision = raw.get("revision")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", candidate_key)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", version_id)
        or candidate_key != candidate_key_field
        or version_id != version_id_field
        or (parent is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", parent))
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= 100
    ):
        return None
    return {
        "candidate_key": candidate_key,
        "candidate_version_id": version_id,
        "parent_candidate_version_id": parent,
        "revision": revision,
    }


def _pending_ranking_spec(recommendation: Mapping[str, Any], preset_id: str) -> dict[str, Any] | None:
    raw = recommendation.get("ranking_spec")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    value = str(raw.get("ranking_spec_id", "")).strip()
    fingerprint = str(raw.get("ranking_spec_fingerprint", "")).strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or not value.endswith(f".{fingerprint}")
        or recommendation.get("ranking_spec_id") != value
        or recommendation.get("ranking_spec_fingerprint") != fingerprint
    ):
        return None
    expected_keys = {"ranking_spec_id", "ranking_spec_fingerprint", "hard_constraints", "sort_keys", "tie_break_policy"}
    if set(raw) != expected_keys:
        return None
    return {
        "ranking_spec_id": value,
        "ranking_spec_fingerprint": fingerprint,
        "hard_constraints": dict(raw["hard_constraints"]) if isinstance(raw.get("hard_constraints"), Mapping) else raw.get("hard_constraints"),
        "sort_keys": [dict(item) for item in raw.get("sort_keys", ()) if isinstance(item, Mapping)],
        "tie_break_policy": raw.get("tie_break_policy"),
    }


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


def _requires_exact_pending_state(recommendation: object, selected_candidate_id: str | None) -> bool:
    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return False
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return False
    return len(candidates) == 1 or selected_candidate_id is not None


def _may_resume_pending(
    pending: Mapping[str, Any] | None,
    current_constraints: Mapping[str, Any],
    catalog_version: str,
    *,
    explicit_candidate_confirmation: bool = False,
    stored_constraints: Mapping[str, Any] | None = None,
) -> bool:
    if (
        not isinstance(pending, Mapping)
        or pending.get("state_schema") != "exact-candidate-v1"
        or str(pending.get("catalog_version", "")) != catalog_version
    ):
        return False
    try:
        exact_constraints = _pending_constraints(pending)
    except (TypeError, ValueError, ValidationError):
        return False
    stored = stored_constraints if isinstance(stored_constraints, Mapping) else exact_constraints
    if not isinstance(stored, Mapping):
        return False
    # 只有会改变候选或合同解释的条件发生变化时才重建候选；交付种类
    # 与格式是已确认结构之后的独立选择。
    if _candidate_constraints_fingerprint(current_constraints) != _candidate_constraints_fingerprint(stored):
        return False
    status = str(pending.get("status", "")).strip().lower()
    if status == "pending_approval":
        return explicit_candidate_confirmation
    return False


def _persisted_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Project user constraints onto the TaskService continuation schema.

    Product-owned term overrides are deliberately not duplicated into the task
    state. They are recovered from the task's authenticated message history by
    :func:`_pending_constraints_from_history` before any binding or execution.
    """

    return {key: value for key, value in constraints.items() if key != "term_overrides"}


def _pending_constraints_from_history(
    pending: Mapping[str, Any] | None,
    history: Sequence[object],
) -> dict[str, Any]:
    """Recover the frozen pre-reply constraints for a persisted candidate."""

    if not isinstance(pending, Mapping):
        return {}
    stored = pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        return {}
    try:
        frozen = _pending_constraints(pending)
    except (TypeError, ValueError, ValidationError):
        return {}
    # A pending candidate is created after the final user turn. On a later
    # confirmation or adjustment, the final turn is new and must not silently
    # become part of the old contract. Excluding it makes a changed override
    # invalidate the candidate rather than execute it under altered terms.
    earlier_turns: Sequence[object] = history[:-1] if history else ()
    recovered = merge_confirmed_constraints(dict(stored), earlier_turns)
    recovered_overrides = recovered.get("term_overrides", {})
    frozen_overrides = frozen.get("term_overrides", {})
    if recovered_overrides and (
        not isinstance(frozen_overrides, Mapping)
        or any(frozen_overrides.get(key) != value for key, value in recovered_overrides.items())
    ):
        return recovered
    return frozen


def _pending_constraints(pending: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the frozen, exact contract inputs from a validated continuation."""

    stored = pending.get("confirmed_constraints")
    overrides = pending.get("term_overrides")
    fingerprint = str(pending.get("term_overrides_fingerprint", ""))
    if not isinstance(stored, Mapping) or not isinstance(overrides, Mapping):
        raise ValidationError("待确认候选缺少冻结条款")
    normalized = {str(key): value for key, value in overrides.items()}
    if _term_overrides_fingerprint(normalized) != fingerprint:
        raise ValidationError("待确认候选冻结条款指纹不一致")
    result = dict(stored)
    result["term_overrides"] = normalized
    return result


def _term_overrides_fingerprint(value: Mapping[str, Any]) -> str:
    clean = {str(key): value[key] for key in sorted(value)}
    return canonical_hash(clean)


def _candidate_constraints_fingerprint(constraints: Mapping[str, Any]) -> str:
    """Fingerprint only the confirmed fields that select or interpret a contract."""

    candidate_fields = {
        key: value
        for key, value in constraints.items()
        if key in {"underlying", "horizon", "market_view", "max_loss", "principal_fluctuation", "term_overrides"}
    }
    return constraints_fingerprint(candidate_fields)


def _pricing_config_for_resolved_contract(
    contract: Mapping[str, Any],
    confirmed_constraints: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the one formal Pricer configuration from a frozen contract.

    ``auto`` retains Pricer's established BS preference for contracts that
    support both methods. A Monte Carlo-only contract cannot be dispatched
    until the user has provided a positive path count. This function is used
    by both candidate evaluation and report detail valuation.
    """

    terms = contract.get("terms")
    if not isinstance(terms, Mapping):
        raise ValidationError("冻结候选缺少正式合同条款")
    methods = tuple(str(value).strip() for value in terms.get("pricing_methods", ()) if str(value).strip())
    if "black_scholes" in methods:
        return {"model_method": "auto"}
    if "monte_carlo" not in methods:
        raise ValidationError("冻结候选未声明可用定价方法")
    value = confirmed_constraints.get("path_count")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return {"model_method": "auto", "path_count": value}


def _selected_candidate_id(prompt: object, recommendation: object) -> str | None:
    """仅接受明确编号、名称或排序选择，不把泛确认解释为主候选选择。"""

    if not isinstance(recommendation, Mapping):
        return None
    rows = recommendation.get("candidates")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return None
    candidates = [dict(item) for item in rows if isinstance(item, Mapping)]
    if len(candidates) <= 1:
        return None
    text = str(prompt or "").strip().lower()
    rank_match = re.search(r"(?:第|选择|选)([123])(?:个|号|名|项|候选)?", text)
    if rank_match:
        rank = int(rank_match.group(1))
        selected = next((item for item in candidates if item.get("rank") == rank), None)
        if selected is not None:
            return str(selected.get("candidate_id", "")).strip() or None
    for candidate in candidates:
        product_id = str(candidate.get("product_id", "")).strip().lower()
        product_name = str(candidate.get("product_name", "")).strip().lower()
        if (product_id and product_id in text) or (product_name and product_name in text):
            return str(candidate.get("candidate_id", "")).strip() or None
    return None


def _current_candidate_binding(
    candidate: Mapping[str, Any],
    constraints: Mapping[str, Any],
    capability_root: Path,
) -> dict[str, Any] | None:
    """用Core合同引擎重建当前候选的唯一条款快照。

    不在任务状态保存ResolvedContract；该对象仅在本次受控执行前由Core解析，
    指纹随客户条件变化而失效。
    """

    try:
        from runtime.contracts.contract_engine import resolve_contract

        product_id = _identifier(candidate.get("product_id"), "product_id")
        underlyings = candidate.get("underlyings")
        if isinstance(underlyings, (str, bytes)) or not isinstance(underlyings, Sequence):
            return None
        ordered = tuple(str(item).strip() for item in underlyings if str(item).strip())
        if not ordered:
            return None
        registry = _load_capability_registry(capability_root)
        term_overrides = _term_overrides_from_constraints(constraints, capability_root, product_id)
        contract = resolve_contract(
            product_id,
            identity={"underlyings": ordered},
            term_overrides=term_overrides,
            registry=registry,
        ).to_protocol_dict()
        fingerprint = str(contract.get("contract_fingerprint", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return None
        return {
            "candidate_id": _identifier(candidate.get("candidate_id"), "candidate_id"),
            "constraints_fingerprint": constraints_fingerprint(constraints),
            "contract_fingerprint": fingerprint,
            "display_terms": _display_terms(contract, registry, constraints),
            "term_overrides": term_overrides,
            "resolved_contract": contract,
        }
    except Exception:
        return None


def _evaluated_candidate_binding(
    recommendation: object,
    continuation: Mapping[str, Any],
    constraints: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use the exact Host-evaluated contract identity when a Mode already has one."""

    if not isinstance(recommendation, Mapping):
        return None
    rows = recommendation.get("candidates")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return None
    candidate_id = str(continuation.get("candidate", {}).get("candidate_id", "")) if isinstance(continuation.get("candidate"), Mapping) else ""
    candidate = next(
        (row for row in rows if isinstance(row, Mapping) and str(row.get("candidate_id", "")) == candidate_id),
        None,
    )
    if not isinstance(candidate, Mapping):
        return None
    fingerprint = str(candidate.get("contract_fingerprint", "")).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None
    terms = candidate.get("key_terms", ())
    overrides = candidate.get("term_overrides", {})
    if (
        isinstance(terms, (str, bytes))
        or not isinstance(terms, Sequence)
        or not isinstance(overrides, Mapping)
    ):
        return None
    return {
        "candidate_id": candidate_id,
        "constraints_fingerprint": constraints_fingerprint(constraints),
        "contract_fingerprint": fingerprint,
        "display_terms": [dict(item) for item in terms if isinstance(item, Mapping)],
        "term_overrides": dict(overrides),
    }


def _pending_candidate_binding(
    tool_executor: object,
    identity: SessionIdentity,
    task_id: str,
    pending: Mapping[str, Any],
    constraints: Mapping[str, Any],
    capability_root: Path,
) -> Mapping[str, Any] | None:
    exact = getattr(tool_executor, "recommendation_candidate_binding", None)
    if callable(exact):
        try:
            return exact(identity, task_id, pending)
        except (AuthorizationError, ValidationError):
            return None
    # Compatibility for isolated legacy ports only. AppConversationToolExecutor
    # always exposes the exact CandidateVariant lookup above.
    return _current_candidate_binding(pending["candidate"], constraints, capability_root)


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
        "exercise_style": "行权方式",
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


def _unavailable_evaluation(
    version_id: str,
    module: str,
    payload: Mapping[str, Any],
    round_no: int,
    limitation: str,
    *,
    status: str = "unsupported",
    input_fingerprint: str | None = None,
) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation_id=f"eval_{canonical_hash({'version': version_id, 'module': module, 'input': payload})[:24]}",
        version_id=version_id,
        module=module,
        input_fingerprint=input_fingerprint or canonical_hash(payload),
        status=status,
        limitation=str(limitation).strip() or f"{module}未完成预选计算",
        idempotency_state="completed",
        round_no=round_no,
        source_mode="unavailable",
    )


def _active_contract_matches(
    value: Mapping[str, Any],
    *,
    product_id: str,
    underlyings: Sequence[str],
    term_overrides: Mapping[str, Any],
) -> bool:
    contract = value.get("resolved_contract")
    if not isinstance(contract, Mapping):
        return False
    identity = contract.get("identity")
    terms = contract.get("terms")
    sources = contract.get("term_sources")
    if not isinstance(identity, Mapping) or not isinstance(terms, Mapping) or not isinstance(sources, Mapping):
        return False
    if identity.get("product_id") != product_id or tuple(identity.get("underlyings", ())) != tuple(underlyings):
        return False
    active_overrides = {str(key) for key, source in sources.items() if source == "override"}
    if active_overrides != set(term_overrides):
        return False
    return all(terms.get(key) == expected for key, expected in term_overrides.items())


def _preview_candidate_contract(
    capability_root: Path,
    *,
    product_id: str,
    underlyings: Sequence[str],
    term_overrides: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Compile a read-only candidate snapshot when calculation data is absent."""

    try:
        from runtime.contracts.contract_engine import resolve_contract

        return resolve_contract(
            product_id,
            identity={"underlyings": tuple(underlyings)},
            term_overrides=dict(term_overrides),
            registry=_load_capability_registry(capability_root),
        ).to_protocol_dict()
    except Exception as error:
        raise ValidationError("候选条款无法通过Core合同校验") from error


def _with_confirmation_terms(
    recommendation: object, candidate: Mapping[str, Any], binding: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(recommendation) if isinstance(recommendation, Mapping) else {"status": "pending_approval"}
    rows = result.get("candidates")
    if isinstance(rows, list):
        prepared = []
        for item in rows:
            current = dict(item) if isinstance(item, Mapping) else item
            if isinstance(current, dict) and current.get("candidate_id") == candidate.get("candidate_id"):
                current["candidate_status"] = "pending_confirmation"
                current["key_terms"] = list(binding["display_terms"])
            prepared.append(current)
        result["candidates"] = prepared
    result["analysis_status"] = "not_started"
    result["delivery_status"] = "pending" if result.get("requested_outputs") else "not_requested"
    return result


def _confirmation_question(candidate: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    terms = binding.get("display_terms") if isinstance(binding.get("display_terms"), Sequence) else ()
    summary = "；".join(f"{item.get('label')}：{item.get('value')}" for item in terms if isinstance(item, Mapping))
    title = str(candidate.get("product_name") or candidate.get("product_id") or "该候选")
    return f"拟采用{title}。{summary}。请明确回复“确认按此候选和条款继续”，或一次说明需要调整的条件。"


def _binding_matches_pending(
    binding: Mapping[str, Any], pending: Mapping[str, Any], *, constraints: Mapping[str, Any] | None = None,
) -> bool:
    candidate = pending.get("candidate")
    version = pending.get("candidate_version")
    try:
        frozen_constraints = dict(constraints) if isinstance(constraints, Mapping) else _pending_constraints(pending)
    except (TypeError, ValueError, ValidationError):
        return False
    term_overrides = pending.get("term_overrides")
    preset = pending.get("preset")
    preset_id = str(preset.get("preset_id", "")) if isinstance(preset, Mapping) else ""
    ranking_spec_id = pending.get("ranking_spec_id")
    ranking_spec_fingerprint = pending.get("ranking_spec_fingerprint")
    ranking_spec = pending.get("ranking_spec")
    ranking_valid = (
        preset_id != "constraint-ranking"
        or (
            isinstance(ranking_spec_id, str)
            and isinstance(ranking_spec_fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", ranking_spec_fingerprint) is not None
            and ranking_spec_id.endswith(f".{ranking_spec_fingerprint}")
            and isinstance(ranking_spec, Mapping)
            and ranking_spec.get("ranking_spec_id") == ranking_spec_id
            and ranking_spec.get("ranking_spec_fingerprint") == ranking_spec_fingerprint
            and binding.get("ranking_spec_anchor") == dict(ranking_spec)
        )
    )
    return (
        isinstance(candidate, Mapping)
        and isinstance(version, Mapping)
        and isinstance(term_overrides, Mapping)
        and isinstance(frozen_constraints, Mapping)
        and binding.get("candidate_id") == candidate.get("candidate_id")
        and binding.get("constraints_fingerprint") == constraints_fingerprint(frozen_constraints)
        and binding.get("contract_fingerprint") == pending.get("contract_fingerprint")
        and binding.get("contract_fingerprint") == _confirmation_contract_fingerprint(pending.get("analysis_case_id"))
        and _term_overrides_fingerprint(term_overrides) == pending.get("term_overrides_fingerprint")
        and ranking_valid
    )


def _recommendation_start_active_fingerprint(
    tool_executor: object, identity: SessionIdentity, task_id: str,
) -> str | None:
    """Capture the CAS basis before the recommendation may spend time running."""

    getter = getattr(tool_executor, "recommendation_active_contract_fingerprint", None)
    if not callable(getter):
        return None
    value = getter(identity, task_id)
    if value is None:
        return None
    fingerprint = str(value).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValidationError("当前ResolvedContract指纹无效")
    return fingerprint


def _confirmation_analysis_case_id(contract_fingerprint: object) -> str:
    fingerprint = str(contract_fingerprint or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValidationError("候选合同指纹无效")
    return f"rc.{fingerprint}"


def _confirmation_contract_fingerprint(analysis_case_id: object) -> str | None:
    matched = re.fullmatch(r"rc\.([0-9a-f]{64})", str(analysis_case_id or ""))
    return matched.group(1) if matched else None


def _analysis_binding_matches(
    analysis: Mapping[str, Any], expected: Mapping[str, Any], *, tenant_id: str, task_id: str, module: str = "payoffer",
) -> bool:
    if not all(
        analysis.get(field) == expected.get(field)
        for field in ("analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint")
    ):
        return False
    raw_ref = analysis.get("module_run_ref")
    if not isinstance(raw_ref, Mapping):
        return False
    try:
        reference = ModuleRunRef(**dict(raw_ref))
    except (TypeError, ValueError):
        return False
    return reference.module == module and reference.tenant_id == tenant_id and reference.task_id == task_id


def _incomplete_report_next_step(missing_modules: Sequence[str]) -> str:
    if not missing_modules:
        return "正式收益分析已完成，但暂未形成可交付文件。请稍后重试。"
    labels = {
        "pricing": "估值定价",
        "backtest": "历史回测",
        "payoff": "收益结构",
    }
    missing = "、".join(labels.get(item, item) for item in missing_modules)
    return f"收益结构已完成，但{missing}未能取得受控正式结果。本次交付仅可标记为部分完成，请检查行情数据后重试。"


def _undelivered_card_next_step(kind: str) -> str:
    """Do not turn a rejected delivery request into a fictitious artifact."""

    label = "研究简报" if kind == "card" else "研究报告"
    return f"正式收益分析已完成，但{label}未满足受控交付条件，当前未生成文件。请检查正式结果后重试。"


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
    """Return the one public RecommendationSet schema on every App continuation.

    The task index retains only the chosen candidate and customer constraints,
    never the original evidence or financial result.  This projection restores
    the typed envelope from that state without re-running Research or exposing
    private identifiers to the customer-facing AgentLoop.
    """

    state = dict(pending or {})
    constraints = state.get("confirmed_constraints")
    candidate_data = state.get("candidate")
    candidate: RecommendationCandidate | None = None
    if isinstance(constraints, Mapping) and isinstance(candidate_data, Mapping):
        try:
            candidate = RecommendationCandidate(
                candidate_id=_identifier(candidate_data.get("candidate_id"), "candidate_id"),
                product_id=_identifier(candidate_data.get("product_id"), "product_id"),
                underlyings=tuple(str(item).strip() for item in candidate_data.get("underlyings", ()) if str(item).strip()),
                rank=1,
                reason="已按客户确认条件保留该候选。",
                suitable_for=(),
                not_suitable_for=(),
                main_risks=(),
                library_status="ready",
                product_name=str(candidate_data.get("product_name", "")).strip() or None,
                candidate_status=candidate_status,
                constraints_fingerprint=constraints_fingerprint(constraints),
            )
        except (TypeError, ValueError, ValidationError):
            candidate = None
    output_type = str(constraints.get("output_type", "")).strip().lower() if isinstance(constraints, Mapping) else ""
    requested_outputs = (
        ("card", "report") if output_type == "both" else
        (output_type,) if output_type in {"card", "quote", "report"} else ()
    )
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
        primary_candidate_id=candidate.candidate_id if candidate is not None else None,
        candidates=(candidate,) if candidate is not None else (),
        analysis_status=analysis_status,
        delivery_status=delivery_status,
        requested_outputs=requested_outputs,
    )
    return result.to_dict()


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


def _confirmation_invalidated(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id, catalog_version, pending, status="pending_approval", analysis_status="not_started", delivery_status="pending",
        ),
        "next_step": "检测到条件已变化，原候选条款已失效。请重新确认新的候选和条款。",
    }


def _approval_text(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text or any(word in text for word in ("不确认", "不同意", "取消", "不要执行", "先不要")):
        return False
    return any(word in text for word in ("确认", "同意", "按此", "按这个", "继续执行", "继续生成", "继续分析", "可以执行", "好的", "好，"))


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text):
        raise ValidationError(f"{field}无效")
    return text


def _candidate_variant_module_identity(candidate_key: str, candidate_version_id: str) -> str:
    """Match the Host-owned computation identity for one CandidateVersion."""

    digest = hashlib.sha256(f"{candidate_key}:{candidate_version_id}".encode("utf-8")).hexdigest()[:24]
    return f"candidate-{digest}"


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
