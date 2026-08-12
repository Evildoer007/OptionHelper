"""历史价格路径对齐、合同期限定位与共享解释器回放。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from runtime.contracts.contract_api import ContractResolutionError, PricePath, ResolvedContract, evaluate_payoff

from .entry_generator import BacktestInputError, BacktestUnsupportedError
from .historical_data import HistoricalData, required_contract_fields


_TERMINATION_EVENTS = ("tau_out", "tau_out_1", "tau_out_2", "tau_hedge", "tau_touch")


@dataclass(frozen=True)
class AlignedHistory:
    """按标的交集对齐后的不复权合同价格与合同所需OHLC字段。"""

    close: pd.DataFrame
    price_fields: Mapping[str, pd.DataFrame]


@dataclass(frozen=True)
class ReplayedPath:
    """共享解释器已结算的单笔有效历史路径，已截断于真正终止点。"""

    dates: pd.DatetimeIndex
    values: np.ndarray
    times: np.ndarray
    price_fields: Mapping[str, np.ndarray]
    outcome: Any


def assert_supported_schedule(contract: ResolvedContract) -> None:
    """拒绝当前公共合同接口不能稳定回放的观察语义。"""
    observation_keys = {"O_KO", "O_KI", "Oc", "Otouch", "Orange", "Ovar", "Ohedge", "Oreset"}
    for key in observation_keys:
        selector = contract.terms.get(key)
        if isinstance(selector, str) and selector.startswith("weekly_"):
            raise BacktestUnsupportedError("weekly_observation_schedule_not_accepted_by_backtester_contract_calendar")
    if "n_obs" in contract.terms:
        selectors = [contract.terms[key] for key in observation_keys if isinstance(contract.terms.get(key), str)]
        if selectors and any(selector != "daily" for selector in selectors):
            raise BacktestUnsupportedError("observation_count_tenor_requires_unavailable_absolute_schedule_interface")


def aligned_history(historical_data: HistoricalData, underlyings: Sequence[str], contract: ResolvedContract) -> AlignedHistory:
    """只保留所有标的共同存在的真实交易日，不填补跨标的价格。"""
    data = historical_data.frame
    required = required_contract_fields(str(contract.terms.get("observation_price", "close")))
    absent = set(required) - set(data.columns)
    if absent:
        raise BacktestUnsupportedError(f"observation_price_requires_unadjusted_fields：{','.join(sorted(absent))}")
    scoped = data[data["asset_id"].isin(underlyings)].copy()
    present = set(scoped["asset_id"])
    missing_assets = [asset for asset in underlyings if asset not in present]
    if missing_assets:
        raise BacktestInputError(f"alignment_failed：历史数据缺少标的{','.join(missing_assets)}")
    close = scoped.pivot(index="date", columns="asset_id", values="close").reindex(columns=underlyings).sort_index().dropna(how="any")
    if len(close) < 2:
        raise BacktestInputError("alignment_failed：历史区间不足以形成完整价格路径")
    fields: dict[str, pd.DataFrame] = {}
    for field_name in required:
        if field_name == "close":
            continue
        matrix = scoped.pivot(index="date", columns="asset_id", values=field_name).reindex(columns=underlyings).reindex(close.index)
        if matrix.isna().any().any():
            raise BacktestInputError(f"alignment_failed：{field_name}与close无法按交易日对齐")
        fields[field_name] = matrix
    return AlignedHistory(close=close, price_fields=fields)


def contract_stop_position(index: pd.DatetimeIndex, start: int, contract: ResolvedContract, complete_tenor: bool) -> tuple[int | None, str | None]:
    """在到期日当天或此前最近交易日结束，绝不以到期后价格结算。"""
    if "n_obs" in contract.terms:
        stop = start + int(contract.terms["n_obs"]) - 1
        if stop < len(index):
            return stop, None
        if not complete_tenor and start < len(index) - 1:
            return len(index) - 1, None
        return None, "insufficient_observation_count_tenor"
    if "T" not in contract.terms:
        return None, "contract_tenor_missing"
    maturity = index[start] + pd.Timedelta(days=float(contract.terms["T"]) * 365.0)
    before = np.flatnonzero(index <= maturity)
    if len(before):
        stop = int(before[-1])
        if stop >= start and (maturity - index[stop]).days <= 7:
            return stop, None
    if not complete_tenor and start < len(index) - 1:
        return len(index) - 1, None
    return None, "insufficient_tenor"


def replay_path(
    contract: ResolvedContract,
    *,
    dates: pd.DatetimeIndex,
    values: np.ndarray,
    price_fields: Mapping[str, np.ndarray],
) -> ReplayedPath:
    """以共享解释器回放，并在最早有效提前终止点截断路径。"""
    times = np.asarray((dates - dates[0]).days, dtype=float) / 365.0
    try:
        full_outcome = evaluate_payoff(contract, _price_path(contract, values, times, dates, price_fields))
    except ValueError as original_error:
        if not _is_early_settlement_endpoint_error(original_error):
            raise BacktestInputError(str(original_error)) from original_error
        contract_times = _complete_count_tenor_times(contract, times, len(dates))
        if np.array_equal(contract_times, times):
            raise BacktestInputError(str(original_error)) from original_error
        times = contract_times
        try:
            full_outcome = evaluate_payoff(contract, _price_path(contract, values, times, dates, price_fields))
        except ValueError as error:
            raise BacktestInputError(str(error)) from error
    if not _has_termination(full_outcome.monitor_values):
        return ReplayedPath(dates, values, times, dict(price_fields), full_outcome)
    for end in range(1, len(dates) - 1):
        candidate_dates = dates[: end + 1]
        candidate_times = times[: end + 1]
        candidate_fields = {name: field[: end + 1] for name, field in price_fields.items()}
        try:
            outcome = evaluate_payoff(contract, _price_path(contract, values[: end + 1], candidate_times, candidate_dates, candidate_fields))
        except ValueError:
            continue
        if _has_termination(outcome.monitor_values):
            return ReplayedPath(candidate_dates, values[: end + 1], candidate_times, candidate_fields, outcome)
    return ReplayedPath(dates, values, times, dict(price_fields), full_outcome)


def _complete_count_tenor_times(contract: ResolvedContract, actual: np.ndarray, observation_count: int) -> np.ndarray:
    """完整计数型路径以最后一个约定观察日作为合同到期时点T。"""
    expected = contract.terms.get("n_obs")
    tenor = contract.terms.get("T")
    if expected is None or tenor is None or observation_count != int(expected) or actual[-1] <= 0.0:
        return actual
    return actual * (float(tenor) / actual[-1])


def _is_early_settlement_endpoint_error(error: ValueError) -> bool:
    """仅识别共享解释器明确声明的未终止路径期限不足错误。"""
    message = str(error)
    return isinstance(error, ContractResolutionError) and all(marker in message for marker in (
        "价格路径终点",
        "年早于合同期限",
        "未终止路径须覆盖至合同到期日或其相邻交易日",
    ))


def entry_hv_feature(
    historical_data: HistoricalData,
    underlyings: Sequence[str],
    entry_date: pd.Timestamp,
    *,
    window: int | None,
    bins: Sequence[float] | None,
) -> dict[str, Any]:
    """只用入场日及以前的前复权收盘价计算可选HV特征。"""
    if window is None:
        return {"status": "not_requested"}
    price_field = historical_data.hv_price_field
    if price_field is None or price_field not in historical_data.frame.columns:
        return {"status": "not_available", "reason": "adj_close_not_provided", "hv_window": window}
    scoped = historical_data.frame[
        (historical_data.frame["asset_id"].isin(underlyings)) & (historical_data.frame["date"] <= entry_date)
    ]
    by_asset: dict[str, float] = {}
    for asset in underlyings:
        prices = scoped.loc[scoped["asset_id"] == asset, price_field].to_numpy(dtype=float)
        if len(prices) < window + 1:
            return {"status": "not_available", "reason": "insufficient_pre_entry_adj_close", "hv_window": window}
        returns = np.diff(np.log(prices[-(window + 1):]))
        by_asset[asset] = float(np.std(returns, ddof=1) * np.sqrt(244.0))
    result: dict[str, Any] = {
        "status": "available", "as_of": _date_text(entry_date), "hv_window": window,
        "price_field": price_field, "adjustment": historical_data.hv_adjustment, "return_basis": "log_return",
        "annualization_days": 244, "by_asset": by_asset,
    }
    if bins:
        value = max(by_asset.values())
        edges = (-np.inf, *bins, np.inf)
        position = next(index for index in range(len(edges) - 1) if edges[index] <= value < edges[index + 1])
        result["grouping"] = {"aggregation": "max_underlying_hv", "bins": list(bins), "bucket": position, "value": value}
    return result


def _price_path(
    contract: ResolvedContract,
    values: np.ndarray,
    times: np.ndarray,
    dates: pd.DatetimeIndex,
    price_fields: Mapping[str, np.ndarray],
) -> PricePath:
    return PricePath.from_values(
        values,
        times=times,
        asset_ids=contract.underlyings,
        dates=dates,
        price_fields=price_fields,
    )


def _has_termination(monitors: Mapping[str, Any]) -> bool:
    return any(
        isinstance(monitors.get(name), (int, float, np.number)) and np.isfinite(float(monitors[name]))
        for name in _TERMINATION_EVENTS
    )


def _date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")
