"""Audited fair-parameter capability directory.

This module is the single static business directory for fair-parameter
capability. Every product and every target is an explicit, product-level audit
decision. The code only joins those decisions with authenticated OptionReg
metadata (labels, domains and current public valuation methods); it never
promotes a target from a field name or from a generic product rule.

The audit records whether a target is an incremental quote or a complete
contract-value quote, the existing public value basis, state applicability,
cashflow evidence and the uniqueness conditions. It does not solve, read
market data or call a pricing engine.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any, Mapping


if __name__ != "modules.pricer.fair_parameter":
    raise ImportError(
        "Pricer fair_parameter必须通过正式modules.pricer.fair_parameter路径导入"
    )

from runtime.contracts.contract_api import load_registry

from .model_router import capability_for as valuation_capability_for


CAPABILITY_VERSION = "optionhelper.pricer-fair-parameter"
CAPABILITY_SCHEMA_ID = "optionhelper.pricer-fair-parameter-capability"

_PUBLIC_METHODS = ("analytical", "monte_carlo")
_TARGET_STATUSES = frozenset({"supported", "unsupported", "solve_semantics_blocked"})
_PRODUCT_STATUSES = frozenset({"supported", "valuation_only", "solve_semantics_blocked"})
_QUOTE_BASES = frozenset({"incremental_net_value", "contract_target_value"})
_QUOTE_VALUE_BASES = frozenset({"pv_percent", "variance_percent"})
_PUBLIC_RATIO_UNITS = frozenset({
    "normalized_point",
    "premium_percent_s0_100",
    "rate",
    "volatility",
    "price",
})
_NON_CONTINUOUS_UNITS = frozenset({
    "count", "day", "enum", "flag", "observation_count", "schedule",
    "schedule_selector", "unit", "year",
})
_CATALOG_UNITS = _PUBLIC_RATIO_UNITS | _NON_CONTINUOUS_UNITS
_UNIT_PUBLIC_TRANSFORMS = {
    "normalized_point": "points_100_to_decimal_ratio",
    # OptionReg records 5% as 5.  The public fair-term protocol exposes the
    # economically meaningful decimal ratio 0.05 and never calls it points.
    "premium_percent_s0_100": "points_100_to_decimal_ratio",
    "rate": "identity_ratio",
    "volatility": "identity_ratio",
    "price": "relative_to_frozen_reference_price_basis",
}

# Numerical controls are algorithm policy, not financial evidence.  They are
# deliberately versioned and expanded into every TargetCapability below so a
# solver run can compare the complete rule directly.  The analytical section
# intentionally contains no invented value or
# slope bound: those require model-specific proof and remain insufficient for
# quote qualification until supplied by a trusted runtime proof.
_SOLVER_RULE_VERSION = "optionhelper.pricer-fair-solver.v1"
_SOLVER_SEARCH_DEFAULT = {
    "rule_version": _SOLVER_RULE_VERSION,
    "kind": "finite_domain_with_controlled_expansion",
    "initial_interval": {
        "kind": "base_relative",
        "base_relative_half_width": 2.0,
        "zero_base_half_width": 1.0,
    },
    "expansion": {
        "factor": 2.0,
        "max_expansions": 8,
    },
    # Open economic domains (for example 0 < K < S_0) cannot be probed at
    # their mathematical endpoint.  The solver consumes this registered
    # rule to choose a finite interior seed.  Both terms are algorithmic
    # controls, not a product-level claim about a valuation model.
    "safe_endpoint": {
        "rule_version": _SOLVER_RULE_VERSION,
        "kind": "base_relative_absolute_epsilon",
        "relative_to_base": 1e-8,
        "absolute_epsilon": 1e-8,
        "zero_base_absolute_epsilon": 1e-8,
    },
    "finite_interval_source": "target_domain_and_resolved_contract_constraints",
    "stop_rule": "stop_on_bracketed_root_or_exhausted_domain",
}
_SOLVER_CONVERGENCE_DEFAULT = {
    "rule_version": _SOLVER_RULE_VERSION,
    "residual_encoding": "decimal_ratio",
    "residual_tolerance": 1e-8,
    "parameter_tolerance": 1e-8,
    "max_iterations": 64,
    "slope_absolute_min": 1e-12,
    "final_revaluation_required": True,
    "stop_rule": "residual_and_parameter_tolerance_or_controlled_failure",
}
_SOLVER_ANALYTICAL_PRECISION = {
    "uncertainty_method": "deterministic_bound",
    "formal_quote_status": "research_only",
    "formal_quote_reason": "未登记产品级参数绝对误差门槛",
    "bound_sources": (
        "trusted_runtime_valuation_error_bound",
        "target_spec_proven_slope_lower_bound",
        "target_spec_parameter_representation_bound",
    ),
    "required_bounds": (
        "valuation_error_upper_bound",
        "residual_upper_bound",
        "slope_absolute_lower_bound",
        "parameter_representation_error_upper_bound",
        "absolute_error_upper_bound",
    ),
    "evidence_required": True,
    "failure_status": "insufficient_evidence",
}
_SOLVER_MC_PRECISION = {
    "uncertainty_method": "paired_independent_batches",
    "formal_quote_status": "research_only",
    "formal_quote_reason": "未登记产品级参数绝对误差门槛",
    "minimum_independent_batches": 2,
    "confidence_level": 0.95,
    "slope_stability_relative_tolerance": 0.25,
    "threshold_sources": {
        "minimum_independent_batches": "registered_solver_rule.monte_carlo.minimum_independent_batches",
        "confidence_level": "registered_solver_rule.monte_carlo.confidence_level",
        "slope_stability_relative_tolerance": "registered_solver_rule.monte_carlo.slope_stability_relative_tolerance",
    },
    "required_fields": (
        "independent_batch_count",
        "slope_stability_status",
        "lower",
        "upper",
        "absolute_error_upper_bound",
    ),
    "failure_status": "insufficient_evidence",
    "quote_gate": "false_when_insufficient_or_unstable",
}

_OPTIONREG_EVIDENCE = "optionreg.registry"
_VALUATION_ROUTE_EVIDENCE = "pricer.model_router.capabilities"
_VALUATION_TEST_EVIDENCE = "pricer.product_acceptance_matrix"
_CASHFLOW_EVIDENCE = "pricer.fair_parameter.cashflow_evidence"
_EVIDENCE_IDS = frozenset({
    _OPTIONREG_EVIDENCE,
    _VALUATION_ROUTE_EVIDENCE,
    _VALUATION_TEST_EVIDENCE,
    _CASHFLOW_EVIDENCE,
})

# These are internal execution rules.  They are intentionally absent from
# ``to_public_dict``: callers may learn whether a target is eligible, but the
# signed capability directory owns the accounting and timing semantics used by
# the solver.
_VALUATION_PERSPECTIVE = "contract_holder"
_INITIAL_PRICING_TIME_RULE = {
    "schema_id": "optionhelper.pricer.initial-pricing-time-rule",
    "kind": "new_issuance_at_contract_start",
    "required_lifecycle_status": "initial",
    "valuation_date": {
        "field": "valuation_date",
        "relation": "equals",
        "other_field": "contract_start_date",
    },
    "prior_events": {
        "field": "occurred_events",
        "count_relation": "equals",
        "count": 0,
    },
    "prior_cashflows": {
        "field": "realized_cashflows",
        "count_relation": "equals",
        "count": 0,
    },
    "first_economic_observation": {
        "field": "first_economic_observation_date",
        "relation": "on_or_after",
        "other_field": "valuation_date",
        "presence": "if_present",
    },
    "failure_status": "initial_pricing_time_invalid",
}


_NEW_ISSUANCE_STATE = {
    "new_issuance": "allowed",
    "surviving": "not_allowed",
    "triggered": "not_allowed",
    "terminated": "not_allowed",
    "reason": "当前OptionReg报价现金流证据只覆盖新发行合同",
}
_UNVERIFIED_STATE = {
    "new_issuance": "not_registered",
    "surviving": "not_allowed",
    "triggered": "not_allowed",
    "terminated": "not_allowed",
    "reason": "尚无该存续状态下的完整反解证据",
}

_NO_TIME_DIMENSION_CHANGE = {
    "kind": "no_time_dimension_change",
    "legal_maturity": "unchanged",
    "observation_schedule": "unchanged",
    "payment_times": "unchanged",
    "data_coverage": "unchanged",
    "source": "PricerTargetRule",
}


def _require_finite_value(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("反解公开值必须为有限数")
    return number


def _catalog_unit_transform(catalog_unit: str) -> str:
    unit = str(catalog_unit).strip()
    if unit not in _UNIT_PUBLIC_TRANSFORMS:
        raise ValueError(f"catalog unit {unit!r}没有连续百分比公开转换")
    return _UNIT_PUBLIC_TRANSFORMS[unit]


def encode_public_value(
    value: float,
    *,
    catalog_unit: str,
    reference_price_basis: float | None = None,
) -> float:
    """Encode a catalog value as the public decimal-ratio representation."""
    number = _require_finite_value(value)
    transform = _catalog_unit_transform(catalog_unit)
    if transform == "points_100_to_decimal_ratio":
        return number / 100.0
    if transform == "identity_ratio":
        return number
    reference = _require_finite_value(reference_price_basis) if reference_price_basis is not None else None
    if reference is None or reference <= 0.0:
        raise ValueError("price公开转换必须绑定正的冻结reference_price_basis")
    return number / reference


def decode_public_value(
    value: float,
    *,
    catalog_unit: str,
    reference_price_basis: float | None = None,
) -> float:
    """Decode a public decimal-ratio value into the catalog's internal unit."""
    number = _require_finite_value(value)
    transform = _catalog_unit_transform(catalog_unit)
    if transform == "points_100_to_decimal_ratio":
        return number * 100.0
    if transform == "identity_ratio":
        return number
    reference = _require_finite_value(reference_price_basis) if reference_price_basis is not None else None
    if reference is None or reference <= 0.0:
        raise ValueError("price公开转换必须绑定正的冻结reference_price_basis")
    return number * reference


