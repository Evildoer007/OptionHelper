"""Mode4当前候选约束排序引擎。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
import json
import math
from typing import Any, Mapping, Sequence

from .models import RankingDecision, RankingSpec, RecommendationCandidate, RecommendationValidationError


class ConstraintRankingError(RecommendationValidationError):
    """Mode4输入、受控指标或审核结论不满足确定性协议。"""


METRIC_SOURCES: Mapping[str, str] = {
    "premium": "contract_terms",
    "pv_percent": "pricer",
    "delta": "pricer",
    "gamma": "pricer",
    "vega": "pricer",
    "theta": "pricer",
    "rho": "pricer",
    "positive_return_count": "backtester",
    "zero_return_count": "backtester",
    "negative_return_count": "backtester",
    "average_contract_settlement_return": "backtester",
    "median_contract_settlement_return": "backtester",
    "minimum_contract_settlement_return": "backtester",
    "maximum_contract_settlement_return": "backtester",
    "max_loss_contract_settlement_return": "backtester",
}
SUPPORTED_OPERATORS = frozenset({"lt", "lte", "gt", "gte", "eq"})
SUPPORTED_DIRECTIONS = frozenset({"asc", "desc"})
SUPPORTED_MISSING_POLICIES = frozenset({"exclude", "first", "last"})
_METRIC_SOURCE_SET = frozenset(METRIC_SOURCES.values())


@dataclass(frozen=True)
class RankingExclusion:
    candidate_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ConstraintRankingResult:
    ranked_candidates: tuple[RecommendationCandidate, ...]
    decisions: tuple[RankingDecision, ...]
    exclusions: tuple[RankingExclusion, ...]


@dataclass(frozen=True)
class ReviewerRankingVerdict:
    approved: bool
    reason: str | None = None


@dataclass(frozen=True)
class ReviewedConstraintRanking:
    ranking: ConstraintRankingResult
    approved: bool
    rejection_reason: str | None = None


def _reject(message: str) -> None:
    raise ConstraintRankingError(message)


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject(f"输出含重复字段：{key}")
        result[key] = value
    return result


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(f"{field_name}必须为对象")
    return dict(value)


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, float) and not math.isfinite(value):
        _reject(f"{field_name}必须为有限数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ConstraintRankingError(f"{field_name}必须为有限数值") from error
    if not result.is_finite():
        _reject(f"{field_name}必须为有限数值")
    return result


def _normalised_spec(spec: RankingSpec) -> tuple[dict[str, tuple[str, Decimal]], tuple[dict[str, str], ...]]:
    if not isinstance(spec, RankingSpec):
        _reject("ranking_spec必须使用RankingSpec")
    hard_constraints = _as_mapping(spec.hard_constraints, "RankingSpec.hard_constraints")
    constraints: dict[str, tuple[str, Decimal]] = {}
    for metric, raw_rule in hard_constraints.items():
        if metric not in METRIC_SOURCES:
            _reject(f"不支持的硬约束指标：{metric}")
        rule = _as_mapping(raw_rule, f"hard_constraints.{metric}")
        if set(rule) != {"operator", "value"}:
            _reject(f"hard_constraints.{metric}字段必须为operator,value")
        operator = str(rule["operator"]).strip().lower()
        if operator not in SUPPORTED_OPERATORS:
            _reject(f"hard_constraints.{metric}.operator无效")
        constraints[str(metric)] = operator, _decimal(rule["value"], f"hard_constraints.{metric}.value")
    sort_keys: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_key in enumerate(spec.sort_keys):
        key = _as_mapping(raw_key, f"sort_keys[{index}]")
        metric = str(key.get("metric", ""))
        if metric not in METRIC_SOURCES or metric in seen:
            _reject(f"不支持或重复的排序指标：{metric}")
        seen.add(metric)
        direction = str(key.get("direction", "")).strip().lower()
        missing = str(key.get("missing_policy", "")).strip().lower()
        if direction not in SUPPORTED_DIRECTIONS or missing not in SUPPORTED_MISSING_POLICIES:
            _reject(f"sort_keys[{index}]方向或缺失策略无效")
        sort_keys.append({"metric": metric, "direction": direction, "missing_policy": missing})
    return constraints, tuple(sort_keys)


def parse_specifier_ranking_spec(value: Mapping[str, Any] | str) -> RankingSpec:
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_strict_json_object_pairs)
        except json.JSONDecodeError as error:
            raise ConstraintRankingError("Specifier输出不是合法JSON") from error
    try:
        spec = RankingSpec.from_mapping(_as_mapping(value, "Specifier输出"))
    except RecommendationValidationError as error:
        raise ConstraintRankingError(str(error)) from error
    _normalised_spec(spec)
    return spec


def required_metric_sources(spec: RankingSpec) -> Mapping[str, str]:
    constraints, sort_keys = _normalised_spec(spec)
    metrics = set(constraints)
    metrics.update(item["metric"] for item in sort_keys)
    return {metric: METRIC_SOURCES[metric] for metric in sorted(metrics)}


def required_modules(spec: RankingSpec) -> tuple[str, ...]:
    sources = set(required_metric_sources(spec).values())
    modules = tuple(sorted(sources - {"contract_terms"}))
    # 纯条款排序也需要正式Run的合同快照证据，期权费来源仍是contract_terms。
    return modules or (("payoffer",) if "contract_terms" in sources else ())


def _validate_candidates(candidates: Sequence[RecommendationCandidate]) -> tuple[RecommendationCandidate, ...]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence) or not candidates:
        _reject("candidates必须为非空RecommendationCandidate数组")
    result = tuple(candidates)
    if len(result) > 10 or any(not isinstance(item, RecommendationCandidate) for item in result):
        _reject("candidates类型或数量无效")
    ids = [item.candidate_id for item in result]
    if len(ids) != len(set(ids)):
        _reject("Mode4候选candidate_id不得重复")
    return result


def _normalise_metrics_by_candidate(
    metrics_by_candidate: Mapping[str, Any],
    *,
    candidate_ids: set[str],
) -> Mapping[str, Mapping[str, Mapping[str, Decimal]]]:
    raw_rows = _as_mapping(metrics_by_candidate, "metrics_by_candidate")
    if set(raw_rows) != candidate_ids:
        _reject("metrics_by_candidate必须与当前候选一一对应")
    result: dict[str, Mapping[str, Mapping[str, Decimal]]] = {}
    for candidate_id in sorted(candidate_ids):
        source_rows = _as_mapping(raw_rows[candidate_id], f"metrics_by_candidate.{candidate_id}")
        unknown_sources = set(source_rows) - _METRIC_SOURCE_SET
        if unknown_sources:
            _reject(f"候选指标含未知来源：{','.join(sorted(unknown_sources))}")
        normalized_sources: dict[str, Mapping[str, Decimal]] = {}
        for source, raw_metrics in source_rows.items():
            metrics = _as_mapping(raw_metrics, f"metrics_by_candidate.{candidate_id}.{source}")
            normalized_metrics: dict[str, Decimal] = {}
            for metric, raw_value in metrics.items():
                if metric not in METRIC_SOURCES or METRIC_SOURCES[metric] != source:
                    _reject(f"指标{metric}来源无效")
                normalized_metrics[str(metric)] = _decimal(raw_value, f"{candidate_id}.{source}.{metric}")
            normalized_sources[str(source)] = normalized_metrics
        result[candidate_id] = normalized_sources
    return result


def _metric_value(metrics: Mapping[str, Mapping[str, Decimal]], metric: str) -> Decimal | None:
    return metrics.get(METRIC_SOURCES[metric], {}).get(metric)


def _matches(value: Decimal, operator: str, threshold: Decimal) -> bool:
    return {"lt": value < threshold, "lte": value <= threshold, "gt": value > threshold, "gte": value >= threshold, "eq": value == threshold}[operator]


def _compare_eligible(
    left: tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]],
    right: tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]],
    sort_keys: Sequence[Mapping[str, str]],
) -> int:
    for key in sort_keys:
        left_value = _metric_value(left[1], key["metric"])
        right_value = _metric_value(right[1], key["metric"])
        if left_value is None or right_value is None:
            if left_value is None and right_value is None:
                continue
            missing_first = key["missing_policy"] == "first"
            return -1 if left_value is None and missing_first or right_value is None and not missing_first else 1
        if left_value != right_value:
            ascending = key["direction"] == "asc"
            return -1 if (left_value < right_value) == ascending else 1
    return -1 if left[0].candidate_id < right[0].candidate_id else 1 if left[0].candidate_id > right[0].candidate_id else 0


def rank_candidates(
    spec: RankingSpec,
    candidates: Sequence[RecommendationCandidate],
    metrics_by_candidate: Mapping[str, Any],
) -> ConstraintRankingResult:
    constraints, sort_keys = _normalised_spec(spec)
    current = _validate_candidates(candidates)
    metrics = _normalise_metrics_by_candidate(metrics_by_candidate, candidate_ids={item.candidate_id for item in current})
    eligible: list[tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]]] = []
    excluded: dict[str, tuple[str, ...]] = {}
    for candidate in current:
        candidate_metrics = metrics[candidate.candidate_id]
        reasons: list[str] = []
        for metric, (operator, threshold) in constraints.items():
            value = _metric_value(candidate_metrics, metric)
            if value is None:
                reasons.append(f"缺少硬约束指标:{metric}")
            elif not _matches(value, operator, threshold):
                reasons.append(f"硬约束不满足:{metric} {operator} {threshold}")
        for key in sort_keys:
            if key["missing_policy"] == "exclude" and _metric_value(candidate_metrics, key["metric"]) is None:
                reasons.append(f"排序指标缺失:{key['metric']}")
        if reasons:
            excluded[candidate.candidate_id] = tuple(reasons)
        else:
            eligible.append((candidate, candidate_metrics))
    eligible.sort(key=cmp_to_key(lambda left, right: _compare_eligible(left, right, sort_keys)))
    ranked = tuple(replace(item, rank=index) for index, (item, _) in enumerate(eligible, start=1))
    ranks = {item.candidate_id: item.rank for item in ranked}
    metric_sources = required_metric_sources(spec)
    decisions = tuple(
        RankingDecision(
            ranking_spec_id=spec.ranking_spec_id, candidate_id=item.candidate_id,
            eligible=item.candidate_id not in excluded,
            exclusion_reasons=excluded.get(item.candidate_id, ()), metric_sources=metric_sources,
            final_rank=ranks.get(item.candidate_id),
        )
        for item in sorted(current, key=lambda candidate: candidate.candidate_id)
    )
    exclusions = tuple(RankingExclusion(candidate_id=key, reasons=excluded[key]) for key in sorted(excluded))
    return ConstraintRankingResult(ranked_candidates=ranked, decisions=decisions, exclusions=exclusions)


def parse_reviewer_ranking_verdict(value: Mapping[str, Any] | str) -> ReviewerRankingVerdict:
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_strict_json_object_pairs)
        except json.JSONDecodeError as error:
            raise ConstraintRankingError("Reviewer输出不是合法JSON") from error
    data = _as_mapping(value, "Reviewer输出")
    if set(data) != {"decision", "reason"}:
        _reject("Reviewer只能输出decision与reason")
    decision = str(data.get("decision", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if decision == "approve" and not reason:
        return ReviewerRankingVerdict(approved=True)
    if decision == "reject" and reason:
        return ReviewerRankingVerdict(approved=False, reason=reason)
    _reject("Reviewer结论或reason无效")


def apply_reviewer_verdict(ranking: ConstraintRankingResult, verdict: ReviewerRankingVerdict) -> ReviewedConstraintRanking:
    if not isinstance(ranking, ConstraintRankingResult) or not isinstance(verdict, ReviewerRankingVerdict):
        _reject("ranking或verdict类型无效")
    return ReviewedConstraintRanking(ranking=ranking, approved=verdict.approved, rejection_reason=verdict.reason)
