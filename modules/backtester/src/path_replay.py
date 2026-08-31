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

from .entry_generator import BacktestInputError, BacktestUnsupportedError, BacktestWindowError
from .historical_data import HistoricalData, required_contract_fields
from .impl.config import BacktestConfig


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


@dataclass(frozen=True)
class EffectiveBacktestWindow:
    """由真实行情、受控日历和合同期限共同确定的可回测入场窗口。"""

    mode: str
    start_date: str
    end_date: str
    requested_start: str | None
    requested_end: str | None
    market_as_of_session: str
    latest_complete_entry_session: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "market_as_of_session": self.market_as_of_session,
            "latest_complete_entry_session": self.latest_complete_entry_session,
        }


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


def controlled_schedule_source(
    contract: ResolvedContract,
    historical_data: HistoricalData,
    observed_dates: pd.DatetimeIndex,
    entry_spots: Mapping[str, float],
) -> ResolvedContract:
    """按受控日历冻结观察日，并确认每个必要观察日都有完整价格。"""
    from .trade_ledger import freeze_trade_contract

    controlled_dates = historical_data.trading_sessions[
        (historical_data.trading_sessions >= observed_dates[0])
        & (historical_data.trading_sessions <= observed_dates[-1])
    ]
    schedule_source, _ = freeze_trade_contract(
        contract,
        entry_date=_date_text(observed_dates[0]),
        trading_dates=controlled_dates,
        entry_spots=entry_spots,
    )
    required_dates = {
        pd.Timestamp(date)
        for schedule in schedule_source.resolved_schedules.values()
        for date in schedule["dates"]
    }
    missing = sorted(required_dates - set(observed_dates))
    if missing:
        raise BacktestInputError(
            "contract_observation_sessions_missing:"
            + ",".join(_date_text(value) for value in missing)
        )
    return schedule_source


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
    *,
    trading_sessions: pd.DatetimeIndex,
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
        return None, "insufficient_observation_count_tenor"
    if "T" not in contract.terms:
        return None, "contract_tenor_missing"
    maturity = entry_date + pd.Timedelta(days=float(contract.terms["T"]) * 365.0)
    maturity_date = maturity.normalize()
    calendar_closed = maturity_date in trading_sessions or bool((trading_sessions > maturity_date).any())
    if not calendar_closed:
        return None, "controlled_calendar_not_closed_through_maturity"
    expected = trading_sessions[(trading_sessions >= entry_date) & (trading_sessions <= maturity)]
    if not len(expected):
        return None, "insufficient_tenor"
    # 到期日可以是周末或节假日。合同终点只能由受控交易日集合向前
    # 归一化，不能拿自然日到期与最后行情日直接比较，也不引入容差天数。
    terminal_session = expected[-1]
    stop = index.get_indexer([terminal_session])[0]
    if stop >= start:
        return int(stop), None
    return None, "missing_controlled_calendar_session_price"


