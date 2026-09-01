"""Mode4约束排序的纯领域引擎。

本模块只接受Host已控制的候选、合同条款指标和模块指标，不调用模型、App
或计算模块。Specifier只能提供排名规则；Reviewer只能整体批准或拒绝结果。
"""

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


# ``contract_terms``不是可调用模块：premium必须由Host解析后的受控合同条款提供。
METRIC_SOURCES: Mapping[str, str] = {
    "premium": "contract_terms",
    "pv_percent": "pricer",
    "delta": "pricer",
    "gamma": "pricer",
    "vega": "pricer",
    "theta": "pricer",
    "rho": "pricer",
    "win_rate": "backtester",
    "average_gross_return": "backtester",
    "max_loss_gross_return": "backtester",
}
SUPPORTED_OPERATORS = frozenset({"lt", "lte", "gt", "gte", "eq"})
SUPPORTED_DIRECTIONS = frozenset({"asc", "desc"})
SUPPORTED_MISSING_POLICIES = frozenset({"exclude", "first", "last"})
_METRIC_SOURCE_SET = frozenset(METRIC_SOURCES.values())


@dataclass(frozen=True)
class RankingExclusion:
    """候选被确定性规则排除的可展示记录。"""

    candidate_key: str
    version_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ConstraintRankingResult:
    """Host可直接持久化的排序事实；候选排名均为连续整数。"""

    ranked_candidates: tuple[RecommendationCandidate, ...]
    decisions: tuple[RankingDecision, ...]
    exclusions: tuple[RankingExclusion, ...]


@dataclass(frozen=True)
class ReviewerRankingVerdict:
    """Reviewer对既定排序的整体结论，不能携带候选级改写。"""

    approved: bool
    reason: str | None = None


@dataclass(frozen=True)
class ReviewedConstraintRanking:
    """审核不改变排序本身，只决定该排序能否进入下一阶段。"""

    ranking: ConstraintRankingResult
    approved: bool
    rejection_reason: str | None = None


def _reject(message: str) -> None:
    raise ConstraintRankingError(message)


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject(f"Specifier输出含重复字段：{key}")
        result[key] = value
    return result


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(f"{field_name}必须为对象")
    return dict(value)


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        _reject(f"{field_name}必须为有限数值")
    if isinstance(value, float) and not math.isfinite(value):
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
    normalised_constraints: dict[str, tuple[str, Decimal]] = {}
    for metric, raw_rule in hard_constraints.items():
        if metric not in METRIC_SOURCES:
            _reject(f"不支持的硬约束指标：{metric}")
        rule = _as_mapping(raw_rule, f"hard_constraints.{metric}")
        if set(rule) != {"operator", "value"}:
            _reject(f"hard_constraints.{metric}字段必须为operator,value")
        operator = str(rule["operator"]).strip().lower()
        if operator not in SUPPORTED_OPERATORS:
            _reject(f"hard_constraints.{metric}.operator必须为lt、lte、gt、gte或eq")
        normalised_constraints[str(metric)] = (operator, _decimal(rule["value"], f"hard_constraints.{metric}.value"))

    normalised_sort_keys: list[dict[str, str]] = []
    seen_metrics: set[str] = set()
    for index, raw_key in enumerate(spec.sort_keys):
        key = _as_mapping(raw_key, f"sort_keys[{index}]")
        metric = key.get("metric")
        if metric not in METRIC_SOURCES:
            _reject(f"不支持的排序指标：{metric}")
        if metric in seen_metrics:
            _reject(f"排序指标重复：{metric}")
        seen_metrics.add(metric)
        direction = str(key.get("direction", "")).strip().lower()
        missing_policy = str(key.get("missing_policy", "")).strip().lower()
        if direction not in SUPPORTED_DIRECTIONS:
            _reject(f"sort_keys[{index}].direction必须为asc或desc")
        if missing_policy not in SUPPORTED_MISSING_POLICIES:
            _reject(f"sort_keys[{index}].missing_policy必须为exclude、first或last")
        normalised_sort_keys.append({"metric": str(metric), "direction": direction, "missing_policy": missing_policy})
    if not normalised_sort_keys:
        _reject("至少需要一个排序指标")
    return normalised_constraints, tuple(normalised_sort_keys)


def parse_specifier_ranking_spec(value: Mapping[str, Any] | str) -> RankingSpec:
    """将Specifier的严格JSON对象解析为可执行的``RankingSpec``。

    JSON使用重复键检测，避免模型或中间层以最后一个同名字段静默覆盖规则。
    """

    if isinstance(value, str):
        try:
            parsed = json.loads(value, object_pairs_hook=_strict_json_object_pairs)
        except json.JSONDecodeError as error:
            raise ConstraintRankingError("Specifier输出不是合法JSON") from error
    else:
        parsed = value
    try:
        spec = RankingSpec.from_mapping(_as_mapping(parsed, "Specifier输出"))
    except RecommendationValidationError as error:
        raise ConstraintRankingError(str(error)) from error
    _normalised_spec(spec)
    return spec