to_public_decimal_ratio = encode_public_value
from_public_decimal_ratio = decode_public_value


@dataclass(frozen=True)
class _AuditTarget:
    """One explicit review decision in the source-of-truth audit table."""

    target_id: str
    support_status: str
    quote_basis: str
    quote_value_basis: str
    quote_target_source: str
    allowed_methods: tuple[str, ...]
    controlled_term_keys: tuple[str, ...]
    solver_class: str
    uniqueness_rule: str
    unsupported_reason: str | None
    state_applicability_rule: Mapping[str, Any]
    cashflow_evidence: Mapping[str, Any] | None
    schedule_impact_rule: Mapping[str, Any]
    evidence: tuple[str, ...]
    transform_rule: Mapping[str, Any] | None = None
    runtime_identifiability_rule: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _AuditProduct:
    product_id: str
    solve_status: str
    targets: tuple[_AuditTarget, ...]
    audit_evidence: tuple[str, ...]


def _target(
    target_id: str,
    status: str,
    *,
    controlled: tuple[str, ...] | None = None,
    quote_basis: str = "contract_target_value",
    quote_value_basis: str = "pv_percent",
    quote_target_source: str = "not_registered",
    allowed_methods: tuple[str, ...] = (),
    solver_class: str = "bounded_monotone",
    uniqueness: str = "未登记唯一性证据",
    reason: str | None = None,
    state_applicability_rule: Mapping[str, Any] | None = None,
    cashflow_evidence: Mapping[str, Any] | None = None,
    schedule_impact_rule: Mapping[str, Any] | None = None,
    evidence: tuple[str, ...] = (_OPTIONREG_EVIDENCE,),
    transform: Mapping[str, Any] | None = None,
    runtime_identifiability_rule: Mapping[str, Any] | None = None,
) -> _AuditTarget:
    """Construct an explicitly supplied audit row; never infer ``status``."""
    return _AuditTarget(
        target_id=target_id,
        support_status=status,
        quote_basis=quote_basis,
        quote_value_basis=quote_value_basis,
        quote_target_source=quote_target_source,
        allowed_methods=tuple(allowed_methods),
        controlled_term_keys=controlled or (target_id,),
        solver_class=solver_class,
        uniqueness_rule=uniqueness,
        unsupported_reason=reason,
        state_applicability_rule=deepcopy(dict(state_applicability_rule or _UNVERIFIED_STATE)),
        cashflow_evidence=None if cashflow_evidence is None else deepcopy(dict(cashflow_evidence)),
        schedule_impact_rule=deepcopy(dict(schedule_impact_rule or _NO_TIME_DIMENSION_CHANGE)),
        evidence=evidence,
        transform_rule=transform,
        runtime_identifiability_rule=(
            None
            if runtime_identifiability_rule is None
            else deepcopy(dict(runtime_identifiability_rule))
        ),
    )


def _supported_premium(
    target_id: str,
    *,
    cashflow_symbol: str,
    allowed_methods: tuple[str, ...] = ("analytical", "monte_carlo"),
) -> _AuditTarget:
    return _target(
        target_id,
        "supported",
        quote_basis="incremental_net_value",
        quote_value_basis="pv_percent",
        quote_target_source="fixed_zero_incremental_net_value",
        allowed_methods=allowed_methods,
        solver_class="affine",
        uniqueness="OptionReg每条路径的期初报价现金流对目标为同号非零仿射项，其他合同条款固定时唯一",
        state_applicability_rule=_NEW_ISSUANCE_STATE,
        cashflow_evidence={
            "kind": "initial_quote_cashflow_affine",
            "target_symbol": cashflow_symbol,
            "cashflow_time": "0",
            "all_path_coefficients": "non_zero_same_sign",
            "coefficient_sign": "negative",
            "future_cashflows_exclude_target": True,
            "public_conversion": "decimal_ratio_to_percentage",
        },
        runtime_identifiability_rule={
            "kind": "initial_cashflow_coefficient",
            "coefficient_nonzero": True,
            "condition": "每条合法路径的折现期初系数绝对值严格大于零且同号",
            "failure_status": "zero_slope",
            "failure_eligibility": "ineligible",
        },
        evidence=(
            _OPTIONREG_EVIDENCE,
            _VALUATION_ROUTE_EVIDENCE,
            _VALUATION_TEST_EVIDENCE,
            _CASHFLOW_EVIDENCE,
        ),
    )


def _unsupported_term(
    target_id: str,
    *,
    quote_basis: str = "contract_target_value",
    quote_value_basis: str = "pv_percent",
    reason: str = "该连续字段尚无经过产品级验证的报价方程与唯一性证据",
) -> _AuditTarget:
    return _target(
        target_id,
        "unsupported",
        quote_basis=quote_basis,
        quote_value_basis=quote_value_basis,
        reason=reason,
        evidence=(_OPTIONREG_EVIDENCE, _VALUATION_ROUTE_EVIDENCE),
    )