def _resolve_effective_backtest_window(
    history: AlignedHistory,
    historical_data: HistoricalData,
    contract: ResolvedContract,
    config: BacktestConfig,
) -> EffectiveBacktestWindow:
    """计算唯一有效窗口；不使用浏览器日期、工作日猜测或期限倒减。"""
    observed = history.trading_sessions
    controlled = historical_data.trading_sessions
    verified_observed = observed[observed.isin(controlled)]
    if not len(verified_observed):
        raise BacktestWindowError(
            "历史行情与受控交易日历没有共同会话",
            code="no_aligned_market_session",
            details={"reason": "no_aligned_market_session"},
        )
    market_as_of = verified_observed[-1]

    def complete_stop(entry_date: pd.Timestamp) -> tuple[int | None, str | None]:
        start = observed.get_indexer([entry_date])[0]
        if start < 0:
            return None, "entry_date_not_in_aligned_trading_calendar"
        stop, reason = contract_stop_position(
            observed,
            int(start),
            contract,
            trading_sessions=controlled,
        )
        if stop is None:
            return None, reason
        if stop <= start:
            return None, "insufficient_path"
        dates = observed[start : stop + 1]
        if requires_daily_observation(contract):
            try:
                assert_daily_observation_sessions(
                    controlled,
                    dates,
                    entry_date=entry_date,
                    terminal_date=observed[stop],
                )
            except BacktestInputError as error:
                return None, str(error)
        try:
            entry_spots = {
                asset_id: float(history.close.iloc[start, asset_index])
                for asset_index, asset_id in enumerate(contract.underlyings)
            }
            controlled_schedule_source(contract, historical_data, dates, entry_spots)
        except (BacktestInputError, ValueError) as error:
            return None, str(error)
        return int(stop), None

    latest_complete: pd.Timestamp | None = None
    failure_reason = "insufficient_complete_tenor"
    for entry_date in reversed(verified_observed):
        stop, reason = complete_stop(entry_date)
        if stop is None:
            failure_reason = reason or failure_reason
            continue
        latest_complete = entry_date
        break
    if latest_complete is None:
        raise BacktestWindowError(
            "历史数据中没有可完成合同期限的入场交易日",
            code="no_complete_entry_session",
            details={
                "market_as_of_session": _date_text(market_as_of),
                "latest_complete_entry_session": None,
                "reason": failure_reason,
            },
        )

    requested_start = pd.Timestamp(config.start_date) if config.start_date else None
    requested_end = pd.Timestamp(config.end_date) if config.end_date else None
    explicit_dates = pd.DatetimeIndex(pd.to_datetime(list(config.entry_dates or ())))
    requested_ceiling = max(
        [value for value in (requested_end, explicit_dates.max() if len(explicit_dates) else None) if value is not None],
        default=None,
    )
    if requested_ceiling is not None and requested_ceiling > latest_complete:
        details = {
            "requested_end": _date_text(requested_ceiling),
            "market_as_of_session": _date_text(market_as_of),
            "latest_complete_entry_session": _date_text(latest_complete),
            "suggested_end_date": _date_text(latest_complete),
            "reason": "requested_end_after_latest_complete_entry_session",
        }
        raise BacktestWindowError(
            "请求的入场截止日超过最新可完整回放的入场交易日",
            code="entry_window_exceeds_complete_history",
            details=details,
        )

    if config.entry_rule == "explicit":
        for entry_date in explicit_dates:
            _, reason = complete_stop(entry_date)
            if reason is None:
                continue
            raise BacktestWindowError(
                f"指定入场日不能形成完整合同期限回放：{reason}",
                code="explicit_entry_incomplete",
                details={
                    "requested_entry": _date_text(entry_date),
                    "requested_end": _date_text(explicit_dates.max()),
                    "market_as_of_session": _date_text(market_as_of),
                    "latest_complete_entry_session": _date_text(latest_complete),
                    "reason": reason,
                },
            )

    if config.entry_rule == "explicit":
        effective_start = requested_start or explicit_dates.min()
        effective_end = requested_end or explicit_dates.max()
        outside = explicit_dates[(explicit_dates < effective_start) | (explicit_dates > effective_end)]
        if len(outside):
            raise BacktestWindowError(
                "指定入场日超出显式入场窗口",
                code="explicit_entry_outside_requested_window",
                details={
                    "requested_start": _date_text(effective_start),
                    "requested_end": _date_text(effective_end),
                    "outside_entry_dates": [_date_text(value) for value in outside],
                    "market_as_of_session": _date_text(market_as_of),
                    "latest_complete_entry_session": _date_text(latest_complete),
                    "reason": "explicit_entry_outside_requested_window",
                },
            )
        mode = "explicit"
    else:
        effective_end = requested_end or latest_complete
        if requested_start is None:
            target = effective_end - pd.DateOffset(years=3)
            candidates = verified_observed[(verified_observed >= target) & (verified_observed <= effective_end)]
            effective_start = next((value for value in candidates if complete_stop(value)[0] is not None), None)
            if effective_start is None:
                raise BacktestWindowError(
                    "最近三年窗口内没有可完整回放的入场交易日",
                    code="no_complete_entry_session_in_automatic_window",
                    details={
                        "requested_start": _date_text(target),
                        "requested_end": _date_text(effective_end),
                        "market_as_of_session": _date_text(market_as_of),
                        "latest_complete_entry_session": _date_text(latest_complete),
                        "reason": "no_complete_entry_session_on_or_after_three_year_boundary",
                    },
                )
        else:
            effective_start = requested_start
        mode = "automatic" if requested_start is None and requested_end is None else "explicit_bounds"
    if effective_start > effective_end:
        raise BacktestWindowError(
            "入场起始日不得晚于入场截止日",
            code="invalid_entry_window_order",
            details={
                "requested_start": _date_text(effective_start),
                "requested_end": _date_text(effective_end),
                "market_as_of_session": _date_text(market_as_of),
                "latest_complete_entry_session": _date_text(latest_complete),
                "reason": "start_after_end",
            },
        )
    return EffectiveBacktestWindow(
        mode=mode,
        start_date=_date_text(effective_start),
        end_date=_date_text(effective_end),
        requested_start=config.start_date,
        requested_end=config.end_date,
        market_as_of_session=_date_text(market_as_of),
        latest_complete_entry_session=_date_text(latest_complete),
    )


def plan_backtest_window(
    contract: ResolvedContract,
    config: BacktestConfig,
    historical_data: HistoricalData,
) -> EffectiveBacktestWindow:
    """公开窗口规划入口：合同、配置、历史数据映射为唯一完整入场窗口。"""
    if not isinstance(contract, ResolvedContract):
        raise TypeError("contract必须为ResolvedContract")
    if not isinstance(config, BacktestConfig):
        raise TypeError("config必须为BacktestConfig")
    if not isinstance(historical_data, HistoricalData):
        raise TypeError("historical_data必须为HistoricalData")
    historical_data.validate_integrity()
    assert_supported_schedule(contract)
    return _resolve_effective_backtest_window(
        aligned_history(historical_data, contract.underlyings, contract),
        historical_data,
        contract,
        config,
    )


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