def required_metric_sources(spec: RankingSpec) -> Mapping[str, str]:
    """返回本轮规则需要的指标及其唯一受控来源。"""

    constraints, sort_keys = _normalised_spec(spec)
    metrics = set(constraints)
    metrics.update(item["metric"] for item in sort_keys)
    return {metric: METRIC_SOURCES[metric] for metric in sorted(metrics)}


def required_modules(spec: RankingSpec) -> tuple[str, ...]:
    """返回需要调度的计算模块；合同条款不是模块调用。"""

    return tuple(sorted({source for source in required_metric_sources(spec).values() if source != "contract_terms"}))


def _candidate_identity(candidate: RecommendationCandidate) -> tuple[str, str]:
    candidate_key = str(candidate.candidate_key or "").strip()
    version_id = str(candidate.candidate_version_id or "").strip()
    if not candidate_key or not version_id:
        _reject("Mode4候选必须携带candidate_key与candidate_version_id")
    return candidate_key, version_id


def _validate_candidates(candidates: Sequence[RecommendationCandidate]) -> tuple[RecommendationCandidate, ...]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence) or not candidates:
        _reject("candidates必须为非空RecommendationCandidate数组")
    if len(candidates) > 10:
        _reject("Mode4最多排序10个候选")
    result = tuple(candidates)
    if any(not isinstance(candidate, RecommendationCandidate) for candidate in result):
        _reject("candidates必须使用RecommendationCandidate")
    keys = [_candidate_identity(candidate)[0] for candidate in result]
    versions = [_candidate_identity(candidate)[1] for candidate in result]
    if len(set(keys)) != len(keys):
        _reject("Mode4候选candidate_key不得重复")
    if len(set(versions)) != len(versions):
        _reject("Mode4候选candidate_version_id不得重复")
    return result


def _normalise_metrics_by_version(
    metrics_by_version: Mapping[str, Any],
    *,
    version_ids: set[str],
) -> Mapping[str, Mapping[str, Mapping[str, Decimal]]]:
    raw_by_version = _as_mapping(metrics_by_version, "metrics_by_version")
    if any(not isinstance(version_id, str) or not version_id.strip() for version_id in raw_by_version):
        _reject("metrics_by_version的版本键必须为非空字符串")
    supplied_versions = set(raw_by_version)
    if supplied_versions != version_ids:
        _reject("metrics_by_version必须与候选版本一一对应")
    normalised: dict[str, Mapping[str, Mapping[str, Decimal]]] = {}
    for version_id in sorted(version_ids):
        source_rows = _as_mapping(raw_by_version[version_id], f"metrics_by_version.{version_id}")
        if any(not isinstance(source, str) for source in source_rows):
            _reject(f"metrics_by_version.{version_id}的来源键必须为字符串")
        unknown_sources = set(source_rows) - _METRIC_SOURCE_SET
        if unknown_sources:
            _reject(f"metrics_by_version.{version_id}含未知来源：{','.join(sorted(unknown_sources))}")
        normalised_sources: dict[str, Mapping[str, Decimal]] = {}
        for source, raw_metrics in source_rows.items():
            metrics = _as_mapping(raw_metrics, f"metrics_by_version.{version_id}.{source}")
            if any(not isinstance(metric, str) for metric in metrics):
                _reject(f"metrics_by_version.{version_id}.{source}的指标键必须为字符串")
            normalised_metrics: dict[str, Decimal] = {}
            for metric, raw_value in metrics.items():
                if metric not in METRIC_SOURCES:
                    _reject(f"metrics_by_version.{version_id}.{source}含未知指标：{metric}")
                if METRIC_SOURCES[metric] != source:
                    _reject(f"指标{metric}必须来自{METRIC_SOURCES[metric]}，不能来自{source}")
                normalised_metrics[str(metric)] = _decimal(
                    raw_value, f"metrics_by_version.{version_id}.{source}.{metric}"
                )
            normalised_sources[str(source)] = normalised_metrics
        normalised[version_id] = normalised_sources
    return normalised


def _metric_value(
    metrics: Mapping[str, Mapping[str, Decimal]],
    metric: str,
) -> Decimal | None:
    return metrics.get(METRIC_SOURCES[metric], {}).get(metric)


def _matches(value: Decimal, operator: str, threshold: Decimal) -> bool:
    return {
        "lt": value < threshold,
        "lte": value <= threshold,
        "gt": value > threshold,
        "gte": value >= threshold,
        "eq": value == threshold,
    }[operator]


