"""Backtester专属运行时配置，不进入OptionReg。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping


class BacktestConfigError(ValueError):
    """历史数据、回放控制或统计口径不满足Backtester要求。"""


@dataclass(frozen=True)
class BacktestConfig:
    """本次回测控制参数。

    实际历史数据由``HistoricalData``承载，不能把文件路径或行情表塞进Config。
    """

    start_date: str | None = None
    end_date: str | None = None
    entry_rule: str = "daily"
    entry_dates: tuple[str, ...] | None = None
    complete_tenor: bool = True
    missing_data_policy: str = "drop_trade"
    alignment_policy: str = "intersection"
    statistics_frequency: str = "all"
    entry_hv_window: int | None = None
    entry_hv_bins: tuple[float, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["entry_dates"] = list(self.entry_dates) if self.entry_dates is not None else None
        data["entry_hv_bins"] = list(self.entry_hv_bins) if self.entry_hv_bins is not None else None
        return data

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "BacktestConfig":
        supplied = dict(value or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise BacktestConfigError(f"BacktestConfig含未知字段：{','.join(sorted(unknown))}")
        if supplied.get("entry_dates") is not None:
            supplied["entry_dates"] = tuple(_iso_date(item, "entry_dates") for item in supplied["entry_dates"])
        for key in ("start_date", "end_date"):
            if supplied.get(key) is not None:
                supplied[key] = _iso_date(supplied[key], key)
        if "complete_tenor" in supplied and not isinstance(supplied["complete_tenor"], bool):
            raise BacktestConfigError("complete_tenor必须为布尔值")
        if supplied.get("entry_hv_window") is not None:
            supplied["entry_hv_window"] = int(supplied["entry_hv_window"])
        if supplied.get("entry_hv_bins") is not None:
            supplied["entry_hv_bins"] = tuple(float(item) for item in supplied["entry_hv_bins"])
        config = cls(**supplied)
        if config.entry_rule not in {"daily", "monthly", "explicit"}:
            raise BacktestConfigError("entry_rule只能为daily、monthly或explicit")
        if config.entry_rule == "explicit" and not config.entry_dates:
            raise BacktestConfigError("entry_rule=explicit时必须提供entry_dates")
        if config.missing_data_policy not in {"drop_trade", "reject"}:
            raise BacktestConfigError("missing_data_policy只能为drop_trade或reject")
        if config.alignment_policy != "intersection":
            raise BacktestConfigError("首期多标的对齐仅支持intersection")
        if config.statistics_frequency not in {"all", "year"}:
            raise BacktestConfigError("statistics_frequency只能为all或year")
        if config.entry_hv_window is not None and config.entry_hv_window not in {5, 10, 20, 60, 122, 244}:
            raise BacktestConfigError("entry_hv_window只能为5、10、20、60、122或244")
        if config.entry_hv_bins is not None and (not config.entry_hv_bins or any(value <= 0 for value in config.entry_hv_bins) or tuple(sorted(set(config.entry_hv_bins)) ) != config.entry_hv_bins):
            raise BacktestConfigError("entry_hv_bins必须为严格递增的正数边界")
        if config.entry_hv_bins is not None and config.entry_hv_window is None:
            raise BacktestConfigError("entry_hv_bins必须与entry_hv_window同时提供")
        return config


def _iso_date(value: Any, field: str) -> str:
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise BacktestConfigError(f"{field}必须为有效ISO日期YYYY-MM-DD") from error
    if parsed.isoformat() != text:
        raise BacktestConfigError(f"{field}必须为有效ISO日期YYYY-MM-DD")
    return text
