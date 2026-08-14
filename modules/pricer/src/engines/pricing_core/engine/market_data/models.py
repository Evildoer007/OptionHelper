"""Immutable market-data records shared by online and offline providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math


def _positive(label: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须为正有限数")
    if not math.isfinite(float(value)) or float(value) <= 0.0:
        raise ValueError(f"{label}必须为正有限数")


@dataclass(frozen=True)
class DailyBar:
    trading_date: date
    code: str
    open: float
    high: float
    low: float
    close: float
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date):
            raise ValueError("trading_date必须为date")
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code必须为非空字符串")
        for name in (
            "open", "high", "low", "close",
            "adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close",
        ):
            _positive(name, getattr(self, name))
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high不得低于open、close或low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low不得高于open、close或high")
        if self.adjusted_high < max(
            self.adjusted_open, self.adjusted_close, self.adjusted_low
        ):
            raise ValueError("adjusted_high口径不一致")
        if self.adjusted_low > min(
            self.adjusted_open, self.adjusted_close, self.adjusted_high
        ):
            raise ValueError("adjusted_low口径不一致")


@dataclass(frozen=True)
class MarketSnapshotRequest:
    code: str
    as_of: date
    asset_type: str
    volatility_window: int
    risk_free_rate: float
    dividend_yield: float = 0.0
    carry: float | None = None
    historical_volatility_windows: tuple[int, ...] = (10, 20, 60, 122)
    annualization_trading_days: int = 244
    lookback_calendar_days: int = 400

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code必须为非空字符串")
        if not isinstance(self.as_of, date):
            raise ValueError("as_of必须为date")
        asset_type = self.asset_type.upper()
        if asset_type not in {"INDEX", "STOCK", "ETF"}:
            raise ValueError("asset_type必须为INDEX、STOCK或ETF")
        object.__setattr__(self, "asset_type", asset_type)
        windows = tuple(self.historical_volatility_windows)
        if not windows or len(set(windows)) != len(windows):
            raise ValueError("historical_volatility_windows必须非空且不得重复")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 2 for value in windows):
            raise ValueError("历史波动率窗口必须为不小于2的整数")
        if self.volatility_window not in windows:
            raise ValueError("volatility_window必须包含在historical_volatility_windows中")
        if not isinstance(self.annualization_trading_days, int) or self.annualization_trading_days <= 0:
            raise ValueError("annualization_trading_days必须为正整数")
        if not isinstance(self.lookback_calendar_days, int) or self.lookback_calendar_days <= 0:
            raise ValueError("lookback_calendar_days必须为正整数")
        for label, value in (
            ("risk_free_rate", self.risk_free_rate),
            ("dividend_yield", self.dividend_yield),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{label}必须为有限数")
        if self.carry is not None and (
            isinstance(self.carry, bool)
            or not isinstance(self.carry, (int, float))
            or not math.isfinite(float(self.carry))
        ):
            raise ValueError("carry必须为有限数")


@dataclass(frozen=True)
class MarketSnapshot:
    schema: str
    code: str
    asset_type: str
    requested_as_of: date
    market_date: date
    spot: float
    volatility: float
    volatility_window: int
    historical_volatility: tuple[tuple[int, float], ...]
    risk_free_rate: float
    dividend_yield: float
    carry: float | None
    provider: str
    source: str
    price_field: str
    volatility_field: str
    annualization_trading_days: int
    observation_count: int
    history_start_date: date
    history_end_date: date
    data_sha256: str

    def to_market_parameters(self) -> dict[str, object]:
        return {
            "as_of": self.market_date.isoformat(),
            "spot": self.spot,
            "volatility": self.volatility,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "carry": self.carry,
            "source": self.source,
        }
