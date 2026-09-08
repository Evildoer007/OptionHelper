"""Market, state and valuation configuration objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ._validation import (
    require_finite_real,
    require_plain_int,
    require_positive_real,
)
from .enums import PricingMethod


@dataclass(frozen=True)
class SchedulePoint:
    trading_day: int
    calendar_day: int
    barrier: float | None = None
    amount: float | None = None

    def __post_init__(self) -> None:
        require_plain_int("trading_day", self.trading_day, positive=True)
        require_plain_int("calendar_day", self.calendar_day, positive=True)
        if self.barrier is not None:
            require_positive_real("barrier", self.barrier)
        if self.amount is not None:
            require_finite_real("amount", self.amount)


@dataclass(frozen=True)
class MarketState:
    as_of: date
    spot: float
    volatility: float
    risk_free_rate: float
    dividend_yield: float = 0.0
    carry: float | None = None
    forward_curve: tuple[tuple[float, int], ...] = ()
    source: str = "unspecified"

    def __post_init__(self) -> None:
        require_positive_real("spot", self.spot)
        require_finite_real("volatility", self.volatility)
        if self.volatility < 0:
            raise ValueError("volatility必须为非负有限数")
        for label, value in (
            ("risk_free_rate", self.risk_free_rate),
            ("dividend_yield", self.dividend_yield),
        ):
            require_finite_real(label, value)
        if self.carry is not None:
            require_finite_real("carry", self.carry)
        previous_day: int | None = None
        for forward_price, trading_day in self.forward_curve:
            require_positive_real("forward_curve远期价格", forward_price)
            require_plain_int("forward_curve交易日", trading_day, positive=True)
            if previous_day is not None and trading_day <= previous_day:
                raise ValueError("forward_curve时间索引必须严格递增")
            previous_day = trading_day


@dataclass(frozen=True)
class ValuationState:
    trading_day: int = 1
    calendar_day: int = 1
    knocked_in: bool = False
    knocked_out: bool = False
    accumulated_count: int = 0
    accumulated_quantity: float = 0.0
    observation_stage: int | None = None
    observation_history: Any = None

    def __post_init__(self) -> None:
        require_plain_int("trading_day", self.trading_day, positive=True)
        require_plain_int("calendar_day", self.calendar_day, positive=True)
        require_plain_int("accumulated_count", self.accumulated_count, nonnegative=True)
        require_finite_real("accumulated_quantity", self.accumulated_quantity)
        if self.accumulated_quantity < 0.0:
            raise ValueError("accumulated_quantity必须为非负有限数")
        if self.observation_stage is not None:
            require_plain_int("observation_stage", self.observation_stage, nonnegative=True)


@dataclass(frozen=True)
class ValuationConfig:
    method: PricingMethod
    paths: int = 10
    seed: int = 20240101
    threads: int = 1
    random_source: Any = None
    greek_bumps: tuple[tuple[str, float], ...] = ()
    diagnostics: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.paths, int) or isinstance(self.paths, bool) or self.paths <= 0:
            raise ValueError("paths必须为正整数")
        if not isinstance(self.threads, int) or isinstance(self.threads, bool) or self.threads <= 0:
            raise ValueError("threads必须为正整数")

    @property
    def monte_carlo(self) -> MonteCarloConfig:
        return MonteCarloConfig(
            paths=self.paths,
            seed=self.seed,
            threads=self.threads,
            random_source=self.random_source,
        )


@dataclass(frozen=True)
class MonteCarloConfig:
    paths: int = 10
    seed: int = 20240101
    threads: int = 1
    random_source: Any = None

    def __post_init__(self) -> None:
        from .random_source import NpyRandomSource, default_random_source

        if not isinstance(self.paths, int) or isinstance(self.paths, bool) or self.paths <= 0:
            raise ValueError("paths必须为正整数")
        if (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not 0 <= self.seed <= 0xFFFFFFFF
        ):
            raise ValueError("seed必须是0至4294967295之间的整数")
        if not isinstance(self.threads, int) or isinstance(self.threads, bool) or self.threads <= 0:
            raise ValueError("threads必须为正整数")
        source = self.random_source
        if source is None:
            source = default_random_source(seed=self.seed)
            object.__setattr__(self, "random_source", source)
        elif not isinstance(source, NpyRandomSource):
            raise ValueError("random_source必须是NpyRandomSource")


@dataclass(frozen=True)
class SolveTarget:
    variable: str
    target_pv: float
    lower_bound: float | None = None
    upper_bound: float | None = None
    solver_absolute_tolerance: float = 1e-8
    target_pv_absolute_tolerance: float = 1e-3
    maximum_iterations: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.variable, str) or not self.variable.strip():
            raise ValueError("SolveTarget.variable必须为非空字符串")
        require_finite_real("target_pv", self.target_pv)
        if self.lower_bound is not None:
            require_finite_real("lower_bound", self.lower_bound)
        if self.upper_bound is not None:
            require_finite_real("upper_bound", self.upper_bound)
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound >= self.upper_bound
        ):
            raise ValueError("SolveTarget要求lower_bound严格小于upper_bound")
        require_positive_real(
            "solver_absolute_tolerance", self.solver_absolute_tolerance
        )
        require_positive_real(
            "target_pv_absolute_tolerance", self.target_pv_absolute_tolerance
        )
        require_plain_int(
            "maximum_iterations", self.maximum_iterations, positive=True
        )
