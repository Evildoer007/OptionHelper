"""Self-contained deterministic inputs for the active STANDARD test suite."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


BASIS = {"notional": 1_000_000.0, "currency": "CNY"}
MARKET = {
    "as_of": "2026-08-06",
    "spot": 100.0,
    "volatility": 0.27,
    "risk_free_rate": 0.03,
    "dividend_yield": 0.01,
    "source": "offline_standard_acceptance",
}
MC_CONFIG = {"paths": 10, "seed": 20240101, "threads": 1}
CORE_GREEKS = ("Delta", "Gamma", "Theta", "Vega", "Rho")
EXTENDED_RISKS = ("Volga", "Vanna")
FAMILY_STRUCTURES = {
    "VANILLA": ("EUROPEAN_VANILLA",),
    "DIGITAL": ("BINARY",),
    "BARRIER": ("BARRIER",),
    "AIRBAG": ("AIRBAG",),
    "ACCUMULATOR": ("STATIC_ACCUMULATOR", "PATH_ACCUMULATOR"),
    "AUTOCALL": ("SNOWBALL", "PHOENIX", "TRIGGER"),
    "OPTIONREG": ("OPTIONREG_PATH",),
}


def _basis() -> dict[str, Any]:
    return deepcopy(BASIS)


def _market(**overrides: Any) -> dict[str, Any]:
    values = deepcopy(MARKET)
    values.update(overrides)
    return values


def _autocall_schedule(kind: str, coupon: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trading_days = (20, 40, 60)
    calendar_days = (30, 60, 90)
    call_schedule: list[dict[str, Any]] = []
    coupon_schedule: list[dict[str, Any]] = []
    for trading_day, calendar_day in zip(trading_days, calendar_days, strict=True):
        call_schedule.append(
            {
                "trading_day": trading_day,
                "calendar_day": calendar_day,
                "barrier": 103.0,
            }
        )
        coupon_schedule.append(
            {
                "trading_day": trading_day,
                "calendar_day": calendar_day,
                "barrier": 75.0,
            }
        )
    return call_schedule, coupon_schedule


def autocall_parameters(kind: str, *, threads: int = 1) -> dict[str, Any]:
    coupons = {"SNOWBALL": 8.0, "PHOENIX": 6.0, "TRIGGER": 2.0}
    coupon = coupons[kind]
    call_schedule, coupon_schedule = _autocall_schedule(kind, coupon)
    final_rebate = coupon * 90.0 / 365.0 if kind == "SNOWBALL" else (
        coupon if kind == "TRIGGER" else 0.0
    )
    return {
        "contract": {
            "call_put": "CALL",
            "strike": 100.0,
            "knock_in": 75.0,
            "knock_out": 103.0,
            "floor": 75.0,
            "coupon": coupon,
            "call_schedule": call_schedule,
            "coupon_schedule": coupon_schedule,
            "final_trading_day": 60,
            "final_calendar_day": 90,
            "final_rebate": final_rebate,
            "margin": 0.0,
            "basis": _basis(),
        },
        "market": _market(),
        "valuation_state": {
            "trading_day": 1,
            "calendar_day": 1,
            "knocked_in": False,
            "knocked_out": False,
        },
        "config": {**MC_CONFIG, "threads": threads},
    }


def path_accumulator_parameters(*, threads: int = 1) -> dict[str, Any]:
    schedule = [
        {
            "trading_day": trading_day,
            "calendar_day": trading_day + (trading_day - 1) // 2,
            "barrier": 105.0,
        }
        for trading_day in range(1, 16)
    ]
    return {
        "contract": {
            "call_put": "CALL",
            "strike": 96.0,
            "knock_out": 105.0,
            "multiplier": 2.0,
            "ko_begin_trading_day": 1,
            "lock_trading_days": 10,
            "ko_terminates": True,
            "observation_schedule": schedule,
            "forward_curve_weight": 1.0,
            "quantity_basis": "WHOLE_CONTRACT",
            "basis": _basis(),
        },
        "market": _market(),
        "valuation_state": {
            "trading_day": 1,
            "calendar_day": 1,
            "knocked_out": False,
            "accumulated_count": 0,
        },
        "config": {**MC_CONFIG, "threads": threads},
    }


def representative_cases() -> list[tuple[str, str, dict[str, Any], str]]:
    cases: list[tuple[str, str, dict[str, Any], str]] = [
        (
            "VANILLA",
            "EUROPEAN_VANILLA",
            {
                "contract": {
                    "strike": 100.0,
                    "maturity_years": 0.75,
                    "call_put": "CALL",
                    "future": False,
                    "basis": _basis(),
                },
                "market": _market(),
            },
            "BLACK_SCHOLES",
        ),
        (
            "DIGITAL",
            "BINARY",
            {
                "contract": {
                    "strike": 100.0,
                    "maturity_years": 0.75,
                    "call_put": "CALL",
                    "payout_type": "Cash-or-Nothing",
                    "payout": 10.0,
                    "future": False,
                    "basis": _basis(),
                },
                "market": _market(),
            },
            "BINARY_ANALYTIC",
        ),
        (
            "BARRIER",
            "BARRIER",
            {
                "contract": {
                    "strike": 100.0,
                    "barrier": 120.0,
                    "maturity_years": 0.75,
                    "call_put": "CALL",
                    "knock": "Out",
                    "monitoring": "Continuous",
                    "rebate": 0.0,
                    "rebate_at_hit": True,
                    "future": False,
                    "basis": _basis(),
                },
                "market": _market(),
            },
            "REINER_RUBINSTEIN",
        ),
        (
            "AIRBAG",
            "AIRBAG",
            {
                "contract": {
                    "legs": [
                        {
                            "weight": 1.0,
                            "structure": "EUROPEAN_VANILLA",
                            "contract": {
                                "strike": 100.0,
                                "maturity_years": 0.75,
                                "call_put": "CALL",
                                "future": False,
                                "basis": _basis(),
                            },
                            "method": "BLACK_SCHOLES",
                            "label": "long_call",
                        },
                        {
                            "weight": -0.5,
                            "structure": "BARRIER",
                            "contract": {
                                "strike": 100.0,
                                "barrier": 80.0,
                                "maturity_years": 0.75,
                                "call_put": "PUT",
                                "knock": "In",
                                "monitoring": "Continuous",
                                "rebate": 0.0,
                                "rebate_at_hit": True,
                                "future": False,
                                "basis": _basis(),
                            },
                            "method": "REINER_RUBINSTEIN",
                            "label": "short_down_in_put",
                        },
                    ],
                    "basis": _basis(),
                },
                "market": _market(),
            },
            "STATIC_REPLICATION",
        ),
        (
            "ACCUMULATOR",
            "STATIC_ACCUMULATOR",
            {
                "contract": {
                    "call_put": "CALL",
                    "initial_spot": 100.0,
                    "strike": 97.0,
                    "barrier": 105.0,
                    "range_payout": 1.2,
                    "knockout_payout": 0.8,
                    "loss_multiplier": 2.0,
                    "first_observation": 2,
                    "observation_count": 6,
                    "total_observations": 8,
                    "accumulator_type": "标准",
                    "expiry_multiplier": 1.25,
                    "day_adjustment": 0.5,
                    "quantity_basis": "WHOLE_CONTRACT",
                    "basis": _basis(),
                },
                "market": _market(),
            },
            "STATIC_REPLICATION",
        ),
    ]
    for structure in ("SNOWBALL", "PHOENIX", "TRIGGER"):
        cases.append(
            ("AUTOCALL", structure, autocall_parameters(structure), "MONTE_CARLO_CPU")
        )
    cases.append(
        (
            "ACCUMULATOR",
            "PATH_ACCUMULATOR",
            path_accumulator_parameters(),
            "MONTE_CARLO_CPU",
        )
    )
    return cases


def case_by_structure(structure: str) -> tuple[str, str, dict[str, Any], str]:
    for case in representative_cases():
        if case[1] == structure:
            return deepcopy(case)
    raise KeyError(structure)
