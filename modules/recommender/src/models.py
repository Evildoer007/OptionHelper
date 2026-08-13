"""Recommender领域对象与严格校验。

本模块只保存推荐流程事实，不保存产品条款、市场数据或计算结果。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Mapping, Sequence

from runtime.protocol.models import ModuleRunRef as CoreModuleRunRef

ROUTES = {
    "chat",
    "knowledge",
    "data",
    "direct_execution",
    "recommendation",
    "professional_report",
    "existing_report",
    "maintenance",
    "freeform",
}
LIBRARY_STATUSES = {"ready", "partial", "conflict", "unavailable"}
CANDIDATE_STATUSES = {
    "candidate",
    "pending_confirmation",
    "pending_data",
    "pending_terms",
    "approved",
    "running",
    "rejected",
    "unsupported",
}
SET_STATUSES = {
    "pending_question",
    "candidate_ready",
    "pending_approval",
    "running",
    "completed",
    "partial",
    "unavailable",
}
RUN_STATUSES = {
    "pending", "running", "succeeded", "partial", "failed", "unsupported", "cancelled", "timed_out",
}
ANALYSIS_STATUSES = {"not_started", "running", "completed", "partial", "failed", "unsupported", "cancelled", "timed_out"}
DELIVERY_STATUSES = {"not_requested", "pending", "completed", "partial", "unavailable"}
RECOMMENDATION_SET_SCHEMA = "optionhelper.recommendation-set/v1.0.0"
_INTERNAL_PUBLIC_TEXT = re.compile(
    r"(?i)(?:candidate|evidence|module_run|run|task|analysis|audit|contract|resolved_contract)[a-z0-9_]*"
)


class RecommendationValidationError(ValueError):
    """结构化推荐对象不满足机器契约。"""


def _required_text(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise RecommendationValidationError(f"{field_name}不能为空")
    return text


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


def _display_terms(value: Any) -> tuple[Mapping[str, str], ...]:
    """接受Host基于Core已解析合同给出的客户条款摘要。

    Recommender不解释金融字段或自行翻译条款；它只保留这份已经面向客户
    归类的摘要，以便在批准前展示并留存确认状态。
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RecommendationValidationError("CandidateContract.display_terms必须为数组")
    result: list[Mapping[str, str]] = []
    for index, item in enumerate(value):
        term = _mapping(item, f"CandidateContract.display_terms[{index}]")
        if set(term) != {"label", "value", "source"}:
            raise RecommendationValidationError("CandidateContract.display_terms字段必须为label,value,source")
        source = _required_text(term.get("source"), f"CandidateContract.display_terms[{index}].source")
        if source not in {"用户输入", "拟采用参数"}:
            raise RecommendationValidationError("CandidateContract.display_terms.source必须为用户输入或拟采用参数")
        result.append({
            "label": _required_text(term.get("label"), f"CandidateContract.display_terms[{index}].label"),
            "value": _required_text(term.get("value"), f"CandidateContract.display_terms[{index}].value"),
            "source": source,
        })
    return tuple(result)


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    structured_output: bool = True
    tool_calling: bool = True
    multi_agent: bool = False
    max_parallel_agents: int = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelCapability":
        data = dict(value)
        return cls(
            model_id=_required_text(data.get("model_id", "unknown"), "model_id"),
            structured_output=bool(data.get("structured_output", False)),
            tool_calling=bool(data.get("tool_calling", False)),
            multi_agent=bool(data.get("multi_agent", False)),
            max_parallel_agents=max(1, int(data.get("max_parallel_agents", 1))),
        )

    @property
    def supports_multi_agent_workflow(self) -> bool:
        return self.structured_output and self.tool_calling and self.multi_agent and self.max_parallel_agents >= 3


