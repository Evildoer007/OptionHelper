"""Single source of truth for the first pricing product catalog."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CORE_GREEKS = ("Delta", "Gamma", "Theta", "Vega", "Rho")
EXTENDED_RISKS = ("Volga", "Vanna", "Duration", "Forward Delta")

FAMILY_STRUCTURES = {
    "CASHFLOW": ("FIXED_CASHFLOW",),
    "VANILLA": ("EUROPEAN_VANILLA",),
    "DIGITAL": ("BINARY",),
    "BARRIER": ("BARRIER",),
    "AIRBAG": ("AIRBAG",),
    "ACCUMULATOR": ("STATIC_ACCUMULATOR",),
    "MULTI_ASSET": ("WORST_OF_CALL",),
    "VOLATILITY": ("VARIANCE_SWAP",),
    "ACCRUAL": ("RANGE_ACCRUAL",),
    "OPTIONREG": ("OPTIONREG_PATH",),
}

ENGINE_ROUTES = {
    "STANDARD_FIXED_CASHFLOW_DISCOUNTING": {
        "instrument_type": "FixedCashflowOption",
        "method": "DISCOUNTED_CASHFLOW",
        "price_handler": "price_fixed_cashflow_standard",
        "solve_handler": None,
    },
    "STANDARD_VANILLA_BLACK_SCHOLES": {
        "instrument_type": "EuropeanVanillaOption",
        "method": "BLACK_SCHOLES",
        "price_handler": "price_vanilla_standard",
        "solve_handler": None,
    },
    "STANDARD_BINARY_ANALYTIC": {
        "instrument_type": "BinaryOption",
        "method": "BINARY_ANALYTIC",
        "price_handler": "price_binary_standard",
        "solve_handler": None,
    },
    "STANDARD_BARRIER_REINER_RUBINSTEIN": {
        "instrument_type": "BarrierOption",
        "method": "REINER_RUBINSTEIN",
        "price_handler": "price_barrier_standard",
        "solve_handler": None,
    },
    "STANDARD_AIRBAG_STATIC_REPLICATION": {
        "instrument_type": "CompositeOption",
        "method": "STATIC_REPLICATION",
        "price_handler": "price_airbag_standard",
        "solve_handler": None,
    },
    "STANDARD_STATIC_ACCUMULATOR": {
        "instrument_type": "StaticAccumulatorOption",
        "method": "STATIC_REPLICATION",
        "price_handler": "price_static_accumulator_standard",
        "solve_handler": None,
    },
    "STANDARD_WORST_OF_ANALYTIC": {
        "instrument_type": "WorstOfCallOption",
        "method": "WORST_OF_ANALYTIC",
        "price_handler": "price_worst_of_standard",
        "solve_handler": None,
    },
    "STANDARD_VARIANCE_EXPECTATION": {
        "instrument_type": "VarianceSwapOption",
        "method": "VARIANCE_EXPECTATION",
        "price_handler": "price_variance_swap_standard",
        "solve_handler": None,
    },
    "STANDARD_RANGE_ACCRUAL_ANALYTIC": {
        "instrument_type": "RangeAccrualOption",
        "method": "RANGE_ACCRUAL_ANALYTIC",
        "price_handler": "price_range_accrual_standard",
        "solve_handler": None,
    },
    "STANDARD_OPTIONREG_DISCRETE_MONTE_CARLO": {
        "instrument_type": "OptionRegPathOption",
        "method": "MONTE_CARLO_CPU",
        "price_handler": "price_optionreg_path_monte_carlo",
        "solve_handler": None,
    },
}


def _risk(status: str, unit: str, bump: float | None, difference: str, time_basis: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "unit": unit,
        "bump": bump,
        "difference": difference,
        "time_basis": time_basis,
    }


_STANDARD_CORE_RISKS = {
    "Delta": _risk("AVAILABLE", "pv_points_100_per_spot", None, "central", None),
    "Gamma": _risk("AVAILABLE", "pv_points_100_per_spot_squared", None, "central", None),
    "Theta": _risk("AVAILABLE", "pv_points_100_per_calendar_day", None, "forward_roll", "calendar_day"),
    "Vega": _risk("AVAILABLE", "pv_points_100_per_1pct_volatility", None, "central", None),
    "Rho": _risk("AVAILABLE", "pv_points_100_per_1pct_rate", None, "central", None),
}
_ANALYTIC_STANDARD_CORE_RISKS = {
    "Delta": _risk("AVAILABLE", "pv_points_100_per_spot", None, "analytic", None),
    "Gamma": _risk("AVAILABLE", "pv_points_100_per_spot_squared", None, "analytic", None),
    "Theta": _risk("AVAILABLE", "pv_points_100_per_calendar_day", None, "analytic", "calendar_day"),
    "Vega": _risk("AVAILABLE", "pv_points_100_per_1pct_volatility", None, "analytic", None),
    "Rho": _risk("AVAILABLE", "pv_points_100_per_1pct_rate", None, "analytic", None),
}

_AIRBAG_LEG_SHAPE = {
    "type": "OptionLeg[]",
    "fields": ("weight", "structure", "contract", "method", "label"),
}

_MARKET_FIELDS = {
    "required": ("as_of", "spot", "volatility", "risk_free_rate"),
    "optional": ("dividend_yield", "carry", "forward_curve", "source"),
}
_STATE_FIELDS_BY_STRUCTURE = {
    "FIXED_CASHFLOW": (),
    "EUROPEAN_VANILLA": (),
    "BINARY": (),
    "BARRIER": (),
    "AIRBAG": (),
    "STATIC_ACCUMULATOR": (),
    "WORST_OF_CALL": (),
    "VARIANCE_SWAP": (),
    "RANGE_ACCRUAL": (),
    "OPTIONREG_PATH": (
        "trading_day", "calendar_day", "knocked_in", "knocked_out",
        "accumulated_count", "accumulated_quantity", "observation_stage",
        "observation_history",
    ),
}
_ANALYTIC_CONFIG_FIELDS = {
    "required": (),
    "optional": ("greek_bumps", "diagnostics"),
}
_MONTE_CARLO_CONFIG_FIELDS = {
    "required": (),
    "optional": (
        "paths", "seed", "threads", "random_source", "greek_bumps", "diagnostics"
    ),
}

_STRUCTURES: dict[str, dict[str, Any]] = {
    "FIXED_CASHFLOW": {
        "family": "CASHFLOW",
        "route_id": "STANDARD_FIXED_CASHFLOW_DISCOUNTING",
        "contract": {
            "required": ("amount", "maturity_years", "basis"),
            "optional": (),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "EUROPEAN_VANILLA": {
        "family": "VANILLA",
        "route_id": "STANDARD_VANILLA_BLACK_SCHOLES",
        "contract": {
            "required": ("strike", "maturity_years", "call_put", "basis"),
            "optional": ("future",),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_ANALYTIC_STANDARD_CORE_RISKS),
    },
    "BINARY": {
        "family": "DIGITAL",
        "route_id": "STANDARD_BINARY_ANALYTIC",
        "contract": {
            "required": ("strike", "maturity_years", "call_put", "payout_type", "basis"),
            "optional": ("payout", "future"),
            "choices": {
                "payout_type": ("Cash-or-Nothing", "Asset-or-Nothing"),
            },
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "BARRIER": {
        "family": "BARRIER",
        "route_id": "STANDARD_BARRIER_REINER_RUBINSTEIN",
        "contract": {
            "required": (
                "strike",
                "barrier",
                "maturity_years",
                "call_put",
                "knock",
                "monitoring",
                "basis",
            ),
            "optional": ("rebate", "rebate_at_hit", "future"),
            "choices": {
                "knock": ("In", "Out"),
                "monitoring": ("Continuous", "HalfDaily", "Daily", "Weekly", "Monthly"),
            },
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "AIRBAG": {
        "family": "AIRBAG",
        "route_id": "STANDARD_AIRBAG_STATIC_REPLICATION",
        "contract": {
            "required": ("legs", "basis"),
            "optional": (),
            "choices": {},
            "nested": {"legs": _AIRBAG_LEG_SHAPE},
        },
        "solve_targets": (),
        "core_greeks": {
            "Delta": _risk("AVAILABLE", "pv_points_100_per_spot", None, "composite", None),
            "Gamma": _risk("AVAILABLE", "pv_points_100_per_spot_squared", None, "composite", None),
            "Theta": _risk("AVAILABLE", "pv_points_100_per_calendar_day", None, "composite", "calendar_day"),
            "Vega": _risk("AVAILABLE", "pv_points_100_per_1pct_volatility", None, "composite", None),
            "Rho": _risk("AVAILABLE", "pv_points_100_per_1pct_rate", None, "composite", None),
        },
    },
    "STATIC_ACCUMULATOR": {
        "family": "ACCUMULATOR",
        "route_id": "STANDARD_STATIC_ACCUMULATOR",
        "contract": {
            "required": (
                "call_put",
                "initial_spot",
                "strike",
                "barrier",
                "range_payout",
                "knockout_payout",
                "loss_multiplier",
                "first_observation",
                "observation_count",
                "total_observations",
                "accumulator_type",
                "basis",
            ),
            "optional": ("expiry_multiplier", "day_adjustment", "quantity_basis"),
            "choices": {
                "accumulator_type": (
                    "标准",
                    "增强",
                    "不敲出",
                    "固定赔付",
                    "不敲出固定赔付",
                    "固定赔付增强",
                    "熔断",
                    "熔断增强",
                    "熔断固定赔付",
                    "熔断固定赔付增强",
                ),
            },
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "WORST_OF_CALL": {
        "family": "MULTI_ASSET",
        "route_id": "STANDARD_WORST_OF_ANALYTIC",
        "contract": {
            "required": ("strike", "maturity_years", "normalized_spots", "volatilities", "dividend_yields", "correlation", "basis"),
            "optional": (),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "VARIANCE_SWAP": {
        "family": "VOLATILITY",
        "route_id": "STANDARD_VARIANCE_EXPECTATION",
        "contract": {
            "required": ("strike_volatility", "annualization_days", "observation_times", "maturity_years", "basis"),
            "optional": ("historical_squared_returns", "historical_return_count", "last_observation_spot"),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "RANGE_ACCRUAL": {
        "family": "ACCRUAL",
        "route_id": "STANDARD_RANGE_ACCRUAL_ANALYTIC",
        "contract": {
            "required": ("lower", "upper", "maximum_coupon", "observation_times", "maturity_years", "basis"),
            "optional": ("historical_in_count", "historical_observation_count"),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
    "OPTIONREG_PATH": {
        "family": "OPTIONREG",
        "route_id": "STANDARD_OPTIONREG_DISCRETE_MONTE_CARLO",
        "contract": {
            "required": ("resolved_contract", "basis"),
            "optional": ("asset_spots", "asset_volatilities", "asset_dividend_yields", "correlation", "trading_sessions", "calendar_id", "calendar_revision"),
            "choices": {},
            "nested": {},
        },
        "solve_targets": (),
        "core_greeks": deepcopy(_STANDARD_CORE_RISKS),
    },
}

_STANDARD_EXTENDED_RISKS = {
    "Volga": _risk("AVAILABLE", "pv_points_100_per_1pct_volatility_squared", None, "central_second_order", None),
    "Vanna": _risk("AVAILABLE", "pv_points_100_per_spot_per_1pct_volatility", None, "central_cross", None),
    "Duration": _risk("NOT_APPLICABLE", "not_applicable", 0.0, "not_applicable", "not_applicable"),
    "Forward Delta": _risk("NOT_IMPLEMENTED", "pv_points_100_per_forward", None, "not_implemented", "forward_curve_tenor"),
}
_EXTENDED_RISKS = {
    structure: deepcopy(_STANDARD_EXTENDED_RISKS)
    for structure in _STRUCTURES
}

RESULT_FIELDS = (
    "pv_amount",
    "pv_percent",
    "pv_points_100",
    "currency",
    "greeks",
    "extended_risks",
    "method",
    "implementation_id",
    "warnings",
    "diagnostics",
    "engine_raw",
)


def list_families() -> tuple[str, ...]:
    return tuple(FAMILY_STRUCTURES)


def list_structures(family: str) -> tuple[str, ...]:
    require_family(family)
    return FAMILY_STRUCTURES[family]


def require_family(family: str) -> None:
    if family not in FAMILY_STRUCTURES:
        raise ValueError(f"未知产品族标识：{family!r}；请使用list_families()返回的精确标识")


def require_family_structure(family: str, structure: str) -> None:
    require_family(family)
    if structure not in FAMILY_STRUCTURES[family]:
        allowed = ", ".join(FAMILY_STRUCTURES[family])
        raise ValueError(f"{family}不包含结构{structure!r}；可用结构：{allowed}")


def structure_spec(family: str, structure: str) -> dict[str, Any]:
    require_family_structure(family, structure)
    return deepcopy(_STRUCTURES[structure])


def methods(family: str, structure: str) -> tuple[str, ...]:
    require_family_structure(family, structure)
    route = route_for(family, structure)
    return (route["method"],)


def route_id(family: str, structure: str) -> str:
    require_family_structure(family, structure)
    return _STRUCTURES[structure]["route_id"]


def route_for(family: str, structure: str) -> dict[str, Any]:
    return deepcopy(ENGINE_ROUTES[route_id(family, structure)])


def family_for_structure(structure: str) -> str:
    spec = _STRUCTURES.get(structure)
    if spec is None:
        raise ValueError(f"未知结构标识：{structure!r}")
    return spec["family"]


def contract_spec(family: str, structure: str) -> dict[str, Any]:
    require_family_structure(family, structure)
    return deepcopy(_STRUCTURES[structure]["contract"])


def parameter_spec(family: str, structure: str) -> dict[str, Any]:
    method = route_for(family, structure)["method"]
    return {
        "contract": contract_spec(family, structure),
        "market": deepcopy(_MARKET_FIELDS),
        "valuation_state": {
            "required": (),
            "optional": _STATE_FIELDS_BY_STRUCTURE[structure],
        },
        "config": deepcopy(
            _MONTE_CARLO_CONFIG_FIELDS
            if method == "MONTE_CARLO_CPU"
            else _ANALYTIC_CONFIG_FIELDS
        ),
    }


def greek_spec(family: str, structure: str) -> dict[str, Any]:
    require_family_structure(family, structure)
    return {
        "core": deepcopy(_STRUCTURES[structure]["core_greeks"]),
        "extended": deepcopy(_EXTENDED_RISKS[structure]),
    }


def describe_structure(family: str, structure: str) -> dict[str, Any]:
    spec = structure_spec(family, structure)
    return {
        "family": family,
        "structure": structure,
        "route_id": route_id(family, structure),
        "methods": methods(family, structure),
        "parameters": parameter_spec(family, structure),
        "greeks": greek_spec(family, structure),
        "solve_targets": spec["solve_targets"],
        "result_fields": RESULT_FIELDS,
    }
