"""Pricer市场历史读取与估值快照：close用于合同现价，HV字段由DataAssetRef声明。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


class MarketDataError(ValueError):
    pass


def _normalise_market_history(data: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "asset_id", "close", "adj_close"}
    if not required.issubset(data.columns):
        raise MarketDataError("历史行情必须含date、asset_id、close、adj_close")
    result = data.loc[:, ["date", "asset_id", "close", "adj_close"]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["asset_id"] = result["asset_id"].astype(str).str.strip()
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result["adj_close"] = pd.to_numeric(result["adj_close"], errors="coerce")
    invalid = result["date"].isna() | result["asset_id"].eq("") | result["close"].isna() | result["adj_close"].isna() | (result["close"] <= 0) | (result["adj_close"] <= 0)
    if invalid.any() or result.duplicated(["date", "asset_id"]).any():
        raise MarketDataError("历史行情含无效或重复的date、asset_id、close、adj_close")
    return result.sort_values(["date", "asset_id"]).reset_index(drop=True)


def load_market_history(path: str | Path) -> pd.DataFrame:
    return _normalise_market_history(pd.read_csv(path))


def load_market_history_bytes(payload: bytes) -> pd.DataFrame:
    """Read one Host-provided CSV payload without resolving a filesystem path."""
    if not isinstance(payload, bytes) or not payload:
        raise MarketDataError("市场历史DataAssetRef内容为空")
    return _normalise_market_history(pd.read_csv(BytesIO(payload)))


def reference_prices_from_history(
    history: pd.DataFrame,
    underlyings: Sequence[str],
    *,
    contract_start_date: str,
) -> dict[str, Any]:
    """Return unadjusted contract-reference closes on or before the true start date.

    A non-trading contract date is allowed, but it never receives a fabricated
    price: every underlying is anchored to its latest declared historical close
    on or before that date.
    """
    try:
        cutoff = pd.Timestamp(contract_start_date)
    except (TypeError, ValueError) as error:
        raise MarketDataError("合同起始日必须为YYYY-MM-DD") from error
    if pd.isna(cutoff):
        raise MarketDataError("合同起始日必须为YYYY-MM-DD")
    required = {"date", "asset_id", "close"}
    if not required.issubset(history.columns):
        raise MarketDataError("历史行情必须含date、asset_id、close")
    data = history.loc[:, ["date", "asset_id", "close"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna().query("close > 0")
    spots: dict[str, float] = {}
    source_dates: dict[str, str] = {}
    for asset in underlyings:
        rows = data[(data["asset_id"] == asset) & (data["date"] <= cutoff)].sort_values("date")
        if rows.empty:
            raise MarketDataError(f"DataAssetRef未覆盖{asset}在合同起始日{cutoff.strftime('%Y-%m-%d')}或此前的未复权close")
        row = rows.iloc[-1]
        spots[str(asset)] = float(row["close"])
        source_dates[str(asset)] = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
    return {
        "reference_prices": spots,
        "reference_dates": source_dates,
        "price_field": "close",
    }


def market_snapshot_from_history(
    history: pd.DataFrame,
    underlyings: Sequence[str],
    *,
    valuation_date: str | None,
    hv_window: int,
    risk_free_rate: float,
    dividend_yield: Any,
    trading_calendar: Mapping[str, Any] | None = None,
    hv_fields_by_asset: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    required = {"date", "asset_id", "close", "adj_close"}
    if not required.issubset(history.columns):
        raise MarketDataError("历史行情必须含date、asset_id、close、adj_close")
    data = history.loc[:, ["date", "asset_id", "close", "adj_close"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data["adj_close"] = pd.to_numeric(data["adj_close"], errors="coerce")
    data = data.dropna().query("close > 0 and adj_close > 0")
    cutoff = pd.Timestamp(valuation_date) if valuation_date else data["date"].max()
    scoped = data[(data["date"] <= cutoff) & data["asset_id"].isin(underlyings)]
    raw = scoped.pivot(index="date", columns="asset_id", values="close").reindex(columns=underlyings).dropna().sort_index()
    adjusted = scoped.pivot(index="date", columns="asset_id", values="adj_close").reindex(columns=underlyings).dropna().sort_index()
    common = raw.index.intersection(adjusted.index)
    raw = raw.reindex(common).dropna(); adjusted = adjusted.reindex(raw.index).dropna()
    if len(adjusted) <= hv_window: raise MarketDataError(f"估值日前样本不足，不能计算HV{hv_window}")
    declared_hv_fields = {
        str(asset): str((hv_fields_by_asset or {}).get(str(asset), "adj_close"))
        for asset in underlyings
    }
    invalid_hv_fields = {
        asset: field for asset, field in declared_hv_fields.items()
        if field not in {"close", "adj_close"}
    }
    if invalid_hv_fields:
        raise MarketDataError("HV价格字段只能为close或adj_close")
    price_fields = {"close": raw, "adj_close": adjusted}
    hv_prices = pd.DataFrame({
        asset: price_fields[field][asset]
        for asset, field in declared_hv_fields.items()
    }, index=common).dropna()
    if len(hv_prices) <= hv_window:
        raise MarketDataError(f"估值日前样本不足，不能计算HV{hv_window}")
    vols = (np.log(hv_prices / hv_prices.shift(1)).dropna().tail(hv_window).std(ddof=1) * np.sqrt(244)).to_dict()
    unique_hv_fields = set(declared_hv_fields.values())
    hv_price_field: str | dict[str, str] = (
        next(iter(unique_hv_fields))
        if len(unique_hv_fields) == 1 else declared_hv_fields
    )
    result = {"valuation_date": raw.index[-1].strftime("%Y-%m-%d"), "history_start_date": raw.index[0].strftime("%Y-%m-%d"), "history_end_date": raw.index[-1].strftime("%Y-%m-%d"), "spot": {key:float(value) for key,value in raw.iloc[-1].to_dict().items()}, "historical_volatility": {key:float(value) for key,value in vols.items()}, "risk_free_rate":float(risk_free_rate), "dividend_yield":dividend_yield, "hv_window":hv_window, "spot_price_field":"close", "hv_price_field":hv_price_field, "hv_fields_by_asset":declared_hv_fields, "return_method":"log_return", "annualization_trading_days":244, "source":"local_close_and_adj_close"}
    if trading_calendar is not None:
        result["trading_calendar"] = dict(trading_calendar)
    return result


__all__ = (
    "MarketDataError", "reference_prices_from_history", "load_market_history",
    "load_market_history_bytes", "market_snapshot_from_history",
)