def _compare_eligible(
    left: tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]],
    right: tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]],
    sort_keys: Sequence[Mapping[str, str]],
) -> int:
    for key in sort_keys:
        metric = key["metric"]
        left_value = _metric_value(left[1], metric)
        right_value = _metric_value(right[1], metric)
        if left_value is None or right_value is None:
            if left_value is None and right_value is None:
                continue
            missing_first = key["missing_policy"] == "first"
            if left_value is None:
                return -1 if missing_first else 1
            return 1 if missing_first else -1
        if left_value != right_value:
            ascending = key["direction"] == "asc"
            return -1 if (left_value < right_value) == ascending else 1
    left_key, _ = _candidate_identity(left[0])
    right_key, _ = _candidate_identity(right[0])
    return -1 if left_key < right_key else 1 if left_key > right_key else 0


def rank_candidates(
    spec: RankingSpec,
    candidates: Sequence[RecommendationCandidate],
    metrics_by_version: Mapping[str, Any],
) -> ConstraintRankingResult:
    """按受控指标执行硬过滤与稳定排序，不采纳模型给出的名次。"""

    constraints, sort_keys = _normalised_spec(spec)
    candidates = _validate_candidates(candidates)
    identities = tuple((candidate, *_candidate_identity(candidate)) for candidate in candidates)
    metrics = _normalise_metrics_by_version(
        metrics_by_version, version_ids={version_id for _, _, version_id in identities}
    )
    metric_sources = required_metric_sources(spec)
    eligible: list[tuple[RecommendationCandidate, Mapping[str, Mapping[str, Decimal]]]] = []
    exclusion_rows: list[tuple[RecommendationCandidate, tuple[str, ...]]] = []
    for candidate, candidate_key, version_id in identities:
        candidate_metrics = metrics[version_id]
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
            exclusion_rows.append((candidate, tuple(reasons)))
        else:
            eligible.append((candidate, candidate_metrics))

    eligible.sort(key=cmp_to_key(lambda left, right: _compare_eligible(left, right, sort_keys)))
    ranked_candidates = tuple(replace(candidate, rank=index) for index, (candidate, _) in enumerate(eligible, start=1))
    rank_by_version = {
        _candidate_identity(candidate)[1]: candidate.rank for candidate in ranked_candidates
    }
    reasons_by_version = {
        _candidate_identity(candidate)[1]: reasons for candidate, reasons in exclusion_rows
    }
    decisions = tuple(
        RankingDecision(
            ranking_spec_id=spec.ranking_spec_id,
            version_id=version_id,
            eligible=version_id not in reasons_by_version,
            exclusion_reasons=reasons_by_version.get(version_id, ()),
            metric_sources=metric_sources,
            final_rank=rank_by_version.get(version_id),
        )
        for _, candidate_key, version_id in sorted(identities, key=lambda item: (item[1], item[2]))
    )
    exclusions = tuple(
        RankingExclusion(candidate_key=candidate_key, version_id=version_id, reasons=reasons_by_version[version_id])
        for _, candidate_key, version_id in sorted(identities, key=lambda item: (item[1], item[2]))
        if version_id in reasons_by_version
    )
    return ConstraintRankingResult(
        ranked_candidates=ranked_candidates, decisions=decisions, exclusions=exclusions
    )


def parse_reviewer_ranking_verdict(value: Mapping[str, Any] | str) -> ReviewerRankingVerdict:
    """解析Reviewer的整体结论；所有候选级字段均属于越权。"""

    if isinstance(value, str):
        try:
            parsed = json.loads(value, object_pairs_hook=_strict_json_object_pairs)
        except json.JSONDecodeError as error:
            raise ConstraintRankingError("Reviewer输出不是合法JSON") from error
    else:
        parsed = value
    data = _as_mapping(parsed, "Reviewer输出")
    if set(data) != {"decision", "reason"}:
        _reject("Reviewer只能输出decision与reason，不能改写候选或排名")
    decision = str(data.get("decision", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if decision == "approve":
        if reason:
            _reject("Reviewer批准时reason必须为空")
        return ReviewerRankingVerdict(approved=True)
    if decision == "reject":
        if not reason:
            _reject("Reviewer整体拒绝时必须说明reason")
        return ReviewerRankingVerdict(approved=False, reason=reason)
    _reject("Reviewer.decision必须为approve或reject")


def apply_reviewer_verdict(
    ranking: ConstraintRankingResult,
    verdict: ReviewerRankingVerdict,
) -> ReviewedConstraintRanking:
    """保留引擎结果原样，仅附加审核是否批准。"""

    if not isinstance(ranking, ConstraintRankingResult):
        _reject("ranking必须使用ConstraintRankingResult")
    if not isinstance(verdict, ReviewerRankingVerdict):
        _reject("verdict必须使用ReviewerRankingVerdict")
    return ReviewedConstraintRanking(
        ranking=ranking, approved=verdict.approved, rejection_reason=verdict.reason
    )