def _blocked_term(
    target_id: str,
    *,
    controlled: tuple[str, ...] | None = None,
    quote_basis: str = "contract_target_value",
    quote_value_basis: str = "pv_percent",
    reason: str,
    state_applicability_rule: Mapping[str, Any] | None = None,
    cashflow_evidence: Mapping[str, Any] | None = None,
    transform: Mapping[str, Any] | None = None,
) -> _AuditTarget:
    return _target(
        target_id,
        "solve_semantics_blocked",
        controlled=controlled,
        quote_basis=quote_basis,
        quote_value_basis=quote_value_basis,
        solver_class="coupled_transform" if transform is not None else "bounded_monotone",
        uniqueness="完整报价方程、合法域或耦合关系尚未证明唯一",
        reason=reason,
        state_applicability_rule=state_applicability_rule,
        cashflow_evidence=cashflow_evidence,
        evidence=(_OPTIONREG_EVIDENCE, _VALUATION_ROUTE_EVIDENCE),
        transform=transform,
    )


def _blocked_value(target_id: str) -> _AuditTarget:
    return _blocked_term(
        target_id,
        reason="OptionReg现金流或本金偿付的公平报价基准尚未完成产品级核验",
    )


def _blocked_constraint(target_id: str, controlled: tuple[str, ...], relation: str) -> _AuditTarget:
    return _blocked_term(
        target_id,
        controlled=controlled,
        reason="目标参与产品等式约束，尚未登记完整耦合报价变换",
        transform={"kind": "registered_constraint_relation", "relation": relation, "controlled_term_keys": list(controlled)},
    )


def _supported_contract_target(
    product_id: str,
    target_id: str,
    cashflow_symbol: str,
    *,
    exposure_rule: str,
    negative_branch: str,
    coefficient_sign: str = "positive",
    solver_class: str = "affine",
    uniqueness: str | None = None,
    allowed_methods: tuple[str, ...] = ("monte_carlo",),
) -> _AuditTarget:
    """Register an incremental-PnL target only with explicit cashflow proof."""
    return _target(
        target_id,
        "supported",
        quote_basis="incremental_net_value",
        quote_value_basis="pv_percent",
        quote_target_source="fixed_zero_incremental_net_value",
        allowed_methods=allowed_methods,
        solver_class=solver_class,
        uniqueness=uniqueness or "产品完整现金流对目标为仿射关系，目标暴露存在时解唯一",
        state_applicability_rule=_NEW_ISSUANCE_STATE,
        cashflow_evidence={
            "kind": "incremental_cashflow_affine",
            "product_id": product_id,
            "target_symbol": cashflow_symbol,
            "exposure_rule": exposure_rule,
            "coefficient_sign": coefficient_sign,
            "negative_branch": negative_branch,
            "public_conversion": "decimal_ratio_to_percentage",
        },
        runtime_identifiability_rule={
            "kind": "runtime_path_exposure_coefficient",
            "coefficient_nonzero": True,
            "scope": "aggregate_discounted_paths",
            "condition": "冻结市场与路径批次下目标折现现金流系数绝对值严格大于零",
            "metric": "aggregate_discounted_cashflow_derivative",
            "failure_status": "zero_slope",
            "failure_eligibility": "ineligible",
        },
        evidence=(
            _OPTIONREG_EVIDENCE,
            _VALUATION_ROUTE_EVIDENCE,
            _VALUATION_TEST_EVIDENCE,
            _CASHFLOW_EVIDENCE,
        ),
    )


def _supported_monotone_target(
    product_id: str,
    target_id: str,
    cashflow_symbol: str,
    *,
    monotonicity_rule: str,
    negative_branch: str,
    coefficient_sign: str = "negative",
    uniqueness: str | None = None,
    allowed_methods: tuple[str, ...] = ("monte_carlo",),
) -> _AuditTarget:
    """Register a bounded target only with a pathwise monotonicity proof."""
    return _target(
        target_id,
        "supported",
        quote_basis="incremental_net_value",
        quote_value_basis="pv_percent",
        quote_target_source="fixed_zero_incremental_net_value",
        allowed_methods=allowed_methods,
        solver_class="bounded_monotone",
        uniqueness=uniqueness or "合法域内报价价值严格单调，且运行时存在非零目标暴露时解唯一",
        state_applicability_rule=_NEW_ISSUANCE_STATE,
        cashflow_evidence={
            "kind": "bounded_monotone_cashflow",
            "product_id": product_id,
            "target_symbol": cashflow_symbol,
            "monotonicity": monotonicity_rule,
            "coefficient_sign": coefficient_sign,
            "negative_branch": negative_branch,
            "boundary_rule": "候选域端点和每个有效路径分支均由Core合同约束重算",
            "public_conversion": "decimal_ratio_to_percentage",
        },
        runtime_identifiability_rule={
            "kind": "runtime_pathwise_monotonicity",
            "coefficient_nonzero": True,
            "scope": "aggregate_discounted_paths",
            "condition": "冻结市场与路径批次下目标暴露概率非零且有限域内方向严格一致",
            "metric": "discounted_pathwise_slope_or_monotone_difference",
            "failure_status": "zero_slope",
            "failure_eligibility": "ineligible",
        },
        evidence=(
            _OPTIONREG_EVIDENCE,
            _VALUATION_ROUTE_EVIDENCE,
            _VALUATION_TEST_EVIDENCE,
            _CASHFLOW_EVIDENCE,
        ),
    )


def _derive_product_status(targets: tuple[_AuditTarget, ...]) -> str:
    statuses = {target.support_status for target in targets}
    if "supported" in statuses:
        return "supported"
    if "solve_semantics_blocked" in statuses:
        return "solve_semantics_blocked"
    return "valuation_only"


def _product(product_id: str, declared_status: str, *targets: _AuditTarget) -> _AuditProduct:
    target_tuple = tuple(targets)
    derived_status = _derive_product_status(target_tuple)
    if declared_status != derived_status:
        raise ValueError(
            f"产品{product_id}手写汇总状态{declared_status}与目标状态推导值{derived_status}不一致"
        )
    return _AuditProduct(
        product_id,
        derived_status,
        target_tuple,
        (_OPTIONREG_EVIDENCE, _VALUATION_ROUTE_EVIDENCE, _VALUATION_TEST_EVIDENCE),
    )


