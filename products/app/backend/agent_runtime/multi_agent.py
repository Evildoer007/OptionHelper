"""Child-session primitives for the OptionHelper Recommender.

The legacy ``OneShotAgentRuntime`` keeps its one-shot contract. The newer
Recommender port declares its selected preset's parallelism and continuable
roles separately, so the two capability surfaces cannot be confused.
"""

from __future__ import annotations

from concurrent.futures import (
    CancelledError as FutureCancelledError,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from threading import BoundedSemaphore, Lock, Thread
from time import sleep
from time import monotonic
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from ..errors import UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway, ResolvedAgentModelRoute
from ..model_gateway.request_control import ModelRequestCancelled, ModelRequestControl
from ..settings.settings_models import ModelSelection
from .session_context import RunId, SessionEventLog, SessionHeader, SessionId
from .runtime_invariants import RUNTIME_INVARIANTS
from .durability_checkpoint import DispatchCheckpoint, DurabilityCheckpointStore, stable_operation_id


def canonical_role_name(role: object) -> str:
    return str(role or "").strip().removeprefix("SingleAgent.")


@dataclass(frozen=True)
class MultiAgentRecommendationPreset:
    preset_id: str
    display_name: str
    execution_strategy: str
    enabled: bool
    max_agent_runs: int
    max_parallel_agent_runs: int
    max_seconds_per_agent_run: float
    disposal_wait_seconds: float = 0.25
    role_model_defaults: Mapping[str, ModelSelection] = field(default_factory=dict)
    disabled_reason: str | None = None
    # Runtime revisions identify the frozen preset definition; they are not
    # product release versions and do not imply compatibility branches.
    roles: tuple[str, ...] = ()
    workflow_total_budget: int | None = None
    allow_single_agent_fallback: bool = False

    def __post_init__(self) -> None:
        if self.max_agent_runs < 1 or self.max_parallel_agent_runs < 1:
            raise ValueError("MultiAgentRecommendationPreset budgets must be positive")
        if self.workflow_total_budget is not None and self.workflow_total_budget < 1:
            raise ValueError("workflow_total_budget must be positive")
        if len(set(self.roles)) != len(self.roles) or any(not role.strip() for role in self.roles):
            raise ValueError("MultiAgentRecommendationPreset roles must be unique non-empty names")

    @property
    def effective_workflow_total_budget(self) -> int:
        """Use the legacy run limit when an older preset has no workflow budget.

        ``min`` keeps callers that narrow ``max_agent_runs`` with
        ``dataclasses.replace`` safe while allowing the new workflow budget to
        be published independently.
        """

        return min(self.max_agent_runs, self.workflow_total_budget or self.max_agent_runs)

    @property
    def revision(self) -> str:
        """Return the deterministic revision of the executable preset policy.

        This is deliberately a content identity, not an App release version.
        Changing topology, roles, budgets, model defaults or context policy
        changes the revision without introducing a second protocol branch.
        """

        payload = {
            "preset_id": self.preset_id,
            "execution_strategy": self.execution_strategy,
            "enabled": self.enabled,
            "max_agent_runs": self.max_agent_runs,
            "max_parallel_agent_runs": self.max_parallel_agent_runs,
            "max_seconds_per_agent_run": self.max_seconds_per_agent_run,
            "disposal_wait_seconds": self.disposal_wait_seconds,
            "roles": list(self.canonical_roles),
            "workflow_total_budget": self.workflow_total_budget,
            "allow_single_agent_fallback": self.allow_single_agent_fallback,
            "role_model_defaults": {
                role: {
                    "provider_id": selection.provider_id,
                    "model_id": selection.model_id,
                }
                for role, selection in sorted(self.role_model_defaults.items())
            },
            "context_policy": dict(_DEFAULT_CONTEXT_POLICY),
        }
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @property
    def canonical_roles(self) -> tuple[str, ...]:
        values: list[str] = []
        for role in self.roles:
            canonical = canonical_role_name(role)
            if canonical and canonical not in values:
                values.append(canonical)
        return tuple(values)

    def accepts_role(self, role: object) -> bool:
        return canonical_role_name(role) in set(self.canonical_roles)


_PRESETS = {
    "sequential-deliberation": MultiAgentRecommendationPreset(
        preset_id="sequential-deliberation", display_name="Mode 1",
        execution_strategy="sequential", enabled=True, max_agent_runs=4,
        max_parallel_agent_runs=1, max_seconds_per_agent_run=45.0,
        roles=("Interpreter", "Selector", "Reviewer"), workflow_total_budget=4,
        allow_single_agent_fallback=False,
    ),
    "product-trader-loop": MultiAgentRecommendationPreset(
        preset_id="product-trader-loop", display_name="Mode 2",
        execution_strategy="structurer_trader_loop", enabled=True, max_agent_runs=8,
        max_parallel_agent_runs=1, max_seconds_per_agent_run=45.0,
        roles=("Structurer", "Trader", "Reviewer"), workflow_total_budget=8,
        allow_single_agent_fallback=False,
    ),
    "independent-council": MultiAgentRecommendationPreset(
        preset_id="independent-council", display_name="Mode 3",
        execution_strategy="independent_council", enabled=True, max_agent_runs=6,
        max_parallel_agent_runs=2, max_seconds_per_agent_run=45.0,
        roles=("Framer", "Matcher", "Hedger", "Moderator"), workflow_total_budget=6,
        allow_single_agent_fallback=False,
    ),
    "constraint-ranking": MultiAgentRecommendationPreset(
        preset_id="constraint-ranking", display_name="Mode 4",
        execution_strategy="constraint_ranking", enabled=True, max_agent_runs=24,
        max_parallel_agent_runs=4, max_seconds_per_agent_run=45.0,
        roles=("Specifier", "Generator", "Evaluator", "Reviewer"), workflow_total_budget=24,
        allow_single_agent_fallback=False,
    ),
}


_DEFAULT_CONTEXT_POLICY = {
    "maxTokens": 16_384,
    "pruneAtTokens": 12_288,
    "compactAtTokens": 15_360,
    "toolResultMaxChars": 4_000,
    "toolResultHeadChars": 800,
    "toolResultTailChars": 800,
    "summaryMaxChars": 1_600,
}


_DEFAULT_AGENT_INSTRUCTIONS = {
    "sequential-deliberation": {
        "Interpreter": """# Interpreter

职责：将用户目标整理为清晰、可验证的产品约束。

- 区分硬约束、偏好和待确认信息。
- 不生成候选产品，不编造行情、定价或回测结论。
- 输出供Selector直接使用的结构化约束。""",
        "Selector": """# Selector

职责：依据已确认约束生成并比较候选结构。

- 只使用输入中已有的产品目录和受控事实。
- 明确说明候选满足或不满足哪些约束。
- 不把缺失数据推断为已验证事实。""",
        "Reviewer": """# Reviewer

职责：复核候选、约束和证据是否一致。

- 检查事实引用、约束遗漏和结论越界。
- 不新增候选，不替代Host执行金融计算。
- 仅批准证据充分且逻辑闭合的结果。""",
    },
    "product-trader-loop": {
        "Structurer": """# Structurer

职责：提出候选结构、条款调整和验证计划。

- 每次调整必须形成明确的CandidateVersion。
- 标出需要Payoffer、Pricer或Backtester验证的事项。
- 不把尚未运行的计算写成事实。""",
        "Trader": """# Trader

职责：基于Host返回的受控事实评估候选。

- 只接受可追溯到FactRef的金融结论。
- 说明接受、退回或继续验证的具体原因。
- 条款变化后必须基于新版本重新评估。""",
        "Reviewer": """# Reviewer

职责：复核最终候选版本及其事实链。

- 检查CandidateVersion、合同指纹和FactRef是否一致。
- 拒绝跨版本拼接或未经验证的结论。
- 不新增金融事实。""",
    },
    "independent-council": {
        "Framer": """# Framer

职责：定义需求边界、比较口径和未决问题。

- 将共同事实与待判断事项分开。
- 为Matcher和Hedger提供同一比较框架。
- 不预先给出候选优劣结论。""",
        "Matcher": """# Matcher

职责：独立评估候选与用户约束的匹配程度。

- 逐项核对约束与受控事实。
- 不代替Hedger判断风险暴露。
- 对缺少证据的候选明确标记不可确认。""",
        "Hedger": """# Hedger

职责：独立识别风险暴露、对冲条件和边界。

- 只引用已提供或工具返回的风险事实。
- 区分产品风险、市场假设和执行限制。
- 不因匹配度高而弱化风险提示。""",
        "Moderator": """# Moderator

职责：汇总独立意见并形成可追溯结论。

- 保留Matcher与Hedger的实质分歧。
- 结论必须对应共同事实和明确证据。
- 不新增其他角色未验证的金融事实。""",
    },
    "constraint-ranking": {
        "Specifier": """# Specifier

职责：把用户要求转换为硬约束和排序规则。

- 硬约束、软偏好和权重必须明确分开。
- 排序规则应可重复执行。
- 不生成候选或评分结果。""",
        "Generator": """# Generator

职责：在受控产品目录和证据范围内生成候选。

- 每个候选必须满足全部硬约束。
- 不自行修改排序规则。
- 不补造缺失的金融事实。""",
        "Evaluator": """# Evaluator

职责：逐个验证候选并返回可追溯事实。

- 评分只使用Specifier冻结的规则。
- 明确记录淘汰原因和FactRef。
- 不以主观偏好替代确定性比较。""",
        "Reviewer": """# Reviewer

职责：终审确定性排序及其证据。

- 检查硬约束、评分口径和排序结果的一致性。
- 只批准或拒绝，不重写评分规则。
- 拒绝缺少FactRef或跨版本拼接的结果。""",
    },
}


def default_agent_instructions(preset_id: str) -> Mapping[str, str]:
    """Return detached product defaults for one App multi-agent preset."""

    return dict(_DEFAULT_AGENT_INSTRUCTIONS.get(str(preset_id), {}))


def effective_agent_instructions(
    preset: MultiAgentRecommendationPreset,
    overrides: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Resolve complete role instructions without changing runtime authority."""

    defaults = default_agent_instructions(preset.preset_id)
    configured = {
        canonical_role_name(role): str(content).replace("\r\n", "\n").replace("\r", "\n").strip()
        for role, content in (overrides or {}).items()
        if canonical_role_name(role) in preset.canonical_roles and str(content).strip()
    }
    return {
        role: configured.get(role, defaults.get(role, f"# {role}\n\n按当前预设完成该角色职责。"))
        for role in preset.canonical_roles
    }


def effective_preset_revision(
    preset: MultiAgentRecommendationPreset,
    role_instructions: Mapping[str, str],
) -> str:
    """Bind the frozen workflow revision to each effective AGENT.md."""

    payload = {
        "preset_revision": preset.revision,
        "agent_instructions": {
            role: str(role_instructions[role])
            for role in preset.canonical_roles
        },
    }
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def default_agent_context_policy(role: str) -> Mapping[str, Any]:
    """Return an explicit, detached context policy for one child role."""

    del role
    return dict(_DEFAULT_CONTEXT_POLICY)


def recommendation_presets() -> Mapping[str, MultiAgentRecommendationPreset]:
    return dict(_PRESETS)


@dataclass(frozen=True)
class ReviewPolicy:
    """A separately gated review topology, never a recommendation Mode."""

    policy_id: str
    enabled: bool
    roles: tuple[str, ...]
    allowed_verdicts: tuple[str, ...] = ("approved", "rework", "rejected")
    disabled_reason: str | None = None
    terminal_role: str | None = None
    verdict_reducer: Callable[[Sequence[Mapping[str, Any]]], object] | None = None
    uses_stage_terminal_role: bool = False

    def __post_init__(self) -> None:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("ReviewPolicy roles must be unique non-empty names")
        if any(not role.strip() for role in self.roles):
            raise ValueError("ReviewPolicy roles must be unique non-empty names")
        has_terminal_role = self.terminal_role is not None
        has_reducer = self.verdict_reducer is not None
        strategies = sum((has_terminal_role, has_reducer, self.uses_stage_terminal_role))
        if strategies != 1:
            raise ValueError("ReviewPolicy must declare exactly one terminal role, stage terminal role, or verdict reducer")
        if has_terminal_role and self.terminal_role not in self.roles:
            raise ValueError("ReviewPolicy terminal role must be one of its roles")
        if self.uses_stage_terminal_role and self.roles:
            raise ValueError("Stage-terminal ReviewPolicy must not own independent roles")
        if has_reducer and not callable(self.verdict_reducer):
            raise ValueError("ReviewPolicy verdict reducer must be callable")
        if not self.allowed_verdicts:
            raise ValueError("ReviewPolicy must allow at least one verdict")


_REVIEW_POLICIES = {
    "standard-review": ReviewPolicy(
        policy_id="standard-review", enabled=True, roles=(), uses_stage_terminal_role=True,
    ),
    "adversarial-review": ReviewPolicy(
        policy_id="adversarial-review", enabled=False, roles=("Challenger", "Arbiter"),
        terminal_role="Arbiter",
        disabled_reason="独立反方模型绑定和对抗收敛协议尚未发布。",
    ),
}


def review_policies() -> Mapping[str, ReviewPolicy]:
    return dict(_REVIEW_POLICIES)


def resolve_review_policy(value: object) -> ReviewPolicy:
    policy_id = str(value).strip().lower()
    policy = _REVIEW_POLICIES.get(policy_id)
    if policy is None:
        raise ValidationError("未知ReviewPolicy")
    if not policy.enabled:
        raise UnavailableCapabilityError("ReviewPolicy", policy.disabled_reason or "该审阅策略尚未发布。")
    return policy


def agent_runtime_capabilities() -> Mapping[str, Any]:
    """Return published Runtime primitives and Mode-specific topology."""

    production_enabled = {
        "one_shot_child_session": True,
        "lifecycle_projection": True,
        "session_event_log": True,
        "model_visible_surface": True,
        "token_meter": True,
        "tool_result_pruning": True,
        "context_compaction": True,
        "provider_usage_capture": True,
        "automatic_context_maintenance": True,
        "continuable_child_session": True,
        "cold_resume": True,
        "agent_team": False,
    }
    optionhelper_runtime = {
        "independent_child_sessions": True,
        "one_shot_child_session": True,
        "continuable_child_session_by_preset": {
            "sequential-deliberation": (),
            "product-trader-loop": ("Structurer", "Trader"),
            "independent-council": ("Matcher", "Hedger"),
            "constraint-ranking": (),
        },
        "max_parallel_agents_by_preset": {
            preset_id: preset.max_parallel_agent_runs
            for preset_id, preset in _PRESETS.items()
        },
        "context_compaction": True,
        "cold_resume": True,
        "workflow_persistence": True,
        "workflow_scoped_cancel": True,
    }
    return {
        **production_enabled,
        # These names are intentionally nested: the top-level fields above
        # remain the compatibility surface of OneShotAgentRuntime.
        "legacy_one_shot": {
            "one_shot_child_session": True,
            "continuable_child_session": False,
            "max_parallel_agents": 1,
        },
        "optionhelper_runtime": optionhelper_runtime,
        "implemented_primitives": {
            "token_meter": True,
            "tool_result_pruning": True,
            "context_compaction": True,
            "automatic_context_maintenance": True,
            "compaction_failure_atomic": True,
            "fact_ref_preservation": True,
            "tool_call_result_pairing": True,
        },
        "context_policy_defaults": dict(_DEFAULT_CONTEXT_POLICY),
        "disabled_reasons": {
            "agent_team": "不提供脱离Recommender预设的持续协作团队。",
        },
    }


class AgentRunConcurrencyGate:
    """App-owned concurrency limit shared by every one-shot runtime."""

    def __init__(self, maximum_active_runs: int) -> None:
        if maximum_active_runs < 1:
            raise ValueError("maximum_active_runs must be positive")
        self.maximum_active_runs = maximum_active_runs
        self._semaphore = BoundedSemaphore(maximum_active_runs)
        self._lock = Lock()
        self._active_runs = 0

    def acquire(self, timeout_seconds: float) -> "AgentRunConcurrencyLease":
        if not self._semaphore.acquire(timeout=max(0.0, timeout_seconds)):
            raise UnavailableCapabilityError("agent_run_concurrency", "App多智能体并发已满，请稍后重试。")
        with self._lock:
            self._active_runs += 1
        return AgentRunConcurrencyLease(self)

    @property
    def active_runs(self) -> int:
        with self._lock:
            return self._active_runs

    def _release(self) -> None:
        with self._lock:
            self._active_runs -= 1
        self._semaphore.release()


class AgentRunConcurrencyLease:
    def __init__(self, gate: AgentRunConcurrencyGate) -> None:
        self._gate = gate
        self._released = False
        self._lock = Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate._release()


def resolve_recommendation_preset(value: object | None) -> MultiAgentRecommendationPreset:
    preset_id = str(value or "sequential-deliberation").strip().lower()
    preset = _PRESETS.get(preset_id)
    if preset is None:
        raise ValidationError("未知MultiAgentRecommendationPreset")
    if not preset.enabled:
        raise UnavailableCapabilityError(
            "MultiAgentRecommendationPreset", preset.disabled_reason or "该多Agent预设尚未发布。",
        )
    return preset


def _require_enabled_runtime_preset(preset: MultiAgentRecommendationPreset) -> None:
    """Reject disabled registered Modes even when callers construct a runtime directly.

    ``OneShotAgentRuntime`` intentionally accepts small test/deployment presets
    that are not in the registry.  A caller may narrow an enabled preset's
    budget with ``dataclasses.replace``.  It must not, however, relabel a
    registered disabled Mode as enabled and bypass the capability gate.
    """

    registered = _PRESETS.get(preset.preset_id)
    if not preset.enabled or (registered is not None and not registered.enabled):
        reason = (
            (registered.disabled_reason if registered is not None else None)
            or preset.disabled_reason
            or "该多Agent预设尚未发布。"
        )
        raise UnavailableCapabilityError("MultiAgentRecommendationPreset", reason)


@dataclass(frozen=True)
class ChildSessionEvent:
    seq: int
    event: str
    timestamp: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "event": self.event, "timestamp": self.timestamp, **dict(self.data)}


class LifecycleProjection:
    """Best-effort external projection; ChildSession remains authoritative."""

    def __init__(self, event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._events: list[dict[str, Any]] = []
        self._event_sink = event_sink
        self._sink_failures = 0
        self._lock = Lock()

    def append(self, event: Mapping[str, Any]) -> None:
        safe = {key: value for key, value in event.items() if key not in {"payload", "result", "secret"}}
        with self._lock:
            self._events.append(dict(safe))
        if self._event_sink is not None:
            try:
                self._event_sink(dict(safe))
            except Exception:
                with self._lock:
                    self._sink_failures += 1

    @property
    def sink_failures(self) -> int:
        with self._lock:
            return self._sink_failures

    def events_for(self, workflow_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events if item.get("workflow_run_id") == workflow_run_id]


class ChildSession:
    """Independent replayable child state with a monotonic event sequence."""

    def __init__(
        self,
        workflow_run_id: str,
        agent_run_id: str,
        role: str,
        projection: LifecycleProjection,
        event_log: SessionEventLog,
        parent_session_id: SessionId,
        session_id: str | None = None,
    ) -> None:
        self.session_id = session_id or f"child-session-{uuid4().hex}"
        self.workflow_run_id = workflow_run_id
        self.agent_run_id = agent_run_id
        self.role = role
        self._projection = projection
        self._event_log = event_log
        self._event_log.ensure_session(
            SessionId(self.session_id),
            kind="child_agent",
            parent_session_id=parent_session_id,
            workflow_run_id=RunId(workflow_run_id),
            agent_run_id=RunId(agent_run_id),
        )

    def append(self, event: str, *, event_id: str | None = None, **data: Any) -> ChildSessionEvent:
        persisted = self._event_log.append(SessionId(self.session_id), event, data, event_id=event_id)
        row = ChildSessionEvent(persisted.seq, event, persisted.timestamp, dict(data))
        self._projection.append({
            "workflow_run_id": self.workflow_run_id,
            "agent_run_id": self.agent_run_id,
            "child_session_id": self.session_id,
            "role": self.role,
            **row.to_dict(),
        })
        return row

    def replay(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "seq": row.seq,
                "event": row.event_type,
                "timestamp": row.timestamp,
                **dict(row.data),
            }
            for row in self._event_log.replay(SessionId(self.session_id), after_seq=after_seq)
        ]

    def project_persisted_event(self, event: Any) -> None:
        self._projection.append({
            "workflow_run_id": self.workflow_run_id,
            "agent_run_id": self.agent_run_id,
            "child_session_id": self.session_id,
            "role": self.role,
            "seq": event.seq,
            "event": event.event_type,
            "timestamp": event.timestamp,
            **dict(event.data),
        })


class WorkflowRun:
    """Parent-owned projection of child publication and settlement."""

    def __init__(self, event_log: SessionEventLog, parent_session_id: SessionId, workflow_run_id: str) -> None:
        self.workflow_run_id = workflow_run_id
        self._event_log = event_log
        self._parent_session_id = parent_session_id
        self._lock = Lock()
        self._started = False
        self._finished = False
        self._finish_requested = False
        self._active_children: set[str] = set()

    def child_started(self, child: ChildSession) -> None:
        with self._lock:
            if not self._started:
                self._event_log.append(
                    self._parent_session_id,
                    "workflow_run.started",
                    {"workflow_run_id": self.workflow_run_id},
                    event_id=f"workflow:{self.workflow_run_id}:started",
                )
                self._started = True
            self._active_children.add(child.agent_run_id)
        self._event_log.append(
            self._parent_session_id,
            "workflow_run.child_started",
            {
                "workflow_run_id": self.workflow_run_id,
                "agent_run_id": child.agent_run_id,
                "child_session_id": child.session_id,
                "role": child.role,
            },
            event_id=f"workflow:{self.workflow_run_id}:child:{child.agent_run_id}:started",
        )

    def child_settled(self, child: ChildSession) -> None:
        self._event_log.append(
            self._parent_session_id,
            "workflow_run.child_settled",
            {
                "workflow_run_id": self.workflow_run_id,
                "agent_run_id": child.agent_run_id,
                "child_session_id": child.session_id,
            },
            event_id=f"workflow:{self.workflow_run_id}:child:{child.agent_run_id}:settled",
        )
    def child_disposed(self, child: ChildSession) -> None:
        """Publish parent completion only after child disposal is durable."""

        with self._lock:
            self._active_children.discard(child.agent_run_id)
            should_finish = self._finish_requested and not self._active_children and not self._finished
        if should_finish:
            self.finish()

    def finish(self) -> None:
        with self._lock:
            if self._finished or not self._started:
                return
            self._event_log.append(
                self._parent_session_id,
                "workflow_run.finish_requested",
                {"workflow_run_id": self.workflow_run_id},
                event_id=f"workflow:{self.workflow_run_id}:finish_requested",
            )
            self._finish_requested = True
            if self._active_children:
                return
            self._event_log.append(
                self._parent_session_id,
                "workflow_run.finished",
                {"workflow_run_id": self.workflow_run_id},
                event_id=f"workflow:{self.workflow_run_id}:finished",
            )
            self._finished = True


@dataclass(frozen=True)
class AgentRunResult:
    workflow_run_id: str
    agent_run_id: str
    child_session_id: str
    role: str
    status: str
    result: Mapping[str, Any] | None
    elapsed_seconds: float
    model_selection: Mapping[str, str]
    model_selection_source: str


class AgentRunTimedOut(RuntimeError):
    pass


class AgentRunCancelled(RuntimeError):
    pass


class AgentRunHandle:
    """Published owner of one child session, result, cancel, and disposal."""

    def __init__(
        self,
        session: ChildSession,
        future: Future[AgentRunResult],
        executor: ThreadPoolExecutor,
        request_control: ModelRequestControl,
        timeout_seconds: float,
        workflow_run: WorkflowRun,
        concurrency_lease: AgentRunConcurrencyLease,
    ) -> None:
        self.session = session
        self._future = future
        self._executor = executor
        self._request_control = request_control
        self._timeout_seconds = timeout_seconds
        self._workflow_run = workflow_run
        self._concurrency_lease = concurrency_lease
        self._dispose_lock = Lock()
        self._settlement_lock = Lock()
        self._dispose_requested = False
        self._disposed = False
        self._callback_installed = False
        self._resources_released = False
        self._disposal_persistence_failed = False
        self._disposal_failure_count = 0

    @property
    def agent_run_id(self) -> str:
        return self.session.agent_run_id

    @property
    def result_future(self) -> Future[AgentRunResult]:
        return self._future

    @property
    def disposed(self) -> bool:
        with self._dispose_lock:
            return self._disposed

    @property
    def disposal_persistence_failed(self) -> bool:
        with self._dispose_lock:
            return self._disposal_persistence_failed

    def result(self, timeout_seconds: float | None = None) -> AgentRunResult:
        timeout = self._timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        deadline = monotonic() + timeout
        while True:
            if self._request_control.cancelled and not self._future.done():
                self.session.append("agent_run.caller_settled", status="cancelled")
                raise AgentRunCancelled(self._request_control.reason or "AgentRun cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                self.cancel("timeout")
                self.session.append("agent_run.caller_settled", status="timed_out")
                raise AgentRunTimedOut("AgentRun exceeded its deadline")
            try:
                return self._future.result(timeout=min(0.02, remaining))
            except FutureTimeoutError:
                continue
            except FutureCancelledError as error:
                raise AgentRunCancelled(self._request_control.reason or "AgentRun cancelled") from error
            except ModelRequestCancelled as error:
                raise AgentRunCancelled(str(error)) from error

    def cancel(self, reason: str = "parent_cancelled") -> None:
        self._request_control.cancel(reason)
        self._future.cancel()
        self.session.append("agent_run.cancel_requested", reason=str(reason)[:80])

    def dispose(self, wait_seconds: float) -> bool:
        """Request teardown and wait briefly; never claim disposal while live."""

        with self._dispose_lock:
            self._dispose_requested = True
        if not self._future.done():
            self.cancel("dispose")
        try:
            self._future.result(timeout=max(0.0, wait_seconds))
        except FutureTimeoutError:
            self.session.append("agent_run.disposal_deferred", provider_quiescent=False)
            self._install_quiescence_callback()
            return False
        except Exception:
            pass
        self._attempt_mark_disposed(propagate=True)
        return True

    def retry_disposal_persistence(self, wait_seconds: float = 0.25) -> bool:
        """Retry parent settlement after provider work has become quiescent."""

        if not self._future.done():
            try:
                self._future.result(timeout=max(0.0, wait_seconds))
            except FutureTimeoutError:
                return False
            except Exception:
                pass
        self._attempt_mark_disposed(propagate=True)
        return self.disposed

    def _install_quiescence_callback(self) -> None:
        with self._dispose_lock:
            if self._callback_installed:
                return
            self._callback_installed = True
        self._future.add_done_callback(lambda _future: self._attempt_mark_disposed(propagate=False))

    def _attempt_mark_disposed(self, *, propagate: bool) -> None:
        try:
            self._mark_disposed()
        except Exception as error:
            self._record_disposal_persistence_failure(error)
            if propagate:
                raise

    def _record_disposal_persistence_failure(self, error: Exception) -> None:
        with self._dispose_lock:
            self._disposal_persistence_failed = True
            self._disposal_failure_count += 1
            attempt = self._disposal_failure_count
        try:
            self.session.append(
                "agent_run.disposal_persistence_failed",
                event_id=f"agent:{self.agent_run_id}:disposal_persistence_failed:{attempt}",
                retryable=True,
                failure_type=type(error).__name__,
            )
        except Exception:
            self.session._projection.append({
                "workflow_run_id": self.session.workflow_run_id,
                "agent_run_id": self.agent_run_id,
                "child_session_id": self.session.session_id,
                "role": self.session.role,
                "event": "agent_run.disposal_persistence_failed",
                "retryable": True,
                "persistence_unavailable": True,
            })

    def _mark_disposed(self) -> None:
        with self._settlement_lock:
            with self._dispose_lock:
                if self._disposed:
                    return
                release_resources = not self._resources_released
            if release_resources:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._concurrency_lease.release()
                with self._dispose_lock:
                    self._resources_released = True
            self._workflow_run.child_settled(self.session)
            self.session.append(
                "agent_run.provider_quiesced",
                event_id=f"agent:{self.agent_run_id}:provider_quiesced",
                provider_quiescent=True,
            )
            self.session.append(
                "agent_run.disposed",
                event_id=f"agent:{self.agent_run_id}:disposed",
                provider_quiescent=True,
            )
            self._workflow_run.child_disposed(self.session)
            with self._dispose_lock:
                self._disposed = True
                self._disposal_persistence_failed = False


class AgentRunSettlementRegistry:
    """App-owned registry that closes abandoned one-shot runs without faking state."""

    def __init__(
        self,
        event_log: SessionEventLog,
        *,
        maximum_attempts: int = 6,
        initial_backoff_seconds: float = 0.05,
        maximum_backoff_seconds: float = 0.5,
        recover_on_start: bool = True,
    ) -> None:
        if maximum_attempts < 1 or initial_backoff_seconds < 0 or maximum_backoff_seconds <= 0:
            raise ValueError("AgentRun settlement retry configuration is invalid")
        self._event_log = event_log
        self._maximum_attempts = maximum_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._maximum_backoff_seconds = maximum_backoff_seconds
        self._lock = Lock()
        self._pending: dict[str, AgentRunHandle] = {}
        if recover_on_start:
            self.recover_incomplete_runs()

    @property
    def pending_runs(self) -> int:
        with self._lock:
            return len(self._pending)

    def retain_for_settlement(self, handle: AgentRunHandle) -> None:
        """Retain a handle and start one bounded idempotent retry worker."""

        if handle.disposed:
            return
        key = handle.session.session_id
        with self._lock:
            if key in self._pending:
                return
            self._pending[key] = handle
        Thread(
            target=self._retry_handle,
            args=(key,),
            name=f"optionhelper-settle-{handle.agent_run_id[:18]}",
            daemon=True,
        ).start()

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = monotonic() + max(0.0, timeout_seconds)
        while monotonic() < deadline:
            if self.pending_runs == 0:
                return True
            sleep(0.01)
        return self.pending_runs == 0

    def recover_incomplete_runs(self) -> int:
        """Settle child sessions left by a previous App process.

        In-process providers cannot survive an App process restart. Recovery
        therefore records provider quiescence, but never invents a model result.
        """

        recovered = 0
        for header in self._event_log.headers(kind="child_agent"):
            if header.parent_session_id is None or header.workflow_run_id is None or header.agent_run_id is None:
                continue
            events = self._event_log.replay(header.session_id)
            event_types = {event.event_type for event in events}
            if "agent_run.disposed" in event_types:
                continue
            workflow_run_id = str(header.workflow_run_id)
            agent_run_id = str(header.agent_run_id)
            self._event_log.append(
                header.session_id,
                "agent_run.restart_recovery_started",
                {"provider_process_survived": False, "result_replayed": False},
                event_id=f"agent:{agent_run_id}:restart_recovery_started",
            )
            self._event_log.append(
                header.parent_session_id,
                "workflow_run.child_settled",
                {
                    "workflow_run_id": workflow_run_id,
                    "agent_run_id": agent_run_id,
                    "child_session_id": str(header.session_id),
                },
                event_id=f"workflow:{workflow_run_id}:child:{agent_run_id}:settled",
            )
            self._event_log.append(
                header.session_id,
                "agent_run.provider_quiesced",
                {"provider_quiescent": True},
                event_id=f"agent:{agent_run_id}:provider_quiesced",
            )
            self._event_log.append(
                header.session_id,
                "agent_run.disposed",
                {"provider_quiescent": True},
                event_id=f"agent:{agent_run_id}:disposed",
            )
            recovered += 1
        self.recover_requested_workflow_completions()
        return recovered

    def recover_requested_workflow_completions(self) -> int:
        """Finish every requested Workflow whose ChildSessions are durably disposed."""

        finished = 0
        child_headers = self._event_log.headers(kind="child_agent")
        for parent_header in self._event_log.headers(kind="conversation"):
            parent_events = self._event_log.replay(parent_header.session_id)
            requested = {
                str(event.data.get("workflow_run_id", ""))
                for event in parent_events
                if event.event_type == "workflow_run.finish_requested"
            }
            completed = {
                str(event.data.get("workflow_run_id", ""))
                for event in parent_events
                if event.event_type == "workflow_run.finished"
            }
            for workflow_run_id in sorted(requested.difference(completed)):
                RUNTIME_INVARIANTS.validate_workflow_recovery(parent_events, workflow_run_id)
                if workflow_run_id and self._finish_recovered_workflow(
                    parent_header.session_id, workflow_run_id, child_headers,
                ):
                    finished += 1
        return finished

    def _retry_handle(self, key: str) -> None:
        with self._lock:
            handle = self._pending.get(key)
        if handle is None:
            return
        delay = self._initial_backoff_seconds
        for attempt in range(1, self._maximum_attempts + 1):
            if delay:
                sleep(delay)
            self._append_retry_event(handle, "agent_run.disposal_retry_attempted", attempt, terminal=False)
            try:
                if handle.retry_disposal_persistence(wait_seconds=self._maximum_backoff_seconds):
                    self._append_retry_event(handle, "agent_run.disposal_retry_succeeded", attempt, terminal=True)
                    with self._lock:
                        self._pending.pop(key, None)
                    return
            except Exception:
                pass
            delay = min(self._maximum_backoff_seconds, max(delay * 2, self._initial_backoff_seconds))
        self._append_retry_event(
            handle,
            "agent_run.disposal_retry_exhausted",
            self._maximum_attempts,
            terminal=True,
            provider_quiescent=handle.result_future.done(),
            disposal_durable=handle.disposed,
            settlement_handle_retained=False,
        )
        # At this point retries have failed conclusively for this process.  Do
        # not keep a permanent in-memory ownership reference.  This is not a
        # disposal claim: if durable settlement still failed, restart recovery
        # sees the missing ``agent_run.disposed`` event and records recovery.
        with self._lock:
            if self._pending.get(key) is handle:
                self._pending.pop(key, None)

    def _append_retry_event(
        self,
        handle: AgentRunHandle,
        event_type: str,
        attempt: int,
        *,
        terminal: bool,
        **data: Any,
    ) -> None:
        try:
            handle.session.append(
                event_type,
                event_id=f"agent:{handle.agent_run_id}:{event_type}:{attempt}",
                attempt=attempt,
                maximum_attempts=self._maximum_attempts,
                terminal=terminal,
                **data,
            )
        except Exception:
            return

    def _finish_recovered_workflow(
        self,
        parent_session_id: SessionId,
        workflow_run_id: str,
        child_headers: Sequence[SessionHeader],
    ) -> bool:
        events = self._event_log.replay(parent_session_id)
        workflow_events = [
            event for event in events
            if str(event.data.get("workflow_run_id", "")) == workflow_run_id
        ]
        RUNTIME_INVARIANTS.validate_workflow_recovery(events, workflow_run_id)
        if not any(event.event_type == "workflow_run.finish_requested" for event in workflow_events):
            return False
        if any(event.event_type == "workflow_run.finished" for event in workflow_events):
            return False
        started_child_ids = {
            str(event.data.get("child_session_id", ""))
            for event in workflow_events if event.event_type == "workflow_run.child_started"
        }
        workflow_children = {
            str(header.session_id): header
            for header in child_headers
            if header.parent_session_id == parent_session_id
            and str(header.workflow_run_id or "") == workflow_run_id
        }
        expected_child_ids = started_child_ids.union(workflow_children)
        if not expected_child_ids or not expected_child_ids.issubset(workflow_children):
            return False
        if any(
            "agent_run.disposed" not in {
                event.event_type for event in self._event_log.replay(workflow_children[child_id].session_id)
            }
            for child_id in expected_child_ids
        ):
            return False
        self._event_log.append(
            parent_session_id,
            "workflow_run.finished",
            {"workflow_run_id": workflow_run_id},
            event_id=f"workflow:{workflow_run_id}:finished",
        )
        return True


class OneShotAgentRuntime:
    def __init__(
        self,
        gateway: ModelGateway,
        identity: SessionIdentity,
        task_id: str,
        *,
        preset: MultiAgentRecommendationPreset,
        selection: ModelSelection | None = None,
        is_cancelled: Callable[[SessionIdentity, str], bool] | None = None,
        lifecycle_projection: LifecycleProjection | None = None,
        role_model_defaults: Mapping[str, ModelSelection] | None = None,
        host_multi_agent: bool = True,
        event_log: SessionEventLog | None = None,
        parent_session_id: SessionId | None = None,
        concurrency_gate: AgentRunConcurrencyGate | None = None,
        workflow_run_id: str | None = None,
        execution_id: str | None = None,
        step_id_namespace: str | None = None,
        settlement_registry: AgentRunSettlementRegistry | None = None,
        visible_event_sink: Callable[[str, str, str], None] | None = None,
    ) -> None:
        _require_enabled_runtime_preset(preset)
        self._gateway = gateway
        self._identity = identity
        self._task_id = task_id
        self._preset = preset
        self._explicit_selection = selection
        self._role_model_defaults = dict(role_model_defaults or preset.role_model_defaults)
        self._is_cancelled = is_cancelled or (lambda _identity, _task_id: False)
        self._projection = lifecycle_projection or LifecycleProjection()
        self._host_multi_agent = host_multi_agent
        self.workflow_run_id = str(workflow_run_id or f"workflow-{uuid4().hex}")
        self.execution_id = str(execution_id or f"execution-{uuid4().hex}")
        self._step_id_namespace = str(step_id_namespace or f"step-{uuid4().hex}")
        self._event_log = event_log or SessionEventLog()
        self._dispatch_checkpoints = DurabilityCheckpointStore(self._event_log)
        self._parent_session_id = parent_session_id or SessionId(f"conversation-{uuid4().hex}")
        self._event_log.ensure_session(self._parent_session_id, kind="conversation")
        self._settlement_registry = settlement_registry or AgentRunSettlementRegistry(
            self._event_log, recover_on_start=False,
        )
        self._workflow_run = WorkflowRun(self._event_log, self._parent_session_id, self.workflow_run_id)
        self._concurrency_gate = concurrency_gate or AgentRunConcurrencyGate(preset.max_parallel_agent_runs)
        self._visible_event_sink = visible_event_sink
        self._started_runs = 0
        self._role_run_counts: dict[str, int] = {}
        self._budget_lock = Lock()

    @property
    def lifecycle_projection(self) -> LifecycleProjection:
        return self._projection

    @property
    def preset(self) -> MultiAgentRecommendationPreset:
        return self._preset

    @property
    def concurrency_gate(self) -> AgentRunConcurrencyGate:
        return self._concurrency_gate

    @property
    def settlement_registry(self) -> AgentRunSettlementRegistry:
        return self._settlement_registry

    def start_run(self, role: str, payload: Mapping[str, Any]) -> AgentRunHandle:
        normalized_role = str(role).removeprefix("SingleAgent.")
        canonical_role = canonical_role_name(normalized_role)
        if not self._preset.accepts_role(canonical_role):
            raise ValidationError("AgentRun角色不属于当前MultiAgentRecommendationPreset")
        declared_roles = {canonical_role_name(item): item for item in self._preset.roles}
        wire_role = normalized_role if normalized_role in self._preset.roles else declared_roles[canonical_role]
        with self._budget_lock:
            if self._started_runs >= self._preset.effective_workflow_total_budget:
                raise ValidationError("MultiAgentRecommendationPreset超过AgentRun预算")
            self._started_runs += 1
        if self._is_cancelled(self._identity, self._task_id):
            raise AgentRunCancelled("parent task already cancelled")
        route = self._resolve_model_route(wire_role)
        request_control_gate = getattr(self._gateway, "require_bounded_request_control", None)
        if not callable(request_control_gate):
            raise UnavailableCapabilityError(
                "bounded_request_control",
                "当前ModelGateway未提供多Agent受控请求门禁。",
            )
        request_control_gate(self._identity, selection=route.dispatch_selection)
        concurrency_lease = self._concurrency_gate.acquire(self._preset.max_seconds_per_agent_run)
        with self._budget_lock:
            role_ordinal = self._role_run_counts.get(canonical_role, 0) + 1
            self._role_run_counts[canonical_role] = role_ordinal
        stable_run_digest = hashlib.sha256(
            f"{self._step_id_namespace}\x1f{canonical_role}\x1f{role_ordinal}".encode("utf-8")
        ).hexdigest()[:24]
        agent_run_id = f"agent-{stable_run_digest}"
        child_session_id = f"child-session-{stable_run_digest}"
        try:
            session = ChildSession(
                self.workflow_run_id,
                agent_run_id,
                wire_role,
                self._projection,
                self._event_log,
                self._parent_session_id,
                child_session_id,
            )
            self._workflow_run.child_started(session)
        except Exception:
            concurrency_lease.release()
            raise
        control = ModelRequestControl(self._preset.max_seconds_per_agent_run)
        session.append(
            "agent_run.started", preset_id=self._preset.preset_id, preset_revision=self._preset.revision,
            role=wire_role,
            model_selection=_selection_projection(route.model_selection), model_selection_source=route.source,
            input_hash=_hash(payload), parent_context_inherited=False, one_shot=True,
        )
        self._emit_visible_event("agent_run", "started", _role_started_summary(normalized_role))
        executor: ThreadPoolExecutor | None = None
        try:
            dispatch_checkpoints = DurabilityCheckpointStore(
                self._event_log, event_sink=session.project_persisted_event,
            )
            checkpoint = dispatch_checkpoints.checkpoint(
                SessionId(session.session_id),
                operation_id=stable_operation_id(session.agent_run_id, "model"),
                operation_kind="child_model",
                safe_metadata={"agent_run_id": session.agent_run_id, "role": wire_role},
            )
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"optionhelper-{agent_run_id[:18]}")
            future = executor.submit(
                self._invoke_model, session, wire_role, payload, route, control,
                dispatch_checkpoints, checkpoint,
            )
        except Exception:
            control.cancel("start_failed")
            session.append("agent_run.start_failed", provider_quiescent=True)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            concurrency_lease.release()
            self._workflow_run.child_settled(session)
            session.append("agent_run.disposed", provider_quiescent=True)
            self._workflow_run.child_disposed(session)
            raise
        Thread(
            target=self._watch_parent_cancellation,
            args=(future, control, session),
            name=f"optionhelper-cancel-{agent_run_id[:18]}",
            daemon=True,
        ).start()
        return AgentRunHandle(
            session, future, executor, control, self._preset.max_seconds_per_agent_run, self._workflow_run,
            concurrency_lease,
        )

    def _watch_parent_cancellation(
        self,
        future: Future[AgentRunResult],
        control: ModelRequestControl,
        session: ChildSession,
    ) -> None:
        while not future.done():
            if self._is_cancelled(self._identity, self._task_id):
                control.cancel("parent_task_cancelled")
                session.append("agent_run.cancel_requested", reason="parent_task_cancelled")
                return
            sleep(0.02)

    def run_step_result(self, role: str, payload: Mapping[str, Any]) -> AgentRunResult:
        handle = self.start_run(role, payload)
        try:
            return handle.result()
        except AgentRunTimedOut as error:
            raise UnavailableCapabilityError("AgentRun", "子Agent超时，未采用其结果。") from error
        except AgentRunCancelled as error:
            raise UnavailableCapabilityError("AgentRun", "父任务已取消，子Agent结果已丢弃。") from error
        finally:
            self._settle_or_retain(handle)

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self.run_step_result(role, payload).result or {})

    def run_steps_results(self, role: str, payloads: Sequence[Mapping[str, Any]]) -> list[AgentRunResult]:
        if not self._host_multi_agent:
            raise ValidationError("single-agent fallback executor cannot publish sibling AgentRuns")
        if len(payloads) > self._preset.max_parallel_agent_runs:
            raise ValidationError("MultiAgentRecommendationPreset超过并行AgentRun预算")
        handles: list[AgentRunHandle] = []
        results: list[AgentRunResult] = []
        try:
            for payload in payloads:
                handles.append(self.start_run(role, payload))
            deadline = monotonic() + self._preset.max_seconds_per_agent_run
            for handle in handles:
                remaining = max(0.0, deadline - monotonic())
                results.append(handle.result(remaining))
            return results
        except Exception:
            for sibling in handles:
                if not sibling.result_future.done():
                    sibling.cancel("sibling_failed")
            raise
        finally:
            for handle in handles:
                self._settle_or_retain(handle)

    def run_steps(self, role: str, payloads: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [dict(item.result or {}) for item in self.run_steps_results(role, payloads)]

    def close(self) -> None:
        self._workflow_run.finish()

    def _settle_or_retain(self, handle: AgentRunHandle) -> None:
        try:
            disposed = handle.dispose(self._preset.disposal_wait_seconds)
        except Exception:
            self._settlement_registry.retain_for_settlement(handle)
            return
        if not disposed:
            self._settlement_registry.retain_for_settlement(handle)

    def _resolve_model_route(self, role: str) -> ResolvedAgentModelRoute:
        normalized = str(role).removeprefix("SingleAgent.")
        canonical = canonical_role_name(normalized)
        default = self._role_model_defaults.get(normalized)
        if default is None:
            default = self._role_model_defaults.get(canonical)
        if default is None:
            default = next(
                (
                    value for key, value in self._role_model_defaults.items()
                    if canonical_role_name(key) == canonical
                ),
                None,
            )
        resolver = getattr(self._gateway, "resolve_agent_model_route", None)
        if callable(resolver):
            return resolver(
                self._identity,
                explicit_selection=self._explicit_selection,
                preset_role_selection=default,
            )
        selection = self._explicit_selection or default
        if selection is None:
            binding = getattr(self._gateway, "model_binding_for", lambda *_args, **_kwargs: {"provider_id": "session", "model_id": "default"})(self._identity)
            selection = ModelSelection(str(binding["provider_id"]), str(binding["model_id"]))
            return ResolvedAgentModelRoute(selection, None, "session_default")
        source = "user_explicit" if self._explicit_selection is not None else "preset_role_default"
        return ResolvedAgentModelRoute(selection, selection, source)

    def _invoke_model(
        self,
        session: ChildSession,
        role: str,
        payload: Mapping[str, Any],
        route: ResolvedAgentModelRoute,
        control: ModelRequestControl,
        dispatch_checkpoints: DurabilityCheckpointStore,
        checkpoint: DispatchCheckpoint,
    ) -> AgentRunResult:
        started = monotonic()
        result: Mapping[str, Any] | None = None
        status = "failed"
        external_settled = False
        try:
            control.raise_if_cancelled()
            session.append("agent_run.model_requested")
            context = {
                "operation": "recommender_fixed_step",
                "child_context": {
                    "child_session_id": session.session_id,
                    "workflow_run_id": self.workflow_run_id,
                    "agent_run_id": session.agent_run_id,
                    "role": role,
                    "preset": {"id": self._preset.preset_id, "revision": self._preset.revision},
                    "one_shot": True,
                    "parent_context_inherited": False,
                },
                "role": role,
                "input": dict(payload),
                "rule": "只返回该步骤的结构化result；不得调用工具、不得产生金融数值。",
            }
            response = self._gateway.decide_for(
                self._identity,
                self._task_id,
                context,
                selection=route.dispatch_selection,
                request_control=control,
            )
            control.raise_if_cancelled()
            if not isinstance(response, Mapping) or set(response) != {"action", "result"} or response.get("action") != "final":
                raise ValidationError("子Agent必须返回{action:'final',result:{...}}")
            raw_result = response.get("result")
            if not isinstance(raw_result, Mapping):
                raise ValidationError("子Agent结构化result必须为对象")
            result = dict(raw_result)
            dispatch_checkpoints.succeeded(checkpoint)
            external_settled = True
            status = "succeeded"
            return AgentRunResult(
                self.workflow_run_id, session.agent_run_id, session.session_id, role, status, result,
                monotonic() - started, _selection_projection(route.model_selection), route.source,
            )
        except ModelRequestCancelled:
            status = "cancelled"
            if not external_settled:
                dispatch_checkpoints.failed(checkpoint)
            raise
        except Exception:
            if not external_settled:
                dispatch_checkpoints.failed(checkpoint)
            raise
        finally:
            session.append(
                "agent_run.finished", status=status, elapsed_seconds=round(monotonic() - started, 6),
                output_hash=_hash(result) if result is not None else None,
            )
            visible_status = "completed" if status == "succeeded" else (
                "cancelled" if status == "cancelled" else "failed"
            )
            self._emit_visible_event("agent_run", visible_status, _role_finished_summary(role, visible_status))

    def _emit_visible_event(self, event_type: str, status: str, summary: str) -> None:
        if self._visible_event_sink is not None:
            self._visible_event_sink(event_type, status, summary)


def _role_started_summary(role: str) -> str:
    labels = {
        "Interpreter": "正在分析需求与约束。",
        "Selector": "正在检索条款并整理候选结构。",
        "Structurer": "正在生成候选结构。",
        "Trader": "正在检查交易约束。",
        "Reviewer": "正在复核候选结构。",
        "Framer": "正在整理需求约束。",
        "Matcher": "正在比较候选结构。",
        "Hedger": "正在检查主要风险。",
        "Moderator": "正在汇总候选意见。",
        "Specifier": "正在整理排序条件。",
        "Generator": "正在生成备选结构。",
        "Evaluator": "正在核对候选依据。",
    }
    return labels.get(role, "正在完成候选分析。")


def _role_finished_summary(role: str, status: str) -> str:
    if status == "completed":
        return "当前分析阶段已完成。"
    if status == "cancelled":
        return "当前分析阶段已取消。"
    return "当前分析阶段未完成。"


def _selection_projection(selection: ModelSelection) -> dict[str, str]:
    return {"provider_id": selection.provider_id, "model_id": selection.model_id}


def _hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValidationError("AgentRun payload must be JSON-compatible") from error
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
