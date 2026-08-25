"""Backtester公开配置入口，唯一实现位于本模块impl.config。"""

from .impl.config import BacktestConfig, BacktestConfigError

__all__ = ("BacktestConfig", "BacktestConfigError")
