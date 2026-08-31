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
from datetime import date
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
from modules.recommender.ports import AgentPort, AgentRunReceipt, AgentStepResult, KnowledgePort, ToolPort
from modules.recommender.service import RecommendationInputRequired, RecommenderService
from modules.recommender.config import RecommenderConfig
from modules.recommender.interaction import (
    classify_term_change,
    constraints_fingerprint,
    merge_confirmed_constraints,
)
from runtime.protocol.models import ModuleRunRef
from runtime.contracts.input_adapter import compile_compute_data_requirements

from ..errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from ..page_registry import PageRegistry
from ..stores.contract_store import ContractStore
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
        "Trader": (
            "evaluate_candidate_payoff", "evaluate_candidate_pricing",
            "evaluate_candidate_backtest", "get_verified_fact", "compare_candidate_versions",
        ),
        "Reviewer": ("get_verified_fact", "compare_candidate_versions"),
    },
    "independent-council": {
        "Framer": (),
        "Matcher": (
            "search_option_structures", "evaluate_candidate_payoff",
            "evaluate_candidate_pricing", "evaluate_candidate_backtest", "get_verified_fact",
        ),
        "Hedger": (
            "search_option_structures", "evaluate_candidate_payoff",
            "evaluate_candidate_pricing", "evaluate_candidate_backtest", "get_verified_fact",
        ),
        "Moderator": ("get_verified_fact", "compare_candidate_versions"),
    },
    "constraint-ranking": {
        "Specifier": (),
        "Generator": ("search_option_structures",),
        "Evaluator": (
            "evaluate_candidate_payoff", "evaluate_candidate_pricing",
            "evaluate_candidate_backtest", "get_verified_fact",
        ),
        "Reviewer": ("get_verified_fact", "compare_candidate_versions"),
    },
}
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
        child_managed_evaluation: bool = False,
    ) -> None:
        self._executor = executor
        self._identity = identity
        self._task_id = task_id
        self._market_data_refs: dict[tuple[str, ...], Mapping[str, Any]] = {}
        self._visible_event_sink = visible_event_sink
        self._child_managed_evaluation = bool(child_managed_evaluation)

    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._executor.call(self._identity, self._task_id, f"{module}.run", payload)

    @property
    def child_managed_evaluation(self) -> bool:
        return self._child_managed_evaluation

    def register_candidate_plan(self, **plan: Any) -> Mapping[str, Any]:
        return self._executor.register_recommendation_candidate_plan(
            self._identity, self._task_id, **plan,
        )

    def resolve_candidate_evaluation(self, candidate_version_id: str) -> Mapping[str, Any]:
        return self._executor.resolve_recommendation_candidate_evaluation(
            self._identity, self._task_id, candidate_version_id,
        )

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
        self._emit_visible_event("host_module", "started", "正在运行候选验证。")
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
        term_change_status = _host_term_change_status(prompt)
        try:
            pending = self._tasks.pending_recommendation(identity, task_id)
        except ValidationError:
            return _confirmation_unavailable(task_id, catalog_version)
        if isinstance(pending, Mapping) and pending.get("status") == "invalid":
            return _confirmation_unavailable(task_id, catalog_version, pending)
        # Shared selection constraints stay compact. Product-owned term
        # overrides are frozen once in each CandidateContract and are merged
        # only while validating that candidate's exact binding.
        stored_constraints = _pending_shared_constraints(pending)
        pending_status = str(pending.get("status", "")).strip().lower() if isinstance(pending, Mapping) else ""
        if pending_status in {"pending_approval", "approval_prepared"} and term_change_status == "unresolved":
            if pending_status == "approval_prepared":
                operation_id = pending.get("approval_operation_id")
                if isinstance(operation_id, str) and operation_id:
                    pending = self._tasks.reopen_prepared_recommendation_approval(
                        identity, task_id, operation_id,
                    )
            return _unparsed_term_change(task_id, catalog_version, pending)
        if pending_status in {"pending_approval", "approval_prepared"} and term_change_status == "rejected":
            if pending_status == "approval_prepared":
                operation_id = pending.get("approval_operation_id")
                if isinstance(operation_id, str) and operation_id:
                    pending = self._tasks.reopen_prepared_recommendation_approval(
                        identity, task_id, operation_id,
                    )
            return _rejected_term_change(task_id, catalog_version, pending)
        if pending_status == "approval_prepared" and term_change_status in {"applied", "cleared"}:
            operation_id = pending.get("approval_operation_id")
            if isinstance(operation_id, str) and operation_id:
                self._tasks.invalidate_prepared_recommendation_approval(
                    identity, task_id, operation_id,
                )
        if pending_status == "approval_prepared" and term_change_status not in {"applied", "cleared"}:
            approved_candidate_ids = _approved_candidate_ids(pending)
            if not _current_constraints_match_pending(
                pending,
                confirmed_constraints,
                approved_candidate_ids,
                stored_constraints,
            ):
                operation_id = pending.get("approval_operation_id")
                if isinstance(operation_id, str) and operation_id:
                    self._tasks.invalidate_prepared_recommendation_approval(
                        identity, task_id, operation_id,
                    )
                return _confirmation_invalidated(task_id, catalog_version, pending)
            return self._resume_prepared_recommendation_approval(
                identity, task_id, catalog_version, pending, stored_constraints,
            )
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
        if term_change_status not in {"applied", "cleared"} and _may_resume_pending(
            pending,
            confirmed_constraints,
            catalog_version,
            approved_candidate_ids=approved_candidate_ids,
            stored_constraints=stored_constraints,
        ):
            assert pending is not None
            expected = _pending_candidate_bindings(
                self._tools,
                identity,
                task_id,
                pending,
                approved_candidate_ids,
                stored_constraints,
                self._registry.capability_root,
            )
            if expected is None or not _bindings_match_pending(
                expected, pending, approved_candidate_ids, constraints=stored_constraints,
            ):
                return _confirmation_unavailable(task_id, catalog_version, pending)
            prepared = self._tasks.prepare_pending_recommendation_approval(
                identity,
                task_id,
                approved_candidate_ids=list(approved_candidate_ids),
            )
            if prepared is None:
                return _confirmation_unavailable(task_id, catalog_version, pending)
            return self._resume_prepared_recommendation_approval(
                identity, task_id, catalog_version, prepared, stored_constraints,
            )
        active_fingerprint_at_start = _recommendation_start_active_fingerprint(self._tools, identity, task_id)
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
            approved_candidate_ids=(),
            candidate_contracts={},
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
                child_managed_evaluation=(
                    self._agent_runtime_mode == "active" and agent_port is runtime_agent_port
                ),
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
            clear_plans = getattr(self._tools, "clear_recommendation_candidate_plans", None)
            if callable(clear_plans):
                clear_plans(identity, task_id)
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
            catalog_version=catalog_version,
            confirmed_constraints=confirmed_constraints,
            preset_id=preset.preset_id,
            preset_revision=preset.revision,
        )
        if continuation is not None:
            continuation["workflow_mode"] = str(recommendation.get("workflow_mode", "single_agent"))
            continuation["confirmed_constraints"] = _persisted_constraints(continuation["confirmed_constraints"])
            freeze = getattr(self._tools, "freeze_recommendation_candidate", None)
            bindings: dict[str, Mapping[str, Any]] = {}
            for candidate_id in continuation["candidate_ids"]:
                contract = continuation["candidate_contracts"][candidate_id]
                binding = _evaluated_candidate_binding(
                    recommendation,
                    candidate_id,
                    confirmed_constraints,
                ) or _current_candidate_binding(
                    contract["candidate"],
                    confirmed_constraints,
                    self._registry.capability_root,
                )
                if binding is None:
                    return _confirmation_unavailable(task_id, catalog_version)
                raw_binding_overrides = binding.get("term_overrides")
                frozen_overrides = (
                    dict(raw_binding_overrides)
                    if isinstance(raw_binding_overrides, Mapping)
                    else _term_overrides_from_constraints(
                        confirmed_constraints,
                        self._registry.capability_root,
                        contract["candidate"]["product_id"],
                    )
                )
                contract["contract_fingerprint"] = binding["contract_fingerprint"]
                contract["term_overrides"] = frozen_overrides
                contract["term_overrides_fingerprint"] = _term_overrides_fingerprint(frozen_overrides)
                contract["expected_active_contract_fingerprint"] = active_fingerprint_at_start
                contract["public_projection"]["key_terms"] = _public_display_terms(
                    binding.get("display_terms"), identity,
                )
                bindings[candidate_id] = binding
                if callable(freeze):
                    try:
                        freeze(identity, task_id, continuation, binding)
                    except (AuthorizationError, ValidationError):
                        return _confirmation_unavailable(task_id, catalog_version)
            self._tasks.save_pending_recommendation(identity, task_id, continuation)
            return {
                "route": dict(route),
                "recommendation_set": _with_confirmation_terms(recommendation, continuation, bindings),
                "next_step": _confirmation_question(continuation, bindings),
            }
        if _requires_exact_pending_state(recommendation):
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
        if not _pending_preset_is_current(pending):
            self._tasks.invalidate_prepared_recommendation_approval(identity, task_id, operation_id)
            return _confirmation_invalidated(task_id, catalog_version, pending)
        approved_candidate_ids = _approved_candidate_ids(pending)
        expected = _pending_candidate_bindings(
            self._tools,
            identity,
            task_id,
            pending,
            approved_candidate_ids,
            stored_constraints,
            self._registry.capability_root,
        )
        if expected is None or not _bindings_match_pending(
            expected, pending, approved_candidate_ids, constraints=stored_constraints,
        ):
            self._tasks.invalidate_prepared_recommendation_approval(identity, task_id, operation_id)
            return _confirmation_invalidated(task_id, catalog_version, pending)
        activate = getattr(self._tools, "activate_recommendation_candidates", None)
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
        if approved is None or not _bindings_match_pending(
            expected,
            approved,
            approved_candidate_ids,
            constraints=_pending_constraints(approved),
        ):
            return _confirmation_unavailable(task_id, catalog_version, pending)
        finalize = getattr(self._tools, "commit_recommendation_candidate_activations", None)
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
            "next_step": "候选与合同条款已确认，可以继续必要计算或生成交付。",
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
        self._recommendation_plan_lock = RLock()
        self._recommendation_plans: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def bind_recommender(self, recommender: RecommenderAdapter) -> None:
        self._recommender = recommender

    def runtime_tool_bridge(self) -> RuntimeToolBridge:
        """Build the task-bound bridge used by direct runtime Child Sessions.

        The bridge is connected to the existing Host dispatcher and stores;
        the child still receives no tool permission unless the selected role
        explicitly declares one in the runtime workflow.
        """

        def module_context_for(module: str, payload: Mapping[str, Any], identity: SessionIdentity) -> object:
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                raise ValidationError("Runtime业务工具缺少Task绑定")
            return self._registry.service_context(
                identity,
                module,
                task_id=task_id,
                analysis_case_id=payload.get("analysis_case_id"),
                candidate_id=payload.get("candidate_id"),
                catalog_version=str(payload.get("catalog_version") or self._registry.manifest["catalog_version"]),
                contract_fingerprint=payload.get("contract_fingerprint"),
            )

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

        def evaluation_handler(module: str):
            def run(
                identity: SessionIdentity,
                task_id: str,
                payload: Mapping[str, Any],
                _connection: object,
            ) -> Mapping[str, Any]:
                return self.evaluate_registered_recommendation_candidate(
                    identity, task_id, module=module, payload=payload,
                )

            return run

        return RuntimeToolBridge(
            dispatcher=self._dispatcher,
            result_store=self._results,
            candidate_store=self._contracts,
            task_service=self._tasks,
            module_context_for=module_context_for,
            search_handler=search_handler,
            handlers={
                "evaluate_candidate_payoff": evaluation_handler("payoffer"),
                "evaluate_candidate_pricing": evaluation_handler("pricer"),
                "evaluate_candidate_backtest": evaluation_handler("backtester"),
            },
        )

    def register_recommendation_candidate_plan(
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
    ) -> Mapping[str, Any]:
        """Register a Host-validated candidate plan without running modules."""

        if self._tasks is not None:
            self._tasks.get(identity, task_id)
        version_id = _identifier(candidate_version_id, "candidate_version_id")
        candidate_value = dict(candidate)
        if str(candidate_value.get("candidate_version_id", "")) != version_id:
            raise ValidationError("候选计划与CandidateVersion不一致")
        normalized_modules = tuple(str(item).strip().lower() for item in modules)
        if (
            not normalized_modules
            or len(set(normalized_modules)) != len(normalized_modules)
            or any(item not in {"payoffer", "pricer", "backtester"} for item in normalized_modules)
        ):
            raise ValidationError("候选计划模块无效")
        key = (identity.tenant_id, identity.principal_id, str(task_id), version_id)
        plan = {
            "candidate": candidate_value,
            "confirmed_constraints": dict(confirmed_constraints),
            "modules": normalized_modules,
            "term_overrides": dict(term_overrides),
            "candidate_version_id": version_id,
            "round_no": int(round_no),
            "input_fingerprints": dict(input_fingerprints or {}),
            "evaluations": {},
            "market_data_ref": None,
        }
        stable_fields = (
            "candidate", "confirmed_constraints", "modules", "term_overrides",
            "round_no", "input_fingerprints",
        )
        with self._recommendation_plan_lock:
            existing = self._recommendation_plans.get(key)
            if existing is not None and any(existing[name] != plan[name] for name in stable_fields):
                raise ValidationError("同一CandidateVersion不能绑定不同候选计划")
            if existing is None:
                self._recommendation_plans[key] = plan
        return {"candidate_version_id": version_id, "modules": list(normalized_modules)}

    def evaluate_registered_recommendation_candidate(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        module: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        version_id = _identifier(payload.get("candidate_version_id"), "candidate_version_id")
        key = (identity.tenant_id, identity.principal_id, str(task_id), version_id)
        with self._recommendation_plan_lock:
            plan = self._recommendation_plans.get(key)
            if plan is None:
                raise ValidationError("CandidateVersion没有Host注册计划")
            if module not in plan["modules"]:
                raise AuthorizationError("agent_runtime.tool", "当前候选计划未授权该计算模块")
            candidate = dict(plan["candidate"])
            if str(payload.get("candidate_key", "")) != str(candidate.get("candidate_key", "")):
                raise ValidationError("工具调用CandidateVersion身份不一致")
            market_data_ref = plan.get("market_data_ref")
        if module in {"pricer", "backtester"} and not isinstance(market_data_ref, Mapping):
            market_data_ref = self.prepare_recommendation_market_data(
                identity, task_id, candidate.get("underlyings", ()),
            )
            with self._recommendation_plan_lock:
                plan["market_data_ref"] = dict(market_data_ref)
        fingerprint = plan["input_fingerprints"].get(module)
        result = self.evaluate_recommendation_candidate(
            identity,
            task_id,
            candidate=candidate,
            confirmed_constraints=plan["confirmed_constraints"],
            modules=(module,),
            term_overrides=plan["term_overrides"],
            candidate_version_id=version_id,
            round_no=plan["round_no"],
            input_fingerprints={module: fingerprint} if fingerprint else None,
            market_data_ref=market_data_ref if isinstance(market_data_ref, Mapping) else None,
        )
        with self._recommendation_plan_lock:
            plan["evaluations"][module] = dict(result)
        facts = [
            {
                "metric": metric,
                "value": detail.get("value"),
                "unit": detail.get("unit"),
                "fact_ref": detail.get("fact_ref"),
            }
            for metric, detail in result.get("verified_metrics", {}).items()
            if isinstance(detail, Mapping)
        ]
        return {
            "status": str(result.get("status", "completed")),
            "module": module,
            "candidate_version_id": version_id,
            "facts": facts,
            "fact_refs": [item["fact_ref"] for item in facts if isinstance(item.get("fact_ref"), str)],
        }

    def resolve_recommendation_candidate_evaluation(
        self,
        identity: SessionIdentity,
        task_id: str,
        candidate_version_id: str,
    ) -> Mapping[str, Any]:
        version_id = _identifier(candidate_version_id, "candidate_version_id")
        key = (identity.tenant_id, identity.principal_id, str(task_id), version_id)
        with self._recommendation_plan_lock:
            plan = self._recommendation_plans.get(key)
            if plan is None:
                raise ValidationError("CandidateVersion没有Host注册计划")
            rows = {name: dict(value) for name, value in plan["evaluations"].items()}
            required = tuple(plan["modules"])
        if set(rows) != set(required):
            missing = sorted(set(required).difference(rows))
            raise ValidationError(f"Child Agent未完成计划模块：{','.join(missing)}")
        records: list[Mapping[str, Any]] = []
        run_refs: list[Mapping[str, Any]] = []
        statuses: dict[str, str] = {}
        metrics: dict[str, Mapping[str, Any]] = {}
        display_terms: list[Mapping[str, Any]] = []
        contract_fingerprint = ""
        for module in required:
            row = rows[module]
            records.extend(item for item in row.get("evaluation_records", ()) if isinstance(item, Mapping))
            run_refs.extend(item for item in row.get("module_run_refs", ()) if isinstance(item, Mapping))
            statuses.update({str(name): str(status) for name, status in row.get("module_statuses", {}).items()})
            metrics.update({
                str(name): dict(detail)
                for name, detail in row.get("verified_metrics", {}).items()
                if isinstance(detail, Mapping)
            })
            if not display_terms:
                display_terms = [dict(item) for item in row.get("display_terms", ()) if isinstance(item, Mapping)]
            contract_fingerprint = str(row.get("contract_fingerprint") or contract_fingerprint)
        return {
            "candidate_version_id": version_id,
            "contract_fingerprint": contract_fingerprint,
            "evaluation_records": records,
            "module_run_refs": run_refs,
            "module_statuses": statuses,
            "display_terms": display_terms,
            "verified_metrics": metrics,
        }

    def clear_recommendation_candidate_plans(
        self,
        identity: SessionIdentity,
        task_id: str,
    ) -> int:
        """Release request-scoped candidate plans after all Child tools settle."""

        prefix = (identity.tenant_id, identity.principal_id, str(task_id))
        with self._recommendation_plan_lock:
            keys = [key for key in self._recommendation_plans if key[:3] == prefix]
            for key in keys:
                self._recommendation_plans.pop(key, None)
        return len(keys)

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
        candidate_id: str,
    ) -> Mapping[str, Any] | None:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        contract = _pending_candidate_contract(pending, candidate_id)
        if contract is None:
            return None
        candidate = contract["candidate"]
        version = contract["candidate_version"]
        record = self._contracts.get_candidate_variant(
            identity,
            task_id,
            str(version.get("candidate_key", "")),
            str(version.get("candidate_version_id", "")),
        )
        if not isinstance(record, Mapping):
            return None
        if (
            record.get("contract_fingerprint") != contract.get("contract_fingerprint")
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
        constraints = _pending_candidate_constraints(pending, candidate_id)
        return {
            "candidate_id": str(candidate.get("candidate_id", "")),
            "constraints_fingerprint": constraints_fingerprint(constraints),
            "contract_fingerprint": str(record["contract_fingerprint"]),
            "display_terms": _display_terms(
                resolved_contract,
                _load_capability_registry(self._registry.capability_root),
                constraints,
            ),
            "term_overrides": dict(contract.get("term_overrides", {})),
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
        candidate_id = _identifier(binding.get("candidate_id"), "candidate_id")
        contract = _pending_candidate_contract(pending, candidate_id)
        if contract is None:
            raise ValidationError("待确认候选不属于当前有序候选集合")
        version = contract["candidate_version"]
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
            stored.get("contract_fingerprint") != contract.get("contract_fingerprint")
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

    def activate_recommendation_candidates(
        self,
        identity: SessionIdentity,
        task_id: str,
        pending: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        candidate_ids = _approved_candidate_ids(pending)
        if not candidate_ids:
            raise ValidationError("审批操作缺少approved_candidate_ids")
        verified: dict[str, Mapping[str, Any]] = {}
        for candidate_id in candidate_ids:
            contract = _pending_candidate_contract(pending, candidate_id)
            if contract is None:
                raise ValidationError("审批候选不属于当前CandidateContract集合")
            version = contract["candidate_version"]
            record = self._contracts.get_candidate_variant(
                identity,
                task_id,
                str(version.get("candidate_key", "")),
                str(version.get("candidate_version_id", "")),
            )
            if not isinstance(record, Mapping) or record.get("contract_fingerprint") != contract.get("contract_fingerprint"):
                raise ValidationError("审批候选与冻结CandidateVariant不一致")
            verified[candidate_id] = record
        if len(candidate_ids) > 1:
            # 多结构比较保留每个已验证CandidateContract，不把其中任意一个
            # 伪装成Task唯一活动合同。Reporter按用户顺序消费全部RunRef。
            return {"candidate_contracts": verified, "activation": "comparison"}
        candidate_id = candidate_ids[0]
        contract = _pending_candidate_contract(pending, candidate_id)
        assert contract is not None
        version = contract["candidate_version"]
        activated = self._contracts.activate_candidate_variant(
            identity,
            task_id,
            str(version.get("candidate_key", "")),
            str(version.get("candidate_version_id", "")),
            expected_active_contract_fingerprint=contract.get("expected_active_contract_fingerprint"),
            approval_operation_id=str(pending.get("approval_operation_id", "")).strip() or None,
        )
        if activated.get("contract_fingerprint") != contract.get("contract_fingerprint"):
            raise ValidationError("激活合同与待确认候选不一致")
        return activated

    def commit_recommendation_candidate_activations(
        self,
        identity: SessionIdentity,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, Any] | None:
        if self._contracts is None:
            raise ValidationError("候选合同版本需要ContractStore")
        pending = self._tasks.pending_recommendation(identity, task_id) if self._tasks is not None else None
        if isinstance(pending, Mapping) and len(_approved_candidate_ids(pending)) > 1:
            return {"status": "comparison_bound", "approved_candidate_ids": _approved_candidate_ids(pending)}
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
        # A preview is sufficient to select the pricing route, but it is not a
        # formal CandidateVariant.  The latter must be written only after the
        # Host has compiled the contract against its verified DataAssetRef.
        # Otherwise a preselection contract would lack S0Raw, contract start
        # date and, where required, the verified observation calendar.
        stored_variant = self._contracts.get_candidate_variant(identity, task_id, candidate_key, version_id)
        if not isinstance(stored_variant, Mapping):
            resolved_contract = _candidate_routing_contract(
                self._registry.capability_root,
                product_id=product_id,
                term_overrides=controlled_overrides,
            )
            contract_fingerprint = ""
        else:
            resolved_contract = stored_variant.get("resolved_contract")
            contract_fingerprint = str(stored_variant.get("contract_fingerprint", ""))
        if not isinstance(resolved_contract, Mapping) or (
            isinstance(stored_variant, Mapping) and not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint)
        ):
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
            "candidate_id": _candidate_variant_module_identity(candidate_key, version_id),
            "catalog_version": catalog_version,
            "candidate_key": candidate_key,
            "candidate_version_id": version_id,
        }
        if contract_fingerprint:
            binding.update({
                "analysis_case_id": _confirmation_analysis_case_id(contract_fingerprint),
                "contract_fingerprint": contract_fingerprint,
            })
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
                # Candidate metadata is present from the first calculation,
                # but no preview contract exists.  ToolGateway therefore
                # compiles the formal Host contract and writes that exact
                # prepared object into the CandidateVariant without touching
                # the task's active contract.
                dispatch_binding = binding if isinstance(stored_variant, Mapping) else {
                    "candidate_id": binding["candidate_id"],
                    "catalog_version": catalog_version,
                }
                response = self._dispatch(
                    identity, task_id, f"{module}.run", module, payload, binding=dispatch_binding,
                    candidate_variant=candidate_variant,
                )
                status = _normalized_module_status(response)
                raw_ref = response.get("module_run_ref")
                reference = ModuleRunRef(**dict(raw_ref)) if isinstance(raw_ref, Mapping) else None
                if status in {"succeeded", "partial"} and reference is None:
                    raise ValidationError(f"{module}成功结果缺少ModuleRunRef")
                if reference is not None and not isinstance(stored_variant, Mapping):
                    stored_variant = self._contracts.get_candidate_variant(
                        identity, task_id, candidate_key, version_id,
                    )
                    resolved_contract = stored_variant.get("resolved_contract")
                    contract_fingerprint = str(stored_variant.get("contract_fingerprint", ""))
                    if not isinstance(resolved_contract, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint):
                        raise ValidationError("Host冻结的CandidateVariant无效")
                    binding = {
                        "analysis_case_id": _confirmation_analysis_case_id(contract_fingerprint),
                        "candidate_id": _candidate_variant_module_identity(candidate_key, version_id),
                        "catalog_version": catalog_version,
                        "contract_fingerprint": contract_fingerprint,
                        "candidate_key": candidate_key,
                        "candidate_version_id": version_id,
                    }
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
                "message": "请选择研究简报、参考报价或研究报告；默认生成HTML，需要PDF时请明确指定。",
            }
        pending = self._tasks.pending_recommendation(identity, task_id)
        if pending is None or pending.get("status") != "approved":
            return {"status": "needs_input", "message": "请先确认推荐候选与合同条款。"}
        requested = {"kind": kind, "format": output_format}
        frozen_constraints = _pending_constraints(pending)
        approved_candidate_ids = _approved_candidate_ids(pending)
        expected = _pending_candidate_bindings(
            self,
            identity,
            task_id,
            pending,
            approved_candidate_ids,
            frozen_constraints,
            self._registry.capability_root,
        )
        if expected is None or not _bindings_match_pending(
            expected, pending, approved_candidate_ids, constraints=frozen_constraints,
        ):
            return {"status": "needs_input", "message": "候选精确版本或合同绑定已失效，请重新推荐。"}
        if pending.get("delivery") is None:
            pending = self._tasks.choose_pending_recommendation_delivery(
                identity,
                task_id,
                requested,
            )
        if pending is None or pending.get("delivery") != requested:
            raise ValidationError("当前候选已绑定不同交付类型")
        if len(approved_candidate_ids) == 1:
            candidate_id = approved_candidate_ids[0]
            contract = _pending_candidate_contract(pending, candidate_id)
            assert contract is not None
            binding = expected[candidate_id]
            result = self.run_recommendation_delivery(
                identity,
                task_id,
                analysis_case_id=str(pending["analysis_case_id"]),
                catalog_version=str(pending["catalog_version"]),
                candidate=contract["candidate"],
                delivery=requested,
                confirmed_constraints=frozen_constraints,
                frozen_binding=binding,
                frozen_candidate_variant=binding.get("candidate_variant"),
                module_run_refs=contract.get("module_run_refs"),
            )
        else:
            result = self.run_recommendation_candidate_set_delivery(
                identity,
                task_id,
                pending=pending,
                approved_candidate_ids=approved_candidate_ids,
                bindings=expected,
                delivery=requested,
                confirmed_constraints=frozen_constraints,
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

    def run_recommendation_candidate_set_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        pending: Mapping[str, Any],
        approved_candidate_ids: Sequence[str],
        bindings: Mapping[str, Mapping[str, Any]],
        delivery: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prepare every approved candidate, then render one ordered comparison."""

        report_candidate_ids: list[str] = []
        for candidate_id in approved_candidate_ids:
            contract = _pending_candidate_contract(pending, candidate_id)
            binding = bindings.get(candidate_id)
            if contract is None or not isinstance(binding, Mapping):
                raise ValidationError("多候选交付缺少已批准CandidateContract")
            prepared = self.run_recommendation_deliveries(
                identity,
                task_id,
                analysis_case_id=str(pending["analysis_case_id"]),
                catalog_version=str(pending["catalog_version"]),
                candidate=contract["candidate"],
                deliveries=(delivery,),
                confirmed_constraints=confirmed_constraints,
                frozen_binding=binding,
                frozen_candidate_variant=binding.get("candidate_variant"),
                module_run_refs=contract.get("module_run_refs"),
                prepare_only=True,
            )
            if prepared.get("status") != "completed":
                return prepared
            variant = binding.get("candidate_variant")
            if not isinstance(variant, Mapping):
                raise ValidationError("多候选交付缺少CandidateVersion绑定")
            report_candidate_ids.append(_candidate_variant_module_identity(
                _identifier(variant.get("candidate_key"), "candidate_key"),
                _identifier(variant.get("candidate_version_id"), "candidate_version_id"),
            ))
        kind = str(delivery.get("kind", "")).strip().lower()
        output_format = str(delivery.get("format", "")).strip().lower()
        prepared_report = self._prepare_report(
            identity,
            task_id,
            {
                "kind": kind,
                "format": output_format,
                "delivery_mode": "comparison",
                "candidate_ids": report_candidate_ids,
            },
            report_run_id=f"chat-{kind}-{hashlib.sha256((task_id + ':' + ':'.join(approved_candidate_ids)).encode()).hexdigest()[:16]}",
        )
        if prepared_report.get("status") == "needs_input":
            return prepared_report
        report = _with_report_delivery(
            self._dispatch(identity, task_id, "reporter.run", "reporter", prepared_report),
            prepared_report,
        )
        report_status = str(report.get("status", "")).lower()
        if report.get("ok") is not True or report_status not in {"succeeded", "completed", "partial"}:
            return {"status": "partial", "delivery_status": "partial", "deliveries": []}
        receipt = dict(report.get("delivery", {}))
        completed = (
            report_status in {"succeeded", "completed"}
            and str(receipt.get("coverage_status", "complete")).lower() == "complete"
        )
        return {
            "status": "completed" if completed else "partial",
            "analysis_status": "completed",
            "delivery_status": "completed" if completed else "partial",
            "deliveries": [receipt],
        }

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
        prepare_only: bool = False,
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
        required_modules = (
            ("pricing",)
            if requested_kinds == {"quote"}
            else _REPORT_MODULES
        )
        resolved_contract = expected.get("resolved_contract")
        if not isinstance(resolved_contract, Mapping):
            return {
                "status": "partial",
                "next_step": "冻结候选缺少正式合同快照，已停止交付。请重新推荐。",
            }
        pricing_config: dict[str, Any] | None = None
        if "pricing" in required_modules:
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
        missing_before_run = [name for name in required_modules if name not in reusable]
        data_ref = None
        if any(name in {"pricing", "backtest"} for name in missing_before_run):
            data_ref = self._fetch_report_market_data(
                identity,
                task_id,
                ordered_underlyings,
                resolved_contract=resolved_contract,
            )
        detail_payloads = {
            "payoff": compute_base,
            "pricing": {
                **compute_base,
                "pricing_config": pricing_config,
                "market_data_refs": [data_ref] if data_ref is not None else [],
            },
            "backtest": {
                **compute_base,
                "backtest_config": {},
                "historical_data": data_ref,
            },
        }
        analysis_runs: dict[str, Mapping[str, Any]] = {}
        missing_analysis_modules: list[str] = []
        terminal_failures: list[str] = []
        for display_module in required_modules:
            compute_module = _REPORT_TO_COMPUTE_MODULE[display_module]
            response = reusable.get(display_module)
            if response is None:
                if display_module in {"pricing", "backtest"} and data_ref is None:
                    missing_analysis_modules.append(display_module)
                    continue
                response = self._dispatch(
                    identity,
                    task_id,
                    f"{compute_module}.run",
                    compute_module,
                    detail_payloads[display_module],
                    binding=binding,
                    candidate_variant=frozen_candidate_variant,
                )
            analysis_runs[display_module] = response
            status = _analysis_status(response)
            if response.get("ok") is not True:
                failure = _public_failure_metadata(response)
                if failure is not None:
                    return {
                        "status": "partial",
                        "analysis_status": status,
                        "delivery_status": "not_requested",
                        "missing_modules": [display_module],
                        "deliveries": [],
                        **failure,
                    }
                return {
                    "status": "partial",
                    "analysis_status": status,
                    "delivery_status": "not_requested",
                    "missing_modules": [display_module],
                    "deliveries": [],
                    "next_step": _incomplete_report_next_step([display_module]),
                }
            if (
                response.get("ok") is not True
                or status != "completed"
                or not _analysis_binding_matches(
                    response,
                    binding,
                    tenant_id=identity.tenant_id,
                    task_id=task_id,
                    module=compute_module,
                )
            ):
                missing_analysis_modules.append(display_module)
                terminal_failures.append(status)
        aggregate_analysis_status = (
            terminal_failures[0]
            if len(required_modules) == 1 and len(terminal_failures) == 1
            else "partial" if missing_analysis_modules
            else "completed"
        )
        # A full report is only meaningful when its requested analysis coverage
        # is complete. Do not pass an incomplete selection to Reporter: that
        # would make an apparently formal report out of partial evidence.
        if missing_analysis_modules:
            return {
                "status": "partial",
                "analysis_status": aggregate_analysis_status,
                "delivery_status": "not_requested",
                "missing_modules": list(missing_analysis_modules),
                "next_step": _incomplete_report_next_step(missing_analysis_modules),
                "deliveries": [],
            }
        if prepare_only:
            return {
                "status": "completed",
                "analysis_status": aggregate_analysis_status,
                "delivery_status": "not_requested",
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
            report = _with_report_delivery(
                self._dispatch(identity, task_id, "reporter.run", "reporter", prepared),
                prepared,
            )
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
        self,
        identity: SessionIdentity,
        task_id: str,
        underlyings: Sequence[str],
        *,
        resolved_contract: Mapping[str, Any],
    ) -> dict[str, str] | None:
        """Fetch one contract-derived task asset before missing computations."""

        today = date.today()
        requirements = compile_compute_data_requirements(
            "backtester",
            {"product_id": resolved_contract.get("product_id", "")},
            resolved_contract,
        )
        history_years = max(3, math.ceil(requirements.tenor_years) + 3)
        request = {
            "action": "fetch", "task_id": task_id, "asset_ids": list(underlyings),
            "start_date": _years_before_date(today, history_years).isoformat(), "end_date": today.isoformat(),
            "fields": list(requirements.historical_fields), "provider": "ifind_http", "frequency": "1d",
            "adjustment": "forward", "cache_policy": "reuse", "offline": True,
        }
        response = self._dispatch(identity, task_id, "datafetcher.fetch", "datafetcher", request)
        raw_ref = response.get("data_asset_ref") if isinstance(response, Mapping) else None
        if not isinstance(response, Mapping):
            raise ValidationError("DataFetcher返回无效响应")
        if response.get("ok") is not True:
            failure_code = response.get("failure_code", response.get("code"))
            message = response.get("message", response.get("reason"))
            stage = response.get("stage")
            next_step = response.get("next_step")
            if (
                isinstance(failure_code, str) and failure_code
                and isinstance(message, str) and message
                and isinstance(stage, str) and stage
                and isinstance(next_step, str) and next_step
            ):
                raise UserActionError(
                    failure_code,
                    message,
                    stage=stage,
                    next_step=next_step,
                )
            raise ValidationError("DataFetcher未返回可用行情数据")
        if not isinstance(raw_ref, Mapping):
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
                source, candidate = _best_report_candidate(sources, expected_binding=expected_binding)
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


def _years_before_date(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


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
    catalog_version: str,
    confirmed_constraints: Mapping[str, Any],
    preset_id: str,
    preset_revision: str,
) -> dict[str, Any] | None:
    """Project the complete ordered candidate set onto the TaskService schema."""

    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return None
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return None
    ranking_spec = _pending_ranking_spec(recommendation, preset_id)
    if preset_id == "constraint-ranking" and ranking_spec is None:
        return None
    candidate_ids: list[str] = []
    candidate_contracts: dict[str, dict[str, Any]] = {}
    for raw_candidate in candidates:
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
        version = _pending_candidate_version(raw_candidate)
        module_run_refs = _pending_module_run_refs(raw_candidate.get("module_run_refs", ()))
        raw_overrides = raw_candidate.get("term_overrides", {})
        if (
            not candidate_id
            or candidate_id in candidate_contracts
            or not product_id
            or not ordered_underlyings
            or version is None
            or module_run_refs is None
            or not isinstance(raw_overrides, Mapping)
        ):
            continue
        term_overrides = {str(key): value for key, value in raw_overrides.items()}
        contract_fingerprint = str(raw_candidate.get("contract_fingerprint", "")).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint):
            contract_fingerprint = "0" * 64
        candidate_ids.append(candidate_id)
        candidate_contracts[candidate_id] = {
            "candidate": {
                "candidate_id": candidate_id,
                "product_id": product_id,
                "product_name": str(raw_candidate.get("product_name", "")).strip(),
                "underlyings": ordered_underlyings,
                "library_status": "ready",
            },
            "candidate_version": version,
            "contract_fingerprint": contract_fingerprint,
            "term_overrides": term_overrides,
            "term_overrides_fingerprint": _term_overrides_fingerprint(term_overrides),
            "expected_active_contract_fingerprint": None,
            "module_run_refs": module_run_refs,
            "approval_status": "pending_approval",
            "public_projection": _public_candidate_projection(raw_candidate, identity),
        }
    if not candidate_ids:
        return None
    return {
        "status": "pending_approval",
        "state_schema": PENDING_RECOMMENDATION_SCHEMA_ID,
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        "workflow_mode": str(recommendation.get("workflow_mode", "single_agent")),
        "preset": {"preset_id": preset_id, "preset_revision": preset_revision},
        "confirmed_constraints": dict(confirmed_constraints),
        "ranking_spec_id": ranking_spec["ranking_spec_id"] if ranking_spec is not None else None,
        "ranking_spec_fingerprint": ranking_spec["ranking_spec_fingerprint"] if ranking_spec is not None else None,
        "ranking_spec": ranking_spec,
        "delivery": None,
        "candidate_ids": candidate_ids,
        "candidate_contracts": candidate_contracts,
        "approved_candidate_ids": [],
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


def _requires_exact_pending_state(recommendation: object) -> bool:
    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return False
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return False
    return bool(candidates)


def _may_resume_pending(
    pending: Mapping[str, Any] | None,
    current_constraints: Mapping[str, Any],
    catalog_version: str,
    *,
    approved_candidate_ids: Sequence[str] = (),
    stored_constraints: Mapping[str, Any] | None = None,
) -> bool:
    if (
        not isinstance(pending, Mapping)
        or pending.get("state_schema") != PENDING_RECOMMENDATION_SCHEMA_ID
        or str(pending.get("catalog_version", "")) != catalog_version
    ):
        return False
    if not _pending_preset_is_current(pending):
        return False
    if not _current_constraints_match_pending(
        pending,
        current_constraints,
        approved_candidate_ids,
        stored_constraints,
    ):
        return False
    status = str(pending.get("status", "")).strip().lower()
    if status == "pending_approval":
        return bool(approved_candidate_ids)
    return False


def _current_constraints_match_pending(
    pending: Mapping[str, Any],
    current_constraints: Mapping[str, Any],
    approved_candidate_ids: Sequence[str],
    stored_constraints: Mapping[str, Any] | None,
) -> bool:
    try:
        exact_constraints = _pending_constraints(pending)
    except (TypeError, ValueError, ValidationError):
        return False
    stored = stored_constraints if isinstance(stored_constraints, Mapping) else exact_constraints
    current_shared = dict(current_constraints)
    current_term_overrides = current_shared.pop("term_overrides", {})
    stored_shared = dict(stored)
    stored_shared.pop("term_overrides", None)
    if _candidate_constraints_fingerprint(current_shared) != _candidate_constraints_fingerprint(stored_shared):
        return False
    if current_term_overrides:
        if not isinstance(current_term_overrides, Mapping) or not approved_candidate_ids:
            return False
        for candidate_id in approved_candidate_ids:
            contract = _pending_candidate_contract(pending, candidate_id)
            frozen_terms = contract.get("term_overrides") if isinstance(contract, Mapping) else None
            if not isinstance(frozen_terms, Mapping) or any(
                frozen_terms.get(str(key)) != value
                for key, value in current_term_overrides.items()
            ):
                return False
    return True


def _pending_preset_is_current(pending: Mapping[str, Any]) -> bool:
    """Bind every resumable approval transition to the frozen Preset content."""

    preset = pending.get("preset")
    if not isinstance(preset, Mapping):
        return False
    try:
        current = resolve_recommendation_preset(str(preset.get("preset_id", "")))
    except (KeyError, TypeError, ValueError, ValidationError):
        return False
    return str(preset.get("preset_revision", "")) == current.revision


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


def _pending_candidate_constraints(
    pending: Mapping[str, Any],
    candidate_id: str,
    *,
    shared_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine shared selection constraints with one candidate's frozen terms."""

    constraints = (
        dict(shared_constraints)
        if isinstance(shared_constraints, Mapping)
        else _pending_constraints(pending)
    )
    contract = _pending_candidate_contract(pending, candidate_id)
    if contract is None:
        raise ValidationError("待确认候选不属于当前有序候选集合")
    term_overrides = contract.get("term_overrides")
    if not isinstance(term_overrides, Mapping):
        raise ValidationError("待确认候选缺少冻结条款")
    if term_overrides:
        constraints["term_overrides"] = dict(term_overrides)
    else:
        constraints.pop("term_overrides", None)
    return constraints


def _term_overrides_fingerprint(value: Mapping[str, Any]) -> str:
    clean = {str(key): value[key] for key in sorted(value)}
    return canonical_hash(clean)


def _candidate_constraints_fingerprint(constraints: Mapping[str, Any]) -> str:
    """Fingerprint only the confirmed fields that select or interpret a contract."""

    candidate_fields = {
        key: value
        for key, value in constraints.items()
        if key in {
            "underlying",
            "horizon",
            "market_view",
            "max_loss",
            "principal_fluctuation",
            "path_count",
            "term_overrides",
        }
    }
    return constraints_fingerprint(candidate_fields)


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


def _selected_candidate_ids(
    prompt: object,
    arguments: Mapping[str, Any],
    pending: Mapping[str, Any] | None,
) -> list[str]:
    """Resolve an ordered user selection to authoritative candidate IDs."""

    if not isinstance(pending, Mapping):
        return []
    raw_ids = pending.get("candidate_ids")
    raw_contracts = pending.get("candidate_contracts")
    if (
        isinstance(raw_ids, (str, bytes))
        or not isinstance(raw_ids, Sequence)
        or not isinstance(raw_contracts, Mapping)
    ):
        return []
    candidate_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
        return []
    supplied = arguments.get("approved_candidate_ids")
    if supplied is not None:
        if isinstance(supplied, (str, bytes)) or not isinstance(supplied, Sequence):
            raise ValidationError("approved_candidate_ids必须是有序数组")
        selected = [str(item).strip() for item in supplied]
        if not selected or any(not item for item in selected) or len(set(selected)) != len(selected):
            raise ValidationError("approved_candidate_ids不能为空或重复")
        if any(item not in raw_contracts for item in selected):
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
    for candidate_id in candidate_ids:
        contract = raw_contracts.get(candidate_id)
        candidate = contract.get("candidate") if isinstance(contract, Mapping) else None
        if not isinstance(candidate, Mapping):
            continue
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
    candidate_id: str,
    constraints: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use the exact Host-evaluated contract identity when a Mode already has one."""

    if not isinstance(recommendation, Mapping):
        return None
    rows = recommendation.get("candidates")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return None
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


def _pending_candidate_contract(
    pending: Mapping[str, Any], candidate_id: str,
) -> Mapping[str, Any] | None:
    contracts = pending.get("candidate_contracts")
    if not isinstance(contracts, Mapping):
        return None
    contract = contracts.get(candidate_id)
    return contract if isinstance(contract, Mapping) else None


def _approved_candidate_ids(pending: Mapping[str, Any]) -> list[str]:
    value = pending.get("approved_candidate_ids")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result = [str(item).strip() for item in value if str(item).strip()]
    return result if len(result) == len(set(result)) else []


def _pending_candidate_bindings(
    tool_executor: object,
    identity: SessionIdentity,
    task_id: str,
    pending: Mapping[str, Any],
    candidate_ids: Sequence[str],
    constraints: Mapping[str, Any],
    capability_root: Path,
) -> dict[str, Mapping[str, Any]] | None:
    exact = getattr(tool_executor, "recommendation_candidate_binding", None)
    result: dict[str, Mapping[str, Any]] = {}
    for candidate_id in candidate_ids:
        contract = _pending_candidate_contract(pending, candidate_id)
        if contract is None:
            return None
        candidate_constraints = _pending_candidate_constraints(
            pending,
            candidate_id,
            shared_constraints=constraints,
        )
        if callable(exact):
            try:
                binding = exact(identity, task_id, pending, candidate_id)
            except (AuthorizationError, ValidationError):
                return None
        else:
            candidate = contract.get("candidate")
            binding = (
                _current_candidate_binding(candidate, candidate_constraints, capability_root)
                if isinstance(candidate, Mapping) else None
            )
        if not isinstance(binding, Mapping):
            return None
        result[candidate_id] = binding
    return result


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


def _candidate_routing_contract(
    capability_root: Path,
    *,
    product_id: str,
    term_overrides: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Read only the registered pricing route before Host compilation.

    Observation products cannot be resolved safely without a verified calendar,
    and every product needs a verified history-derived identity before it may be
    frozen.  The route is therefore the sole pre-compilation fact retained here;
    Core and the Host remain the only source of a ResolvedContract.
    """

    registry = _load_capability_registry(capability_root)
    product = registry.get("products", {}).get(product_id)
    terms = product.get("terms") if isinstance(product, Mapping) else None
    if not isinstance(terms, Mapping):
        raise ValidationError("候选产品缺少已登记定价条款")
    methods = terms.get("pricing_methods")
    if isinstance(methods, (str, bytes)) or not isinstance(methods, Sequence):
        raise ValidationError("候选产品未声明可用定价方法")
    return {"terms": {"pricing_methods": list(methods)}}


def _with_confirmation_terms(
    recommendation: object,
    pending: Mapping[str, Any],
    bindings: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(recommendation) if isinstance(recommendation, Mapping) else {"status": "pending_approval"}
    rows = result.get("candidates")
    if isinstance(rows, list):
        prepared = []
        for item in rows:
            current = dict(item) if isinstance(item, Mapping) else item
            candidate_id = str(current.get("candidate_id", "")) if isinstance(current, dict) else ""
            binding = bindings.get(candidate_id)
            if isinstance(current, dict) and isinstance(binding, Mapping):
                current["candidate_status"] = "pending_confirmation"
                current["key_terms"] = list(binding["display_terms"])
            prepared.append(current)
        result["candidates"] = prepared
    result["analysis_status"] = "not_started"
    result["delivery_status"] = "pending" if result.get("requested_outputs") else "not_requested"
    return result


def _confirmation_question(
    pending: Mapping[str, Any], bindings: Mapping[str, Mapping[str, Any]],
) -> str:
    candidate_ids = pending.get("candidate_ids")
    contracts = pending.get("candidate_contracts")
    if not isinstance(candidate_ids, Sequence) or isinstance(candidate_ids, (str, bytes)) or not isinstance(contracts, Mapping):
        return "请明确选择需要继续的候选。"
    if len(candidate_ids) > 1:
        choices = []
        for index, candidate_id in enumerate(candidate_ids, start=1):
            contract = contracts.get(candidate_id)
            candidate = contract.get("candidate") if isinstance(contract, Mapping) else None
            title = str(candidate.get("product_name") or candidate.get("product_id") or candidate_id) if isinstance(candidate, Mapping) else str(candidate_id)
            choices.append(f"{index}.{title}")
        return "候选为" + "；".join(choices) + "。请明确选择一个或多个候选；多选顺序即后续对比顺序，也可一次说明需要调整的条款。"
    candidate_id = str(candidate_ids[0])
    contract = contracts.get(candidate_id)
    candidate = contract.get("candidate") if isinstance(contract, Mapping) else {}
    binding = bindings.get(candidate_id, {})
    terms = binding.get("display_terms") if isinstance(binding.get("display_terms"), Sequence) else ()
    summary = "；".join(f"{item.get('label')}：{item.get('value')}" for item in terms if isinstance(item, Mapping))
    title = str(candidate.get("product_name") or candidate.get("product_id") or "该候选") if isinstance(candidate, Mapping) else "该候选"
    return f"拟采用{title}。{summary}。请明确回复“确认按此候选和条款继续”，或一次说明需要调整的条件。"


def _bindings_match_pending(
    bindings: Mapping[str, Mapping[str, Any]],
    pending: Mapping[str, Any],
    candidate_ids: Sequence[str],
    *,
    constraints: Mapping[str, Any] | None = None,
) -> bool:
    try:
        frozen_constraints = dict(constraints) if isinstance(constraints, Mapping) else _pending_constraints(pending)
    except (TypeError, ValueError, ValidationError):
        return False
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
        )
    )
    if not ranking_valid or not isinstance(frozen_constraints, Mapping):
        return False
    for candidate_id in candidate_ids:
        contract = _pending_candidate_contract(pending, candidate_id)
        binding = bindings.get(candidate_id)
        if contract is None or not isinstance(binding, Mapping):
            return False
        try:
            candidate_constraints = _pending_candidate_constraints(
                pending,
                candidate_id,
                shared_constraints=frozen_constraints,
            )
        except (TypeError, ValueError, ValidationError):
            return False
        expected_constraints = constraints_fingerprint(candidate_constraints)
        candidate = contract.get("candidate")
        version = contract.get("candidate_version")
        term_overrides = contract.get("term_overrides")
        if not (
            isinstance(candidate, Mapping)
            and isinstance(version, Mapping)
            and isinstance(term_overrides, Mapping)
            and binding.get("candidate_id") == candidate_id == candidate.get("candidate_id")
            and binding.get("constraints_fingerprint") == expected_constraints
            and binding.get("contract_fingerprint") == contract.get("contract_fingerprint")
            and _term_overrides_fingerprint(term_overrides) == contract.get("term_overrides_fingerprint")
            and (
                preset_id != "constraint-ranking"
                or binding.get("ranking_spec_anchor") == dict(ranking_spec)
            )
        ):
            return False
    return True


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

    The task state retains the complete ordered candidate set and per-candidate
    contract bindings, never the original evidence payload or financial result.
    This projection restores the typed envelope without re-running Selector or
    exposing private runtime identifiers to the customer-facing AgentLoop.
    """

    state = dict(pending or {})
    constraints = state.get("confirmed_constraints")
    candidate_ids = state.get("candidate_ids")
    candidate_contracts = state.get("candidate_contracts")
    approved_candidate_ids = _approved_candidate_ids(state)
    candidates: list[RecommendationCandidate] = []
    if (
        isinstance(constraints, Mapping)
        and isinstance(candidate_ids, Sequence)
        and not isinstance(candidate_ids, (str, bytes))
        and isinstance(candidate_contracts, Mapping)
    ):
        for rank, candidate_id in enumerate(candidate_ids, start=1):
            contract = candidate_contracts.get(str(candidate_id))
            candidate_data = contract.get("candidate") if isinstance(contract, Mapping) else None
            public_projection = contract.get("public_projection") if isinstance(contract, Mapping) else None
            if not isinstance(candidate_data, Mapping) or not isinstance(public_projection, Mapping):
                continue
            try:
                candidates.append(RecommendationCandidate(
                    candidate_id=_identifier(candidate_data.get("candidate_id"), "candidate_id"),
                    product_id=_identifier(candidate_data.get("product_id"), "product_id"),
                    underlyings=tuple(str(item).strip() for item in candidate_data.get("underlyings", ()) if str(item).strip()),
                    rank=rank,
                    reason=str(public_projection.get("reason", "")),
                    suitable_for=tuple(map(str, public_projection.get("suitable_for", ()))),
                    not_suitable_for=tuple(map(str, public_projection.get("not_suitable_for", ()))),
                    main_risks=tuple(map(str, public_projection.get("main_risks", ()))),
                    library_status="ready",
                    product_name=str(candidate_data.get("product_name", "")).strip() or None,
                    key_terms=tuple(
                        dict(item) for item in public_projection.get("key_terms", ())
                        if isinstance(item, Mapping)
                    ),
                    candidate_status=(candidate_status if str(candidate_id) in approved_candidate_ids else "candidate"),
                    constraints_fingerprint=constraints_fingerprint(constraints),
                ))
            except (TypeError, ValueError, ValidationError):
                continue
    output_type = str(constraints.get("output_type", "")).strip().lower() if isinstance(constraints, Mapping) else ""
    requested_outputs = (
        ("card", "report") if output_type == "both" else
        (output_type,) if output_type in {"card", "quote", "report"} else ()
    )
    workflow_mode = str(state.get("workflow_mode", "single_agent"))
    if workflow_mode not in {"single_agent", "multi_agent"}:
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