# ---------------------------------------------------------------------------
# Explicit product-by-product audit. Changing OptionReg products must trigger
# a deliberate review instead of silently changing inverse capability.
# ---------------------------------------------------------------------------
_AUDIT_PRODUCTS: tuple[_AuditProduct, ...] = (
    _product("1.1", "supported", _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("1.2", "supported", _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("2.1", "supported", _unsupported_term("K1"), _unsupported_term("K2"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("2.2", "supported", _unsupported_term("K1"), _unsupported_term("K2"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("2.3", "supported", _unsupported_term("K1"), _unsupported_term("K2"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("2.4", "supported", _unsupported_term("K1"), _unsupported_term("K2"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("3.1", "supported", _unsupported_term("K"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("3.2", "supported", _unsupported_term("Kp"), _unsupported_term("Kc"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product(
        "3.3", "supported",
        _blocked_constraint("K1", ("K1", "K2", "K3"), "2 * K2 == K1 + K3"),
        _blocked_constraint("K2", ("K1", "K2", "K3"), "2 * K2 == K1 + K3"),
        _blocked_constraint("K3", ("K1", "K2", "K3"), "2 * K2 == K1 + K3"),
        _supported_premium("P_net", cashflow_symbol="P_net"),
    ),
    _product("3.4", "supported", _unsupported_term("K1"), _unsupported_term("K2"), _unsupported_term("K3"), _unsupported_term("K4"), _supported_premium("P_net", cashflow_symbol="P_net")),
    _product("4.1", "supported", _unsupported_term("H_KO"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.2", "supported", _unsupported_term("H_KO"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.3", "supported", _unsupported_term("H_KI"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.4", "supported", _unsupported_term("H_KI"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.5", "supported", _unsupported_term("H_KO"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.6", "supported", _unsupported_term("H_KO"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.7", "supported", _unsupported_term("H_KI"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("4.8", "supported", _unsupported_term("H_KI"), _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.1", "supported", _unsupported_term("K"), _unsupported_term("A"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.2", "supported", _unsupported_term("K"), _unsupported_term("A"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.3", "supported", _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.4", "supported", _unsupported_term("K"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.5", "supported", _unsupported_term("Htouch"), _unsupported_term("A"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("5.6", "supported", _unsupported_term("Htouch"), _unsupported_term("A"), _supported_premium("Pi_0", cashflow_symbol="P")),
    _product("6.1", "supported", _unsupported_term("B"), _unsupported_term("g"), _supported_premium("p", cashflow_symbol="p")),
    _product(
        "6.2", "valuation_only",
        _unsupported_term("B"), _unsupported_term("g"), _unsupported_term("r_cap"),
        _unsupported_term("p", reason="产品约束固定p == 0且现金流只有非负未来收益，不能构造零合同价值报价目标"),
    ),
    _product(
        "6.3", "valuation_only",
        _unsupported_term("B"), _unsupported_term("g"),
        _unsupported_term("alpha", reason="现金流只有非负未来参与收益，不能以零合同价值证明alpha的公平报价解"),
        _unsupported_term("p", reason="产品约束固定p == 0且现金流只有非负未来收益，不能构造零合同价值报价目标"),
    ),
    _product("7.1", "valuation_only", _unsupported_term("K"), _unsupported_term("H_KO")),
    _product("8.1", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.1", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入后r_T可为负")),
    _product("8.2", "supported", _unsupported_term("g"), _supported_monotone_target("8.2", "K", "K", monotonicity_rule="在0<K<S_0合法域内，未敲出敲入损失随K严格递减", negative_branch="敲入分支S_T-K可为负", coefficient_sign="negative", uniqueness="固定路径与市场状态后，存在非零敲入损失暴露时PV对K严格递减，解唯一"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.2", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入后S_T-K可为负")),
    _product("8.3", "supported", _unsupported_term("g"), _supported_monotone_target("8.3", "K", "K", monotonicity_rule="在0<K<S_0合法域内，未敲出敲入损失随K严格递减", negative_branch="敲入分支S_T-K可为负", coefficient_sign="negative", uniqueness="固定路径与市场状态后，存在非零敲入损失暴露时PV对K严格递减，解唯一"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.3", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入后S_T-K可为负")),
    _product("8.4", "supported", _unsupported_term("g"), _supported_monotone_target("8.4", "K", "K", monotonicity_rule="在0<K<S_0合法域内，敲入终值S_T/K-1随K严格递减", negative_branch="敲入分支S_T-K可为负", coefficient_sign="negative", uniqueness="固定路径与市场状态后，存在非零敲入路径时PV对K严格递减，解唯一"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.4", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支S_T-K可为负")),
    _product("8.5", "supported", _unsupported_term("g", reason="约束固定g == 1，属于产品身份参数"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.5", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支min((S_T-S_0)/S_0,0)为非正且可为负")),
    _product("8.6", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.6", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支含r_T和H_floor/S_0-1负损失")),
    _product("8.7", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI_1"), _unsupported_term("H_KI_2"), _supported_contract_target("8.7", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="变化敲入后r_T可为负")),
    _product("8.8", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.8", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支min((S_T-S_0)/S_0,0)为非正且可为负")),
    _product("8.9", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KI"), _supported_contract_target("8.9", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支min((S_T-S_0)/S_0,0)为非正且可为负")),
    _product("8.10", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO_regular"), _unsupported_term("H_KO_final"), _unsupported_term("H_KI"), _supported_contract_target("8.10", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="最终敲入分支min((S_T-S_0)/S_0,0)为非正且可为负")),
    _product("8.11", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.11", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支r_T可为负"), _supported_monotone_target("8.11", "alpha", "alpha", monotonicity_rule="敲出路径的S_out-H_out非负，alpha在合法域内不减且存在正暴露", negative_branch="敲入分支r_T提供负值路径", coefficient_sign="positive", uniqueness="固定路径与市场状态后，存在敲出超额价格暴露时PV对alpha严格递增，解唯一")),
    _product("8.12", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.12", "c1", "c_1", exposure_rule="n_out <= n_switch的合法敲出路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="敲入后min((S_T-S_0)/S_0,0)为非正且可为负"), _supported_contract_target("8.12", "c2", "c_2", exposure_rule="迟敲出及未敲出合法路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="敲入后min((S_T-S_0)/S_0,0)为非正且可为负")),
    _product("8.13", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.13", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支含r_T和H_floor/S_0-1负损失")),
    _product("8.14", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.14", "c", "c", exposure_rule="未重置或重置前阶段存在c票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支min((S_T-S_0)/S_0,0)为非正且可为负"), _supported_contract_target("8.14", "c_reset", "c_reset", exposure_rule="重置后敲出合法路径存在c_reset暴露，运行时按路径聚合确认非零", negative_branch="敲入分支min((S_T-S_0)/S_0,0)为非正且可为负"), _unsupported_term("Hreset")),
    _product("8.15", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.15", "c", "c", exposure_rule="非对冲合法路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支r_T可为负"), _supported_contract_target("8.15", "c_hedge", "c_hedge", exposure_rule="对冲先于敲出的合法路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="未触发对冲的敲入分支r_T可为负")),
    _product("8.16", "supported", _blocked_constraint("H_KO", ("H_KO",), "H_out == 1.03 * S_0"), _supported_contract_target("8.16", "c1", "c_1", exposure_rule="敲出路径对c1有正暴露", negative_branch="期初p支付提供负现金流"), _blocked_constraint("c2", ("c2",), "c_2 == 0"), _supported_contract_target("8.16", "p", "p", exposure_rule="每条路径的期初支付扣除未来返还后仍为严格负暴露", negative_branch="期初-N*p*T为负且敲出返还不足以抵消", coefficient_sign="negative")),
    _product("8.17", "valuation_only", _unsupported_term("g", reason="约束固定g == 1，属于产品身份参数"), _unsupported_term("H_KO", reason="约束固定H_out == 1.03 * S_0，属于产品身份参数"), _unsupported_term("c1", reason="约束固定c_1 == 0.065，属于产品身份参数"), _unsupported_term("c2", reason="约束固定c_2 == 0.0055，属于产品身份参数")),
    _product("8.18", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.18", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="Worst-Of终值W_T/100-1可为负")),
    _product("8.19", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.19", "c", "c", exposure_rule="存在月度票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支S_T/K-1可为负"), _blocked_constraint("Hc", ("Hc", "H_KI"), "H_c == H_in")),
    _product("8.20", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.20", "c", "c", exposure_rule="存在月度票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支S_T/K-1或F-1可为负"), _blocked_constraint("Hc", ("Hc", "H_KI", "Hfloor", "F"), "H_c == H_in == H_floor == F * S_0"), _blocked_constraint("Hfloor", ("Hc", "H_KI", "Hfloor", "F"), "H_c == H_in == H_floor == F * S_0"), _blocked_constraint("F", ("Hc", "H_KI", "Hfloor", "F"), "H_c == H_in == H_floor == F * S_0")),
    _product("8.21", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _supported_contract_target("8.21", "c", "c", exposure_rule="合法路径存在正票息暴露，运行时按路径聚合确认非零", negative_branch="未敲出分支S_T/K-1可为负")),
    _product("8.22", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.22", "c", "c", exposure_rule="存在票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支S_T/K-1可为负")),
    _product("8.23", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.23", "c", "c", exposure_rule="存在票息暴露，运行时按路径聚合确认非零", negative_branch="敲入分支S_T/K-1或F-1可为负"), _blocked_constraint("Hfloor", ("Hfloor", "H_KI", "F"), "H_in == H_floor == F * S_0"), _blocked_constraint("F", ("Hfloor", "H_KI", "F"), "H_in == H_floor == F * S_0")),
    _product("8.24", "supported", _unsupported_term("g", reason="约束固定g == 0.2，属于产品身份参数"), _supported_contract_target("8.24", "c", "c", exposure_rule="存在票息暴露，运行时按路径聚合确认非零", negative_branch="未敲出分支S_T/S_0-1可为负"), _unsupported_term("Hc", reason="约束固定H_c == 0.8 * S_0，属于产品身份参数")),
    _product("8.25", "supported", _unsupported_term("g", reason="约束固定g == 0.2，属于产品身份参数"), _supported_contract_target("8.25", "c", "c", exposure_rule="存在票息暴露，运行时按路径聚合确认非零", negative_branch="未敲出分支max(S_T/S_0-1,F-1)可为负"), _unsupported_term("Hc", reason="约束固定H_c == F * S_0，属于产品身份参数"), _unsupported_term("F", reason="约束固定F == 0.8，属于产品身份参数")),
    _product("8.26", "supported", _unsupported_term("g"), _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("H_KI"), _supported_contract_target("8.26", "r_out", "r_out", exposure_rule="敲出路径对r_out有正暴露", negative_branch="未敲入到期分支S_T/K-1可为负"), _supported_contract_target("8.26", "r_mat", "r_mat", exposure_rule="未敲出且未敲入到期路径对r_mat有正暴露", negative_branch="敲入分支S_T/K-1可为负")),
    _product("8.27", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _supported_contract_target("8.27", "c", "c", exposure_rule="合法敲出路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="未敲出缓冲收益在低S_T时可为负"), _blocked_term("Bbuf", reason="固定MC路径下S_T与K_buf分支切换产生阶跃并存在平台，尚未证明连续严格单调唯一")),
    _product("8.28", "supported", _unsupported_term("g"), _unsupported_term("H_KO"), _supported_contract_target("8.28", "c", "c", exposure_rule="合法敲出路径存在票息暴露，运行时按路径聚合确认非零", negative_branch="未敲出分支包含r_T和负限损项"), _supported_monotone_target("8.28", "Lmax", "ell", monotonicity_rule="在0<=ell<=1合法域内，限损分支随ell严格递减且阈值由Core重算", negative_branch="未敲出低位分支为-N*ell", coefficient_sign="negative", uniqueness="固定路径与市场状态后，存在低位限损暴露时PV对ell严格递减，解唯一")),
    _product("8.29", "supported", _unsupported_term("g", reason="约束固定g == 0.2，属于产品身份参数"), _unsupported_term("K", reason="约束固定K == S_0，属于产品身份参数"), _unsupported_term("H_KO", reason="约束固定H_out == 1.03 * S_0，属于产品身份参数"), _supported_contract_target("8.29", "r_out", "r_out", exposure_rule="合法敲出路径存在r_out暴露，运行时按路径聚合确认非零", negative_branch="未敲出分支含r_T和负限损项"), _blocked_term("alpha", reason="alpha乘以r_T，路径系数可正可负，尚未证明严格单调"), _supported_monotone_target("8.29", "Lmax", "ell", monotonicity_rule="在0<=ell<=1合法域内，限损分支随ell严格递减且阈值由Core重算", negative_branch="未敲出低位分支为-N*ell", coefficient_sign="negative", uniqueness="固定路径与市场状态后，存在低位限损暴露时PV对ell严格递减，解唯一")),
    _product("9.1", "supported", _unsupported_term("K"), _supported_premium("p", cashflow_symbol="p")),
    _product("9.2", "supported", _unsupported_term("g"), _unsupported_term("p", reason="产品约束固定p == 0，属于产品身份参数"), _supported_monotone_target("9.2", "K1", "K_1", monotonicity_rule="在0<K_1<K_2合法域内，低于K1的固定损失随K1严格上移", negative_branch="低于K1分支为-N*(S_0-K_1)/S_0", coefficient_sign="positive", uniqueness="固定路径与市场状态后，存在非零低位损失概率时PV对K1严格递增，解唯一", allowed_methods=("analytical", "monte_carlo")), _unsupported_term("K2", reason="约束固定K_2 == S_0，属于产品身份参数"), _blocked_term("alpha", reason="alpha仅参与未来r_T收益，缺少不受市场状态影响的严格单调证据")),
    _product(
        "9.3",
        "supported",
        _blocked_constraint("K1", ("K1", "p"), "p == (S_0 - K_1) / S_0"),
        _blocked_constraint("K2", ("K2",), "K_2 == S_0"),
        _unsupported_term("alpha"),
        _supported_premium("p", cashflow_symbol="p"),
    ),
    _product("9.4", "solve_semantics_blocked", _blocked_term("Ksig", quote_value_basis="variance_percent", reason="方差互换必须保留variance_percent；已实现方差状态、结算目标和唯一报价证据尚未登记")),
    _product("9.5", "supported", _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("eta"), _supported_premium("p", cashflow_symbol="p")),
    _product("9.6", "supported", _unsupported_term("K"), _unsupported_term("H_KO"), _unsupported_term("eta"), _supported_premium("p", cashflow_symbol="p")),
    _product("9.7", "supported", _unsupported_term("Ku"), _unsupported_term("Kd"), _unsupported_term("H_KO_2"), _unsupported_term("H_KO_1"), _unsupported_term("eta_u"), _unsupported_term("eta_d"), _unsupported_term("alpha_u"), _unsupported_term("alpha_d"), _supported_premium("p", cashflow_symbol="p", allowed_methods=("monte_carlo",))),
    _product("9.8", "supported", _unsupported_term("Hlow"), _unsupported_term("Hup"), _unsupported_term("c_max"), _supported_premium("p", cashflow_symbol="p")),
)


@dataclass(frozen=True)
class TargetCapability:
    """Full internal declaration, including fields hidden from public clients."""

    target_id: str
    display_name: str
    symbol: str
    value_encoding: str
    display_unit: str
    allowed_methods: tuple[str, ...]
    quote_basis: str
    quote_value_basis: str
    quote_target_source: str
    quote_target_descriptor: Mapping[str, Any]
    support_status: str
    unsupported_reason: str | None
    controlled_term_keys: tuple[str, ...]
    fixed_dependencies: tuple[str, ...]
    internal_value_basis: Mapping[str, Any]
    domain: Mapping[str, Any]
    search_bracket_policy: Mapping[str, Any]
    runtime_identifiability_rule: Mapping[str, Any]
    solver_class: str
    monotonic_direction: str | None
    uniqueness_rule: str
    state_applicability_rule: Mapping[str, Any]
    valuation_perspective: str | None
    cashflow_sign_convention: Mapping[str, Any] | None
    initial_pricing_time_rule: Mapping[str, Any] | None
    schedule_impact_rule: Mapping[str, Any]
    cashflow_evidence: Mapping[str, Any] | None
    transform_rule: Mapping[str, Any] | None
    convergence_rule: Mapping[str, Any]
    precision_rule: Mapping[str, Any]
    evidence_status: str
    evidence: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return self.support_status

    def to_solver_spec(self) -> dict[str, Any]:
        """Return the complete target rule consumed by the current solve."""
        return {
            "schema_id": CAPABILITY_SCHEMA_ID,
            "target_id": self.target_id,
            "display_name": self.display_name,
            "symbol": self.symbol,
            "value_encoding": self.value_encoding,
            "display_unit": self.display_unit,
            "allowed_methods": self.allowed_methods,
            "quote_basis": self.quote_basis,
            "quote_value_basis": self.quote_value_basis,
            "quote_target_source": self.quote_target_source,
            "quote_target_descriptor": self.quote_target_descriptor,
            "support_status": self.support_status,
            "controlled_term_keys": self.controlled_term_keys,
            "fixed_dependencies": self.fixed_dependencies,
            "internal_value_basis": self.internal_value_basis,
            "domain": self.domain,
            "search_bracket_policy": self.search_bracket_policy,
            "runtime_identifiability_rule": self.runtime_identifiability_rule,
            "solver_class": self.solver_class,
            "monotonic_direction": self.monotonic_direction,
            "uniqueness_rule": self.uniqueness_rule,
            "state_applicability_rule": self.state_applicability_rule,
            "valuation_perspective": self.valuation_perspective,
            "cashflow_sign_convention": self.cashflow_sign_convention,
            "initial_pricing_time_rule": self.initial_pricing_time_rule,
            "schedule_impact_rule": self.schedule_impact_rule,
            "cashflow_evidence": self.cashflow_evidence,
            "transform_rule": self.transform_rule,
            "convergence_rule": self.convergence_rule,
            "precision_rule": self.precision_rule,
            "evidence_status": self.evidence_status,
            "evidence": self.evidence,
            "evidence_ids": self.evidence_ids or self.evidence,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "display_name": self.display_name,
            "symbol": self.symbol,
            "value_encoding": self.value_encoding,
            "display_unit": self.display_unit,
            "allowed_methods": list(self.allowed_methods),
            "quote_basis": self.quote_basis,
            "quote_value_basis": self.quote_value_basis,
            "quote_target_descriptor": deepcopy(dict(self.quote_target_descriptor)),
            "support_status": self.support_status,
            "unsupported_reason": self.unsupported_reason,
        }


@dataclass(frozen=True)
class ProductCapabilityRecord:
    product_id: str
    rule_revision: int
    canonical_name: str
    valuation_methods: tuple[str, ...]
    solve_status: str
    targets: tuple[TargetCapability, ...]
    audit_evidence: tuple[str, ...]

    @property
    def fair_parameter_status(self) -> str:
        return self.solve_status

    @property
    def supported_targets(self) -> tuple[TargetCapability, ...]:
        return tuple(item for item in self.targets if item.support_status == "supported")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "rule_revision": self.rule_revision,
            "canonical_name": self.canonical_name,
            "valuation_methods": list(self.valuation_methods),
            "fair_parameter_status": self.solve_status,
            "targets": [item.to_public_dict() for item in self.targets],
        }


def _methods(product_id: str, product: Mapping[str, Any]) -> tuple[str, ...]:
    declared = product.get("terms", {}).get("pricing_methods", ())
    valuation = valuation_capability_for(product_id)
    if valuation is None:
        return ()
    return tuple(method for method in _PUBLIC_METHODS if method in declared and method in valuation.methods)


def _solver_search_rule(
    audit: _AuditTarget,
    domain_envelope: Mapping[str, Any],
    monotonic_direction: str | None,
) -> dict[str, Any]:
    rule = deepcopy(_SOLVER_SEARCH_DEFAULT)
    rule.update({
        "target_id": audit.target_id,
        "solver_class": audit.solver_class,
        "source": "PricerTargetRule",
        "domain_envelope": deepcopy(dict(domain_envelope)),
        "initial_interval_source": "target_domain_intersection_with_resolved_contract_constraints",
        "expansion_cap_source": "registered_solver_rule.expansion.max_expansions",
        "runtime_requires_domain_containment": True,
        "runtime_requires_nonzero_coefficient": audit.support_status == "supported",
        "monotonic_direction": monotonic_direction,
    })
    # Keep the executable controls visible at the policy root as well as in
    # their grouped form.  This makes the spec unambiguous to a direct solver
    # while retaining a readable nested representation for audit consumers.
    rule["expansion_factor"] = rule["expansion"]["factor"]
    rule["max_expansions"] = rule["expansion"]["max_expansions"]
    rule["max_bracket_expansions"] = rule["expansion"]["max_expansions"]
    return rule


def _monotonic_direction(cashflow_evidence: Mapping[str, Any] | None) -> str | None:
    """Derive the executable direction from the audited coefficient sign.

    A direction is deliberately not inferred from a sampled solver slope.  A
    supported row without an independent sign certificate is a directory
    error, rather than an invitation for the runtime to guess.
    """
    if not isinstance(cashflow_evidence, Mapping):
        return None
    sign = str(cashflow_evidence.get("coefficient_sign", "")).strip().lower()
    if sign in {"positive", "+", "increasing"}:
        return "increasing"
    if sign in {"negative", "-", "decreasing"}:
        return "decreasing"
    return None


def _cashflow_sign_convention(
    cashflow_evidence: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Compile audited cashflow evidence into a machine-readable sign rule.

    ``coefficient_sign`` is already a required field in every supported audit
    row.  This function is deliberately the only conversion from that audit
    evidence into the rule consumed by Core and the future solver; no sign is
    sampled or inferred from a numerical slope.
    """

    if not isinstance(cashflow_evidence, Mapping):
        return None
    target_symbol = cashflow_evidence.get("target_symbol")
    coefficient_sign = str(cashflow_evidence.get("coefficient_sign", "")).strip().lower()
    direction = {"positive": 1, "negative": -1}.get(coefficient_sign)
    if not isinstance(target_symbol, str) or not target_symbol.strip() or direction is None:
        raise ValueError("支持目标现金流证据必须包含合法target_symbol和coefficient_sign")
    evidence_kind = cashflow_evidence.get("kind")
    if not isinstance(evidence_kind, str) or not evidence_kind.strip():
        raise ValueError("支持目标现金流证据必须包含kind")
    return {
        "schema_id": "optionhelper.pricer.cashflow-sign-convention",
        "valuation_perspective": _VALUATION_PERSPECTIVE,
        "holder_cashflow_signs": {
            "received": 1,
            "paid": -1,
        },
        "target_coefficient_sign": coefficient_sign,
        "target_coefficient": {
            "symbol": target_symbol,
            "sign": coefficient_sign,
            "direction": direction,
        },
        "residual": {
            "operation": "subtract",
            "left": "V",
            "right": "Q",
            "equals": 0.0,
        },
        "source": {
            "kind": evidence_kind,
            "target_symbol": target_symbol,
            "coefficient_sign": coefficient_sign,
        },
    }


def _solver_convergence_rule(audit: _AuditTarget) -> dict[str, Any]:
    rule = deepcopy(_SOLVER_CONVERGENCE_DEFAULT)
    rule.update({
        "target_id": audit.target_id,
        "solver_class": audit.solver_class,
        "residual_tolerance_source": "registered_solver_rule.residual_tolerance",
        "parameter_tolerance_source": "registered_solver_rule.parameter_tolerance",
    })
    return rule


def _solver_precision_rule(audit: _AuditTarget, methods: tuple[str, ...]) -> dict[str, Any]:
    return {
        "rule_version": _SOLVER_RULE_VERSION,
        "target_id": audit.target_id,
        "solver_class": audit.solver_class,
        "allowed_methods": tuple(methods),
        "quote_gate": "solution_and_final_valuation",
        # No model-risk or slope constants are fabricated here.  A trusted
        # runtime proof may provide them to deterministic_uncertainty; absent
        # that proof the solve can finish but the quote gate stays closed.
        "analytical": deepcopy(_SOLVER_ANALYTICAL_PRECISION),
        "monte_carlo": deepcopy(_SOLVER_MC_PRECISION),
    }


def _target_from_audit(
    audit: _AuditTarget,
    *,
    product: Mapping[str, Any],
    catalog: Mapping[str, Any],
    methods: tuple[str, ...],
) -> TargetCapability:
    evidence_ids = tuple(audit.evidence)
    metadata = catalog.get(audit.target_id)
    if not isinstance(metadata, Mapping):
        raise ValueError(f"反解能力目录目标{audit.target_id}不在term_catalog")
    terms = product.get("terms", {})
    if audit.target_id not in terms:
        raise ValueError(f"反解能力目录目标{audit.target_id}不属于产品terms")
    if audit.support_status not in _TARGET_STATUSES:
        raise ValueError(f"反解能力目录状态无效：{audit.support_status}")
    if audit.quote_basis not in _QUOTE_BASES:
        raise ValueError(f"目标{audit.target_id}缺少有效报价基准")
    if audit.quote_value_basis not in _QUOTE_VALUE_BASES:
        raise ValueError(f"目标{audit.target_id}缺少有效quote_value_basis")
    if not audit.quote_target_source:
        raise ValueError(f"目标{audit.target_id}缺少quote_target_source")
    if not isinstance(audit.state_applicability_rule, Mapping) or not audit.state_applicability_rule:
        raise ValueError(f"目标{audit.target_id}缺少state_applicability_rule")
    if (
        not isinstance(audit.schedule_impact_rule, Mapping)
        or audit.schedule_impact_rule.get("kind") != "no_time_dimension_change"
    ):
        raise ValueError(f"目标{audit.target_id}缺少no_time_dimension_change规则")
    requested_methods = tuple(dict.fromkeys(str(method) for method in audit.allowed_methods))
    if any(method not in _PUBLIC_METHODS for method in requested_methods):
        raise ValueError(f"目标{audit.target_id}含未公开的定价方法")
    if any(method not in methods for method in requested_methods):
        raise ValueError(f"目标{audit.target_id}声明的方法不在产品估值能力内")
    if audit.support_status == "supported":
        if not requested_methods:
            raise ValueError(f"supported目标{audit.target_id}缺少逐目标公开定价方法")
        if not audit.cashflow_evidence:
            raise ValueError(f"supported目标{audit.target_id}缺少现金流/代数证据")
        if not audit.runtime_identifiability_rule:
            raise ValueError(f"supported目标{audit.target_id}缺少运行时可辨识性规则")
    elif requested_methods:
        raise ValueError(f"非supported目标{audit.target_id}不得登记可用定价方法")
    fixed = tuple(sorted(set(str(key) for key in terms) - set(audit.controlled_term_keys)))
    term_domain = deepcopy(dict(metadata.get("domain", {})))
    domain_envelope = {
        **term_domain,
        "contract_constraints": [str(item) for item in terms.get("constraints", ())],
    }
    catalog_unit = str(metadata.get("unit", "")).strip()
    if catalog_unit not in _CATALOG_UNITS:
        raise ValueError(f"目标{audit.target_id}的catalog unit未登记：{catalog_unit}")
    monotonic_direction = _monotonic_direction(audit.cashflow_evidence)
    if audit.support_status == "supported" and monotonic_direction is None:
        raise ValueError(f"supported目标{audit.target_id}缺少现金流系数方向证据")
    public_transform = _UNIT_PUBLIC_TRANSFORMS.get(catalog_unit, "not_applicable_non_continuous")
    descriptor = {
        "source": audit.quote_target_source,
        "quote_basis": audit.quote_basis,
        "quote_value_basis": audit.quote_value_basis,
        "value_encoding": "decimal_ratio",
        "target_value": 0.0 if audit.support_status == "supported" else None,
    }
    display_name = str(metadata.get("name_zh", audit.target_id))
    # OptionReg stores some terms as points internally.  The shared public
    # projection is percentage-only, so its labels must not expose that
    # internal unit either.
    display_name = display_name.replace("点数", "百分比")
    return TargetCapability(
        target_id=audit.target_id,
        display_name=display_name,
        symbol=str(metadata.get("symbol", audit.target_id)),
        value_encoding="decimal_ratio",
        display_unit="percentage",
        allowed_methods=requested_methods,
        quote_basis=audit.quote_basis,
        quote_value_basis=audit.quote_value_basis,
        quote_target_source=audit.quote_target_source,
        quote_target_descriptor=descriptor,
        support_status=audit.support_status,
        unsupported_reason=audit.unsupported_reason,
        controlled_term_keys=audit.controlled_term_keys,
        fixed_dependencies=fixed,
        internal_value_basis={
            "term_key": audit.target_id,
            "catalog_unit": catalog_unit,
            "public_scale": "decimal_ratio",
            "public_transform": public_transform,
            "public_conversion": public_transform,
            "reference_price_basis_rule": (
                "freeze_reference_price_basis"
                if catalog_unit == "price"
                else "not_applicable"
            ),
            "quote_value_basis": audit.quote_value_basis,
            "cashflow_evidence": None if audit.cashflow_evidence is None else deepcopy(dict(audit.cashflow_evidence)),
        },
        domain=deepcopy(dict(metadata.get("domain", {}))),
        search_bracket_policy=_solver_search_rule(audit, domain_envelope, monotonic_direction),
        runtime_identifiability_rule=deepcopy(dict(audit.runtime_identifiability_rule or {})),
        solver_class=audit.solver_class,
        monotonic_direction=monotonic_direction,
        uniqueness_rule=audit.uniqueness_rule,
        state_applicability_rule=deepcopy(dict(audit.state_applicability_rule)),
        valuation_perspective=(
            _VALUATION_PERSPECTIVE if audit.support_status == "supported" else None
        ),
        cashflow_sign_convention=(
            _cashflow_sign_convention(audit.cashflow_evidence)
            if audit.support_status == "supported"
            else None
        ),
        initial_pricing_time_rule=(
            deepcopy(_INITIAL_PRICING_TIME_RULE)
            if audit.support_status == "supported"
            else None
        ),
        schedule_impact_rule=deepcopy(dict(audit.schedule_impact_rule)),
        cashflow_evidence=None if audit.cashflow_evidence is None else deepcopy(dict(audit.cashflow_evidence)),
        transform_rule=None if audit.transform_rule is None else deepcopy(dict(audit.transform_rule)),
        convergence_rule=_solver_convergence_rule(audit),
        precision_rule=_solver_precision_rule(audit, requested_methods),
        evidence_status=(
            "complete" if audit.support_status == "supported"
            else ("blocked" if audit.support_status == "solve_semantics_blocked" else "unverified")
        ),
        evidence=evidence_ids,
        evidence_ids=evidence_ids,
    )


def _validate_audit_table(registry: Mapping[str, Any]) -> None:
    product_ids = set(registry.get("products", {}))
    audit_ids = [item.product_id for item in _AUDIT_PRODUCTS]
    if len(audit_ids) != len(set(audit_ids)) or set(audit_ids) != product_ids:
        raise ValueError("Pricer反解能力矩阵必须逐一覆盖当前OptionReg的65个产品且不得重复")
    term_catalog = registry.get("term_catalog", {})

    def validate_evidence_ids(evidence_ids: tuple[str, ...], context: str) -> None:
        if not evidence_ids:
            raise ValueError(f"{context}缺少逻辑证据id")
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or evidence_id not in _EVIDENCE_IDS:
                raise ValueError(f"{context}引用未知逻辑证据id：{evidence_id}")

    for item in _AUDIT_PRODUCTS:
        product = registry["products"][item.product_id]
        if item.solve_status not in _PRODUCT_STATUSES:
            raise ValueError(f"产品{item.product_id}汇总状态无效：{item.solve_status}")
        derived_status = _derive_product_status(item.targets)
        if item.solve_status != derived_status:
            raise ValueError(f"产品{item.product_id}汇总状态与目标状态不一致")
        targets = [target.target_id for target in item.targets]
        if len(targets) != len(set(targets)):
            raise ValueError(f"产品{item.product_id}反解目标重复")
        if not item.targets:
            raise ValueError(f"产品{item.product_id}缺少反解审计目标记录")
        validate_evidence_ids(item.audit_evidence, f"产品{item.product_id}审计证据")
        for target in item.targets:
            if target.support_status not in _TARGET_STATUSES:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}状态无效：{target.support_status}")
            if target.target_id not in product.get("terms", {}):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}不属于OptionReg terms")
            if target.target_id not in term_catalog:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少term_catalog证据")
            if not isinstance(term_catalog[target.target_id].get("domain"), Mapping):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少合法域证据")
            catalog_unit = str(term_catalog[target.target_id].get("unit", "")).strip()
            if catalog_unit not in _CATALOG_UNITS:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}的catalog unit未登记：{catalog_unit}")
            if not set(target.controlled_term_keys).issubset(set(product.get("terms", {}))):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}的受控字段不属于terms")
            if target.quote_basis not in _QUOTE_BASES or target.quote_value_basis not in _QUOTE_VALUE_BASES:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}报价口径无效")
            if not target.quote_target_source or not target.state_applicability_rule:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少审计字段")
            if (
                not target.schedule_impact_rule
                or target.schedule_impact_rule.get("kind") != "no_time_dimension_change"
            ):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少时间维度规则")
            state = target.state_applicability_rule
            if any(state.get(key) != "not_allowed" for key in ("surviving", "triggered", "terminated")):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}只允许new_issuance状态")
            if state.get("new_issuance") not in {"allowed", "not_registered"}:
                raise ValueError(f"产品{item.product_id}目标{target.target_id}的new_issuance状态无效")
            declared_methods = tuple(dict.fromkeys(str(method) for method in target.allowed_methods))
            if any(method not in _PUBLIC_METHODS for method in declared_methods):
                raise ValueError(f"产品{item.product_id}目标{target.target_id}含未公开的定价方法")
            validate_evidence_ids(
                target.evidence,
                f"产品{item.product_id}目标{target.target_id}证据",
            )
            if target.support_status == "supported":
                if not target.allowed_methods:
                    raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少逐目标方法登记")
                if not target.cashflow_evidence or not target.runtime_identifiability_rule:
                    raise ValueError(f"产品{item.product_id}目标{target.target_id}缺少现金流或运行时辨识证据")
            elif target.allowed_methods:
                raise ValueError(f"产品{item.product_id}非supported目标{target.target_id}不得登记方法")
        if item.solve_status == "supported" and not any(target.support_status == "supported" for target in item.targets):
            raise ValueError(f"产品{item.product_id}汇总supported但没有supported目标")


@lru_cache(maxsize=1)
def capability_matrix() -> tuple[ProductCapabilityRecord, ...]:
    """Return the explicit, complete 65-product audit matrix."""
    registry = load_registry()
    _validate_audit_table(registry)
    term_catalog = registry["term_catalog"]
    products: list[ProductCapabilityRecord] = []
    for audit in _AUDIT_PRODUCTS:
        product = registry["products"][audit.product_id]
        methods = _methods(audit.product_id, product)
        targets = tuple(_target_from_audit(target, product=product, catalog=term_catalog, methods=methods) for target in audit.targets)
        products.append(ProductCapabilityRecord(
            product_id=audit.product_id,
            rule_revision=int(product["identity"]["rule_revision"]),
            canonical_name=str(product.get("identity", {}).get("name_zh", audit.product_id)),
            valuation_methods=methods,
            solve_status=audit.solve_status,
            targets=targets,
            audit_evidence=audit.audit_evidence,
        ))
    return tuple(products)


def product_capability(product_id: str) -> ProductCapabilityRecord | None:
    return next((item for item in capability_matrix() if item.product_id == str(product_id)), None)


def target_capability(product_id: str, target_id: str) -> TargetCapability | None:
    product = product_capability(product_id)
    return None if product is None else next((item for item in product.targets if item.target_id == str(target_id)), None)


def capability_for_target(product_id: str, target_id: str) -> TargetCapability | None:
    return target_capability(product_id, target_id)


def public_capability_package() -> dict[str, Any]:
    products = [item.to_public_dict() for item in capability_matrix()]
    return {
        "schema_id": CAPABILITY_SCHEMA_ID,
        "capability_version": CAPABILITY_VERSION,
        "products": products,
    }


def eligibility_for_target(product: Any, target_id: str, method: str) -> dict[str, Any]:
    """Check only frozen contract fields plus static target/method capability."""
    product_id = str(getattr(product, "product_id", ""))
    target = target_capability(product_id, target_id)
    requested_method = str(method).strip() if method is not None else ""
    if target is None:
        return {
            "eligible": False,
            "status": "ineligible",
            "reason": "目标未登记",
            "target_id": str(target_id),
            "method": requested_method,
        }
    if target.support_status != "supported":
        return {
            "eligible": False,
            "status": "ineligible",
            "reason": target.unsupported_reason or "目标未获得反解支持",
            "target_id": target.target_id,
            "method": requested_method,
        }
    if requested_method not in target.allowed_methods:
        return {
            "eligible": False,
            "status": "ineligible",
            "reason": "目标不支持该公开定价方法",
            "target_id": target.target_id,
            "method": requested_method,
        }
    terms = getattr(product, "terms", {})
    if not isinstance(terms, Mapping) or any(key not in terms for key in target.fixed_dependencies):
        return {
            "eligible": False,
            "status": "ineligible",
            "reason": "基础合同缺少目标固定依赖",
            "target_id": target.target_id,
            "method": requested_method,
        }
    return {
        "eligible": True,
        "status": "eligible",
        "reason": None,
        "target_id": target.target_id,
        "method": requested_method,
    }


# Stable aliases; these are the same one directory, not parallel sources.
build_capability_matrix = capability_matrix
fair_parameter_capability_for = target_capability


__all__ = (
    "CAPABILITY_SCHEMA_ID", "CAPABILITY_VERSION", "ProductCapabilityRecord", "TargetCapability",
    "decode_public_value", "encode_public_value", "from_public_decimal_ratio",
    "build_capability_matrix", "capability_for_target", "capability_matrix", "eligibility_for_target",
    "fair_parameter_capability_for", "product_capability",
    "public_capability_package", "target_capability", "to_public_decimal_ratio",
)
