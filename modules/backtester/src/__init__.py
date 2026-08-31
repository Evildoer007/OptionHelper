"""Backtester module public surface."""

from .impl.config import BacktestConfig, BacktestConfigError
from .historical_data import HistoricalData, HistoricalDataError
from .entry_generator import BacktestWindowError
from .impl.engine import BacktestInputError, BacktestResult, backtest
from .models import BacktestInput
from .path_replay import EffectiveBacktestWindow, plan_backtest_window

__all__ = (
    "BacktestConfig", "BacktestConfigError", "BacktestInput", "BacktestInputError",
    "BacktestResult", "BacktestWindowError", "EffectiveBacktestWindow", "HistoricalData", "HistoricalDataError",
    "backtest", "plan_backtest_window",
)
