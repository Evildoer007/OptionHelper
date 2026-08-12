"""Backtester正式输入对象。"""

from __future__ import annotations

from dataclasses import dataclass
from runtime.contracts.contract_api import ResolvedContract

from .config import BacktestConfig
from .historical_data import HistoricalData


@dataclass(frozen=True)
class BacktestInput:
    contract: ResolvedContract
    backtest_config: BacktestConfig
    historical_data: HistoricalData

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ResolvedContract):
            raise TypeError("BacktestInput.contract必须为ResolvedContract")
        if not isinstance(self.backtest_config, BacktestConfig):
            raise TypeError("BacktestInput.backtest_config必须为BacktestConfig")
        if not isinstance(self.historical_data, HistoricalData):
            raise TypeError("BacktestInput.historical_data必须为HistoricalData")


__all__ = ("BacktestInput", "HistoricalData")
