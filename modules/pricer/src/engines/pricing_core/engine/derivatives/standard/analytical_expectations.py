"""Deterministic risk-neutral expectations for three registered structures."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta
from itertools import pairwise

from scipy.special import ndtr
from scipy.stats import multivariate_normal

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import RangeAccrualOption, VarianceSwapOption, WorstOfCallOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import (
    ThetaNotApplicableError,
    ThetaRollValue,
    calculate_standard_greeks,
    raw_price_to_points_100,
)


def _bivariate_cdf(x: float, y: float, correlation: float) -> float:
    return float(multivariate_normal.cdf((x, y), mean=(0.0, 0.0), cov=((1.0, correlation), (correlation, 1.0))))


def _worst_of_price(instrument: WorstOfCallOption, market: MarketState) -> float:
    """Exact two-lognormal call-on-minimum value under constant parameters."""
    base_spot = instrument.normalized_spots[0]
    scale = market.spot / base_spot
    spots = tuple(value * scale for value in instrument.normalized_spots)
    volatility_shift = market.volatility - instrument.volatilities[0]
    volatilities = tuple(max(value + volatility_shift, 1e-8) for value in instrument.volatilities)
    dividend_shift = market.dividend_yield - instrument.dividend_yields[0]
    dividends = tuple(value + dividend_shift for value in instrument.dividend_yields)
    rho = float(instrument.correlation)
    maturity = float(instrument.maturity_years)
    strike_log = math.log(float(instrument.strike))
    variances = tuple(value * value * maturity for value in volatilities)
    covariance = rho * volatilities[0] * volatilities[1] * maturity
    means = tuple(
        math.log(spots[index])
        + (market.risk_free_rate - dividends[index] - 0.5 * volatilities[index] ** 2) * maturity
        for index in range(2)
    )
    difference_variance = variances[0] + variances[1] - 2.0 * covariance
    if difference_variance <= 1e-16:
        raise ValueError("Worst-Of解析解要求两标的相对波动不为零")
    difference_deviation = math.sqrt(difference_variance)

    tilted_terms: list[float] = []
    for index, other in ((0, 1), (1, 0)):
        shifted = (
            means[0] + (variances[0] if index == 0 else covariance),
            means[1] + (covariance if index == 0 else variances[1]),
        )
        asset_deviation = math.sqrt(variances[index])
        threshold = (strike_log - shifted[index]) / asset_deviation
        difference_mean = shifted[index] - shifted[other]
        difference_threshold = -difference_mean / difference_deviation
        difference_covariance = variances[index] - covariance
        joint_correlation = difference_covariance / (asset_deviation * difference_deviation)
        probability = float(ndtr(difference_threshold)) - _bivariate_cdf(
            threshold, difference_threshold, joint_correlation,
        )
        tilted_terms.append(spots[index] * math.exp(-dividends[index] * maturity) * probability)

    thresholds = tuple(
        (strike_log - means[index]) / math.sqrt(variances[index])
        for index in range(2)
    )
    joint_survival = (
        1.0 - float(ndtr(thresholds[0])) - float(ndtr(thresholds[1]))
        + _bivariate_cdf(thresholds[0], thresholds[1], rho)
    )
    return max(0.0, sum(tilted_terms) - instrument.strike * math.exp(-market.risk_free_rate * maturity) * joint_survival)


def _variance_price(instrument: VarianceSwapOption, market: MarketState) -> float:
    times = instrument.observation_times
    intervals = tuple(right - left for left, right in pairwise(times))
    drift = market.risk_free_rate - market.dividend_yield - 0.5 * market.volatility ** 2
    expected_squared_returns = list(
        market.volatility ** 2 * interval + drift ** 2 * interval ** 2
        for interval in intervals
    )
    if instrument.last_observation_spot is not None:
        boundary_return = math.log(market.spot / instrument.last_observation_spot)
        expected_squared_returns[0] = (
            market.volatility ** 2 * intervals[0]
            + (boundary_return + drift * intervals[0]) ** 2
        )
    realized_variance_points = (
        instrument.annualization_days
        * (instrument.historical_squared_returns + sum(expected_squared_returns))
        / (instrument.historical_return_count + len(expected_squared_returns))
        * 10_000.0
    )
    payoff = realized_variance_points - instrument.strike_volatility ** 2
    return payoff * math.exp(-market.risk_free_rate * instrument.maturity_years)


def _range_accrual_price(instrument: RangeAccrualOption, market: MarketState) -> float:
    probabilities: list[float] = []
    for observation_time in instrument.observation_times:
        if observation_time == 0.0 and instrument.historical_observation_count:
            continue
        if observation_time == 0.0:
            probabilities.append(float(instrument.lower <= market.spot <= instrument.upper))
            continue
        deviation = market.volatility * math.sqrt(observation_time)
        mean = math.log(market.spot) + (
            market.risk_free_rate - market.dividend_yield - 0.5 * market.volatility ** 2
        ) * observation_time
        lower = (math.log(instrument.lower) - mean) / deviation
        upper = (math.log(instrument.upper) - mean) / deviation
        probabilities.append(float(ndtr(upper) - ndtr(lower)))
    coupon_points = 100.0 * instrument.maximum_coupon * (
        instrument.historical_in_count + sum(probabilities)
    ) / (instrument.historical_observation_count + len(probabilities))
    return coupon_points * math.exp(-market.risk_free_rate * instrument.maturity_years)


def _rolled_times(values: tuple[float, ...], day_shift: int, *, minimum_count: int) -> tuple[float, ...]:
    shift = day_shift / 365.0
    rolled = tuple(value - shift for value in values if value > shift)
    if len(rolled) < minimum_count:
        raise ThetaNotApplicableError("观察日程在Theta推进后不足以完成定价")
    return rolled


def _roll_observation_instrument(instrument, days: int, *, minimum_count: int):
    remaining = instrument.maturity_years - days / 365.0
    has_history = (
        getattr(instrument, "last_observation_spot", None) is not None
        or getattr(instrument, "historical_observation_count", 0) > 0
    )
    if has_history:
        # Same expiry sensitivity as the path engine: retain actual facts
        # and shorten the future contract window. A valuation-date roll
        # would require newly observed prices that are not available here.
        times = tuple(value for value in instrument.observation_times if value <= remaining + 1e-12)
        if len(times) < max(2, minimum_count):
            raise ThetaNotApplicableError("历史状态下缩短期限后没有未来观察")
    else:
        times = _rolled_times(instrument.observation_times, days, minimum_count=minimum_count)
    return replace(instrument, maturity_years=remaining, observation_times=times)


def _price_with_standard_risk(
    instrument,
    market: MarketState,
    config: ValuationConfig,
    *,
    expected_method: PricingMethod,
    implementation_id: str,
    model: str,
    raw_pricer,
    theta_instrument,
) -> PricingResult:
    if config.method is not expected_method:
        raise ValueError(f"{type(instrument).__name__}不支持{config.method.name}")
    raw = float(raw_pricer(instrument, market))
    converted = convert_points_100(raw, instrument.basis)

    def price_market(changed_market: MarketState) -> float:
        return raw_price_to_points_100(raw_pricer(instrument, changed_market), instrument.basis)

    def theta_roll(convention) -> ThetaRollValue:
        rolled_instrument = theta_instrument(instrument, convention.theta_calendar_day_shift)
        rolled_market = replace(market, as_of=market.as_of + timedelta(days=convention.theta_calendar_day_shift))
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(raw_pricer(rolled_instrument, rolled_market), instrument.basis),
            calendar_day_shift=convention.theta_calendar_day_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description=(
                "保留已实现观察，缩短剩余合同期限"
                if getattr(instrument, "last_observation_spot", None) is not None or getattr(instrument, "historical_observation_count", 0)
                else "合同剩余期限与观察日程同步推进"
            ),
        )

    greeks, extended, diagnostics = calculate_standard_greeks(
        base_price_points_100=converted.pv_points_100,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
    )
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        extended_greeks=extended,
        method=config.method,
        implementation_id=implementation_id,
        warnings=converted.warnings,
        diagnostics={
            "engine": "standard.analytical_expectations",
            "runtime": "STANDARD_ONLY",
            "model": model,
            "standard_greeks": diagnostics,
        },
        engine_raw={"price": raw, "unit": "POINTS_100"},
    )


def price_worst_of_standard(instrument: WorstOfCallOption, market: MarketState, config: ValuationConfig, valuation_state: ValuationState | None = None) -> PricingResult:
    del valuation_state
    return _price_with_standard_risk(
        instrument, market, config,
        expected_method=PricingMethod.WORST_OF_ANALYTIC,
        implementation_id="standard-worst-of-two-lognormal",
        model="two-lognormal call on minimum",
        raw_pricer=_worst_of_price,
        theta_instrument=lambda value, days: replace(value, maturity_years=value.maturity_years - days / 365.0),
    )


def price_variance_swap_standard(instrument: VarianceSwapOption, market: MarketState, config: ValuationConfig, valuation_state: ValuationState | None = None) -> PricingResult:
    del valuation_state
    return _price_with_standard_risk(
        instrument, market, config,
        expected_method=PricingMethod.VARIANCE_EXPECTATION,
        implementation_id="standard-expected-realized-variance",
        model="exact expected squared log returns on frozen sessions",
        raw_pricer=_variance_price,
        theta_instrument=lambda value, days: _roll_observation_instrument(value, days, minimum_count=2),
    )


def price_range_accrual_standard(instrument: RangeAccrualOption, market: MarketState, config: ValuationConfig, valuation_state: ValuationState | None = None) -> PricingResult:
    del valuation_state
    return _price_with_standard_risk(
        instrument, market, config,
        expected_method=PricingMethod.RANGE_ACCRUAL_ANALYTIC,
        implementation_id="standard-range-accrual-marginal-expectation",
        model="sum of exact marginal lognormal in-range probabilities",
        raw_pricer=_range_accrual_price,
        theta_instrument=lambda value, days: _roll_observation_instrument(value, days, minimum_count=1),
    )


__all__ = (
    "price_range_accrual_standard",
    "price_variance_swap_standard",
    "price_worst_of_standard",
)
