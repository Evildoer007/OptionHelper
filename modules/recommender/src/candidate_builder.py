"""将Research与Critic结果聚合为严格RecommendationCandidate。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .candidate_critic import critique_candidates
from .interaction import constraints_fingerprint
from .models import EvidenceRef, RecommendationCandidate, RecommendationValidationError


_STATUS_SCORE = {"ready": 0, "partial": 20, "conflict": 40, "unavailable": 60}


def _controlled_product_evidence(product_id: str, evidence: Sequence[EvidenceRef]) -> tuple[tuple[EvidenceRef, ...], str | None]:
    """返回同产品三库证据或唯一拒绝理由；不在此解析资料或合同。"""
    refs = tuple(item for item in evidence if item.product_id == product_id)
    versions = {item.catalog_version for item in refs}
    if len(versions) != 1:
        return refs, "候选三库证据CatalogVersion不唯一或缺失"
    by_source: dict[str, list[EvidenceRef]] = {}
    for item in refs:
        by_source.setdefault(item.source.lower(), []).append(item)
    optionlist = by_source.get("optionlist", ())
    optionlib = by_source.get("optionlib", ())
    optionreg = by_source.get("optionreg_status", ())
    if not optionlist:
        return refs, "缺少OptionList身份"
    if not optionlib:
        return refs, "缺少OptionLib章节证据"
    if not optionreg:
        return refs, "缺少OptionReg entry_status证据"
    if any(not item.identity or item.identity.get("product_id") != product_id for item in optionlist):
        return refs, "OptionList身份与候选产品不一致"
    if any(item.entry_status is not True for item in optionreg):
        return refs, "OptionReg entry_status=False或冲突，禁止推荐为可执行候选"
    return refs, None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _explicit_direction_conflict(product_name: str, constraints: Mapping[str, Any] | None) -> bool:
    """仅拦截与客户明确涨跌判断相反、且产品名称明确表态的候选。"""

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
        if ref.source.lower() != "optionlist":
            continue
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
    proposals = research.get("proposals", ())
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise RecommendationValidationError("Research.proposals必须为数组")
    review_index = critique_candidates(research, critic)
    evidence_index = {item.evidence_id: item for item in evidence}
    seen_products: set[str] = set()
    accepted_rows: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    rejected: list[Mapping[str, Any]] = []

    for position, raw in enumerate(proposals, start=1):
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Research.proposal必须为对象")
        row = dict(raw)
        forbidden = sorted({"candidate_status", "module_run_refs", "key_terms"} & set(row))
        if forbidden:
            raise RecommendationValidationError(
                f"Research不得声明候选状态、模块结果或合同条款：{','.join(forbidden)}"
            )
        product_id = str(row.get("product_id", "")).strip()
        if not product_id:
            raise RecommendationValidationError("Research候选product_id不能为空")
        if product_id in seen_products:
            raise RecommendationValidationError(f"Research重复候选：{product_id}")
        seen_products.add(product_id)
        review = dict(review_index.get(product_id, {}))
        if bool(review.get("hard_reject")):
            rejected.append({
                "product_id": product_id,
                "reason": str(review.get("rejection_reason") or "Critic判定与已确认约束冲突").strip(),
                "source": "Critic",
            })
            continue
        refs, gate_reason = _controlled_product_evidence(product_id, evidence)
        if gate_reason:
            rejected.append({"product_id": product_id, "reason": gate_reason, "source": "evidence_gate"})
            continue
        controlled_name = _controlled_product_name(refs)
        if controlled_name:
            row["product_name"] = controlled_name
        if controlled_name and _explicit_direction_conflict(controlled_name, confirmed_constraints):
            rejected.append({
                "product_id": product_id,
                "reason": "候选产品的明确方向与客户已确认市场观点相反",
                "source": "constraint_gate",
            })
            continue
        requested_underlying = str((confirmed_constraints or {}).get("underlying", "")).strip().upper()
        proposal_underlyings = tuple(str(item).strip().upper() for item in _string_list(row.get("underlyings")))
        if requested_underlying and proposal_underlyings != (requested_underlying,):
            rejected.append({
                "product_id": product_id,
                "reason": "候选标的与客户已确认标的不一致",
                "source": "constraint_gate",
            })
            continue
        ref_ids = _string_list(row.get("evidence_ref_ids", ()))
        referenced = [evidence_index.get(item) for item in ref_ids]
        if not ref_ids or any(item is None for item in referenced):
            rejected.append({"product_id": product_id, "reason": "缺少有效Knowledger证据", "source": "evidence_gate"})
            continue
        if any(item.product_id != product_id for item in referenced if item is not None):
            rejected.append({"product_id": product_id, "reason": "证据与产品编号冲突", "source": "evidence_gate"})
            continue
        row["evidence_ref_ids"] = [item.evidence_id for item in refs]
        status = str(row.get("library_status", "unavailable")).strip()
        evidence_statuses = {"ready" if item.material_status == "ready" else item.material_status for item in refs if item}
        if "conflict" in evidence_statuses:
            status = "conflict"
        elif "unavailable" in evidence_statuses:
            status = "unavailable"
        elif "partial" in evidence_statuses and status == "ready":
            status = "partial"
        row["library_status"] = status
        row["not_suitable_for"] = list(dict.fromkeys(
            _string_list(row.get("not_suitable_for")) + _string_list(review.get("additional_not_suitable_for"))
        ))
        row["main_risks"] = list(dict.fromkeys(
            _string_list(row.get("main_risks")) + _string_list(review.get("additional_risks"))
        ))
        adjustment = max(-5, min(5, int(review.get("rank_adjustment", 0))))
        score = (_STATUS_SCORE.get(status, 100), adjustment, position)
        accepted_rows.append((score, row))

    accepted_rows.sort(key=lambda item: (item[0], str(item[1].get("product_id"))))
    candidates: list[RecommendationCandidate] = []
    for rank, (_, row) in enumerate(accepted_rows[:max_candidates], start=1):
        row = dict(row)
        row["candidate_id"] = f"{run_id}_candidate_{rank:02d}"
        row["rank"] = rank
        row["candidate_status"] = "candidate"
        row["constraints_fingerprint"] = constraints_fingerprint(confirmed_constraints)
        row["module_run_refs"] = []
        row["module_statuses"] = {}
        row["key_terms"] = []
        candidates.append(RecommendationCandidate.from_mapping(row, evidence_index=evidence_index))
    for _, row in accepted_rows[max_candidates:]:
        rejected.append({"product_id": row["product_id"], "reason": "超出最大候选数量", "source": "aggregation"})
    return tuple(candidates), tuple(rejected)
