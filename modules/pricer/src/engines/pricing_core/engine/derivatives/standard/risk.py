"""Unified, auditable STANDARD bump-and-revalue risk calculations."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Callable

from ..basis import ResultBasis
from ..basis import convert_points_100
from ..models import MarketState, ValuationConfig
from ..results import GreekValue


PriceMarket = Callable[[MarketState], float]


class ThetaNotApplicableError(ValueError):
    """The contract has no full roll interval left for a Theta estimate."""


def effective_dividend_yield(market: MarketState, *, future: bool = False) -> float:
    """Return the dividend yield implied by the selected carry convention.

    For spot models, an explicitly supplied carry ``b`` takes precedence and
    implies ``q=r-b``.  For Black-76 inputs the quoted underlying is already a
    forward or future, so its asset discount rate is the risk-free rate.
    """
    if future:
        return market.risk_free_rate
    if market.carry is None:
        return market.dividend_yield
    return market.risk_free_rate - market.carry


@dataclass(frozen=True)
class StandardGreekConvention:
    """Explicit numerical convention shared by every STANDARD engine."""

    spot_relative_bump: float = 0.01
    volatility_absolute_bump: float = 0.01
    risk_free_rate_absolute_bump: float = 0.01
    theta_calendar_day_shift: int = 1
    theta_trading_day_shift: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("spot_relative_bump", self.spot_relative_bump),
            ("volatility_absolute_bump", self.volatility_absolute_bump),
            ("risk_free_rate_absolute_bump", self.risk_free_rate_absolute_bump),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError(f"{name}必须为正有限数")
        if self.spot_relative_bump >= 1.0:
            raise ValueError("spot_relative_bump必须小于1")
        for name, value in (
            ("theta_calendar_day_shift", self.theta_calendar_day_shift),
            ("theta_trading_day_shift", self.theta_trading_day_shift),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name}必须为正整数")

    @classmethod
    def from_valuation_config(cls, config: ValuationConfig) -> StandardGreekConvention:
        overrides = dict(config.greek_bumps)
        allowed = {
            "spot_relative_bump",
            "volatility_absolute_bump",
            "risk_free_rate_absolute_bump",
            "theta_calendar_day_shift",
            "theta_trading_day_shift",
        }
        unknown = sorted(set(overrides) - allowed)
        if unknown:
            raise ValueError("greek_bumps包含未知字段：" + ", ".join(unknown))
        return cls(**overrides)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "spot_relative_bump": self.spot_relative_bump,
            "volatility_absolute_bump": self.volatility_absolute_bump,
            "risk_free_rate_absolute_bump": self.risk_free_rate_absolute_bump,
            "theta_calendar_day_shift": self.theta_calendar_day_shift,
            "theta_trading_day_shift": self.theta_trading_day_shift,
        }


@dataclass(frozen=True)
class ThetaRollValue:
    price_points_100: float
    calendar_day_shift: int
    trading_day_shift: int
    description: str


def _require_finite(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}必须为有限数")
    return result


def make_risk_value(
    value_points_100: float,
    *,
    unit: str,
    bump: float | None,
    difference: str,
    basis: ResultBasis,
    time_basis: str | None = None,
    bump_details: dict[str, Any] | None = None,
) -> GreekValue:
    points = _require_finite("Greek", value_points_100)
    percent = points / 100.0
    amount = None
    if basis.cashflow_scale is not None and basis.currency is not None:
        amount = (
            points * basis.cashflow_scale
            if basis.cashflow_scale_kind == "variance_notional"
            else percent * basis.cashflow_scale
        )
    if not unit.startswith("pv_points_100"):
        raise ValueError("STANDARD Greek标准单位必须以pv_points_100开头")
    suffix = unit.removeprefix("pv_points_100")
    return GreekValue(
        value=points,
        unit=unit,
        bump=bump,
        difference=difference,
        time_basis=time_basis,
        bump_details={} if bump_details is None else dict(bump_details),
        pv_amount_value=amount,
        pv_amount_unit=(
            None if amount is None else f"{basis.currency}{suffix}"
        ),
        pv_percent_value=percent,
        pv_percent_unit=f"pv_percent{suffix}",
        pv_points_100_value=points,
        pv_points_100_unit=unit,
    )


def raw_price_to_points_100(raw_value: float, basis: ResultBasis) -> float:
    converted = convert_points_100(_require_finite("raw_price", raw_value), basis)
    return float(converted.pv_points_100)


def calculate_standard_greeks(
    *,
    base_price_points_100: float,
    market: MarketState,
    config: ValuationConfig,
    basis: ResultBasis,
    price_market: PriceMarket,
    theta_roll: Callable[[StandardGreekConvention], ThetaRollValue],
    requested_greeks: tuple[str, ...] | None = None,
) -> tuple[dict[str, GreekValue], dict[str, GreekValue], dict[str, object]]:
    """Calculate core and extended Greeks using common bump definitions.

    ``price_market`` must use the same contract, state and random draws for
    every call.  That invariant gives structured products common-random-number
    finite differences instead of differences between unrelated simulations.
    """

    allowed_greeks = frozenset({"delta", "gamma", "theta", "vega", "rho", "vanna", "volga"})
    selected_greeks = allowed_greeks if requested_greeks is None else frozenset(requested_greeks)
    unknown_greeks = selected_greeks - allowed_greeks
    if unknown_greeks:
        raise ValueError("requested_greeks包含未知Greek：" + ", ".join(sorted(unknown_greeks)))
    if not selected_greeks:
        raise ValueError("requested_greeks不能为空")

    convention = StandardGreekConvention.from_valuation_config(config)
    base = _require_finite("base_price_points_100", base_price_points_100)
    spot_bump = market.spot * convention.spot_relative_bump
    volatility_bump = min(
        convention.volatility_absolute_bump,
        market.volatility / 2.0,
    )
    if volatility_bump <= 0.0:
        raise ValueError("STANDARD Greek要求volatility大于0")
    rate_bump = convention.risk_free_rate_absolute_bump

    spot_up = spot_down = None
    if selected_greeks & {"delta", "gamma", "vanna"}:
        spot_up = _require_finite(
            "spot_up_price", price_market(replace(market, spot=market.spot + spot_bump))
        )
        spot_down = _require_finite(
            "spot_down_price", price_market(replace(market, spot=market.spot - spot_bump))
        )

    volatility_up = volatility_down = None
    if selected_greeks & {"vega", "volga", "vanna"}:
        volatility_up = _require_finite(
            "volatility_up_price",
            price_market(replace(market, volatility=market.volatility + volatility_bump)),
        )
        volatility_down = _require_finite(
            "volatility_down_price",
            price_market(replace(market, volatility=market.volatility - volatility_bump)),
        )

    rate_up = rate_down = None
    if "rho" in selected_greeks:
        rate_up = _require_finite(
            "rate_up_price",
            price_market(replace(market, risk_free_rate=market.risk_free_rate + rate_bump)),
        )
        rate_down = _require_finite(
            "rate_down_price",
            price_market(replace(market, risk_free_rate=market.risk_free_rate - rate_bump)),
        )

    rolled = None
    theta_unavailable_reason = None
    if "theta" in selected_greeks:
        try:
            rolled = theta_roll(convention)
        except ThetaNotApplicableError as error:
            theta_unavailable_reason = str(error)
        if rolled is not None and rolled.calendar_day_shift <= 0:
            raise ValueError("Theta自然日推进必须为正数")

    cross_values = None
    if "vanna" in selected_greeks:
        cross_values = (
            _require_finite(
                "vanna_spot_up_volatility_up_price",
                price_market(replace(market, spot=market.spot + spot_bump, volatility=market.volatility + volatility_bump)),
            ),
            _require_finite(
                "vanna_spot_up_volatility_down_price",
                price_market(replace(market, spot=market.spot + spot_bump, volatility=market.volatility - volatility_bump)),
            ),
            _require_finite(
                "vanna_spot_down_volatility_up_price",
                price_market(replace(market, spot=market.spot - spot_bump, volatility=market.volatility + volatility_bump)),
            ),
            _require_finite(
                "vanna_spot_down_volatility_down_price",
                price_market(replace(market, spot=market.spot - spot_bump, volatility=market.volatility - volatility_bump)),
            ),
        )

    core: dict[str, GreekValue] = {}
    if "delta" in selected_greeks:
        core["delta"] = make_risk_value(
            (spot_up - spot_down) / (2.0 * spot_bump),
            unit="pv_points_100_per_spot",
            bump=spot_bump,
            difference="central",
            basis=basis,
            bump_details={
                "spot_absolute_bump": spot_bump,
                "spot_relative_bump": convention.spot_relative_bump,
            },
        )
    if "gamma" in selected_greeks:
        core["gamma"] = make_risk_value(
            (spot_up - 2.0 * base + spot_down) / (spot_bump * spot_bump),
            unit="pv_points_100_per_spot_squared",
            bump=spot_bump,
            difference="central",
            basis=basis,
            bump_details={
                "spot_absolute_bump": spot_bump,
                "spot_relative_bump": convention.spot_relative_bump,
            },
        )
    if "theta" in selected_greeks:
        if rolled is None:
            core["theta"] = GreekValue(
                value=None,
                unit="pv_points_100_per_calendar_day",
                bump=float(convention.theta_calendar_day_shift),
                difference="not_applicable",
                time_basis="calendar_day",
                bump_details={
                    "calendar_day_shift": convention.theta_calendar_day_shift,
                    "trading_day_shift": convention.theta_trading_day_shift,
                },
                pv_percent_unit="pv_percent_per_calendar_day",
                pv_points_100_unit="pv_points_100_per_calendar_day",
                status="not_applicable",
                reason=theta_unavailable_reason,
            )
        else:
            core["theta"] = make_risk_value(
                (rolled.price_points_100 - base) / rolled.calendar_day_shift,
                unit="pv_points_100_per_calendar_day",
                bump=float(rolled.calendar_day_shift),
                difference="forward_roll",
                basis=basis,
                time_basis="calendar_day",
                bump_details={
                    "calendar_day_shift": rolled.calendar_day_shift,
                    "trading_day_shift": rolled.trading_day_shift,
                },
            )
    if "vega" in selected_greeks:
        core["vega"] = make_risk_value(
            (volatility_up - volatility_down) / (2.0 * volatility_bump) * 0.01,
            unit="pv_points_100_per_1pct_volatility",
            bump=volatility_bump,
            difference="central",
            basis=basis,
            bump_details={
                "volatility_absolute_bump": volatility_bump,
                "reported_volatility_change": 0.01,
            },
        )
    if "rho" in selected_greeks:
        core["rho"] = make_risk_value(
            (rate_up - rate_down) / (2.0 * rate_bump) * 0.01,
            unit="pv_points_100_per_1pct_rate",
            bump=rate_bump,
            difference="central",
            basis=basis,
            bump_details={
                "risk_free_rate_absolute_bump": rate_bump,
                "reported_rate_change": 0.01,
            },
        )

    extended: dict[str, GreekValue] = {}
    if "volga" in selected_greeks:
        extended["volga"] = make_risk_value(
            (volatility_up - 2.0 * base + volatility_down)
            / (volatility_bump * volatility_bump)
            * 0.01 * 0.01,
            unit="pv_points_100_per_1pct_volatility_squared",
            bump=volatility_bump,
            difference="central_second_order",
            basis=basis,
            bump_details={
                "volatility_absolute_bump": volatility_bump,
                "reported_volatility_change": 0.01,
            },
        )
    if "vanna" in selected_greeks:
        cross_up_up, cross_up_down, cross_down_up, cross_down_down = cross_values
        extended["vanna"] = make_risk_value(
            (cross_up_up - cross_up_down - cross_down_up + cross_down_down)
            / (4.0 * spot_bump * volatility_bump) * 0.01,
            unit="pv_points_100_per_spot_per_1pct_volatility",
            bump=spot_bump,
            difference="central_cross",
            basis=basis,
            bump_details={
                "spot_absolute_bump": spot_bump,
                "spot_relative_bump": convention.spot_relative_bump,
                "volatility_absolute_bump": volatility_bump,
                "reported_volatility_change": 0.01,
            },
        )
    if "theta" not in selected_greeks:
        theta_roll_diagnostics = None
    elif rolled is None:
        theta_roll_diagnostics = {
            "calendar_day_shift": convention.theta_calendar_day_shift,
            "trading_day_shift": convention.theta_trading_day_shift,
            "status": "not_applicable",
            "reason": theta_unavailable_reason,
        }
    else:
        theta_roll_diagnostics = {
            "calendar_day_shift": rolled.calendar_day_shift,
            "trading_day_shift": rolled.trading_day_shift,
            "description": rolled.description,
            "status": "available",
        }

    diagnostics = {
        "convention": convention.as_dict(),
        "effective_spot_absolute_bump": spot_bump,
        "effective_volatility_absolute_bump": volatility_bump,
        "effective_risk_free_rate_absolute_bump": rate_bump,
        "theta_roll": theta_roll_diagnostics,
        "common_random_numbers_required": True,
        "canonical_value_basis": "PV_POINTS_100",
        "risk_free_rate_shock_convention": (
            "explicit_forward_or_carry_curve_held_fixed_and_discount_rate_shocked"
            if market.forward_curve or market.carry is not None
            else "carry_curve_rebuilt_from_shocked_risk_free_rate"
        ),
    }
    return core, extended, diagnostics


__all__ = [
    "StandardGreekConvention",
    "ThetaNotApplicableError",
    "ThetaRollValue",
    "calculate_standard_greeks",
    "effective_dividend_yield",
    "make_risk_value",
    "raw_price_to_points_100",
]
