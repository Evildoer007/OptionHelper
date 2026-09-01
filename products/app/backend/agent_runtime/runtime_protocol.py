"""Neutral contracts for the OptionHelper Agent runtime boundary.

The protocol is deliberately independent from a particular runtime process.
OptionHelper owns task and model selection authority; this module only validates
the data crossing the runtime boundary and produces JSON-safe projections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..errors import ValidationError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_PRESET_REVISION = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPES = frozenset(
    {
        "runtime.started",
        "runtime.ready",
        "runtime.closed",
        "runtime.error",
        "runtime.recovered",
        "workflow.started",
        "workflow.completed",
        "workflow.failed",
        "workflow.cancelled",
        "agent.started",
        "agent.status",
        "agent.completed",
        "agent.failed",
        "agent.cancelled",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "step.started",
        "step.completed",
        "assistant.block_started",
        "assistant.block_completed",
        "assistant.text_delta",
        "assistant.reasoning_delta",
        "assistant.message",
        "tool.requested",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "tool.cancelled",
        "compaction.started",
        "compaction.completed",
        "compaction.failed",
        "usage.updated",
        "model.route_bound",
        "session.interrupted",
        "session.recovered",
    }
)
_AGENT_STATUSES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "waiting_tool",
        "waiting_parent",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "secret",
        "secrets",
        "password",
        "credential",
        "credentials",
        "apikey",
        "access_token",
        "authorization",
        "private_key",
        "system_prompt",
        "hidden_reasoning",
        "reasoning_content",
    }
)

# These are Host-owned safety limits.  A runtime implementation may choose a
# smaller value for a particular role, but it must not enlarge the published
# boundary without a new protocol version.
MAX_WORKFLOW_STEPS = 8
MAX_WORKFLOW_TOOLS = 12
MAX_WORKFLOW_EVALUATORS = 4
MAX_WORKFLOW_REWORK_ROUNDS = 2


@dataclass(frozen=True)
class FactRef:
    """Opaque reference to one Host-verified financial fact.

    The value is deliberately a reference only.  Numeric values belong to a
    verified Host projection and are never authoritative merely because a
    model returned them.
    """

    fact_ref: str

    def __post_init__(self) -> None:
        value = str(self.fact_ref or "").strip()
        if not value.startswith("fact") or len(value) > 160 or any(ord(char) < 33 for char in value):
            raise ValidationError("FactRef必须是受控的不透明事实引用")
        object.__setattr__(self, "fact_ref", value)

    def to_dict(self) -> dict[str, str]:
        return {"fact_ref": self.fact_ref}

    @classmethod
    def from_value(cls, value: object) -> "FactRef":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(value.get("fact_ref", ""))
        return cls(value)


def _identifier(value: object, field_name: str) -> str:
    result = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(result):
        raise ValidationError(f"{field_name}必须是可打印的受控标识")
    return result


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _identifier(value, field_name)


def _preset_revision(value: object) -> str:
    revision = str(value or "").strip()
    if not _PRESET_REVISION.fullmatch(revision):
        raise ValidationError("preset_revision必须是预设内容的SHA-256标识")
    return revision


def _json_value(value: object, field_name: str = "payload") -> Any:
    """Return a detached JSON value and reject non-JSON runtime objects."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        result = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValidationError(f"{field_name}必须是有限的JSON值") from error
    _assert_safe_keys(result, field_name)
    return result


def _assert_safe_keys(value: object, field_name: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or any(
                marker in normalized
                for marker in ("secret", "password", "credential", "api_key", "access_token")
            ):
                raise ValidationError(f"{field_name}包含不允许跨越运行时边界的字段")
            _assert_safe_keys(item, field_name)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_safe_keys(item, field_name)


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name}必须是对象")
    return dict(_json_value(dict(value), field_name))


