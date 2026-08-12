"""Backtester公开配置入口。实现保留在core.config。"""

from .impl.config import BacktestConfig, BacktestConfigError

__all__ = ("BacktestConfig", "BacktestConfigError")
