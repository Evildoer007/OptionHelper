"""Deterministic maturity cashflow used by exact static replications."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import FixedCashflowOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import ThetaRollValue, calculate_standard_greeks, raw_price_to_points_100


_IMPLEMENTATION_ID = "standard-fixed-cashflow"


def _raw_price(instrument: FixedCashflowOption, market: MarketState) -> float:
    return instrument.amount * math.exp(-market.risk_free_rate * instrument.maturity_years)


def price_fixed_cashflow_standard(
    instrument: FixedCashflowOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    del valuation_state
    if config.method is not PricingMethod.DISCOUNTED_CASHFLOW:
        raise ValueError("FixedCashflow STANDARD仅支持DISCOUNTED_CASHFLOW")

    raw = _raw_price(instrument, market)
    converted = convert_points_100(raw, instrument.basis)

    def price_market(changed_market: MarketState) -> float:
        return raw_price_to_points_100(_raw_price(instrument, changed_market), instrument.basis)

    def theta_roll(convention) -> ThetaRollValue:
        day_shift = convention.theta_calendar_day_shift
        remaining = instrument.maturity_years - day_shift / 365.0
        if remaining <= 0.0:
            raise ValueError("FixedCashflow Theta推进后不得到期或过期")
        rolled = replace(instrument, maturity_years=remaining)
        rolled_market = replace(market, as_of=market.as_of + timedelta(days=day_shift))
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(
                _raw_price(rolled, rolled_market), instrument.basis,
            ),
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
            "engine": "standard.fixed_cashflow",
            "runtime": "STANDARD_ONLY",
            "model": "deterministic discounted cashflow",
            "standard_greeks": greek_diagnostics,
        },
        engine_raw={"price": raw, "unit": "POINTS_100"},
    )