def _alias(raw: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return default


def _json_container(value: object, field_name: str) -> Mapping[str, Any] | list[Any]:
    if isinstance(value, Mapping):
        return _mapping(value, field_name)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = _json_value(list(value), field_name)
        if not isinstance(normalized, list):
            raise ValidationError(f"{field_name}必须是对象或数组")
        return normalized
    raise ValidationError(f"{field_name}必须是对象或数组")


def _string_tuple(value: object, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError(f"{field_name}必须是字符串数组")
    values = tuple(_identifier(item, field_name) for item in value)
    if not allow_empty and not values:
        raise ValidationError(f"{field_name}不能为空")
    if len(set(values)) != len(values):
        raise ValidationError(f"{field_name}不能包含重复项")
    return values


@dataclass(frozen=True)
class ModelRouteRef:
    """A host-issued non-secret model route.

    The authority marker is process-local and is intentionally omitted from
    serialization. A route reconstructed from JSON is therefore untrusted and
    must be re-issued by the host before it can reach ModelGateway.
    """

    provider_id: str
    model_id: str
    generation: str = "current"
    source: str = "host"
    route_id: str = ""
    _authority_marker: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        provider = _identifier(self.provider_id, "provider_id")
        model = _identifier(self.model_id, "model_id")
        generation = _identifier(self.generation, "generation")
        source = _identifier(self.source, "source")
        route_id = self.route_id.strip() if isinstance(self.route_id, str) else ""
        if route_id and not _IDENTIFIER.fullmatch(route_id):
            raise ValidationError("route_id必须是可打印的受控标识")
        if not route_id:
            route_id = "route-" + hashlib.sha256(
                f"{provider}\x1f{model}\x1f{generation}".encode("utf-8")
            ).hexdigest()[:32]
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "model_id", model)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "route_id", route_id)

    @property
    def trusted(self) -> bool:
        return bool(self._authority_marker)

    def to_dict(self) -> dict[str, str]:
        return {
            "route_id": self.route_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "generation": self.generation,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ModelRouteRef":
        raw = _mapping(value, "model_route_ref")
        allowed = {"route_id", "provider_id", "model_id", "generation", "source"}
        if set(raw).difference(allowed):
            raise ValidationError("model_route_ref包含未知字段")
        return cls(
            provider_id=raw.get("provider_id", ""),
            model_id=raw.get("model_id", ""),
            generation=raw.get("generation", "current"),
            source=raw.get("source", "host"),
            route_id=raw.get("route_id", ""),
        )


class ModelRouteIssuer:
    """Issues process-local route references after Host-side model resolution."""

    def __init__(self) -> None:
        self._marker = uuid4().hex

    def issue(
        self,
        provider_id: str,
        model_id: str,
        *,
        generation: str = "current",
        source: str = "host",
    ) -> ModelRouteRef:
        route = ModelRouteRef(provider_id, model_id, generation, source)
        return ModelRouteRef(
            route.provider_id,
            route.model_id,
            route.generation,
            route.source,
            route.route_id,
            self._marker,
        )

    def owns(self, route: ModelRouteRef) -> bool:
        return isinstance(route, ModelRouteRef) and route._authority_marker == self._marker


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    persona: str = ""
    model_selection_id: str | None = None
    model_route_ref: ModelRouteRef | None = None
    allowed_tools: tuple[str, ...] = ()
    context_policy: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _identifier(self.role_id, "role_id"))
        if not isinstance(self.persona, str) or len(self.persona) > 16_000:
            raise ValidationError("persona必须是有限长度文本")
        if self.model_selection_id is not None:
            object.__setattr__(self, "model_selection_id", _identifier(self.model_selection_id, "model_selection_id"))
        object.__setattr__(self, "allowed_tools", _string_tuple(self.allowed_tools, "allowed_tools"))
        object.__setattr__(self, "context_policy", _mapping(self.context_policy, "context_policy"))
        object.__setattr__(self, "budget", _mapping(self.budget, "budget"))
        if self.model_route_ref is not None and not isinstance(self.model_route_ref, ModelRouteRef):
            raise ValidationError("model_route_ref类型无效")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "persona": self.persona,
            "model_selection_id": self.model_selection_id,
            "model_route_ref": self.model_route_ref.to_dict() if self.model_route_ref else None,
            "allowed_tools": list(self.allowed_tools),
            "context_policy": dict(self.context_policy),
            "budget": dict(self.budget),
        }

    @classmethod
    def from_dict(cls, value: object) -> "RoleSpec":
        raw = _mapping(value, "role")
        allowed = {
            "role_id", "persona", "model_selection_id", "model_route_ref",
            "allowed_tools", "context_policy", "budget",
            "roleId", "id", "modelSelectionId", "modelRouteRef", "allowedTools", "contextPolicy",
        }
        if set(raw).difference(allowed):
            raise ValidationError("RoleSpec包含未知字段")
        route = _alias(raw, "model_route_ref", "modelRouteRef")
        return cls(
            role_id=_alias(raw, "role_id", "roleId", "id", default=""),
            persona=str(raw.get("persona", "")),
            model_selection_id=_alias(raw, "model_selection_id", "modelSelectionId"),
            model_route_ref=ModelRouteRef.from_dict(route) if route is not None else None,
            allowed_tools=_string_tuple(_alias(raw, "allowed_tools", "allowedTools", default=()), "allowed_tools"),
            context_policy=_alias(raw, "context_policy", "contextPolicy", default={}),
            budget=raw.get("budget", {}),
        )


