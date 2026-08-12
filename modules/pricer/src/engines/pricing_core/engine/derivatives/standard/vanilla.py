"""Independent Black-Scholes and Black-76 engine for European vanilla options."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import hashlib
import math
from statistics import NormalDist

import numpy as np

from ..basis import convert_points_100
from ..enums import CallPut, PricingMethod
from ..instruments import EuropeanVanillaOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..random_source import default_random_source
from ..results import PricingResult
from .risk import (
    StandardGreekConvention,
    ThetaRollValue,
    calculate_standard_greeks,
    effective_dividend_yield,
    make_risk_value,
    raw_price_to_points_100,
)


_VERSION = "standard-vanilla-4"
_PI = 3.14159265358979
_STANDARD_NORMAL = NormalDist()


@dataclass(frozen=True)
class BlackScholesAnalytics:
    """Explicitly named analytic values that are not part of the five core Greeks."""

    d1: float
    d2: float
    normal_cdf_d1: float
    normal_cdf_d2: float
    model_in_the_money_probability: float
    risk_neutral_density_from_in_the_money_probability: float
    driftless_theta_per_calendar_day: float
    large_move_delta: float
    large_move_delta_relative_spot_bump: float


def _normal_cdf(value: float) -> float:
    """Hart双精度正态CDF。"""
    magnitude = abs(value)
    if magnitude > 37.0:
        result = 0.0
    else:
        exponential = math.exp(-(magnitude ** 2) / 2.0)
        if magnitude < 7.07106781186547:
            numerator = 3.52624965998911e-02 * magnitude + 0.700383064443688
            numerator = numerator * magnitude + 6.37396220353165
            numerator = numerator * magnitude + 33.912866078383
            numerator = numerator * magnitude + 112.079291497871
            numerator = numerator * magnitude + 221.213596169931
            numerator = numerator * magnitude + 220.206867912376
            denominator = 8.83883476483184e-02 * magnitude + 1.75566716318264
            denominator = denominator * magnitude + 16.064177579207
            denominator = denominator * magnitude + 86.7807322029461
            denominator = denominator * magnitude + 296.564248779674
            denominator = denominator * magnitude + 637.333633378831
            denominator = denominator * magnitude + 793.826512519948
            denominator = denominator * magnitude + 440.413735824752
            result = exponential * numerator / denominator
        else:
            denominator = magnitude + 0.65
            denominator = magnitude + 4.0 / denominator
            denominator = magnitude + 3.0 / denominator
            denominator = magnitude + 2.0 / denominator
            denominator = magnitude + 1.0 / denominator
            result = exponential / (denominator * 2.506628274631)
    return 1.0 - result if value > 0.0 else result


def _normal_pdf(value: float) -> float:
    return math.exp(-(value ** 2) / 2.0) / math.sqrt(2.0 * _PI)


def calculate_risk_neutral_density_from_in_the_money_probability(
    *,
    strike: float,
    maturity_years: float,
    risk_free_rate: float,
    volatility: float,
    in_the_money_probability: float,
) -> float:
    """Return state-price density implied by an explicitly supplied ITM probability."""
    values = {
        "strike": strike,
        "maturity_years": maturity_years,
        "risk_free_rate": risk_free_rate,
        "volatility": volatility,
        "in_the_money_probability": in_the_money_probability,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name}必须是有限数值")
        if not math.isfinite(float(value)):
            raise ValueError(f"{name}必须是有限数值")
    if strike <= 0.0:
        raise ValueError("strike必须大于0")
    if maturity_years <= 0.0:
        raise ValueError("maturity_years必须大于0")
    if volatility <= 0.0:
        raise ValueError("volatility必须大于0")
    if not 0.0 < in_the_money_probability < 1.0:
        raise ValueError("in_the_money_probability必须严格位于0和1之间")

    probability_z_score = _STANDARD_NORMAL.inv_cdf(in_the_money_probability)
    return (
        math.exp(-risk_free_rate * maturity_years)
        * _STANDARD_NORMAL.pdf(probability_z_score)
        / (strike * volatility * math.sqrt(maturity_years))
    )


def _d1_d2(
    spot: float,
    strike: float,
    maturity: float,
    volatility: float,
    rate: float,
    dividend: float,
    future: bool,
) -> tuple[float, float]:
    drift = 0.5 * volatility * volatility
    if not future:
        drift += rate - dividend
    root_time = math.sqrt(maturity)
    d1 = (math.log(spot / strike) + drift * maturity) / (volatility * root_time)
    return d1, d1 - volatility * root_time


def _raw_price_and_greeks(
    instrument: EuropeanVanillaOption,
    market: MarketState,
) -> tuple[float, dict[str, float]]:
    spot = market.spot
    strike = instrument.strike
    maturity = instrument.maturity_years
    volatility = market.volatility
    rate = market.risk_free_rate
    dividend = effective_dividend_yield(market, future=instrument.future)
    future = instrument.future
    d1, d2 = _d1_d2(
        spot,
        strike,
        maturity,
        volatility,
        rate,
        dividend,
        future,
    )
    root_time = math.sqrt(maturity)
    discount_rate = math.exp(-rate * maturity)
    discount_spot = discount_rate if future else math.exp(-dividend * maturity)
    density = _normal_pdf(d1)

    if instrument.call_put is CallPut.CALL:
        cdf_d1 = _normal_cdf(d1)
        cdf_d2 = _normal_cdf(d2)
        raw = spot * discount_spot * cdf_d1 - strike * discount_rate * cdf_d2
        delta = discount_spot * cdf_d1
        if future:
            theta = (
                -spot * volatility * discount_rate * density / (2.0 * root_time)
                + discount_rate * rate * spot * cdf_d1
                - rate * strike * discount_rate * cdf_d2
            )
            rho = -rate * discount_rate * (spot * cdf_d1 - strike * cdf_d2)
        else:
            theta = (
                -spot * volatility * discount_spot * density / (2.0 * root_time)
                + discount_spot * dividend * spot * cdf_d1
                - rate * strike * discount_rate * cdf_d2
            )
            rho = strike * maturity * discount_rate * cdf_d2
    else:
        cdf_minus_d1 = _normal_cdf(-d1)
        cdf_minus_d2 = _normal_cdf(-d2)
        raw = strike * discount_rate * cdf_minus_d2 - spot * discount_spot * cdf_minus_d1
        delta = discount_spot * (_normal_cdf(d1) - 1.0)
        if future:
            theta = (
                -spot * volatility * discount_rate * density / (2.0 * root_time)
                - discount_rate * rate * spot * cdf_minus_d1
                + rate * strike * discount_rate * cdf_minus_d2
            )
            rho = -rate * discount_rate * (
                strike * cdf_minus_d2 - spot * cdf_minus_d1
            )
        else:
            theta = (
                -spot * volatility * discount_spot * density / (2.0 * root_time)
                - discount_spot * dividend * spot * cdf_minus_d1
                + rate * strike * discount_rate * cdf_minus_d2
            )
            rho = -strike * maturity * discount_rate * cdf_minus_d2

    gamma = discount_spot * density / (spot * volatility * root_time)
    vega = spot * root_time * density * discount_spot
    return raw, {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def calculate_black_scholes_analytics(
    instrument: EuropeanVanillaOption,
    market: MarketState,
    *,
    large_move_delta_relative_spot_bump: float = 0.25,
) -> BlackScholesAnalytics:
    """Calculate explicitly named Black-Scholes diagnostics."""
    if (
        isinstance(large_move_delta_relative_spot_bump, bool)
        or not isinstance(large_move_delta_relative_spot_bump, (int, float))
        or not math.isfinite(float(large_move_delta_relative_spot_bump))
        or not 0.0 < large_move_delta_relative_spot_bump < 1.0
    ):
        raise ValueError("large_move_delta_relative_spot_bump必须严格位于0和1之间")

    spot = market.spot
    maturity = instrument.maturity_years
    volatility = market.volatility
    rate = market.risk_free_rate
    dividend = effective_dividend_yield(market, future=instrument.future)
    d1, d2 = _d1_d2(
        spot,
        instrument.strike,
        maturity,
        volatility,
        rate,
        dividend,
        instrument.future,
    )
    normal_cdf_d1 = _normal_cdf(d1)
    normal_cdf_d2 = _normal_cdf(d2)
    model_in_the_money_probability = (
        normal_cdf_d2
        if instrument.call_put is CallPut.CALL
        else _normal_cdf(-d2)
    )
    if model_in_the_money_probability in (0.0, 1.0):
        risk_neutral_density = 0.0
    else:
        risk_neutral_density = (
            calculate_risk_neutral_density_from_in_the_money_probability(
                strike=instrument.strike,
                maturity_years=maturity,
                risk_free_rate=rate,
                volatility=volatility,
                in_the_money_probability=model_in_the_money_probability,
            )
        )

    discount_spot = (
        math.exp(-rate * maturity)
        if instrument.future
        else math.exp(-dividend * maturity)
    )
    driftless_theta_per_calendar_day = (
        -spot
        * discount_spot
        * _normal_pdf(d1)
        * volatility
        / (2.0 * math.sqrt(maturity))
        / 365.0
    )

    upper_spot = spot * (1.0 + large_move_delta_relative_spot_bump)
    lower_spot = spot * (1.0 - large_move_delta_relative_spot_bump)
    upper_price, _ = _raw_price_and_greeks(
        instrument,
        replace(market, spot=upper_spot),
    )
    lower_price, _ = _raw_price_and_greeks(
        instrument,
        replace(market, spot=lower_spot),
    )
    large_move_delta = (
        (upper_price - lower_price)
        / (2.0 * spot * large_move_delta_relative_spot_bump)
    )

    return BlackScholesAnalytics(
        d1=d1,
        d2=d2,
        normal_cdf_d1=normal_cdf_d1,
        normal_cdf_d2=normal_cdf_d2,
        model_in_the_money_probability=model_in_the_money_probability,
        risk_neutral_density_from_in_the_money_probability=risk_neutral_density,
        driftless_theta_per_calendar_day=driftless_theta_per_calendar_day,
        large_move_delta=large_move_delta,
        large_move_delta_relative_spot_bump=large_move_delta_relative_spot_bump,
    )


def price_vanilla_standard(
    instrument: EuropeanVanillaOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    """Price one European vanilla with the STANDARD analytic engine."""
    del valuation_state
    if config.method is not PricingMethod.BLACK_SCHOLES:
        raise ValueError("Vanilla STANDARD仅支持BLACK_SCHOLES")

    raw, analytic_values = _raw_price_and_greeks(instrument, market)
    if instrument.future or market.carry is not None:
        analytic_values["rho"] = -instrument.maturity_years * raw
    analytics = calculate_black_scholes_analytics(instrument, market)
    converted = convert_points_100(raw, instrument.basis)
    if converted.pv_points_100 is None:
        raise ValueError("Vanilla STANDARD Greek要求可转换为pv_points_100的basis")

    def price_market(changed_market: MarketState) -> float:
        changed_raw, _ = _raw_price_and_greeks(instrument, changed_market)
        return raw_price_to_points_100(changed_raw, instrument.basis)

    def theta_roll(convention) -> ThetaRollValue:
        day_shift = convention.theta_calendar_day_shift
        remaining = instrument.maturity_years - day_shift / 365.0
        if remaining <= 0.0:
            raise ValueError("Vanilla Theta推进后不得到期或过期")
        rolled_raw, _ = _raw_price_and_greeks(
            replace(instrument, maturity_years=remaining),
            replace(market, as_of=market.as_of + timedelta(days=day_shift)),
        )
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(rolled_raw, instrument.basis),
            calendar_day_shift=day_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description="maturity_years减少calendar_day_shift/365",
        )

    _, extended_greeks, greek_diagnostics = calculate_standard_greeks(
        base_price_points_100=converted.pv_points_100,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
    )
    greeks = {
        "delta": make_risk_value(
            raw_price_to_points_100(analytic_values["delta"], instrument.basis),
            unit="pv_points_100_per_spot",
            bump=None,
            difference="analytic",
            basis=instrument.basis,
            bump_details={"method": "analytic", "bump_required": False},
        ),
        "gamma": make_risk_value(
            raw_price_to_points_100(analytic_values["gamma"], instrument.basis),
            unit="pv_points_100_per_spot_squared",
            bump=None,
            difference="analytic",
            basis=instrument.basis,
            bump_details={"method": "analytic", "bump_required": False},
        ),
        "theta": make_risk_value(
            raw_price_to_points_100(analytic_values["theta"], instrument.basis) / 365.0,
            unit="pv_points_100_per_calendar_day",
            bump=None,
            difference="analytic",
            basis=instrument.basis,
            time_basis="calendar_day",
            bump_details={"method": "analytic", "bump_required": False},
        ),
        "vega": make_risk_value(
            raw_price_to_points_100(analytic_values["vega"], instrument.basis) * 0.01,
            unit="pv_points_100_per_1pct_volatility",
            bump=None,
            difference="analytic",
            basis=instrument.basis,
            bump_details={"method": "analytic", "bump_required": False},
        ),
        "rho": make_risk_value(
            raw_price_to_points_100(analytic_values["rho"], instrument.basis) * 0.01,
            unit="pv_points_100_per_1pct_rate",
            bump=None,
            difference="analytic",
            basis=instrument.basis,
            bump_details={
                "method": "analytic",
                "bump_required": False,
                "held_constant": (
                    "future_price"
                    if instrument.future
                    else "carry"
                    if market.carry is not None
                    else "dividend_yield"
                ),
            },
        ),
    }
    greek_diagnostics["core_method"] = "analytic"
    greek_diagnostics["extended_method"] = "central_bump_and_revalue"
    warnings = list(converted.warnings)
    diagnostics = {
        "engine": "standard.vanilla",
        "runtime": "STANDARD_ONLY",
        "model": "Black-76" if instrument.future else "Black-Scholes",
        "carry_convention": (
            "explicit_carry" if market.carry is not None else "risk_free_minus_dividend"
        ),
        "black_scholes_analytics": asdict(analytics),
        "standard_greeks": greek_diagnostics,
    }
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=config.method,
        version=_VERSION,
        extended_greeks=extended_greeks,
        warnings=tuple(warnings),
        diagnostics=diagnostics,
        engine_raw={
            "price": raw,
            "unit": "POINTS_100",
            "note": "STANDARD raw output retained in the shared raw-result field",
        },
    )


def price_vanilla_monte_carlo(
    instrument: EuropeanVanillaOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    """同一EuropeanVanillaOption的固定随机源风险中性MC。"""
    del valuation_state
    if config.method is not PricingMethod.MONTE_CARLO_CPU:
        raise ValueError("Vanilla MC仅支持MONTE_CARLO_CPU")
    source = default_random_source()
    if config.seed != source.info.seed:
        raise ValueError(f"Vanilla MC固定seed={source.info.seed}")
    z = source.load(config.paths, 1)[:, 0]
    sign = 1.0 if instrument.call_put is CallPut.CALL else -1.0
    tau = instrument.maturity_years
    q = effective_dividend_yield(market, future=instrument.future)

    def values(local: MarketState, maturity: float = tau) -> np.ndarray:
        carry = local.risk_free_rate - effective_dividend_yield(
            local, future=instrument.future
        )
        terminal = local.spot * np.exp(
            (carry - 0.5 * local.volatility ** 2) * maturity
            + local.volatility * math.sqrt(maturity) * z
        )
        payoff = np.maximum(sign * (terminal - instrument.strike), 0.0)
        return math.exp(-local.risk_free_rate * maturity) * payoff

    path_values = values(market)
    raw = float(np.mean(path_values))
    converted = convert_points_100(raw, instrument.basis)
    raw_standard_error = float(np.std(path_values, ddof=1) / math.sqrt(config.paths))
    converted_standard_error = convert_points_100(raw_standard_error, instrument.basis)
    bump = StandardGreekConvention.from_valuation_config(config)
    ds = market.spot * bump.spot_relative_bump
    dv = min(bump.volatility_absolute_bump, market.volatility / 2)
    dr = bump.risk_free_rate_absolute_bump
    up = float(np.mean(values(replace(market, spot=market.spot + ds))))
    down = float(np.mean(values(replace(market, spot=market.spot - ds))))
    vup = float(np.mean(values(replace(market, volatility=market.volatility + dv))))
    vdown = float(np.mean(values(replace(market, volatility=market.volatility - dv))))
    rup = float(np.mean(values(replace(market, risk_free_rate=market.risk_free_rate + dr))))
    rdown = float(np.mean(values(replace(market, risk_free_rate=market.risk_free_rate - dr))))
    rolled_tau = tau - bump.theta_calendar_day_shift / 365
    if rolled_tau <= 0:
        raise ValueError("Vanilla MC Theta推进后不得到期")
    rolled = float(np.mean(values(market, rolled_tau)))
    greeks = {
        "delta": make_risk_value(
            (up - down) / (2 * ds), unit="pv_points_100_per_spot", bump=ds,
            difference="common_random_numbers_central", basis=instrument.basis,
        ),
        "gamma": make_risk_value(
            (up - 2 * raw + down) / (ds * ds),
            unit="pv_points_100_per_spot_squared", bump=ds,
            difference="common_random_numbers_central", basis=instrument.basis,
        ),
        "vega": make_risk_value(
            (vup - vdown) / (2 * dv) * 0.01,
            unit="pv_points_100_per_1pct_volatility", bump=dv,
            difference="common_random_numbers_central", basis=instrument.basis,
        ),
        "theta": make_risk_value(
            (rolled - raw) / bump.theta_calendar_day_shift,
            unit="pv_points_100_per_calendar_day",
            bump=float(bump.theta_calendar_day_shift),
            difference="common_random_numbers_forward_roll", basis=instrument.basis,
            time_basis="calendar_day",
        ),
        "rho": make_risk_value(
            (rup - rdown) / (2 * dr) * 0.01,
            unit="pv_points_100_per_1pct_rate", bump=dr,
            difference="common_random_numbers_central", basis=instrument.basis,
        ),
    }
    terminal = market.spot * np.exp(
        (market.risk_free_rate - q - 0.5 * market.volatility ** 2) * tau
        + market.volatility * math.sqrt(tau) * z
    )
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=config.method,
        version="standard-vanilla-mc-1",
        warnings=converted.warnings,
        diagnostics={
            "engine": "standard.vanilla_mc",
            "paths": config.paths,
            "standard_error_points_100": raw_standard_error,
            "random_source": asdict(source.info),
            "path_pv_sha256_float64": hashlib.sha256(np.ascontiguousarray(path_values).tobytes()).hexdigest(),
            "model_in_the_money_probability": float(np.mean(sign * (terminal - instrument.strike) > 0)),
        },
        engine_raw={"price": raw, "unit": "POINTS_100"},
        standard_error=converted_standard_error.pv_amount,
    )