@dataclass(frozen=True)
class RouteDecision:
    route: str
    confidence: float
    reason: str
    fixed_recommendation_workflow: bool
    agent_policy: str

    def __post_init__(self) -> None:
        if self.route not in ROUTES:
            raise RecommendationValidationError(f"未知路由：{self.route}")
        if not 0 <= self.confidence <= 1:
            raise RecommendationValidationError("confidence必须位于[0,1]")
        _required_text(self.reason, "reason")
        if self.agent_policy not in {"single", "capability_based"}:
            raise RecommendationValidationError("agent_policy无效")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationCase:
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
    approved_candidate_ids: tuple[str, ...] = ()
    candidate_contracts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecommendationCase":
        data = dict(value)
        outputs = data.get("requested_outputs", ())
        approved = data.get("approved_candidate_ids", ())
        approved_ids = _text_tuple(approved, "approved_candidate_ids", allow_empty=True)
        if len(set(approved_ids)) != len(approved_ids):
            raise RecommendationValidationError("approved_candidate_ids不得重复")
        return cls(
            analysis_case_id=_required_text(data.get("analysis_case_id"), "analysis_case_id"),
            task_id=_required_text(data.get("task_id"), "task_id"),
            tenant_id=_required_text(data.get("tenant_id"), "tenant_id"),
            prompt=_required_text(data.get("prompt"), "prompt"),
            catalog_version=_required_text(data.get("catalog_version"), "catalog_version"),
            run_id=str(data.get("run_id", "")).strip() or None,
            requested_outputs=_text_tuple(outputs, "requested_outputs", allow_empty=True),
            audience=_required_text(data.get("audience", "internal"), "audience"),
            conversation_ref=str(data["conversation_ref"]).strip() if data.get("conversation_ref") else None,
            confirmed_constraints=_mapping(data.get("confirmed_constraints", {}), "confirmed_constraints"),
            approved_candidate_ids=approved_ids,
            candidate_contracts={
                _required_text(key, "candidate_contracts.key"): _mapping(item, f"candidate_contracts.{key}")
                for key, item in _mapping(data.get("candidate_contracts", {}), "candidate_contracts").items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        data = dict(value)
        catalog_version = _required_text(data.get("catalog_version"), "evidence.catalog_version")
        if catalog_version != expected_catalog_version:
            raise RecommendationValidationError(
                f"证据catalog_version={catalog_version}与任务版本{expected_catalog_version}不一致"
            )
        status = _required_text(data.get("library_status", data.get("material_status", "ready")), "evidence.library_status")
        if status not in LIBRARY_STATUSES:
            raise RecommendationValidationError(f"资料状态无效：{status}")
        excerpt = _required_text(data.get("excerpt"), "evidence.excerpt")
        excerpt_hash = _required_text(data.get("excerpt_hash"), "evidence.excerpt_hash")
        import hashlib
        actual_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        if excerpt_hash != actual_hash:
            raise RecommendationValidationError("evidence.excerpt_hash与原文不一致")
        product_id = _required_text(data.get("product_id"), "evidence.product_id")
        source = _required_text(data.get("source"), "evidence.source").lower()
        identity = _mapping(data.get("identity", {}), "evidence.identity")
        if identity and _required_text(identity.get("product_id"), "evidence.identity.product_id") != product_id:
            raise RecommendationValidationError("evidence.identity.product_id与evidence.product_id不一致")
        entry_status = data.get("entry_status", identity.get("entry_status"))
        if source == "optionlist" and not identity:
            raise RecommendationValidationError("OptionList证据必须携带产品身份")
        if source == "optionreg_status" and not isinstance(entry_status, bool):
            raise RecommendationValidationError("OptionReg证据必须携带布尔entry_status")
        return cls(
            evidence_id=_required_text(data.get("evidence_id"), "evidence_id"),
            product_id=product_id,
            catalog_version=catalog_version,
            source=source,
            section=_required_text(data.get("section"), "evidence.section"),
            material_status=status,
            excerpt_hash=excerpt_hash,
            excerpt=excerpt,
            identity=identity,
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
    contract_fingerprint: str,
) -> tuple[str, CoreModuleRunRef | None, str | None]:
    """将Tool响应收敛为正式Core引用与独立生命周期状态。

    推荐模块不保存另一套弱化RunRef。成功或部分成功必须携带Core
    ``ModuleRunRef``；失败终态则只保留状态及面向流程的受控说明。
    """

    data = dict(value)
    raw_status = str(data.get("status", "")).strip().lower()
    aliases = {"completed": "succeeded", "complete": "succeeded", "unavailable": "unsupported", "timeout": "timed_out"}
    raw_status = raw_status or ("succeeded" if data.get("ok") is True else "failed")
    status = aliases.get(raw_status, raw_status)
    if status not in RUN_STATUSES:
        raise RecommendationValidationError(f"{module}返回未知运行状态：{status}")
    expected_binding = {
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "contract_fingerprint": contract_fingerprint,
    }
    supplied_binding = {name: data.get(name) for name in expected_binding}
    mismatched = [name for name, expected in expected_binding.items() if supplied_binding[name] != expected]
    if status in {"succeeded", "partial"} and mismatched:
        raise RecommendationValidationError(f"{module}返回未绑定当前CandidateContract的ModuleRun：{','.join(mismatched)}")
    if status not in {"succeeded", "partial"} and any(value is not None for value in supplied_binding.values()) and mismatched:
        raise RecommendationValidationError(f"{module}失败终态的CandidateContract绑定冲突：{','.join(mismatched)}")
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
    candidate_id: str
    product_id: str
    underlyings: tuple[str, ...]
    rank: int
    reason: str
    suitable_for: tuple[str, ...]
    not_suitable_for: tuple[str, ...]
    main_risks: tuple[str, ...]
    library_status: str
    product_name: str | None = None
    key_terms: tuple[Mapping[str, Any], ...] = ()
    candidate_status: str = "candidate"
    constraints_fingerprint: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    module_run_refs: tuple[CoreModuleRunRef, ...] = ()
    module_statuses: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        evidence_index: Mapping[str, EvidenceRef],
    ) -> "RecommendationCandidate":
        data = dict(value)
        allowed = {
            "candidate_id", "product_id", "underlyings", "rank", "reason",
            "suitable_for", "not_suitable_for", "main_risks", "library_status",
            "product_name", "key_terms", "candidate_status", "evidence_ref_ids",
            "missing_inputs", "module_run_refs", "module_statuses", "constraints_fingerprint",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise RecommendationValidationError(f"候选含未知字段：{','.join(unknown)}")
        product_id = _required_text(data.get("product_id"), "product_id")
        ref_ids = _text_tuple(data.get("evidence_ref_ids", ()), "evidence_ref_ids", allow_empty=True)
        refs: list[EvidenceRef] = []
        for ref_id in ref_ids:
            ref = evidence_index.get(ref_id)
            if ref is None:
                raise RecommendationValidationError(f"候选引用不存在的证据：{ref_id}")
            if ref.product_id != product_id:
                raise RecommendationValidationError(f"证据{ref_id}不属于产品{product_id}")
            refs.append(ref)
        library_status = _required_text(data.get("library_status", "unavailable"), "library_status")
        if library_status not in LIBRARY_STATUSES:
            raise RecommendationValidationError(f"资料状态无效：{library_status}")
        if library_status == "ready" and not refs:
            raise RecommendationValidationError("ready候选必须引用受控证据")
        candidate_status = _required_text(data.get("candidate_status", "candidate"), "candidate_status")
        if candidate_status not in CANDIDATE_STATUSES:
            raise RecommendationValidationError(f"候选状态无效：{candidate_status}")
        fingerprint = _required_text(data.get("constraints_fingerprint"), "constraints_fingerprint")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecommendationValidationError("constraints_fingerprint必须为64位小写十六进制")
        raw_runs = data.get("module_run_refs", ())
        if isinstance(raw_runs, (str, bytes)) or not isinstance(raw_runs, Sequence):
            raise RecommendationValidationError("module_run_refs必须为Core ModuleRunRef数组")
        try:
            runs = tuple(CoreModuleRunRef(**dict(item)) if isinstance(item, Mapping) else item for item in raw_runs)
        except (TypeError, ValueError) as error:
            raise RecommendationValidationError("module_run_refs包含无效Core ModuleRunRef") from error
        if any(not isinstance(item, CoreModuleRunRef) for item in runs):
            raise RecommendationValidationError("module_run_refs必须使用Core ModuleRunRef")
        statuses = _mapping(data.get("module_statuses", {}), "module_statuses")
        if any(str(module) not in {"payoffer", "pricer", "backtester"} or str(status) not in RUN_STATUSES for module, status in statuses.items()):
            raise RecommendationValidationError("module_statuses包含无效模块或状态")
        return cls(
            candidate_id=_required_text(data.get("candidate_id"), "candidate_id"),
            product_id=product_id,
            underlyings=_text_tuple(data.get("underlyings", ()), "underlyings"),
            rank=int(data.get("rank", 0)),
            reason=_required_text(data.get("reason"), "reason"),
            suitable_for=_text_tuple(data.get("suitable_for", ()), "suitable_for"),
            not_suitable_for=_text_tuple(data.get("not_suitable_for", ()), "not_suitable_for"),
            main_risks=_text_tuple(data.get("main_risks", ()), "main_risks"),
            library_status=library_status,
            product_name=str(data.get("product_name", "")).strip() or None,
            key_terms=tuple(_mapping(item, "key_terms[]") for item in data.get("key_terms", ())),
            candidate_status=candidate_status,
            constraints_fingerprint=fingerprint,
            evidence_refs=tuple(refs),
            missing_inputs=_text_tuple(data.get("missing_inputs", ()), "missing_inputs", allow_empty=True),
            module_run_refs=runs,
            module_statuses={str(module): str(status) for module, status in statuses.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for ref in result.get("evidence_refs", []):
            ref.pop("excerpt", None)
        return _json_value(result)


@dataclass(frozen=True)
class CandidateContract:
    """已批准候选与Core已解析合同的绑定；不在此解析金融条款。"""

    candidate_id: str
    product_id: str
    underlyings: tuple[str, ...]
    catalog_version: str
    evidence_ref_ids: tuple[str, ...]
    contract_fingerprint: str
    constraints_fingerprint: str
    resolved_contract: Mapping[str, Any] = field(default_factory=dict)
    module_inputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    display_terms: tuple[Mapping[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, candidate: RecommendationCandidate) -> "CandidateContract":
        data = dict(value)
        candidate_id = _required_text(data.get("candidate_id"), "CandidateContract.candidate_id")
        if candidate_id != candidate.candidate_id:
            raise RecommendationValidationError(f"CandidateContract与候选{candidate.candidate_id}的candidate_id冲突")
        product_id = _required_text(data.get("product_id"), "CandidateContract.product_id")
        if product_id != candidate.product_id:
            raise RecommendationValidationError(f"CandidateContract与候选{candidate_id}的product_id冲突")
        underlyings = _text_tuple(data.get("underlyings", ()), "CandidateContract.underlyings")
        if underlyings != candidate.underlyings:
            raise RecommendationValidationError(f"CandidateContract与候选{candidate_id}的标的顺序冲突")
        evidence_versions = {item.catalog_version for item in candidate.evidence_refs}
        if len(evidence_versions) != 1:
            raise RecommendationValidationError(f"候选{candidate_id}证据CatalogVersion不唯一")
        catalog_version = _required_text(data.get("catalog_version"), "CandidateContract.catalog_version")
        if catalog_version != next(iter(evidence_versions)):
            raise RecommendationValidationError(f"CandidateContract与候选{candidate_id}的CatalogVersion冲突")
        evidence_ref_ids = _text_tuple(data.get("evidence_ref_ids", ()), "CandidateContract.evidence_ref_ids")
        expected_refs = tuple(item.evidence_id for item in candidate.evidence_refs)
        if len(set(evidence_ref_ids)) != len(evidence_ref_ids) or set(evidence_ref_ids) != set(expected_refs):
            raise RecommendationValidationError(f"CandidateContract与候选{candidate_id}的evidence_ref_ids冲突")
        constraints = _required_text(data.get("constraints_fingerprint"), "CandidateContract.constraints_fingerprint")
        if constraints != candidate.constraints_fingerprint:
            raise RecommendationValidationError(f"CandidateContract与候选{candidate_id}的constraints_fingerprint冲突")
        resolved_contract = _mapping(data.get("resolved_contract"), "CandidateContract.resolved_contract")
        fingerprint = _required_text(
            data.get("contract_fingerprint") or resolved_contract.get("contract_fingerprint"),
            "CandidateContract.contract_fingerprint",
        )
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecommendationValidationError("CandidateContract.contract_fingerprint必须为64位小写十六进制")
        try:
            from runtime.contracts.contract_engine import ResolvedContract

            core_contract = ResolvedContract(**dict(resolved_contract))
        except (TypeError, ValueError) as error:
            raise RecommendationValidationError("CandidateContract必须携带Core验证通过的ResolvedContract") from error
        if core_contract.contract_fingerprint != fingerprint:
            raise RecommendationValidationError("ResolvedContract.contract_fingerprint与CandidateContract不一致")
        if core_contract.product_id != product_id or tuple(core_contract.underlyings) != underlyings:
            raise RecommendationValidationError("ResolvedContract身份与CandidateContract不一致")
        module_inputs = _mapping(data.get("module_inputs", {}), "CandidateContract.module_inputs")
        if any(not isinstance(item, Mapping) for item in module_inputs.values()):
            raise RecommendationValidationError("CandidateContract.module_inputs必须按模块映射对象")
        display_terms = _display_terms(data.get("display_terms", ()))
        return cls(
            candidate_id=candidate_id, product_id=product_id, underlyings=underlyings,
            catalog_version=catalog_version, evidence_ref_ids=evidence_ref_ids,
            contract_fingerprint=fingerprint, constraints_fingerprint=constraints, resolved_contract=resolved_contract,
            module_inputs={str(key): dict(item) for key, item in module_inputs.items()},
            display_terms=display_terms,
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


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

    def __post_init__(self) -> None:
        if self.schema != RECOMMENDATION_SET_SCHEMA:
            raise RecommendationValidationError("RecommendationSet.schema无效")
        if self.route not in {"recommendation", "professional_report"}:
            raise RecommendationValidationError("RecommendationSet只用于结构推荐路由")
        if self.workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
            raise RecommendationValidationError("workflow_mode无效")
        if self.status not in SET_STATUSES:
            raise RecommendationValidationError(f"RecommendationSet状态无效：{self.status}")
        if self.analysis_status not in ANALYSIS_STATUSES:
            raise RecommendationValidationError("analysis_status无效")
        if self.delivery_status not in DELIVERY_STATUSES:
            raise RecommendationValidationError("delivery_status无效")
        if len(self.candidates) > 3:
            raise RecommendationValidationError("最多允许一个主候选和两个备选")
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RecommendationValidationError("candidate_id必须唯一")
        ranks = [item.rank for item in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise RecommendationValidationError("候选必须按1开始连续排名")
        if self.candidates:
            if self.primary_candidate_id != self.candidates[0].candidate_id:
                raise RecommendationValidationError("primary_candidate_id必须指向排名第一的候选")
        elif self.primary_candidate_id is not None:
            raise RecommendationValidationError("无候选时primary_candidate_id必须为空")
        if self.status == "pending_question" and not self.next_question:
            raise RecommendationValidationError("pending_question必须提供next_question")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "analysis_case_id": self.analysis_case_id,
            "catalog_version": self.catalog_version,
            "route": self.route,
            "workflow_mode": self.workflow_mode,
            "status": self.status,
            "primary_candidate_id": self.primary_candidate_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "analysis_status": self.analysis_status,
            "delivery_status": self.delivery_status,
            "requested_outputs": list(self.requested_outputs),
            "rejected": _json_value(self.rejected),
            "missing_information": list(self.missing_information),
            "next_question": self.next_question,
            "limitations": list(self.limitations),
            "audit_trail": [item.to_dict() for item in self.audit_trail],
        }

    def to_public_dict(self) -> dict[str, Any]:
        """唯一面向客户的投影，不泄露内部合同、运行或审计对象。"""

        material_status = {
            "ready": "资料完整",
            "partial": "资料不完整",
            "conflict": "资料存在冲突",
            "unavailable": "资料暂不可用",
        }
        deliveries = {
            "card": "研究简报",
            "report": "完整研究报告",
        }
        recommendations = []
        for candidate in self.candidates:
            terms = [
                {
                    "label": public_text(str(item.get("label", ""))),
                    "value": public_text(str(item.get("value", ""))),
                    "source": str(item.get("source")) if str(item.get("source")) in {"用户输入", "拟采用参数"} else "拟采用参数",
                }
                for item in candidate.key_terms
                if public_text(str(item.get("label", ""))) and public_text(str(item.get("value", "")))
            ]
            recommendations.append({
                "产品编号": candidate.product_id,
                "产品名称": public_text(candidate.product_name or "") or candidate.product_id,
                "排名": candidate.rank,
                "推荐理由": public_text(candidate.reason) or "该结构与已确认条件相匹配。",
                "适用条件": public_text_list(candidate.suitable_for),
                "不适用条件": public_text_list(candidate.not_suitable_for),
                "主要风险": public_text_list(candidate.main_risks),
                "资料状态": material_status[candidate.library_status],
                "拟采用条款": terms,
            })
        if self.next_question:
            message = self.next_question
        elif self.status == "unavailable":
            message = "当前条件暂不足以形成可靠推荐。请检查市场观点、期限或风险约束后重试。"
        elif self.status == "partial":
            message = "已形成可用的推荐结论，但部分验证尚未完成。"
        elif recommendations:
            message = "结构筛选完成，请查看适用条件、主要风险和拟采用条款。"
        else:
            message = "当前尚未形成可供展示的推荐结论。"
        return {
            "说明": message,
            "推荐结果": recommendations,
            "交付": [deliveries[item] for item in self.requested_outputs if item in deliveries],
        }


def public_text(value: str) -> str:
    text = str(value).strip()
    return "" if _INTERNAL_PUBLIC_TEXT.search(text) else text


def public_text_list(values: Sequence[str]) -> list[str]:
    return [text for item in values if (text := public_text(str(item)))]
