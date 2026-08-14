"""Independent Reiner-Rubinstein STANDARD engine for European barriers."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import BarrierOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import (
    ThetaRollValue,
    calculate_standard_greeks,
    effective_dividend_yield,
    raw_price_to_points_100,
)


_IMPLEMENTATION_ID = "standard-barrier"
_FREQUENCY_TO_DT = {
    "Continuous": 0.0,
    "HalfDaily": 1 / 488,
    "Daily": 1 / 244,
    "Weekly": 1 / 52,
    "Monthly": 1 / 12,
}


def _normal_cdf(value: float) -> float:
    """Hart双精度正态CDF。"""
    magnitude = abs(value)
    if magnitude > 37:
        result = 0.0
    else:
        exponential = math.exp(-(magnitude ** 2) / 2)
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
            denominator = magnitude + 4 / denominator
            denominator = magnitude + 3 / denominator
            denominator = magnitude + 2 / denominator
            denominator = magnitude + 1 / denominator
            result = exponential / (denominator * 2.506628274631)
    if value > 0:
        result = 1 - result
    return result


def _vanilla_price(
    spot: float,
    strike: float,
    maturity: float,
    volatility: float,
    rate: float,
    future: bool,
    call_put: str,
    dividend: float,
) -> float:
    """Vanilla boundary price used when spot equals the barrier."""
    if future:
        d1 = (
            math.log(spot / strike) + (volatility ** 2 / 2) * maturity
        ) / (volatility * math.sqrt(maturity))
    else:
        d1 = (
            math.log(spot / strike)
            + (rate - dividend + volatility ** 2 / 2) * maturity
        ) / (volatility * math.sqrt(maturity))
    d2 = d1 - volatility * math.sqrt(maturity)
    if call_put == "Call":
        if future:
            return (
                spot * math.exp(-rate * maturity) * _normal_cdf(d1)
                - strike * math.exp(-rate * maturity) * _normal_cdf(d2)
            )
        return (
            spot * math.exp(-dividend * maturity) * _normal_cdf(d1)
            - strike * math.exp(-rate * maturity) * _normal_cdf(d2)
        )
    if call_put == "Put":
        if future:
            return (
                strike * math.exp(-rate * maturity) * _normal_cdf(-d2)
                - spot * math.exp(-rate * maturity) * _normal_cdf(-d1)
            )
        return (
            strike * math.exp(-rate * maturity) * _normal_cdf(-d2)
            - spot * math.exp(-dividend * maturity) * _normal_cdf(-d1)
        )
    return 0.0


def _premium(
    spot: float,
    strike: float,
    barrier: float,
    maturity: float,
    volatility: float,
    rate: float,
    rebate: float,
    future: bool,
    call_put: str,
    knock: str,
    monitoring: str,
    rebate_at_hit: bool,
    dividend: float,
) -> float:
    """BGK-adjusted Reiner-Rubinstein construction blocks."""
    observation_dt = _FREQUENCY_TO_DT.get(monitoring, 0.0)

    if spot > barrier:
        adjusted_barrier = barrier * math.exp(
            -0.5826 * volatility * math.sqrt(observation_dt)
        )
    elif spot < barrier:
        adjusted_barrier = barrier * math.exp(
            0.5826 * volatility * math.sqrt(observation_dt)
        )
    else:
        adjusted_barrier = barrier

    if future:
        dividend = rate
    carry = rate - dividend

    miu = (carry - volatility ** 2 / 2) / (volatility ** 2)
    lambda_ = math.sqrt(miu ** 2 + 2 * rate / (volatility ** 2))
    z = (
        math.log(adjusted_barrier / spot) / (volatility * math.sqrt(maturity))
        + lambda_ * volatility * math.sqrt(maturity)
    )
    x1 = (
        math.log(spot / strike) / (volatility * math.sqrt(maturity))
        + (1 + miu) * volatility * math.sqrt(maturity)
    )
    x2 = (
        math.log(spot / adjusted_barrier) / (volatility * math.sqrt(maturity))
        + (1 + miu) * volatility * math.sqrt(maturity)
    )
    y1 = (
        math.log(adjusted_barrier ** 2 / (spot * strike))
        / (volatility * math.sqrt(maturity))
        + (1 + miu) * volatility * math.sqrt(maturity)
    )
    y2 = (
        math.log(adjusted_barrier / spot) / (volatility * math.sqrt(maturity))
        + (1 + miu) * volatility * math.sqrt(maturity)
    )

    i1 = 1 if call_put == "Call" else -1
    if spot > barrier:
        i2 = 1
    elif spot < barrier:
        i2 = -1
    else:
        i2 = 0
    i3, i4 = (1, 0) if rebate_at_hit else (0, 1)

    alpha = (carry - 0.5 * volatility ** 2) / volatility
    beta = (
        math.log(adjusted_barrier / spot) / volatility
        if spot != adjusted_barrier
        else 0.0
    )
    volatility_root_time = volatility * math.sqrt(maturity)

    aa = (
        i1 * spot * math.exp((carry - rate) * maturity) * _normal_cdf(i1 * x1)
        - i1 * strike * math.exp(-rate * maturity)
        * _normal_cdf(i1 * x1 - i1 * volatility_root_time)
    )
    bb = (
        i1 * spot * math.exp((carry - rate) * maturity) * _normal_cdf(i1 * x2)
        - i1 * strike * math.exp(-rate * maturity)
        * _normal_cdf(i1 * x2 - i1 * volatility_root_time)
    )
    cc = (
        i1 * spot * math.exp((carry - rate) * maturity)
        * (adjusted_barrier / spot) ** (2 * (miu + 1))
        * _normal_cdf(i2 * y1)
        - i1 * strike * math.exp(-rate * maturity)
        * (adjusted_barrier / spot) ** (2 * miu)
        * _normal_cdf(i2 * y1 - i2 * volatility_root_time)
    )
    dd = (
        i1 * spot * math.exp((carry - rate) * maturity)
        * (adjusted_barrier / spot) ** (2 * (miu + 1))
        * _normal_cdf(i2 * y2)
        - i1 * strike * math.exp(-rate * maturity)
        * (adjusted_barrier / spot) ** (2 * miu)
        * _normal_cdf(i2 * y2 - i2 * volatility_root_time)
    )
    ee = rebate * math.exp(-rate * maturity) * (
        _normal_cdf(i2 * x2 - i2 * volatility_root_time)
        - (adjusted_barrier / spot) ** (2 * miu)
        * _normal_cdf(i2 * y2 - i2 * volatility_root_time)
    )
    ff = rebate * (
        (adjusted_barrier / spot) ** (miu + lambda_) * _normal_cdf(i2 * z)
        + (adjusted_barrier / spot) ** (miu - lambda_)
        * _normal_cdf(i2 * z - 2 * i2 * lambda_ * volatility_root_time)
    )

    discounted_rebate = rebate * math.exp(-rate * maturity)
    pmax = (
        1 - _normal_cdf((beta - alpha * maturity) / math.sqrt(maturity))
        + math.exp(2 * alpha * beta)
        * _normal_cdf((-beta - alpha * maturity) / math.sqrt(maturity))
    )
    pmin = (
        1 - _normal_cdf((-beta + alpha * maturity) / math.sqrt(maturity))
        + math.exp(2 * alpha * beta)
        * _normal_cdf((beta + alpha * maturity) / math.sqrt(maturity))
    )
    mo = discounted_rebate * pmax
    mi = discounted_rebate * (1 - pmax)
    no = discounted_rebate * pmin
    ni = discounted_rebate * (1 - pmin)

    if spot < barrier:
        if knock == "In":
            if call_put == "Call":
                if strike < barrier:
                    return bb - cc + dd + i3 * ee + i4 * mi
                return aa + i3 * ee + i4 * mi
            if call_put == "Put":
                if strike < barrier:
                    return cc + i3 * ee + i4 * mi
                return aa - bb + dd + i3 * ee + i4 * mi
        if knock == "Out":
            if call_put == "Call":
                if strike < barrier:
                    return aa - bb + cc - dd + i3 * ff + i4 * mo
                return i3 * ff + i4 * mo
            if call_put == "Put":
                if strike < barrier:
                    return aa - cc + i3 * ff + i4 * mo
                return bb - dd + i3 * ff + i4 * mo

    if spot > barrier:
        if knock == "In":
            if call_put == "Call":
                if strike < barrier:
                    return aa - bb + dd + i3 * ee + i4 * ni
                return cc + i3 * ee + i4 * ni
            if call_put == "Put":
                if strike < barrier:
                    return aa + i3 * ee + i4 * ni
                return bb - cc + dd + i3 * ee + i4 * ni
        if knock == "Out":
            if call_put == "Call":
                if strike < barrier:
                    return bb - dd + i3 * ff + i4 * no
                return aa - cc + i3 * ff + i4 * no
            if call_put == "Put":
                if strike < barrier:
                    return i3 * ff + i4 * no
                return aa - bb + cc - dd + i3 * ff + i4 * no

    if spot == barrier:
        if knock == "In":
            return _vanilla_price(
                spot,
                strike,
                maturity,
                volatility,
                rate,
                future,
                call_put,
                dividend,
            )
        if knock == "Out":
            return i3 * ff + i4 * math.exp(-rate * maturity) * rebate
    return 0.0


def _raw_price_and_greeks(
    instrument: BarrierOption,
    market: MarketState,
) -> tuple[float, dict[str, float]]:
    arguments = (
        market.spot,
        instrument.strike,
        instrument.barrier,
        instrument.maturity_years,
        market.volatility,
        market.risk_free_rate,
        instrument.rebate,
        instrument.future,
        instrument.call_put.value,
        instrument.knock,
        instrument.monitoring,
        instrument.rebate_at_hit,
        effective_dividend_yield(market, future=instrument.future),
    )
    raw = _premium(*arguments)
    spot = market.spot
    spot_up = _premium(spot * 1.005, *arguments[1:])
    spot_down = _premium(spot * 0.995, *arguments[1:])
    delta = (spot_up - spot_down) / (spot * 0.01)
    gamma = (spot_up - 2 * raw + spot_down) / ((spot * 0.01) ** 2)
    theta_arguments = (
        spot,
        instrument.strike,
        instrument.barrier,
        instrument.maturity_years - 1 / 244,
        market.volatility,
        market.risk_free_rate,
        instrument.rebate,
        instrument.future,
        instrument.call_put.value,
        instrument.knock,
        instrument.monitoring,
        instrument.rebate_at_hit,
        market.dividend_yield,
    )
    theta = _premium(*theta_arguments) - raw
    vega_arguments = list(arguments)
    vega_arguments[4] = market.volatility + 0.01
    vega = _premium(*vega_arguments) - raw
    rho_arguments = list(arguments)
    rho_arguments[5] = market.risk_free_rate + 0.01
    rho = _premium(*rho_arguments) - raw
    return raw, {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def price_barrier_standard(
    instrument: BarrierOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    """Price one barrier with the STANDARD analytic engine."""
    del valuation_state
    if config.method is not PricingMethod.REINER_RUBINSTEIN:
        raise ValueError("Barrier STANDARD仅支持REINER_RUBINSTEIN")
    if instrument.maturity_years <= 1 / 365:
        raise ValueError("Barrier Theta要求maturity_years大于1/365")

    raw, _ = _raw_price_and_greeks(instrument, market)
    converted = convert_points_100(raw, instrument.basis)
    if converted.pv_points_100 is None:
        raise ValueError("Barrier STANDARD Greek要求可转换为pv_points_100的basis")

    def raw_price(changed_instrument: BarrierOption, changed_market: MarketState) -> float:
        return _premium(
            changed_market.spot,
            changed_instrument.strike,
            changed_instrument.barrier,
            changed_instrument.maturity_years,
            changed_market.volatility,
            changed_market.risk_free_rate,
            changed_instrument.rebate,
            changed_instrument.future,
            changed_instrument.call_put.value,
            changed_instrument.knock,
            changed_instrument.monitoring,
            changed_instrument.rebate_at_hit,
            effective_dividend_yield(
                changed_market, future=changed_instrument.future
            ),
        )

    def price_market(changed_market: MarketState) -> float:
        return raw_price_to_points_100(
            raw_price(instrument, changed_market), instrument.basis
        )

    def theta_roll(convention) -> ThetaRollValue:
        day_shift = convention.theta_calendar_day_shift
        remaining = instrument.maturity_years - day_shift / 365.0
        if remaining <= 0.0:
            raise ValueError("Barrier Theta推进后不得到期或过期")
        rolled_raw = raw_price(
            replace(instrument, maturity_years=remaining),
            replace(market, as_of=market.as_of + timedelta(days=day_shift)),
        )
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(rolled_raw, instrument.basis),
            calendar_day_shift=day_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description="maturity_years减少calendar_day_shift/365并重新执行BGK修正",
        )

    greeks, extended_greeks, greek_diagnostics = calculate_standard_greeks(
        base_price_points_100=converted.pv_points_100,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
    )
    warnings = list(converted.warnings)
    warnings.append("障碍Greek每次扰动重定价均重新执行BGK离散障碍修正")
    if instrument.future:
        warnings.append("期货障碍价格按Black-76输入约定处理")
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=config.method,
        implementation_id=_IMPLEMENTATION_ID,
        extended_greeks=extended_greeks,
        warnings=tuple(warnings),
        diagnostics={
            "engine": "standard.barrier",
            "runtime": "STANDARD_ONLY",
            "model": "BGK-adjusted Reiner-Rubinstein",
            "carry_convention": (
                "explicit_carry"
                if market.carry is not None
                else "risk_free_minus_dividend"
            ),
            "monitoring": instrument.monitoring,
            "observation_interval_years": _FREQUENCY_TO_DT[instrument.monitoring],
            "standard_greeks": greek_diagnostics,
        },
        engine_raw={
            "price": raw,
            "unit": "POINTS_100",
            "note": "STANDARD raw output retained in the shared raw-result field",
        },
    )
