"""将Selector与Reviewer结果聚合为当前RecommendationCandidate快照。"""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, Mapping, Sequence

from .candidate_critic import critique_candidates
from .models import EvidenceRef, RecommendationCandidate, RecommendationValidationError


_STATUS_SCORE = {"ready": 0, "partial": 20, "conflict": 40, "unavailable": 60}


def _positive_rule_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecommendationValidationError("rule_revision必须为正整数")
    return value


def _normalized_current_inputs(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecommendationValidationError("current_inputs必须为对象")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key:
            raise RecommendationValidationError("current_inputs字段名不能为空")
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise RecommendationValidationError(f"current_inputs.{key}必须为有限数值")
        if not isinstance(raw_value, (str, int, float, bool, list, tuple, Mapping, type(None))):
            raise RecommendationValidationError(f"current_inputs.{key}类型无效")
        result[key] = raw_value
    return result


def create_term_variant(
    candidate: RecommendationCandidate,
    *,
    term_overrides: Mapping[str, str | int | float],
    generation_reason: str = "user_term_override",
) -> RecommendationCandidate:
    """在原candidate_id上更新当前输入，不创建父子关系或历史版本。"""
    if not isinstance(candidate, RecommendationCandidate):
        raise RecommendationValidationError("candidate必须为RecommendationCandidate")
    overrides = _normalized_current_inputs(term_overrides)
    reason = str(generation_reason).strip()
    if not reason:
        raise RecommendationValidationError("generation_reason不能为空")
    current_inputs = dict(candidate.current_inputs)
    current_inputs["term_overrides"] = overrides
    current_inputs["generation_reason"] = reason
    return replace(
        candidate,
        current_inputs=current_inputs,
        key_terms=(),
        candidate_status="candidate",
        module_run_refs=(),
        module_statuses={},
        evaluation_records=(),
    )


def _controlled_product_evidence(
    product_id: str,
    evidence: Sequence[EvidenceRef],
) -> tuple[tuple[EvidenceRef, ...], str | None]:
    refs = tuple(item for item in evidence if item.product_id == product_id)
    catalogs = {item.catalog_version for item in refs}
    if len(catalogs) != 1:
        return refs, "候选三库证据目录版本不唯一或缺失"
    by_source: dict[str, list[EvidenceRef]] = {}
    for item in refs:
        by_source.setdefault(item.source.lower(), []).append(item)
    if not by_source.get("optionlist"):
        return refs, "缺少OptionList身份"
    if not by_source.get("optionlib"):
        return refs, "缺少OptionLib章节证据"
    optionreg = by_source.get("optionreg_status", ())
    if not optionreg:
        return refs, "缺少OptionReg entry_status证据"
    if any(not item.identity or item.identity.get("product_id") != product_id for item in by_source["optionlist"]):
        return refs, "OptionList身份与候选产品不一致"
    if any(item.entry_status is not True for item in optionreg):
        return refs, "OptionReg entry_status=False或冲突，禁止推荐为可执行候选"
    return refs, None


def _rule_revision(row: Mapping[str, Any], refs: Sequence[EvidenceRef]) -> int:
    supplied = row.get("rule_revision")
    if supplied is not None:
        return _positive_rule_revision(supplied)
    observed = {
        item.identity.get("rule_revision")
        for item in refs
        if item.source.lower() == "optionreg_status" and item.identity.get("rule_revision") is not None
    }
    if len(observed) > 1:
        raise RecommendationValidationError("OptionReg证据包含冲突rule_revision")
    return _positive_rule_revision(next(iter(observed), 1))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _explicit_direction_conflict(product_name: str, constraints: Mapping[str, Any] | None) -> bool:
    market_view = str((constraints or {}).get("market_view", ""))
    bearish = ("熊市看涨价差", "熊市看跌价差", "熊市价差", "看跌期权")
    bullish = ("牛市看涨价差", "牛市看跌价差", "牛市价差", "看涨期权")
    direction = "看跌" if any(term in product_name for term in bearish) else \
        "看涨" if any(term in product_name for term in bullish) else None
    if "上涨" in market_view:
        return direction == "看跌"
    if "看跌" in market_view:
        return direction == "看涨"
    return False


def _controlled_product_name(refs: Sequence[EvidenceRef]) -> str | None:
    for ref in refs:
        if ref.source.lower() == "optionlist":
            name = str(ref.identity.get("name_zh", "")).strip()
            if name:
                return name
    return None


def build_candidates(
    research: Mapping[str, Any],
    critic: Mapping[str, Any],
    *,
    evidence: Sequence[EvidenceRef],
    run_id: str,
    max_candidates: int = 3,
    confirmed_constraints: Mapping[str, Any] | None = None,
) -> tuple[tuple[RecommendationCandidate, ...], tuple[Mapping[str, Any], ...]]:
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 10:
        raise RecommendationValidationError("max_candidates必须位于1至10")
    proposals = research.get("proposals", ())
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise RecommendationValidationError("Selector.proposals必须为数组")
    review_index = critique_candidates(research, critic)
    evidence_index = {item.evidence_id: item for item in evidence}
    seen_products: set[str] = set()
    accepted_rows: list[tuple[tuple[int, int, int], dict[str, Any], tuple[EvidenceRef, ...]]] = []
    rejected: list[Mapping[str, Any]] = []

    for position, raw in enumerate(proposals, start=1):
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Selector.proposal必须为对象")
        row = dict(raw)
        forbidden = sorted({"candidate_status", "module_run_refs", "key_terms"} & set(row))
        if forbidden:
            raise RecommendationValidationError(f"Selector不得声明运行或展示状态：{','.join(forbidden)}")
        product_id = str(row.get("product_id", "")).strip()
        if not product_id or product_id in seen_products:
            raise RecommendationValidationError("Selector候选product_id为空或重复")
        seen_products.add(product_id)
        review = dict(review_index.get(product_id, {}))
        if bool(review.get("hard_reject")):
            rejected.append({"product_id": product_id, "reason": str(review.get("rejection_reason") or "Reviewer拒绝"), "source": "Reviewer"})
            continue
        refs, gate_reason = _controlled_product_evidence(product_id, evidence)
        if gate_reason:
            rejected.append({"product_id": product_id, "reason": gate_reason, "source": "evidence_gate"})
            continue
        controlled_name = _controlled_product_name(refs)
        if controlled_name:
            row["product_name"] = controlled_name
        if controlled_name and _explicit_direction_conflict(controlled_name, confirmed_constraints):
            rejected.append({"product_id": product_id, "reason": "候选方向与已确认观点相反", "source": "constraint_gate"})
            continue
        requested_underlying = str((confirmed_constraints or {}).get("underlying", "")).strip().upper()
        underlyings = tuple(str(item).strip().upper() for item in _string_list(row.get("underlyings")))
        if requested_underlying and underlyings != (requested_underlying,):
            rejected.append({"product_id": product_id, "reason": "候选标的与已确认标的不一致", "source": "constraint_gate"})
            continue
        requested_refs = _string_list(row.get("evidence_ref_ids", ()))
        if not requested_refs or any(item not in evidence_index for item in requested_refs):
            rejected.append({"product_id": product_id, "reason": "缺少有效Knowledger证据", "source": "evidence_gate"})
            continue
        row["evidence_ref_ids"] = [item.evidence_id for item in refs]
        status = str(row.get("library_status", "unavailable")).strip()
        evidence_statuses = {item.material_status for item in refs}
        if "conflict" in evidence_statuses:
            status = "conflict"
        elif "unavailable" in evidence_statuses:
            status = "unavailable"
        elif "partial" in evidence_statuses and status == "ready":
            status = "partial"
        row["library_status"] = status
        row["not_suitable_for"] = list(dict.fromkeys(_string_list(row.get("not_suitable_for")) + _string_list(review.get("additional_not_suitable_for"))))
        row["main_risks"] = list(dict.fromkeys(_string_list(row.get("main_risks")) + _string_list(review.get("additional_risks"))))
        adjustment = max(-5, min(5, int(review.get("rank_adjustment", 0))))
        accepted_rows.append(((_STATUS_SCORE.get(status, 100), adjustment, position), row, refs))

    accepted_rows.sort(key=lambda item: (item[0], str(item[1].get("product_id"))))
    candidates: list[RecommendationCandidate] = []
    for rank, (_, row, refs) in enumerate(accepted_rows[:max_candidates], start=1):
        row = dict(row)
        candidate_id = str(row.get("candidate_id") or f"{run_id}_candidate_{rank:02d}")
        row.update({
            "candidate_id": candidate_id,
            "rule_revision": _rule_revision(row, refs),
            "rank": rank,
            "candidate_status": "candidate",
            "module_run_refs": [],
            "module_statuses": {},
            "key_terms": [],
            "evaluation_records": [],
            "current_inputs": {
                "confirmed_constraints": {
                    str(key): value
                    for key, value in (confirmed_constraints or {}).items()
                    if not str(key).startswith("_")
                },
                "term_overrides": dict(row.get("term_overrides", {})) if isinstance(row.get("term_overrides"), Mapping) else {},
            },
        })
        row.pop("term_overrides", None)
        candidates.append(RecommendationCandidate.from_mapping(row, evidence_index=evidence_index))
    for _, row, _ in accepted_rows[max_candidates:]:
        rejected.append({"product_id": row["product_id"], "reason": "超出最大候选数量", "source": "aggregation"})
    return tuple(candidates), tuple(rejected)
