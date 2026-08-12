"""Backtester module public surface."""

from .impl.config import BacktestConfig, BacktestConfigError
from .historical_data import HistoricalData, HistoricalDataError
from .impl.engine import BacktestInputError, BacktestResult, backtest
from .models import BacktestInput

__all__ = (
    "BacktestConfig", "BacktestConfigError", "BacktestInput", "BacktestInputError",
    "BacktestResult", "HistoricalData", "HistoricalDataError", "backtest",
)