@dataclass(frozen=True)
class WorkflowSpec:
    """One workflow contract shared by the controller and recommender.

    ``stages`` and the round fields are compatibility fields for the former
    recommender-only definition.  Keeping them here makes old callers use the
    same class as the runtime controller while the serialized boundary stays
    owned by this module.
    """

    workflow_id: str = ""
    task_id: str = ""
    preset_id: str = ""
    preset_revision: str = ""
    root_session_id: str = ""
    roles: tuple[RoleSpec, ...] = ()
    execution_graph: Mapping[str, Any] | Sequence[Any] = field(default_factory=dict)
    max_parallel_agents: int = 1
    max_iterations: int = 1
    deadline_seconds: float = 60.0
    output_contract: Mapping[str, Any] = field(default_factory=dict)
    # Compatibility with the former recommender WorkflowSpec.
    workflow_total_budget: int | None = None
    stages: tuple[Any, ...] = ()
    max_rounds: int = 1
    max_rework_rounds: int = 0
    # Runtime-wide bounded operations.  Role-specific budgets may be lower.
    max_steps_per_run: int = MAX_WORKFLOW_STEPS
    max_tools: int = MAX_WORKFLOW_TOOLS
    max_evaluators: int = MAX_WORKFLOW_EVALUATORS

    def __post_init__(self) -> None:
        preset_id = str(self.preset_id or "workflow").strip()
        preset_id = _identifier(preset_id, "preset_id")
        object.__setattr__(self, "preset_id", preset_id)
        defaults = {
            "workflow_id": f"workflow-{preset_id}",
            "task_id": f"task-{preset_id}",
            "root_session_id": f"agent-root-{preset_id}",
        }
        for field_name in ("workflow_id", "task_id", "root_session_id"):
            object.__setattr__(
                self, field_name,
                _identifier(getattr(self, field_name) or defaults[field_name], field_name),
            )
        object.__setattr__(self, "preset_revision", _preset_revision(self.preset_revision))

        stages = self.stages
        if isinstance(stages, (str, bytes)) or not isinstance(stages, Sequence):
            raise ValidationError("WorkflowSpec.stages必须是数组")
        stages = tuple(stages)
        object.__setattr__(self, "stages", stages)

        if stages:
            stage_ids: list[str] = []
            for stage in stages:
                raw_stage = getattr(stage, "stage", None)
                if raw_stage is None and isinstance(stage, Mapping):
                    raw_stage = _alias(stage, "stage", default=None)
                stage_id = getattr(raw_stage, "value", raw_stage)
                if not isinstance(stage_id, str) or not stage_id.strip():
                    raise ValidationError("WorkflowSpec兼容stages必须声明stage")
                if stage_id in stage_ids:
                    raise ValidationError("WorkflowSpec兼容stage标识不能重复")
                stage_ids.append(stage_id)
            declared_stage_ids = set(stage_ids)
            for stage in stages:
                rework_target = getattr(stage, "rework_to_stage", None)
                if rework_target is None and isinstance(stage, Mapping):
                    rework_target = _alias(stage, "rework_to_stage", "reworkToStage", default=None)
                rework_id = getattr(rework_target, "value", rework_target)
                if rework_id is not None and rework_id not in declared_stage_ids:
                    raise ValidationError("WorkflowSpec兼容stages的rework目标未声明")

        roles = tuple(self.roles)
        if not roles and stages:
            derived_role_ids: list[str] = []
            for stage in stages:
                raw_roles = getattr(stage, "roles", None)
                if raw_roles is None and isinstance(stage, Mapping):
                    raw_roles = _alias(stage, "roles", default=())
                if isinstance(raw_roles, (str, bytes)) or not isinstance(raw_roles, Sequence):
                    raise ValidationError("WorkflowSpec兼容stages的roles必须是数组")
                for raw_role in raw_roles:
                    role_id = _identifier(raw_role, "role_id")
                    if role_id not in derived_role_ids:
                        derived_role_ids.append(role_id)
            roles = tuple(RoleSpec(role_id) for role_id in derived_role_ids)
        object.__setattr__(self, "roles", roles)
        if not roles:
            raise ValidationError("WorkflowSpec至少需要一个角色")
        if any(not isinstance(role, RoleSpec) for role in roles):
            raise ValidationError("WorkflowSpec.roles必须全部是RoleSpec")
        role_ids = [role.role_id for role in roles]
        if len(set(role_ids)) != len(role_ids):
            raise ValidationError("WorkflowSpec角色标识不能重复")
        if isinstance(self.max_parallel_agents, bool) or not isinstance(self.max_parallel_agents, int) or self.max_parallel_agents < 1:
            raise ValidationError("max_parallel_agents必须是正整数")
        stage_parallelism = max((len(getattr(stage, "roles", ())) for stage in stages), default=0)
        max_parallel_agents = max(self.max_parallel_agents, stage_parallelism)
        if max_parallel_agents > len(roles):
            raise ValidationError("max_parallel_agents不能超过角色数量")
        object.__setattr__(self, "max_parallel_agents", max_parallel_agents)
        if isinstance(self.max_iterations, bool) or not isinstance(self.max_iterations, int) or self.max_iterations < 1:
            raise ValidationError("max_iterations必须是正整数")
        max_rounds = self.max_rounds
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValidationError("max_rounds必须是正整数")
        max_iterations = max(self.max_iterations, max_rounds)
        object.__setattr__(self, "max_iterations", max_iterations)
        if isinstance(self.max_rework_rounds, bool) or not isinstance(self.max_rework_rounds, int) or not 0 <= self.max_rework_rounds <= MAX_WORKFLOW_REWORK_ROUNDS:
            raise ValidationError("max_rework_rounds超出运行时上限")
        if self.workflow_total_budget is not None:
            if isinstance(self.workflow_total_budget, bool) or not isinstance(self.workflow_total_budget, int) or self.workflow_total_budget < 1:
                raise ValidationError("workflow_total_budget必须是正整数")
            workflow_total_budget = self.workflow_total_budget
        else:
            workflow_total_budget = max(1, len(stages) or max_iterations)
        object.__setattr__(self, "workflow_total_budget", workflow_total_budget)
        if isinstance(self.deadline_seconds, bool) or not isinstance(self.deadline_seconds, (int, float)) or self.deadline_seconds <= 0:
            raise ValidationError("deadline_seconds必须是正数")
        if isinstance(self.max_steps_per_run, bool) or not isinstance(self.max_steps_per_run, int) or not 1 <= self.max_steps_per_run <= MAX_WORKFLOW_STEPS:
            raise ValidationError("max_steps_per_run必须位于1至8")
        if isinstance(self.max_tools, bool) or not isinstance(self.max_tools, int) or not 0 <= self.max_tools <= MAX_WORKFLOW_TOOLS:
            raise ValidationError("max_tools必须位于0至12")
        if isinstance(self.max_evaluators, bool) or not isinstance(self.max_evaluators, int) or not 1 <= self.max_evaluators <= MAX_WORKFLOW_EVALUATORS:
            raise ValidationError("max_evaluators必须位于1至4")
        object.__setattr__(self, "execution_graph", _json_container(self.execution_graph, "execution_graph"))
        object.__setattr__(self, "output_contract", _mapping(self.output_contract, "output_contract"))

    def role(self, role_id: str) -> RoleSpec:
        for role in self.roles:
            if role.role_id == role_id:
                return role
        raise ValidationError("WorkflowSpec未声明该角色")

    @property
    def declared_run_count(self) -> int:
        return sum(len(getattr(stage, "roles", ())) for stage in self.stages)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "preset_id": self.preset_id,
            "preset_revision": self.preset_revision,
            "root_session_id": self.root_session_id,
            "roles": [role.to_dict() for role in self.roles],
            "execution_graph": dict(self.execution_graph) if isinstance(self.execution_graph, Mapping) else list(self.execution_graph),
            "max_parallel_agents": self.max_parallel_agents,
            "max_iterations": self.max_iterations,
            "deadline_seconds": self.deadline_seconds,
            "output_contract": dict(self.output_contract),
            "workflow_total_budget": self.workflow_total_budget,
            "max_rounds": self.max_rounds,
            "max_rework_rounds": self.max_rework_rounds,
            "max_steps_per_run": self.max_steps_per_run,
            "max_tools": self.max_tools,
            "max_evaluators": self.max_evaluators,
        }
        if self.stages:
            # Compatibility objects are kept in-process.  A serialized
            # runtime request uses execution_graph/roles instead of relying on
            # Python stage objects crossing the boundary.
            result["stages"] = [
                stage.to_dict() if callable(getattr(stage, "to_dict", None)) else dict(stage)
                if isinstance(stage, Mapping) else str(stage)
                for stage in self.stages
            ]
        return result

    @classmethod
    def from_dict(cls, value: object) -> "WorkflowSpec":
        raw = _mapping(value, "workflow_spec")
        allowed = {
            "workflow_id", "task_id", "preset_id", "preset_revision", "root_session_id",
            "roles", "execution_graph", "max_parallel_agents", "max_iterations",
            "deadline_seconds", "output_contract",
            "workflow_total_budget", "stages", "max_rounds", "max_rework_rounds",
            "max_steps_per_run", "max_tools", "max_evaluators",
            "workflowId", "taskId", "presetId", "presetRevision", "rootSessionId",
            "executionGraph", "maxParallelAgents", "maxIterations", "deadlineSeconds",
            "outputContract", "workflowTotalBudget", "maxRounds", "maxReworkRounds",
            "maxStepsPerRun", "maxTools", "maxEvaluators",
        }
        if set(raw).difference(allowed):
            raise ValidationError("WorkflowSpec包含未知字段")
        roles = raw.get("roles")
        if isinstance(roles, (str, bytes)) or not isinstance(roles, Sequence):
            raise ValidationError("WorkflowSpec.roles必须是数组")
        return cls(
            workflow_id=_alias(raw, "workflow_id", "workflowId", default=""),
            task_id=_alias(raw, "task_id", "taskId", default=""),
            preset_id=_alias(raw, "preset_id", "presetId", default=""),
            preset_revision=_alias(raw, "preset_revision", "presetRevision", default=""),
            root_session_id=_alias(raw, "root_session_id", "rootSessionId", default=""),
            roles=tuple(RoleSpec.from_dict(item) for item in roles),
            execution_graph=_alias(raw, "execution_graph", "executionGraph", default={}),
            max_parallel_agents=_alias(raw, "max_parallel_agents", "maxParallelAgents", default=1),
            max_iterations=_alias(raw, "max_iterations", "maxIterations", default=1),
            deadline_seconds=_alias(raw, "deadline_seconds", "deadlineSeconds", default=60.0),
            output_contract=_alias(raw, "output_contract", "outputContract", default={}),
            workflow_total_budget=_alias(raw, "workflow_total_budget", "workflowTotalBudget"),
            stages=tuple(raw.get("stages", ())) if isinstance(raw.get("stages", ()), Sequence) and not isinstance(raw.get("stages", ()), (str, bytes)) else (),
            max_rounds=_alias(raw, "max_rounds", "maxRounds", default=1),
            max_rework_rounds=_alias(raw, "max_rework_rounds", "maxReworkRounds", default=0),
            max_steps_per_run=_alias(raw, "max_steps_per_run", "maxStepsPerRun", default=MAX_WORKFLOW_STEPS),
            max_tools=_alias(raw, "max_tools", "maxTools", default=MAX_WORKFLOW_TOOLS),
            max_evaluators=_alias(raw, "max_evaluators", "maxEvaluators", default=MAX_WORKFLOW_EVALUATORS),
        )


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    workflow_id: str
    parent_run_id: str | None
    session_id: str
    role_id: str
    status: str = "queued"
    model_route_ref: ModelRouteRef | None = None
    started_at: str | None = None
    finished_at: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    input_fact_refs: tuple[Mapping[str, Any], ...] = ()
    output_fact_refs: tuple[Mapping[str, Any], ...] = ()
    error: Mapping[str, Any] | None = None
    parent_session_id: str | None = None
    label: str = ""
    mode: str = "one-shot"
    usage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("run_id", "workflow_id", "session_id", "role_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        object.__setattr__(self, "parent_run_id", _optional_identifier(self.parent_run_id, "parent_run_id"))
        object.__setattr__(self, "parent_session_id", _optional_identifier(self.parent_session_id, "parent_session_id"))
        if self.status not in _AGENT_STATUSES:
            raise ValidationError("AgentRun.status无效")
        if not isinstance(self.label, str) or len(self.label) > 512:
            raise ValidationError("AgentRun.label必须是有限长度文本")
        if self.mode not in {"one-shot", "continuable"}:
            raise ValidationError("AgentRun.mode无效")
        object.__setattr__(self, "usage", _mapping(self.usage, "usage"))
        if self.model_route_ref is not None and not isinstance(self.model_route_ref, ModelRouteRef):
            raise ValidationError("AgentRun.model_route_ref类型无效")
        for field_name in ("tool_calls", "input_fact_refs", "output_fact_refs"):
            raw = getattr(self, field_name)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise ValidationError(f"{field_name}必须是对象数组")
            normalized = tuple(_mapping(item, field_name) for item in raw)
            object.__setattr__(self, field_name, normalized)
        if self.error is not None:
            object.__setattr__(self, "error", _mapping(self.error, "error"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "parent_run_id": self.parent_run_id,
            "parent_session_id": self.parent_session_id,
            "session_id": self.session_id,
            "role_id": self.role_id,
            "label": self.label,
            "mode": self.mode,
            "status": self.status,
            "model_route_ref": self.model_route_ref.to_dict() if self.model_route_ref else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "input_fact_refs": [dict(item) for item in self.input_fact_refs],
            "output_fact_refs": [dict(item) for item in self.output_fact_refs],
            "error": dict(self.error) if self.error else None,
            "usage": dict(self.usage),
        }

    @classmethod
    def from_dict(cls, value: object) -> "AgentRun":
        raw = _mapping(value, "agent_run")
        allowed = {
            "run_id", "workflow_id", "parent_run_id", "session_id", "role_id", "status",
            "model_route_ref", "started_at", "finished_at", "tool_calls", "input_fact_refs",
            "output_fact_refs", "error", "parent_session_id", "label", "mode", "usage",
            "runId", "workflowId", "parentRunId", "sessionId", "roleId", "modelRouteRef",
            "parentSessionId", "startedAt", "finishedAt", "toolCalls", "inputFactRefs",
            "outputFactRefs",
        }
        if set(raw).difference(allowed):
            raise ValidationError("AgentRun包含未知字段")
        route = raw.get("model_route_ref")
        return cls(
            run_id=_alias(raw, "run_id", "runId", default=""),
            workflow_id=_alias(raw, "workflow_id", "workflowId", default=""),
            parent_run_id=_alias(raw, "parent_run_id", "parentRunId"),
            parent_session_id=_alias(raw, "parent_session_id", "parentSessionId"),
            session_id=_alias(raw, "session_id", "sessionId", default=""),
            role_id=_alias(raw, "role_id", "roleId", default=""),
            status=raw.get("status", "queued"),
            model_route_ref=ModelRouteRef.from_dict(_alias(raw, "model_route_ref", "modelRouteRef")) if _alias(raw, "model_route_ref", "modelRouteRef") is not None else None,
            started_at=_alias(raw, "started_at", "startedAt"),
            finished_at=_alias(raw, "finished_at", "finishedAt"),
            tool_calls=_alias(raw, "tool_calls", "toolCalls", default=()),
            input_fact_refs=_alias(raw, "input_fact_refs", "inputFactRefs", default=()),
            output_fact_refs=_alias(raw, "output_fact_refs", "outputFactRefs", default=()),
            error=raw.get("error"),
            label=str(raw.get("label", "")),
            mode=str(raw.get("mode", "one-shot")),
            usage=raw.get("usage", {}),
        )


def _safe_event_payload(event_type: str, payload: object) -> dict[str, Any]:
    value = _mapping(payload, "event_payload")
    if event_type in {"assistant.block_started", "assistant.block_completed"}:
        index = value.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValidationError("Assistant Block索引必须是非负整数")
        result: dict[str, Any] = {"index": index}
        if event_type == "assistant.block_started":
            block_type = str(value.get("block_type", value.get("blockType", ""))).replace("_", "-")
            if block_type not in {"text", "reasoning", "tool-call"}:
                raise ValidationError("Assistant Block类型无效")
            result["block_type"] = block_type
            return result
        block = value.get("block")
        if not isinstance(block, Mapping):
            raise ValidationError("Assistant Block结束事件缺少Block")
        block_type = str(block.get("type", "")).replace("_", "-")
        if block_type in {"text", "reasoning"}:
            text = _safe_reasoning_text(block.get("text", "")) if block_type == "reasoning" else str(block.get("text", ""))[:64_000]
            result["block"] = {"type": block_type, "text": text}
        elif block_type == "tool-call":
            result["block"] = {"type": "tool-call", "name": str(block.get("name", ""))[:80]}
        else:
            raise ValidationError("Assistant Block类型无效")
        return result
    if event_type == "assistant.reasoning_delta":
        content = value.get("delta", value.get("content", value.get("text", "")))
        content = _safe_reasoning_text(content)
        raw_chars = value.get("chars", 0)
        chars = len(content)
        if not content and isinstance(raw_chars, int) and not isinstance(raw_chars, bool) and raw_chars >= 0:
            chars = min(raw_chars, 64_000)
        result: dict[str, Any] = {
            "available": bool(content) or value.get("available") is True,
            "chars": chars,
        }
        if content:
            result["delta"] = content
        if value.get("truncated") is True:
            result["truncated"] = True
        provider_chars = value.get("provider_chars")
        if isinstance(provider_chars, int) and not isinstance(provider_chars, bool) and provider_chars >= chars:
            result["provider_chars"] = provider_chars
        index = value.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            result["index"] = index
        return result
    if event_type == "assistant.text_delta":
        content = str(value.get("delta", value.get("content", value.get("text", ""))))[:64_000]
        result = {"delta": content, "text": content}
        index = value.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            result["index"] = index
        return result
    if event_type == "assistant.message":
        result = dict(value)
        blocks = value.get("blocks")
        if isinstance(blocks, list):
            safe_blocks: list[dict[str, Any]] = []
            for raw in blocks:
                if not isinstance(raw, Mapping):
                    continue
                block_type = str(raw.get("type", "")).replace("_", "-")
                if block_type == "text":
                    safe_blocks.append({"type": "text", "text": str(raw.get("text", ""))[:64_000]})
                elif block_type == "reasoning":
                    safe_blocks.append({"type": "reasoning", "text": _safe_reasoning_text(raw.get("text", ""))})
                elif block_type == "tool-call":
                    safe_blocks.append({"type": "tool-call", "name": str(raw.get("name", ""))[:80]})
            result["blocks"] = safe_blocks
        return result
    return value


def _safe_reasoning_text(value: object) -> str:
    text = str(value or "")[:64_000]
    text = re.sub(
        r"(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password|authorization|private[_ -]?key|base[_ -]?url)\s*[:=]\s*[^\s,;]+",
        r"\1: [已过滤]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [已过滤]", text, flags=re.IGNORECASE)


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    seq: int
    task_id: str
    workflow_id: str
    session_id: str
    agent_run_id: str | None
    turn: int | None
    step: int | None
    type: str
    timestamp: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        for field_name in ("task_id", "workflow_id", "session_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        object.__setattr__(self, "agent_run_id", _optional_identifier(self.agent_run_id, "agent_run_id"))
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValidationError("RuntimeEvent.seq必须是非负整数")
        for field_name in ("turn", "step"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValidationError(f"RuntimeEvent.{field_name}必须是非负整数")
        if self.type not in _EVENT_TYPES:
            raise ValidationError("RuntimeEvent.type未注册")
        object.__setattr__(self, "payload", _safe_event_payload(self.type, self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "turn": self.turn,
            "step": self.step,
            "type": self.type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeEvent":
        raw = _mapping(value, "runtime_event")
        allowed = {
            "event_id", "seq", "task_id", "workflow_id", "session_id", "agent_run_id",
            "turn", "step", "type", "timestamp", "payload", "event",
        }
        if set(raw).difference(allowed):
            raise ValidationError("RuntimeEvent包含未知字段")
        return cls(
            event_id=raw.get("event_id", ""),
            seq=raw.get("seq", 0),
            task_id=raw.get("task_id", ""),
            workflow_id=raw.get("workflow_id", ""),
            session_id=raw.get("session_id", ""),
            agent_run_id=raw.get("agent_run_id"),
            turn=raw.get("turn"),
            step=raw.get("step"),
            type=raw.get("type", raw.get("event", "")),
            timestamp=raw.get("timestamp", ""),
            payload=raw.get("payload", {}),
        )


@dataclass(frozen=True)
class RuntimeRpcRequest:
    request_id: str
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "method", _identifier(self.method.replace("/", "."), "method"))
        object.__setattr__(self, "params", _mapping(self.params, "params"))

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "method": self.method, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeRpcRequest":
        raw = _mapping(value, "rpc_request")
        return cls(raw.get("request_id", ""), raw.get("method", ""), raw.get("params", {}))


@dataclass(frozen=True)
class RuntimeRpcResponse:
    request_id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        if not isinstance(self.ok, bool):
            raise ValidationError("rpc response ok必须是布尔值")
        if self.ok and self.error is not None:
            raise ValidationError("成功RPC响应不能包含error")
        if not self.ok and self.result is not None:
            raise ValidationError("失败RPC响应不能包含result")
        if self.result is not None:
            object.__setattr__(self, "result", _mapping(self.result, "result"))
        if self.error is not None:
            object.__setattr__(self, "error", _mapping(self.error, "error"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ok": self.ok,
            "result": dict(self.result) if self.result else None,
            "error": dict(self.error) if self.error else None,
        }


__all__ = [
    "AgentRun",
    "FactRef",
    "MAX_WORKFLOW_EVALUATORS",
    "MAX_WORKFLOW_REWORK_ROUNDS",
    "MAX_WORKFLOW_STEPS",
    "MAX_WORKFLOW_TOOLS",
    "ModelRouteIssuer",
    "ModelRouteRef",
    "RoleSpec",
    "RuntimeEvent",
    "RuntimeRpcRequest",
    "RuntimeRpcResponse",
    "WorkflowSpec",
]
