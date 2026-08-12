"""Backtester入场样本生成的唯一职责文件。"""

from __future__ import annotations

import pandas as pd

from .impl.config import BacktestConfig


class BacktestInputError(ValueError):
    """合同、历史数据或回测配置不能形成可信逐笔回放。"""


class BacktestUnsupportedError(BacktestInputError):
    """当前共享合同接口无法表达请求的经济语义。"""


class ZeroValidSamplesError(BacktestInputError):
    """候选入场日存在但没有任何完整有效样本。"""


def entry_positions(index: pd.DatetimeIndex, config: BacktestConfig) -> tuple[list[int], tuple[str, ...]]:
    """生成候选入场位置，并返回不在对齐交易日历内的指定日期。"""
    missing_explicit: tuple[str, ...] = ()
    if config.entry_rule == "daily":
        candidates = list(range(len(index)))
    elif config.entry_rule == "monthly":
        months = index.to_period("M")
        candidates = [position for position in range(len(index)) if position == 0 or months[position] != months[position - 1]]
    else:
        try:
            wanted = {pd.Timestamp(value) for value in config.entry_dates or ()}
        except (TypeError, ValueError) as error:
            raise BacktestInputError("entry_dates必须为YYYY-MM-DD") from error
        candidates = [position for position, date in enumerate(index) if date in wanted]
        missing_explicit = tuple(sorted(value.strftime("%Y-%m-%d") for value in wanted - set(index)))
    try:
        start = pd.Timestamp(config.start_date) if config.start_date else None
        end = pd.Timestamp(config.end_date) if config.end_date else None
    except (TypeError, ValueError) as error:
        raise BacktestInputError("start_date和end_date必须为YYYY-MM-DD") from error
    if start is not None and end is not None and start > end:
        raise BacktestInputError("start_date不得晚于end_date")
    positions = [position for position in candidates if (start is None or index[position] >= start) and (end is None or index[position] <= end)]
    return positions, missing_explicit
