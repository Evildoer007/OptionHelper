"""Independent analytic STANDARD engine for European binary options."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

from ..basis import convert_points_100
from ..enums import CallPut, PricingMethod
from ..instruments import BinaryOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import (
    ThetaRollValue,
    calculate_standard_greeks,
    effective_dividend_yield,
    raw_price_to_points_100,
)


_IMPLEMENTATION_ID = "standard-digital"
_CASH_OR_NOTHING = "Cash-or-Nothing"
_ASSET_OR_NOTHING = "Asset-or-Nothing"


def _normal_cdf(value: float) -> float:
    """Hart双精度正态CDF，用于稳定复现解析价格精度。"""
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


def _raw_price(instrument: BinaryOption, market: MarketState) -> float:
    if market.volatility <= 0.0:
        raise ValueError("Binary STANDARD Greek要求volatility大于0")
    spot = market.spot
    strike = instrument.strike
    maturity = instrument.maturity_years
    volatility = market.volatility
    rate = market.risk_free_rate
    dividend = effective_dividend_yield(market, future=instrument.future)
    root_time = math.sqrt(maturity)
    if instrument.future:
        d1 = (
            math.log(spot / strike) + 0.5 * volatility ** 2 * maturity
        ) / (volatility * root_time)
        discount_asset = math.exp(-rate * maturity)
    else:
        d1 = (
            math.log(spot / strike)
            + (rate - dividend + 0.5 * volatility ** 2) * maturity
        ) / (volatility * root_time)
        discount_asset = math.exp(-dividend * maturity)
    d2 = d1 - volatility * root_time
    discount_rate = math.exp(-rate * maturity)
    call = instrument.call_put is CallPut.CALL
    if instrument.payout_type == _CASH_OR_NOTHING:
        probability = _normal_cdf(d2 if call else -d2)
        return instrument.payout * discount_rate * probability
    if instrument.payout_type == _ASSET_OR_NOTHING:
        probability = _normal_cdf(d1 if call else -d1)
        return spot * discount_asset * probability
    raise ValueError("payout_type必须为Cash-or-Nothing或Asset-or-Nothing")


def price_binary_standard(
    instrument: BinaryOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    del valuation_state
    if config.method is not PricingMethod.BINARY_ANALYTIC:
        raise ValueError("Binary STANDARD仅支持BINARY_ANALYTIC")

    raw = _raw_price(instrument, market)
    converted = convert_points_100(raw, instrument.basis)

    def price_market(changed_market: MarketState) -> float:
        return raw_price_to_points_100(
            _raw_price(instrument, changed_market), instrument.basis
        )

    def theta_roll(convention) -> ThetaRollValue:
        day_shift = convention.theta_calendar_day_shift
        remaining = instrument.maturity_years - day_shift / 365.0
        if remaining <= 0.0:
            raise ValueError("Binary Theta推进后不得到期或过期")
        rolled_raw = _raw_price(
            replace(instrument, maturity_years=remaining),
            replace(market, as_of=market.as_of + timedelta(days=day_shift)),
        )
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(rolled_raw, instrument.basis),
            calendar_day_shift=day_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description="maturity_years减少calendar_day_shift/365",
        )

    greeks, extended_greeks, greek_diagnostics = calculate_standard_greeks(
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
        method=config.method,
        implementation_id=_IMPLEMENTATION_ID,
        extended_greeks=extended_greeks,
        warnings=converted.warnings,
        diagnostics={
            "engine": "standard.digital",
            "runtime": "STANDARD_ONLY",
            "model": "Binary Black-76" if instrument.future else "Binary Black-Scholes",
            "standard_greeks": greek_diagnostics,
        },
        engine_raw={"price": raw, "unit": "POINTS_100"},
    )
