"""历史价格路径对齐、合同期限定位与共享解释器回放。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from runtime.contracts.contract_api import (
    PricePath,
    ResolvedContract,
    compute_monitor_values,
    evaluate_contract,
)

from .entry_generator import BacktestInputError, BacktestUnsupportedError
from .historical_data import HistoricalData, required_contract_fields


_TERMINATION_EVENTS = ("tau_out", "tau_out_1", "tau_out_2", "tau_hedge", "tau_touch")
_OBSERVATION_SCHEDULE_KEYS = ("O_KO", "O_KI", "Oc", "Otouch", "Orange", "Ovar", "Ohedge", "Oreset")


@dataclass(frozen=True)
class AlignedHistory:
    """按标的交集对齐后的不复权合同价格与合同所需OHLC字段。"""

    close: pd.DataFrame
    price_fields: Mapping[str, pd.DataFrame]
    trading_sessions: pd.DatetimeIndex


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
    for key in _OBSERVATION_SCHEDULE_KEYS:
        selector = contract.terms.get(key)
        if isinstance(selector, str) and selector.startswith("weekly_"):
            raise BacktestUnsupportedError("weekly_observation_schedule_not_accepted_by_backtester_contract_calendar")
    if "n_obs" in contract.terms:
        selectors = [contract.terms[key] for key in _OBSERVATION_SCHEDULE_KEYS if isinstance(contract.terms.get(key), str)]
        if selectors and any(selector != "daily" for selector in selectors):
            raise BacktestUnsupportedError("observation_count_tenor_requires_unavailable_absolute_schedule_interface")


def requires_daily_observation(contract: ResolvedContract) -> bool:
    """合同存在逐日观察时，不能以跨标的价格交集替代受控交易日历。"""
    return any(contract.terms.get(key) == "daily" for key in _OBSERVATION_SCHEDULE_KEYS)


def assert_daily_observation_sessions(
    controlled_sessions: pd.DatetimeIndex,
    observed_sessions: pd.DatetimeIndex,
    *,
    entry_date: pd.Timestamp,
    terminal_date: pd.Timestamp,
) -> None:
    """逐日观察的合同窗口内，每个受控交易日都必须有全部标的价格。"""
    required = controlled_sessions[(controlled_sessions >= entry_date) & (controlled_sessions <= terminal_date)]
    missing = required.difference(observed_sessions)
    if len(missing):
        dates = ",".join(date.strftime("%Y-%m-%d") for date in missing)
        raise BacktestInputError(f"daily_observation_sessions_missing:{dates}")


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
    return AlignedHistory(close=close, price_fields=fields, trading_sessions=pd.DatetimeIndex(close.index))


def contract_stop_position(
    index: pd.DatetimeIndex,
    start: int,
    contract: ResolvedContract,
    complete_tenor: bool,
    *,
    trading_sessions: pd.DatetimeIndex,
    calendar_coverage_end: pd.Timestamp | None,
) -> tuple[int | None, str | None]:
    """按受控交易日历定位合同终点，绝不以工作日猜测到期。"""
    entry_date = index[start]
    if entry_date not in trading_sessions:
        return None, "entry_date_missing_from_controlled_calendar"
    if "n_obs" in contract.terms:
        expected = trading_sessions[trading_sessions >= entry_date]
        target = int(contract.terms["n_obs"])
        if len(expected) >= target:
            terminal_session = expected[target - 1]
            stop = index.get_indexer([terminal_session])[0]
            if stop >= start:
                return int(stop), None
            return None, "missing_controlled_calendar_session_price"
        if not complete_tenor and start < len(index) - 1:
            return len(index) - 1, None
        return None, "insufficient_observation_count_tenor"
    if "T" not in contract.terms:
        return None, "contract_tenor_missing"
    maturity = entry_date + pd.Timedelta(days=float(contract.terms["T"]) * 365.0)
    if calendar_coverage_end is None or calendar_coverage_end < maturity:
        if not complete_tenor and start < len(index) - 1:
            return len(index) - 1, None
        return None, "calendar_not_covered_to_contract_maturity"
    expected = trading_sessions[(trading_sessions >= entry_date) & (trading_sessions <= maturity)]
    if len(expected):
        terminal_session = expected[-1]
        stop = index.get_indexer([terminal_session])[0]
        if stop >= start:
            return int(stop), None
        return None, "missing_controlled_calendar_session_price"
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
    """以共享解释器回放，并在最早有效提前终止点截断路径。

    提前结算的经济事实只能来自终止日前的真实行情。共享解释器把``T``
    解释为价格路径终点，因此直接把前缀交给解释器会把``tau_out < T``
    误判为到期事件；而先跑完整历史再复制path/case又会读取终止后的行情。
    本函数先在真实前缀上识别最早终止，再以终止日价格平坦延展到合同到期
    仅供解释器完成合同期内的路径选择。输出仍保留真实前缀及其监控事实。
    """
    times = np.asarray((dates - dates[0]).days, dtype=float) / 365.0
    times = _complete_count_tenor_times(contract, times, len(dates))
    for end in range(1, len(dates) - 1):
        candidate_dates = dates[: end + 1]
        candidate_times = times[: end + 1]
        candidate_fields = {name: field[: end + 1] for name, field in price_fields.items()}
        try:
            prefix_path = _path_with_controlled_terminal(
                contract,
                dates=candidate_dates,
                values=values[: end + 1],
                times=candidate_times,
                price_fields=candidate_fields,
                controlled_dates=dates,
                controlled_times=times,
            )
            prefix_monitors = compute_monitor_values(contract, prefix_path)
        except ValueError:
            continue
        if not _has_termination(prefix_monitors, through_time=float(candidate_times[-1])):
            continue
        prefix_monitors = _monitor_values_through(prefix_monitors, float(candidate_times[-1]))
        outcome = _evaluate_terminated_prefix(
            contract,
            dates=candidate_dates,
            values=values[: end + 1],
            times=candidate_times,
            price_fields=candidate_fields,
            prefix_monitors=prefix_monitors,
            controlled_dates=dates,
            controlled_times=times,
        )
        if outcome is None:
            # 例如累购的``tau_out``只影响到期收益分段，并不终止合同；
            # 合同现金流仍在到期日，因此必须继续保留完整真实路径。
            continue
        return ReplayedPath(candidate_dates, values[: end + 1], candidate_times, candidate_fields, outcome)
    try:
        outcome = evaluate_contract(contract, _price_path(contract, values, times, dates, price_fields))
    except ValueError as error:
        raise BacktestInputError(str(error)) from error
    return ReplayedPath(dates, values, times, dict(price_fields), outcome)


def _evaluate_terminated_prefix(
    contract: ResolvedContract,
    *,
    dates: pd.DatetimeIndex,
    values: np.ndarray,
    times: np.ndarray,
    price_fields: Mapping[str, np.ndarray],
    prefix_monitors: Mapping[str, Any],
    controlled_dates: pd.DatetimeIndex,
    controlled_times: np.ndarray,
) -> Any | None:
    """以平坦合成终点解释已发生提前结算，不读取实际后缀行情。"""
    if "T" not in contract.terms:
        raise BacktestInputError("early_termination_without_contract_tenor")
    try:
        outcome = evaluate_contract(
            contract,
            _path_with_controlled_terminal(
                contract,
                dates=dates,
                values=values,
                times=times,
                price_fields=price_fields,
                controlled_dates=controlled_dates,
                controlled_times=controlled_times,
            ),
        )
    except ValueError as error:
        raise BacktestInputError(str(error)) from error
    if any(float(flow.time) > float(times[-1]) + 1e-12 for flow in outcome.cashflows):
        return None
    return replace(outcome, monitor_values=dict(prefix_monitors))


def _path_with_controlled_terminal(
    contract: ResolvedContract,
    *,
    dates: pd.DatetimeIndex,
    values: np.ndarray,
    times: np.ndarray,
    price_fields: Mapping[str, np.ndarray],
    controlled_dates: pd.DatetimeIndex,
    controlled_times: np.ndarray,
) -> PricePath:
    """用受控交易日历平坦延展价格，不读取终止后真实行情。"""
    if len(controlled_dates) != len(controlled_times) or not np.array_equal(controlled_dates[:len(dates)], dates):
        raise BacktestInputError("early_termination_controlled_calendar_mismatch")
    if len(controlled_dates) == len(dates):
        return _price_path(contract, values, times, dates, price_fields)
    extension_size = len(controlled_dates) - len(dates)
    return _price_path(
        contract,
        np.vstack((values, np.repeat(values[-1:], extension_size, axis=0))),
        controlled_times,
        controlled_dates,
        {name: np.vstack((field, np.repeat(field[-1:], extension_size, axis=0))) for name, field in price_fields.items()},
    )


def _complete_count_tenor_times(contract: ResolvedContract, actual: np.ndarray, observation_count: int) -> np.ndarray:
    """完整计数型路径以最后一个约定观察日作为合同到期时点T。"""
    expected = contract.terms.get("n_obs")
    tenor = contract.terms.get("T")
    if expected is None or tenor is None or observation_count != int(expected) or actual[-1] <= 0.0:
        return actual
    return actual * (float(tenor) / actual[-1])


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
        window_prices = prices[-(window + 1):]
        if not np.isfinite(window_prices).all() or (window_prices <= 0).any():
            return {"status": "not_available", "reason": "invalid_pre_entry_adj_close", "hv_window": window}
        returns = np.diff(np.log(window_prices))
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


def _has_termination(monitors: Mapping[str, Any], *, through_time: float | None = None) -> bool:
    return any(
        isinstance(monitors.get(name), (int, float, np.number))
        and np.isfinite(float(monitors[name]))
        and (through_time is None or float(monitors[name]) <= through_time + 1e-12)
        for name in _TERMINATION_EVENTS
    )


def _monitor_values_through(monitors: Mapping[str, Any], through_time: float) -> dict[str, Any]:
    """不让为日历解析附加的平坦终点伪造终止事件。"""
    result = dict(monitors)
    for name in _TERMINATION_EVENTS:
        value = result.get(name)
        if isinstance(value, (int, float, np.number)) and np.isfinite(float(value)) and float(value) > through_time + 1e-12:
            result[name] = float("inf")
    return result


def _date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")
