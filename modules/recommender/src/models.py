"""Recommender领域对象与严格校验。

本模块只保存当前推荐快照、解释和运行引用。产品条款、市场数据、已编译合同
及历史候选拓扑均由所属Host在确认后按最新OptionReg处理。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Mapping, Sequence

from runtime.protocol.models import ModuleRunRef as CoreModuleRunRef


ROUTES = {
    "chat", "knowledge", "data", "direct_execution", "recommendation",
    "professional_report", "existing_report", "maintenance", "freeform",
}
LIBRARY_STATUSES = {"ready", "partial", "conflict", "unavailable"}
CANDIDATE_STATUSES = {
    "candidate", "pending_confirmation", "pending_data", "pending_terms",
    "approved", "running", "rejected", "unsupported",
}
SET_STATUSES = {
    "pending_question", "candidate_ready", "pending_approval", "running",
    "completed", "partial", "unavailable",
}
RUN_STATUSES = {
    "pending", "running", "succeeded", "partial", "failed", "unsupported",
    "cancelled", "timed_out",
}
ANALYSIS_STATUSES = {
    "not_started", "running", "completed", "partial", "failed", "unsupported",
    "cancelled", "timed_out",
}
DELIVERY_STATUSES = {"not_requested", "pending", "completed", "partial", "unavailable"}
RECOMMENDATION_SET_SCHEMA = "optionhelper.recommendation-set"
_INTERNAL_PUBLIC_TEXT = re.compile(
    r"(?i)(?:candidate|evidence|module_run|run|task|analysis|audit|resolved_contract)[a-z0-9_]*"
)


class RecommendationValidationError(ValueError):
    """结构化推荐对象不满足机器契约。"""


def _required_text(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise RecommendationValidationError(f"{field_name}不能为空")
    return text


def _identifier(value: Any, field_name: str) -> str:
    identifier = _required_text(value, field_name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", identifier):
        raise RecommendationValidationError(f"{field_name}必须为安全标识符")
    return identifier


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _identifier(value, field_name)


def _positive_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecommendationValidationError(f"{field_name}必须为正整数")
    return value


def _text_tuple(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise RecommendationValidationError(f"{field_name}必须为字符串数组")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if not allow_empty and not result:
        raise RecommendationValidationError(f"{field_name}不能为空")
    return result


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecommendationValidationError(f"{field_name}必须为对象")
    return dict(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RecommendationValidationError(f"当前输入含不可序列化类型：{type(value).__name__}")


def _display_terms(value: Any) -> tuple[Mapping[str, str], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RecommendationValidationError("key_terms必须为数组")
    result: list[Mapping[str, str]] = []
    for index, item in enumerate(value):
        term = _mapping(item, f"key_terms[{index}]")
        if set(term) != {"label", "value", "source"}:
            raise RecommendationValidationError("key_terms字段必须为label,value,source")
        source = _required_text(term.get("source"), f"key_terms[{index}].source")
        if source not in {"用户输入", "拟采用参数"}:
            raise RecommendationValidationError("key_terms.source必须为用户输入或拟采用参数")
        result.append({
            "label": _required_text(term.get("label"), f"key_terms[{index}].label"),
            "value": _required_text(term.get("value"), f"key_terms[{index}].value"),
            "source": source,
        })
    return tuple(result)


@dataclass(frozen=True)
class CandidateSelectionSpec:
    requested_candidate_count: int = 3

    def __post_init__(self) -> None:
        value = _positive_integer(self.requested_candidate_count, "requested_candidate_count")
        if value > 10:
            raise RecommendationValidationError("requested_candidate_count必须位于1至10")

    @property
    def candidate_count(self) -> int:
        return self.requested_candidate_count

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateSelectionSpec":
        data = dict(value)
        unknown = sorted(set(data) - {"requested_candidate_count", "candidate_count"})
        if unknown:
            raise RecommendationValidationError(f"CandidateSelectionSpec含未知字段：{','.join(unknown)}")
        requested = data.get("requested_candidate_count")
        alias = data.get("candidate_count")
        if requested is not None and alias is not None and requested != alias:
            raise RecommendationValidationError("CandidateSelectionSpec数量字段冲突")
        return cls(requested if requested is not None else alias if alias is not None else 3)

    def to_dict(self) -> dict[str, Any]:
        return {"requested_candidate_count": self.requested_candidate_count}


@dataclass(frozen=True)
class EvaluationRecord:
    """一个计算模块对当前候选的评估事实。"""

    evaluation_id: str
    candidate_id: str
    module: str
    status: str
    module_run_ref: CoreModuleRunRef | None = None
    limitation: str | None = None
    idempotency_state: str = "new"
    round_no: int = 1
    source_mode: str = "live"

    def __post_init__(self) -> None:
        _identifier(self.evaluation_id, "evaluation_id")
        _identifier(self.candidate_id, "EvaluationRecord.candidate_id")
        if self.module not in {"payoffer", "pricer", "backtester"}:
            raise RecommendationValidationError("EvaluationRecord.module必须为计算模块")
        if self.status not in RUN_STATUSES:
            raise RecommendationValidationError("EvaluationRecord.status无效")
        reference = self.module_run_ref
        if reference is not None and not isinstance(reference, CoreModuleRunRef):
            raise RecommendationValidationError("EvaluationRecord.module_run_ref必须使用Core ModuleRunRef")
        if reference is not None and reference.module != self.module:
            raise RecommendationValidationError("EvaluationRecord.module_run_ref与module冲突")
        limitation = str(self.limitation).strip() if self.limitation is not None else None
        if bool(reference) == bool(limitation):
            raise RecommendationValidationError("EvaluationRecord必须且只能携带module_run_ref或limitation")
        if self.status in {"succeeded", "partial"} and reference is None:
            raise RecommendationValidationError("成功或部分成功的EvaluationRecord必须携带Core ModuleRunRef")
        if self.status not in {"succeeded", "partial"} and limitation is None:
            raise RecommendationValidationError("未产出运行引用的EvaluationRecord必须说明limitation")
        if self.idempotency_state not in {"new", "busy", "uncertain", "completed"}:
            raise RecommendationValidationError("EvaluationRecord.idempotency_state无效")
        if _positive_integer(self.round_no, "EvaluationRecord.round_no") > 10:
            raise RecommendationValidationError("EvaluationRecord.round_no必须位于1至10")
        if self.source_mode not in {"live", "reused", "recovered", "unavailable"}:
            raise RecommendationValidationError("EvaluationRecord.source_mode无效")
        object.__setattr__(self, "limitation", limitation)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvaluationRecord":
        data = dict(value)
        allowed = {
            "evaluation_id", "candidate_id", "module", "status", "module_run_ref",
            "limitation", "idempotency_state", "round_no", "source_mode",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"EvaluationRecord含未知字段：{','.join(unknown)}")
        raw_reference = data.get("module_run_ref")
        try:
            reference = CoreModuleRunRef(**dict(raw_reference)) if isinstance(raw_reference, Mapping) else raw_reference
        except (TypeError, ValueError) as error:
            raise RecommendationValidationError("EvaluationRecord.module_run_ref无效") from error
        return cls(
            evaluation_id=data.get("evaluation_id"), candidate_id=data.get("candidate_id"),
            module=data.get("module"), status=data.get("status"), module_run_ref=reference,
            limitation=data.get("limitation"), idempotency_state=data.get("idempotency_state", "new"),
            round_no=data.get("round_no", 1), source_mode=data.get("source_mode", "live"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class RankingSpec:
    """当前一次排序使用的显式规则，不构造内容摘要身份。"""

    ranking_spec_id: str
    hard_constraints: Mapping[str, Any]
    sort_keys: tuple[Mapping[str, Any], ...]
    tie_break_policy: str = "candidate_id"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranking_spec_id", _identifier(self.ranking_spec_id, "ranking_spec_id"))
        object.__setattr__(self, "hard_constraints", _mapping(self.hard_constraints, "RankingSpec.hard_constraints"))
        if isinstance(self.sort_keys, (str, bytes)) or not isinstance(self.sort_keys, Sequence) or not self.sort_keys:
            raise RecommendationValidationError("RankingSpec.sort_keys必须为非空数组")
        normalized: list[Mapping[str, str]] = []
        for index, raw in enumerate(self.sort_keys):
            item = _mapping(raw, f"RankingSpec.sort_keys[{index}]")
            if set(item) != {"metric", "direction", "missing_policy"}:
                raise RecommendationValidationError("RankingSpec.sort_keys字段必须为metric,direction,missing_policy")
            raw_direction = str(item.get("direction", "")).strip().lower()
            direction = {"ascending": "asc", "descending": "desc"}.get(raw_direction, raw_direction)
            missing = str(item.get("missing_policy", "")).strip().lower()
            if direction not in {"asc", "desc"} or missing not in {"exclude", "first", "last"}:
                raise RecommendationValidationError("RankingSpec排序方向或缺失值策略无效")
            normalized.append({
                "metric": _identifier(item.get("metric"), f"RankingSpec.sort_keys[{index}].metric"),
                "direction": direction,
                "missing_policy": missing,
            })
        if len({item["metric"] for item in normalized}) != len(normalized):
            raise RecommendationValidationError("RankingSpec.sort_keys.metric不得重复")
        if self.tie_break_policy != "candidate_id":
            raise RecommendationValidationError("RankingSpec.tie_break_policy必须为candidate_id")
        object.__setattr__(self, "sort_keys", tuple(normalized))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RankingSpec":
        data = dict(value)
        allowed = {"ranking_spec_id", "hard_constraints", "sort_keys", "tie_break_policy"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"RankingSpec含未知字段：{','.join(unknown)}")
        return cls(
            ranking_spec_id=data.get("ranking_spec_id"), hard_constraints=data.get("hard_constraints"),
            sort_keys=data.get("sort_keys"), tie_break_policy=data.get("tie_break_policy", "candidate_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class RankingDecision:
    ranking_spec_id: str
    candidate_id: str
    eligible: bool
    exclusion_reasons: tuple[str, ...]
    metric_sources: Mapping[str, str]
    final_rank: int | None

    def __post_init__(self) -> None:
        _identifier(self.ranking_spec_id, "ranking_spec_id")
        _identifier(self.candidate_id, "RankingDecision.candidate_id")
        if not isinstance(self.eligible, bool):
            raise RecommendationValidationError("RankingDecision.eligible必须为布尔值")
        reasons = _text_tuple(self.exclusion_reasons, "exclusion_reasons", allow_empty=True)
        sources = _mapping(self.metric_sources, "metric_sources")
        if self.eligible:
            if reasons or isinstance(self.final_rank, bool) or not isinstance(self.final_rank, int) or not 1 <= self.final_rank <= 10:
                raise RecommendationValidationError("eligible RankingDecision必须有1至10的名次且无排除原因")
        elif not reasons or self.final_rank is not None:
            raise RecommendationValidationError("非eligible RankingDecision必须有排除原因且无名次")
        object.__setattr__(self, "exclusion_reasons", reasons)
        object.__setattr__(self, "metric_sources", {str(key): str(item) for key, item in sorted(sources.items())})

    @property
    def rank(self) -> int | None:
        return self.final_rank

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RankingDecision":
        data = dict(value)
        allowed = {"ranking_spec_id", "candidate_id", "eligible", "exclusion_reasons", "metric_sources", "final_rank"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"RankingDecision含未知字段：{','.join(unknown)}")
        return cls(
            ranking_spec_id=data.get("ranking_spec_id"), candidate_id=data.get("candidate_id"),
            eligible=data.get("eligible"), exclusion_reasons=data.get("exclusion_reasons", ()),
            metric_sources=data.get("metric_sources", {}), final_rank=data.get("final_rank"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    structured_output: bool = True
    tool_calling: bool = True
    multi_agent: bool = False
    max_parallel_agents: int = 1
    independent_child_sessions: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelCapability":
        data = dict(value)
        return cls(
            model_id=_required_text(data.get("model_id", "unknown"), "model_id"),
            structured_output=bool(data.get("structured_output", False)),
            tool_calling=bool(data.get("tool_calling", False)),
            multi_agent=bool(data.get("multi_agent", False)),
            max_parallel_agents=max(1, int(data.get("max_parallel_agents", 1))),
            independent_child_sessions=bool(data.get("independent_child_sessions", False)),
        )

    @property
    def supports_multi_agent_workflow(self) -> bool:
        return self.structured_output and self.multi_agent and self.independent_child_sessions


@dataclass(frozen=True)
class RouteDecision:
    route: str
    confidence: float
    reason: str
    fixed_recommendation_workflow: bool
    agent_policy: str

    def __post_init__(self) -> None:
        if self.route not in ROUTES or not 0 <= self.confidence <= 1:
            raise RecommendationValidationError("路由或confidence无效")
        _required_text(self.reason, "reason")
        if self.agent_policy not in {"single", "capability_based"}:
            raise RecommendationValidationError("agent_policy无效")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationCase:
    """一次推荐请求。任务身份不携带产品、标的、合同或数据绑定。"""

    analysis_case_id: str
    task_id: str
    tenant_id: str
    prompt: str
    catalog_version: str
    run_id: str | None = None
    requested_outputs: tuple[str, ...] = ()
    audience: str = "internal"
    conversation_ref: str | None = None
    confirmed_constraints: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecommendationCase":
        data = dict(value)
        allowed = {
            "analysis_case_id", "task_id", "tenant_id", "prompt", "catalog_version", "run_id",
            "requested_outputs", "audience", "conversation_ref", "confirmed_constraints",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"RecommendationCase含任务级业务绑定：{','.join(unknown)}")
        return cls(
            analysis_case_id=_required_text(data.get("analysis_case_id"), "analysis_case_id"),
            task_id=_required_text(data.get("task_id"), "task_id"),
            tenant_id=_required_text(data.get("tenant_id"), "tenant_id"),
            prompt=_required_text(data.get("prompt"), "prompt"),
            catalog_version=_required_text(data.get("catalog_version"), "catalog_version"),
            run_id=str(data.get("run_id", "")).strip() or None,
            requested_outputs=_text_tuple(data.get("requested_outputs", ()), "requested_outputs", allow_empty=True),
            audience=_required_text(data.get("audience", "internal"), "audience"),
            conversation_ref=str(data.get("conversation_ref", "")).strip() or None,
            confirmed_constraints=_mapping(data.get("confirmed_constraints", {}), "confirmed_constraints"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    product_id: str
    catalog_version: str
    source: str
    section: str
    material_status: str
    excerpt_hash: str
    excerpt: str = field(repr=False, compare=False)
    identity: Mapping[str, Any] = field(default_factory=dict)
    entry_status: bool | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, expected_catalog_version: str) -> "EvidenceRef":
        import hashlib

        data = dict(value)
        catalog = _required_text(data.get("catalog_version"), "evidence.catalog_version")
        if catalog != expected_catalog_version:
            raise RecommendationValidationError("证据目录版本与本次查询不一致")
        status = _required_text(data.get("library_status", data.get("material_status", "ready")), "evidence.library_status")
        if status not in LIBRARY_STATUSES:
            raise RecommendationValidationError("证据资料状态无效")
        excerpt = _required_text(data.get("excerpt"), "evidence.excerpt")
        excerpt_hash = _required_text(data.get("excerpt_hash"), "evidence.excerpt_hash")
        if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != excerpt_hash:
            raise RecommendationValidationError("证据摘要与原文不一致")
        product_id = _required_text(data.get("product_id"), "evidence.product_id")
        source = _required_text(data.get("source"), "evidence.source").lower()
        identity = _mapping(data.get("identity", {}), "evidence.identity")
        if any(str(key).lower().endswith("_hash") for key in identity):
            raise RecommendationValidationError("evidence.identity不得携带业务内容摘要")
        entry_status = data.get("entry_status", identity.get("entry_status"))
        if source == "optionlist" and identity.get("product_id") != product_id:
            raise RecommendationValidationError("OptionList证据必须携带匹配产品身份")
        if source == "optionreg_status" and not isinstance(entry_status, bool):
            raise RecommendationValidationError("OptionReg证据必须携带布尔entry_status")
        return cls(
            evidence_id=_required_text(data.get("evidence_id"), "evidence_id"), product_id=product_id,
            catalog_version=catalog, source=source, section=_required_text(data.get("section"), "section"),
            material_status=status, excerpt_hash=excerpt_hash, excerpt=excerpt, identity=identity,
            entry_status=entry_status if isinstance(entry_status, bool) else None,
        )

    def to_dict(self, *, include_excerpt: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_excerpt:
            result.pop("excerpt", None)
        return result


def module_execution_from_tool_result(
    module: str,
    value: Mapping[str, Any],
    *,
    tenant_id: str,
    task_id: str,
    candidate_id: str,
    catalog_version: str,
) -> tuple[str, CoreModuleRunRef | None, str | None]:
    data = dict(value)
    raw_status = str(data.get("status", "")).strip().lower()
    aliases = {"completed": "succeeded", "complete": "succeeded", "unavailable": "unsupported", "timeout": "timed_out"}
    status = aliases.get(raw_status, raw_status) or ("succeeded" if data.get("ok") is True else "failed")
    if status not in RUN_STATUSES:
        raise RecommendationValidationError(f"{module}返回未知运行状态：{status}")
    for name, expected in {"candidate_id": candidate_id, "catalog_version": catalog_version}.items():
        if data.get(name) is not None and data.get(name) != expected:
            raise RecommendationValidationError(f"{module}返回结果未绑定当前候选：{name}")
    limitation = str(data.get("message") or data.get("reason") or "").strip() or None
    if status not in {"succeeded", "partial"}:
        return status, None, limitation
    raw_ref = data.get("module_run_ref")
    if not isinstance(raw_ref, Mapping):
        raise RecommendationValidationError(f"{module}成功结果缺少Core正式ModuleRunRef")
    try:
        reference = CoreModuleRunRef(**dict(raw_ref))
    except (TypeError, ValueError) as error:
        raise RecommendationValidationError(f"{module}返回的Core ModuleRunRef无效") from error
    if reference.module != module or reference.tenant_id != tenant_id or reference.task_id != task_id:
        raise RecommendationValidationError(f"{module}返回的Core ModuleRunRef不属于当前任务")
    return status, reference, limitation


@dataclass(frozen=True)
class RecommendationCandidate:
    """一个产品在其当前规则下生成的当前候选快照。"""

    candidate_id: str
    product_id: str
    rule_revision: int
    underlyings: tuple[str, ...]
    rank: int
    reason: str
    suitable_for: tuple[str, ...]
    not_suitable_for: tuple[str, ...]
    main_risks: tuple[str, ...]
    library_status: str
    product_name: str | None = None
    current_inputs: Mapping[str, Any] = field(default_factory=dict)
    key_terms: tuple[Mapping[str, Any], ...] = ()
    candidate_status: str = "candidate"
    evidence_refs: tuple[EvidenceRef, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    module_run_refs: tuple[CoreModuleRunRef, ...] = ()
    module_statuses: Mapping[str, str] = field(default_factory=dict)
    evaluation_records: tuple[EvaluationRecord, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        _required_text(self.product_id, "product_id")
        _positive_integer(self.rule_revision, "rule_revision")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise RecommendationValidationError("rank必须为正整数")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        evidence_index: Mapping[str, EvidenceRef],
    ) -> "RecommendationCandidate":
        data = dict(value)
        allowed = {
            "candidate_id", "product_id", "rule_revision", "underlyings", "rank", "reason",
            "suitable_for", "not_suitable_for", "main_risks", "library_status", "product_name",
            "current_inputs", "key_terms", "candidate_status", "evidence_ref_ids", "missing_inputs",
            "module_run_refs", "module_statuses", "evaluation_records",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"候选含未知字段：{','.join(unknown)}")
        product_id = _required_text(data.get("product_id"), "product_id")
        ref_ids = _text_tuple(data.get("evidence_ref_ids", ()), "evidence_ref_ids", allow_empty=True)
        refs: list[EvidenceRef] = []
        for ref_id in ref_ids:
            ref = evidence_index.get(ref_id)
            if ref is None or ref.product_id != product_id:
                raise RecommendationValidationError(f"候选证据不属于产品：{ref_id}")
            refs.append(ref)
        library_status = _required_text(data.get("library_status", "unavailable"), "library_status")
        if library_status not in LIBRARY_STATUSES or (library_status == "ready" and not refs):
            raise RecommendationValidationError("候选资料状态或证据不完整")
        candidate_status = _required_text(data.get("candidate_status", "candidate"), "candidate_status")
        if candidate_status not in CANDIDATE_STATUSES:
            raise RecommendationValidationError("candidate_status无效")
        raw_runs = data.get("module_run_refs", ())
        if isinstance(raw_runs, (str, bytes)) or not isinstance(raw_runs, Sequence):
            raise RecommendationValidationError("module_run_refs必须为数组")
        try:
            runs = tuple(CoreModuleRunRef(**dict(item)) if isinstance(item, Mapping) else item for item in raw_runs)
        except (TypeError, ValueError) as error:
            raise RecommendationValidationError("module_run_refs包含无效引用") from error
        if any(not isinstance(item, CoreModuleRunRef) for item in runs):
            raise RecommendationValidationError("module_run_refs必须使用Core正式引用")
        raw_records = data.get("evaluation_records", ())
        records = tuple(EvaluationRecord.from_mapping(item) if isinstance(item, Mapping) else item for item in raw_records)
        candidate_id = _identifier(data.get("candidate_id"), "candidate_id")
        if any(not isinstance(item, EvaluationRecord) or item.candidate_id != candidate_id for item in records):
            raise RecommendationValidationError("evaluation_records必须绑定当前candidate_id")
        statuses = _mapping(data.get("module_statuses", {}), "module_statuses")
        if any(str(module) not in {"payoffer", "pricer", "backtester"} or str(status) not in RUN_STATUSES for module, status in statuses.items()):
            raise RecommendationValidationError("module_statuses含无效模块或状态")
        return cls(
            candidate_id=candidate_id, product_id=product_id,
            rule_revision=_positive_integer(data.get("rule_revision"), "rule_revision"),
            underlyings=_text_tuple(data.get("underlyings", ()), "underlyings"),
            rank=data.get("rank"), reason=_required_text(data.get("reason"), "reason"),
            suitable_for=_text_tuple(data.get("suitable_for", ()), "suitable_for"),
            not_suitable_for=_text_tuple(data.get("not_suitable_for", ()), "not_suitable_for"),
            main_risks=_text_tuple(data.get("main_risks", ()), "main_risks"),
            library_status=library_status, product_name=str(data.get("product_name", "")).strip() or None,
            current_inputs=_json_value(_mapping(data.get("current_inputs", {}), "current_inputs")),
            key_terms=_display_terms(data.get("key_terms", ())), candidate_status=candidate_status,
            evidence_refs=tuple(refs),
            missing_inputs=_text_tuple(data.get("missing_inputs", ()), "missing_inputs", allow_empty=True),
            module_run_refs=runs, module_statuses={str(key): str(item) for key, item in statuses.items()},
            evaluation_records=records,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for ref in result.get("evidence_refs", []):
            ref.pop("excerpt", None)
        return _json_value(result)

    def to_confirmation_dict(self) -> dict[str, Any]:
        """返回Host确认后按最新产品规则编译所需的当前输入。"""

        return {
            "candidate_id": self.candidate_id,
            "product_id": self.product_id,
            "rule_revision": self.rule_revision,
            "underlyings": list(self.underlyings),
            "current_inputs": _json_value(self.current_inputs),
        }


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    stage: str
    status: str
    agent_role: str | None
    mode: str
    input_hash: str
    output_hash: str | None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class RecommendationSet:
    schema: str
    task_id: str
    run_id: str
    analysis_case_id: str
    catalog_version: str
    route: str
    workflow_mode: str
    status: str
    primary_candidate_id: str | None
    candidates: tuple[RecommendationCandidate, ...]
    analysis_status: str = "not_started"
    delivery_status: str = "not_requested"
    requested_outputs: tuple[str, ...] = ()
    rejected: tuple[Mapping[str, Any], ...] = ()
    missing_information: tuple[str, ...] = ()
    next_question: str | None = None
    limitations: tuple[str, ...] = ()
    audit_trail: tuple[AuditEvent, ...] = ()
    requested_candidate_count: int | None = None
    returned_candidate_count: int | None = None
    ranking_spec_id: str | None = None
    ranking_spec: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema != RECOMMENDATION_SET_SCHEMA or self.route not in {"recommendation", "professional_report"}:
            raise RecommendationValidationError("RecommendationSet类型或路由无效")
        if self.workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
            raise RecommendationValidationError("workflow_mode无效")
        if self.status not in SET_STATUSES or self.analysis_status not in ANALYSIS_STATUSES or self.delivery_status not in DELIVERY_STATUSES:
            raise RecommendationValidationError("RecommendationSet状态无效")
        if len(self.candidates) > 10:
            raise RecommendationValidationError("最多允许10个候选")
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RecommendationValidationError("candidate_id必须唯一")
        if [item.rank for item in self.candidates] != list(range(1, len(self.candidates) + 1)):
            raise RecommendationValidationError("候选必须按1开始连续排名")
        if self.candidates and self.primary_candidate_id != self.candidates[0].candidate_id:
            raise RecommendationValidationError("primary_candidate_id必须指向排名第一候选")
        if not self.candidates and self.primary_candidate_id is not None:
            raise RecommendationValidationError("无候选时primary_candidate_id必须为空")
        requested = len(self.candidates) if self.requested_candidate_count is None else self.requested_candidate_count
        returned = len(self.candidates) if self.returned_candidate_count is None else self.returned_candidate_count
        if isinstance(requested, bool) or not isinstance(requested, int) or not 0 <= requested <= 10:
            raise RecommendationValidationError("requested_candidate_count必须位于0至10")
        if returned != len(self.candidates) or requested < returned:
            raise RecommendationValidationError("候选数量声明不一致")
        object.__setattr__(self, "requested_candidate_count", requested)
        object.__setattr__(self, "returned_candidate_count", returned)
        ranking_id = _optional_identifier(self.ranking_spec_id, "ranking_spec_id")
        if (ranking_id is None) != (self.ranking_spec is None):
            raise RecommendationValidationError("ranking_spec_id与ranking_spec必须同时提供")
        if self.ranking_spec is not None:
            spec = RankingSpec.from_mapping(self.ranking_spec)
            if spec.ranking_spec_id != ranking_id:
                raise RecommendationValidationError("RankingSpec身份不一致")
            object.__setattr__(self, "ranking_spec", spec.to_dict())
        object.__setattr__(self, "ranking_spec_id", ranking_id)
        if self.status == "pending_question" and not self.next_question:
            raise RecommendationValidationError("pending_question必须提供next_question")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": self.schema, "task_id": self.task_id, "run_id": self.run_id,
            "analysis_case_id": self.analysis_case_id, "catalog_version": self.catalog_version,
            "route": self.route, "workflow_mode": self.workflow_mode, "status": self.status,
            "primary_candidate_id": self.primary_candidate_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "analysis_status": self.analysis_status, "delivery_status": self.delivery_status,
            "requested_outputs": list(self.requested_outputs), "rejected": _json_value(self.rejected),
            "missing_information": list(self.missing_information), "next_question": self.next_question,
            "limitations": list(self.limitations), "audit_trail": [item.to_dict() for item in self.audit_trail],
            "requested_candidate_count": self.requested_candidate_count,
            "returned_candidate_count": self.returned_candidate_count,
        }
        if self.ranking_spec_id is not None:
            result.update({"ranking_spec_id": self.ranking_spec_id, "ranking_spec": _json_value(self.ranking_spec)})
        return result

    def to_public_dict(self) -> dict[str, Any]:
        material_status = {"ready": "资料完整", "partial": "资料不完整", "conflict": "资料存在冲突", "unavailable": "资料暂不可用"}
        rows = []
        for candidate in self.candidates:
            rows.append({
                "产品编号": candidate.product_id,
                "产品名称": public_text(candidate.product_name or "") or candidate.product_id,
                "排名": candidate.rank,
                "推荐理由": public_text(candidate.reason) or "该结构与已确认条件相匹配。",
                "适用条件": public_text_list(candidate.suitable_for),
                "不适用条件": public_text_list(candidate.not_suitable_for),
                "主要风险": public_text_list(candidate.main_risks),
                "资料状态": material_status[candidate.library_status],
                "拟采用条款": [dict(item) for item in candidate.key_terms],
            })
        message = self.next_question or (
            "当前条件暂不足以形成可靠推荐。" if self.status == "unavailable" else
            "已形成可用的推荐结论，但部分验证尚未完成。" if self.status == "partial" else
            "结构筛选完成，请查看适用条件、主要风险和拟采用条款。" if rows else
            "当前尚未形成可供展示的推荐结论。"
        )
        deliveries = {"card": "研究简报", "quote": "参考报价", "report": "完整研究报告"}
        return {"说明": message, "推荐结果": rows, "交付": [deliveries[item] for item in self.requested_outputs if item in deliveries]}


def public_text(value: str) -> str:
    text = str(value).strip()
    internal_hash = re.search(r"(?i)(?:\b[0-9a-f]{16,}\b|\b(?:ck|cv)_[a-z0-9]{8,}\b)", text)
    return "" if _INTERNAL_PUBLIC_TEXT.search(text) or internal_hash else text


def public_text_list(values: Sequence[str]) -> list[str]:
    return [text for item in values if (text := public_text(item))]
