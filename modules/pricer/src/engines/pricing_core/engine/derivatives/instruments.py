"""Immutable derivative contract terms, separated from live market data."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from ._validation import (
    require_finite_real,
    require_nonnegative_real,
    require_plain_int,
    require_positive_real,
)
from .basis import ResultBasis
from .enums import (
    AccumulatorQuantityBasis,
    AutocallKind,
    CallPut,
    PricingMethod,
)
from .models import SchedulePoint


@dataclass(frozen=True, kw_only=True)
class OptionInstrument:
    basis: ResultBasis


@dataclass(frozen=True, kw_only=True)
class FixedCashflowOption(OptionInstrument):
    """One deterministic contractual payment at maturity, in 100-point units."""

    amount: float
    maturity_years: float

    def __post_init__(self) -> None:
        _require_finite("amount", self.amount)
        _require_positive("maturity_years", self.maturity_years)


@dataclass(frozen=True, kw_only=True)
class OptionRegPathOption(OptionInstrument):
    """A resolved OptionReg contract valued by the shared path interpreter.

    The contract remains immutable.  This is deliberately one generic
    instrument rather than a class per OptionReg product.
    """

    resolved_contract: Any
    asset_spots: tuple[float, ...] = ()
    asset_volatilities: tuple[float, ...] = ()
    asset_dividend_yields: tuple[float, ...] = ()
    correlation: tuple[tuple[float, ...], ...] | None = None
    trading_sessions: tuple[str, ...] = ()
    calendar_id: str = ""
    calendar_revision: str = ""


@dataclass(frozen=True, kw_only=True)
class EuropeanVanillaOption(OptionInstrument):
    strike: float
    maturity_years: float
    call_put: CallPut
    future: bool = False

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("maturity_years", self.maturity_years)


@dataclass(frozen=True, kw_only=True)
class BinaryOption(OptionInstrument):
    strike: float
    maturity_years: float
    call_put: CallPut
    payout_type: str
    payout: float = 0.0
    future: bool = False

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("maturity_years", self.maturity_years)
        _require_finite("payout", self.payout)


@dataclass(frozen=True, kw_only=True)
class BarrierOption(OptionInstrument):
    strike: float
    barrier: float
    maturity_years: float
    call_put: CallPut
    knock: str
    monitoring: str
    rebate: float = 0.0
    rebate_at_hit: bool = True
    future: bool = False

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("barrier", self.barrier)
        _require_positive("maturity_years", self.maturity_years)
        _require_finite("rebate", self.rebate)


@dataclass(frozen=True, kw_only=True)
class StaticAccumulatorOption(OptionInstrument):
    call_put: CallPut
    initial_spot: float
    strike: float
    barrier: float
    range_payout: float
    knockout_payout: float
    loss_multiplier: float
    first_observation: int
    observation_count: int
    total_observations: int
    accumulator_type: str
    expiry_multiplier: float = 1.0
    day_adjustment: float = 0.0
    quantity_basis: AccumulatorQuantityBasis = AccumulatorQuantityBasis.WHOLE_CONTRACT

    def __post_init__(self) -> None:
        _require_positive("initial_spot", self.initial_spot)
        _require_positive("strike", self.strike)
        _require_positive("barrier", self.barrier)
        require_nonnegative_real("range_payout", self.range_payout)
        require_nonnegative_real("knockout_payout", self.knockout_payout)
        _require_positive("loss_multiplier", self.loss_multiplier)
        _require_positive_int("first_observation", self.first_observation)
        _require_positive_int("observation_count", self.observation_count)
        _require_positive_int("total_observations", self.total_observations)
        if self.first_observation + self.observation_count - 1 > self.total_observations:
            raise ValueError("最后估值观察序号不得超过total_observations")
        _require_positive("expiry_multiplier", self.expiry_multiplier)
        require_nonnegative_real("day_adjustment", self.day_adjustment)
        if self.day_adjustment >= self.first_observation:
            raise ValueError("day_adjustment必须小于first_observation以保证期限为正")
        if self.quantity_basis is not AccumulatorQuantityBasis.WHOLE_CONTRACT:
            raise ValueError("Static Accumulator当前只支持WHOLE_CONTRACT数量口径")


@dataclass(frozen=True, kw_only=True)
class WorstOfCallOption(OptionInstrument):
    """两标的终值相对表现最小值看涨。"""

    strike: float
    maturity_years: float
    normalized_spots: tuple[float, float]
    volatilities: tuple[float, float]
    dividend_yields: tuple[float, float]
    correlation: float

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("maturity_years", self.maturity_years)
        for label, values in (
            ("normalized_spots", self.normalized_spots),
            ("volatilities", self.volatilities),
            ("dividend_yields", self.dividend_yields),
        ):
            if len(values) != 2:
                raise ValueError(f"{label}必须包含两个标的值")
        for value in self.normalized_spots:
            _require_positive("normalized_spots", value)
        for value in self.volatilities:
            _require_positive("volatilities", value)
        for value in self.dividend_yields:
            _require_finite("dividend_yields", value)
        _require_finite("correlation", self.correlation)
        if not -1.0 < float(self.correlation) < 1.0:
            raise ValueError("correlation必须严格位于-1和1之间")


@dataclass(frozen=True, kw_only=True)
class VarianceSwapOption(OptionInstrument):
    """按真实观察间隔年化的方差互换。"""

    strike_volatility: float
    annualization_days: int
    observation_times: tuple[float, ...]
    maturity_years: float
    historical_squared_returns: float = 0.0
    historical_return_count: int = 0
    last_observation_spot: float | None = None

    def __post_init__(self) -> None:
        _require_positive("strike_volatility", self.strike_volatility)
        _require_positive_int("annualization_days", self.annualization_days)
        _require_positive("maturity_years", self.maturity_years)
        if len(self.observation_times) < 2:
            raise ValueError("Variance Swap至少需要两个观察时点")
        _require_strict_times("observation_times", self.observation_times)
        require_nonnegative_real("historical_squared_returns", self.historical_squared_returns)
        require_plain_int("historical_return_count", self.historical_return_count, nonnegative=True)
        if self.last_observation_spot is not None:
            _require_positive("last_observation_spot", self.last_observation_spot)


@dataclass(frozen=True, kw_only=True)
class RangeAccrualOption(OptionInstrument):
    """逐观察日计息的区间累计结构。"""

    lower: float
    upper: float
    maximum_coupon: float
    observation_times: tuple[float, ...]
    maturity_years: float
    historical_in_count: int = 0
    historical_observation_count: int = 0

    def __post_init__(self) -> None:
        _require_positive("lower", self.lower)
        _require_positive("upper", self.upper)
        if self.lower >= self.upper:
            raise ValueError("Range Accrual下界必须小于上界")
        require_nonnegative_real("maximum_coupon", self.maximum_coupon)
        _require_positive("maturity_years", self.maturity_years)
        if not self.observation_times and not self.historical_observation_count:
            raise ValueError("Range Accrual观察时点不得为空")
        _require_nondecreasing_times("observation_times", self.observation_times)
        require_plain_int("historical_in_count", self.historical_in_count, nonnegative=True)
        require_plain_int("historical_observation_count", self.historical_observation_count, nonnegative=True)
        if self.historical_in_count > self.historical_observation_count:
            raise ValueError("历史区间内观察数不得超过历史观察数")


@dataclass(frozen=True, kw_only=True)
class AutocallOption(OptionInstrument):
    kind: AutocallKind
    call_put: CallPut
    strike: float
    knock_in: float
    knock_out: float
    floor: float
    coupon: float
    call_schedule: tuple[SchedulePoint, ...]
    coupon_schedule: tuple[SchedulePoint, ...]
    final_trading_day: int
    final_calendar_day: int
    final_rebate: float
    margin: float = 0.0
    knock_out_step_down: float | None = None
    forward_curve_weight: float = 1.0
    parachute: bool = False
    enhanced_strike: float | None = None
    participation: float = 0.0

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("knock_in", self.knock_in)
        _require_positive("knock_out", self.knock_out)
        require_nonnegative_real("floor", self.floor)
        _require_finite("coupon", self.coupon)
        _require_positive_int("final_trading_day", self.final_trading_day)
        _require_positive_int("final_calendar_day", self.final_calendar_day)
        _require_finite("final_rebate", self.final_rebate)
        _require_finite("margin", self.margin)
        if self.knock_out_step_down is not None:
            _require_finite("knock_out_step_down", self.knock_out_step_down)
        _require_weight("forward_curve_weight", self.forward_curve_weight)
        if self.enhanced_strike is not None:
            _require_positive("enhanced_strike", self.enhanced_strike)
        _require_finite("participation", self.participation)


@dataclass(frozen=True, kw_only=True)
class PathAccumulatorOption(OptionInstrument):
    call_put: CallPut
    strike: float
    knock_out: float
    multiplier: float
    ko_begin_trading_day: int
    lock_trading_days: int
    ko_terminates: bool
    observation_schedule: tuple[SchedulePoint, ...]
    forward_curve_weight: float = 1.0
    quantity_basis: AccumulatorQuantityBasis = AccumulatorQuantityBasis.WHOLE_CONTRACT

    def __post_init__(self) -> None:
        _require_positive("strike", self.strike)
        _require_positive("knock_out", self.knock_out)
        _require_positive("multiplier", self.multiplier)
        _require_positive_int("ko_begin_trading_day", self.ko_begin_trading_day)
        require_plain_int("lock_trading_days", self.lock_trading_days, nonnegative=True)
        _require_weight("forward_curve_weight", self.forward_curve_weight)
        if self.quantity_basis is not AccumulatorQuantityBasis.WHOLE_CONTRACT:
            raise ValueError("Path Accumulator当前只支持WHOLE_CONTRACT数量口径")


@dataclass(frozen=True)
class OptionLeg:
    weight: float
    instrument: OptionInstrument
    method: PricingMethod
    label: str

    def __post_init__(self) -> None:
        _require_finite("weight", self.weight)


@dataclass(frozen=True, kw_only=True)
class CompositeOption(OptionInstrument):
    legs: tuple[OptionLeg, ...]


def _require_positive(label: str, value: float) -> None:
    require_positive_real(label, value)


def _require_finite(label: str, value: float) -> None:
    require_finite_real(label, value)


def _require_positive_int(label: str, value: int) -> None:
    require_plain_int(label, value, positive=True)


def _require_weight(label: str, value: float) -> None:
    require_finite_real(label, value)
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{label}必须位于0和1之间")


def _require_strict_times(label: str, values: tuple[float, ...]) -> None:
    for value in values:
        require_nonnegative_real(label, value)
    if any(right <= left for left, right in pairwise(values)):
        raise ValueError(f"{label}必须严格递增")


def _require_nondecreasing_times(label: str, values: tuple[float, ...]) -> None:
    for value in values:
        require_nonnegative_real(label, value)
    if any(right <= left for left, right in pairwise(values)):
        raise ValueError(f"{label}必须严格递增")
