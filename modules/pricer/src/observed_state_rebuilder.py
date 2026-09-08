"""从Host验证行情和冻结日历重建Pricer存续状态。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from math import isclose, isfinite
from typing import TYPE_CHECKING, Any, Mapping

import pandas as pd

from runtime.contracts.contract_api import (
    PricePath,
    ResolvedContract,
    compute_monitor_values,
    evaluate_contract,
)

from .observed_state import ObservedContractState

if TYPE_CHECKING:
    from .models import HistoricalData


_FULL_HISTORY_MONITORS = frozenset({
    "n_in",
    "sigma_realized",
    "Q_acc",
    "tau_reset",
})
_KNOWN_MONITOR_FIELDS = frozenset({
    "n_in",
    "n_obs_actual",
    "sigma_realized",
    "Q_acc",
    "S_out",
    "n_coupon",
    "n_out",
    "tau_hedge",
    "tau_hedge_raw",
    "tau_in",
    "tau_out",
    "tau_out_1",
    "tau_out_2",
    "tau_reset",
    "tau_touch",
    "terminal_in_observed",
    "terminal_out_observed",
    "terminal_touch_observed",
})
_TERMINATION_MONITORS = frozenset({
    "tau_hedge",
    "tau_out",
    "tau_out_1",
    "tau_out_2",
    "tau_touch",
})


def rebuild_observed_state_from_verified_history(
    *,
    contract: ResolvedContract,
    valuation_date: str | None,
    historical: HistoricalData | None,
    market_ref: Any,
    trading_calendar: Mapping[str, object] | None,
    calendar_ref: Any,
    supplied_state: Any,
) -> Any:
    """重建缺失的存续状态，同时保留数值核的缺失状态保护。

    对已开始的路径合同重建缺失状态；已携带观察历史的Host状态也必须与本次
    验证行情重新核对。事件和现金流分别来自共享monitor与现金流解释器，
    不复制产品公式。
    """

    if supplied_state is not None and not (
        isinstance(supplied_state, Mapping) and supplied_state.get("observation_history")
        or isinstance(supplied_state, ObservedContractState) and supplied_state.observation_history
    ):
        return supplied_state
    monitor = contract.terms.get("monitor")
    if not isinstance(monitor, Mapping) or not monitor:
        return supplied_state
    start_value = contract.identity.get("contract_start_date")
    if start_value in {None, ""} or valuation_date in {None, ""}:
        return supplied_state
    try:
        start = date.fromisoformat(str(start_value))
        valuation = date.fromisoformat(str(valuation_date))
    except ValueError as error:
        raise ValueError("自动状态重建要求有效的合同起始日和估值日") from error
    if valuation <= start:
        return supplied_state

    unknown_monitors = sorted(
        str(key) for key in monitor
        if str(key) not in _KNOWN_MONITOR_FIELDS
    )
    if unknown_monitors:
        raise ValueError(
            "当前observed_contract_state不能承载合同历史监测字段："
            + ",".join(unknown_monitors)
        )
    if historical is None or market_ref is None:
        raise ValueError("存续期自动状态重建要求已验证market-history资产")
    if trading_calendar is None or calendar_ref is None:
        raise ValueError("存续期自动状态重建要求已验证trading-calendar资产")

    observed_path, completed_sessions = _verified_observed_path(
        contract=contract,
        start=start,
        valuation=valuation,
        historical=historical,
        trading_calendar=trading_calendar,
    )
    full_history = bool(_FULL_HISTORY_MONITORS.intersection(monitor))
    history = _observation_history_payload(observed_path, completed_sessions) if full_history else None
    if supplied_state is not None and history is not None:
        supplied = ObservedContractState.from_value(supplied_state, valuation_date=valuation_date)
        if supplied.to_dict().get("observation_history") != history:
            raise ValueError("Host历史状态与本次已验证行情不一致")
    try:
        # Full-path completeness checks are deferred to the settlement
        # interpreter. Historical facts use the unchanged economic formulas.
        prefix_monitor = {key: value for key, value in monitor.items() if key != "n_obs_actual"}
        if len(completed_sessions) < 2:
            prefix_monitor.pop("sigma_realized", None)
        prefix_contract = replace(contract, terms={**contract.terms, "monitor": prefix_monitor}, path_case_applicability=None)
        monitor_values = compute_monitor_values(prefix_contract, observed_path)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{contract.product_id}无法由共享monitor解释器重建存续状态：{error}"
        ) from error

    termination = _termination_event(monitor_values, observed_path, completed_sessions)
    realized_cashflows: tuple[Mapping[str, Any], ...] = ()
    lifecycle_status = "active"
    accumulation_stop = None
    if termination is not None:
        settlement = _terminated_cashflows(
            contract=contract,
            observed_path=observed_path,
            valuation=valuation,
            termination=termination,
        )
        if settlement is not None:
            realized_cashflows = settlement
            lifecycle_status = "terminated"
        else:
            accumulation_stop = termination
            termination = None

    cutoff_time = None if termination is None else float(termination["event_time"])
    events = _barrier_events(
        monitor_values,
        observed_path,
        completed_sessions,
        cutoff_time=cutoff_time,
    )
    if accumulation_stop is not None:
        for event in events:
            if event["event_type"] == "knock_out":
                event["event_type"] = "accumulation_stopped"
    reset_time = _finite_monitor_time(monitor_values.get("tau_reset"))
    if reset_time is not None and (cutoff_time is None or reset_time <= cutoff_time):
        reset_date, reset_stage = _event_position(reset_time, observed_path, completed_sessions)
        events.append({"event_type": "reset", "event_date": reset_date, "observation_stage": reset_stage})
    if termination is not None and str(termination["event_type"]) not in {"knock_out"}:
        events.append({
            "event_type": str(termination["event_type"]),
            "event_date": str(termination["event_date"]),
            "observation_stage": int(termination["observation_stage"]),
        })
    checkpoint: dict[str, Any] = {
        "event_type": "observation_checkpoint",
        "event_date": valuation.isoformat(),
        "observation_stage": len(completed_sessions) - 1,
    }
    if "n_coupon" in monitor_values:
        checkpoint["accumulated_count"] = _nonnegative_integer(
            monitor_values["n_coupon"], "n_coupon",
        )
    if "Q_acc" in monitor_values:
        checkpoint["accumulated_quantity"] = _nonnegative_number(
            monitor_values["Q_acc"], "Q_acc",
        )
    events.append(checkpoint)
    events.sort(key=_event_sort_key)

    source_refs = (
        f"data-asset:{market_ref.data_asset_id}:{market_ref.content_hash}",
        f"data-asset:{calendar_ref.data_asset_id}:{calendar_ref.content_hash}",
    )
    return ObservedContractState(
        valuation_date=valuation.isoformat(),
        lifecycle_status=lifecycle_status,
        occurred_events=tuple(events),
        realized_cashflows=realized_cashflows,
        source_refs=source_refs,
        observation_history=history,
    )


def _verified_observed_path(
    *,
    contract: ResolvedContract,
    start: date,
    valuation: date,
    historical: HistoricalData,
    trading_calendar: Mapping[str, object],
) -> tuple[PricePath, tuple[str, ...]]:
    raw_sessions = trading_calendar.get("sessions", ())
    if isinstance(raw_sessions, (str, bytes)) or not isinstance(raw_sessions, (tuple, list)):
        raise ValueError("已验证trading-calendar.sessions必须为列表")
    try:
        calendar_sessions = tuple(date.fromisoformat(str(value)) for value in raw_sessions)
    except ValueError as error:
        raise ValueError("已验证trading-calendar.sessions必须为YYYY-MM-DD") from error
    if calendar_sessions != tuple(sorted(set(calendar_sessions))):
        raise ValueError("已验证trading-calendar.sessions必须严格递增且不重复")
    completed = tuple(value for value in calendar_sessions if start <= value <= valuation)
    if not completed:
        raise ValueError("冻结交易日历在合同起始日至估值日之间没有已完成观察日")
    completed_sessions = tuple(value.isoformat() for value in completed)

    observation_price = str(contract.terms.get("observation_price", "close"))
    if observation_price not in {"close", "open", "high", "low"}:
        raise ValueError("合同observation_price必须为close、open、high或low")
    required_fields = {"date", "asset_id", "close", observation_price}
    frame = pd.DataFrame([dict(row) for row in historical.rows])
    missing_fields = sorted(required_fields - set(frame.columns))
    if missing_fields:
        raise ValueError("存续期自动状态重建缺少行情字段：" + ",".join(missing_fields))
    frame = frame.loc[:, sorted(required_fields)].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise ValueError("存续期自动状态重建行情日期无效")
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    frame["asset_id"] = frame["asset_id"].astype(str)
    for field in {"close", observation_price}:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        if frame[field].isna().any():
            raise ValueError(f"存续期自动状态重建行情字段{field}必须为数值")
    assets = tuple(contract.underlyings)
    frame = frame[
        frame["asset_id"].isin(assets)
        & frame["date"].between(start.isoformat(), valuation.isoformat())
    ]
    if frame.duplicated(("date", "asset_id")).any():
        raise ValueError("存续期自动状态重建同一标的交易日行情不得重复")

    # Check coverage before intersecting: a truncated calendar must not erase
    # verified past observations or turn a trading start into a synthetic anchor.
    assets_per_session = frame.groupby("date")["asset_id"].nunique()
    common_market_sessions = assets_per_session.index[assets_per_session == len(assets)]
    missing_calendar_sessions = sorted(set(common_market_sessions) - set(completed_sessions))
    if missing_calendar_sessions:
        raise ValueError(
            "历史交易日历缺少已验证行情中的共同交易日："
            + "、".join(missing_calendar_sessions[:5])
            + f"，共{len(missing_calendar_sessions)}日"
        )
    frame = frame[frame["date"].isin(completed_sessions)]

    close_values = _price_matrix(frame, completed_sessions, assets, "close")
    observed_values = _price_matrix(frame, completed_sessions, assets, observation_price)
    if (close_values <= 0.0).any() or (observed_values <= 0.0).any():
        raise ValueError("存续期自动状态重建价格必须严格为正")

    path_dates = list(completed_sessions)
    path_close = close_values.tolist()
    path_observed = observed_values.tolist()
    first_session = completed[0]
    if first_session > start:
        references = contract.identity.get("reference_prices")
        if not isinstance(references, Mapping) or set(references) != set(assets):
            raise ValueError("非交易日起始合同必须提供全部标的reference_prices")
        anchor = [_positive_number(references[asset], f"reference_prices.{asset}") for asset in assets]
        path_dates.insert(0, start.isoformat())
        path_close.insert(0, anchor)
        path_observed.insert(0, anchor)
    if len(path_dates) < 2:
        raise ValueError("存续期自动状态重建价格路径至少需要两个时点")
    path_times = tuple(
        (date.fromisoformat(value) - start).days / 365.0
        for value in path_dates
    )
    price_fields = (
        None
        if observation_price == "close"
        else {observation_price: path_observed}
    )
    return PricePath.from_values(
        path_close,
        times=path_times,
        asset_ids=assets,
        dates=path_dates,
        price_fields=price_fields,
    ), completed_sessions


def _price_matrix(
    frame: pd.DataFrame,
    sessions: tuple[str, ...],
    assets: tuple[str, ...],
    field: str,
):
    matrix = frame.pivot(index="date", columns="asset_id", values=field)
    missing_sessions = [value for value in sessions if value not in matrix.index]
    missing_assets = [value for value in assets if value not in matrix.columns]
    if missing_sessions or missing_assets:
        details = [
            *(f"交易日{value}" for value in missing_sessions),
            *(f"标的{value}" for value in missing_assets),
        ]
        raise ValueError("存续期自动状态重建缺少冻结行情：" + ",".join(details))
    selected = matrix.loc[list(sessions), list(assets)]
    if selected.isna().any().any():
        raise ValueError(f"存续期自动状态重建字段{field}未逐日覆盖全部标的")
    return selected.to_numpy(dtype=float)


def _termination_event(
    monitors: Mapping[str, Any],
    path: PricePath,
    completed_sessions: tuple[str, ...],
) -> dict[str, Any] | None:
    candidates: list[tuple[float, str]] = []
    for name in sorted(_TERMINATION_MONITORS):
        event_time = _finite_monitor_time(monitors.get(name))
        if event_time is not None:
            candidates.append((event_time, name))
    if not candidates:
        return None
    event_time, monitor_name = min(candidates)
    event_date, observation_stage = _event_position(event_time, path, completed_sessions)
    return {
        "event_time": event_time,
        "event_date": event_date,
        "observation_stage": observation_stage,
        "event_type": (
            "touch"
            if monitor_name == "tau_touch"
            else "hedge"
            if monitor_name == "tau_hedge"
            else "knock_out"
        ),
        "monitor_name": monitor_name,
    }


def _terminated_cashflows(
    *,
    contract: ResolvedContract,
    observed_path: PricePath,
    valuation: date,
    termination: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...] | None:
    try:
        outcome = evaluate_contract(contract, observed_path)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{contract.product_id}已发生{termination['monitor_name']}，但共享现金流解释器"
            f"无法确认历史结算：{error}"
        ) from error
    termination_time = float(termination["event_time"])
    if any(float(flow.time) > termination_time + 1e-12 for flow in outcome.cashflows):
        if "Q_acc" in contract.terms.get("monitor", {}):
            return None
        raise ValueError(
            f"{contract.product_id}的{termination['monitor_name']}不会立即终止合同，"
            "当前observed_contract_state不能把该历史事件继续传入剩余路径"
        )
    result: list[Mapping[str, Any]] = []
    for index, flow in enumerate(outcome.cashflows):
        payment_date = _cashflow_date(float(flow.time), observed_path, contract)
        if date.fromisoformat(payment_date) > valuation:
            raise ValueError("共享现金流解释器返回了估值日后的已实现现金流")
        result.append({
            "cashflow_id": (
                f"contract-{contract.product_id}-path{outcome.selected_path + 1}"
                f"-case{outcome.selected_case + 1}-flow{index + 1}"
            ),
            "payment_date": payment_date,
            "amount": float(flow.amount),
            "currency": contract.currency,
            "status": "realized",
        })
    return tuple(result)


def _observation_history_payload(path: PricePath, sessions: tuple[str, ...]) -> dict[str, Any]:
    """只保留真实交易观察，非交易日起始参考锚点不作为历史价格。"""
    indices = [index for index, day in enumerate(path.dates) if str(pd.Timestamp(day).date()) in sessions]
    return {
        "dates": list(sessions),
        "asset_ids": list(path.asset_ids),
        "close": path.values[indices].tolist(),
        "price_fields": {name: values[indices].tolist() for name, values in (path.price_fields or {}).items()},
    }


def analytical_history_inputs(contract: ResolvedContract, history: Mapping[str, Any] | None) -> dict[str, Any]:
    """共享monitor提供历史统计量，解析引擎只计算未来条件期望。"""
    if history is None or contract.product_id not in {"9.4", "9.8"}:
        return {}
    start = date.fromisoformat(str(contract.identity["contract_start_date"]))
    dates = list(history["dates"])
    prices = list(history["close"])
    fields = {key: list(values) for key, values in history["price_fields"].items()}
    schedule_key = "Ovar" if contract.product_id == "9.4" else "Orange"
    scheduled = set(contract.resolved_schedules[schedule_key]["dates"])
    observed_count = sum(day in scheduled for day in dates)
    if not observed_count:
        raise ValueError("历史行情未包含合同要求的实际观察")
    if len(dates) == 1:
        if start.isoformat() >= dates[0]:
            raise ValueError("单时点历史缺少早于首次观察的合同参考锚点")
        anchor = [float(contract.identity["reference_prices"][asset]) for asset in contract.underlyings]
        dates.insert(0, start.isoformat())
        prices.insert(0, anchor)
        for values in fields.values():
            values.insert(0, anchor)
    path = PricePath.from_values(
        prices, dates=dates, asset_ids=history["asset_ids"], price_fields=fields,
        times=[(date.fromisoformat(day) - start).days / 365.0 for day in dates],
    )
    monitor = {key: value for key, value in contract.terms["monitor"].items() if key != "n_obs_actual"}
    if observed_count < 2:
        monitor.pop("sigma_realized", None)
    prefix = replace(contract, terms={**contract.terms, "monitor": monitor}, path_case_applicability=None)
    values = compute_monitor_values(prefix, path)
    if contract.product_id == "9.8":
        return {"historical_in_count": int(values["n_in"]), "historical_observation_count": observed_count}
    return_count = observed_count - 1
    annualization = float(contract.terms["annualization_days"])
    selected_prices = history["price_fields"].get(str(contract.terms.get("observation_price", "close")), history["close"])
    last_index = max(index for index, day in enumerate(history["dates"]) if day in scheduled)
    return {
        "historical_squared_returns": float(values.get("sigma_realized", 0.0)) ** 2 * return_count / (annualization * 10_000.0),
        "historical_return_count": return_count,
        "last_observation_spot": float(selected_prices[last_index][0]) / float(contract.identity["reference_prices"][contract.underlyings[0]]) * float(contract.terms.get("S0", 100.0)),
    }


def _cashflow_date(
    cashflow_time: float,
    observed_path: PricePath,
    contract: ResolvedContract,
) -> str:
    if isclose(cashflow_time, 0.0, rel_tol=0.0, abs_tol=1e-12):
        return str(contract.identity["contract_start_date"])
    matches = [
        index for index, value in enumerate(observed_path.times)
        if isclose(float(value), cashflow_time, rel_tol=0.0, abs_tol=1e-12)
    ]
    if len(matches) == 1 and observed_path.dates is not None:
        return str(pd.Timestamp(observed_path.dates[matches[0]]).date())
    start = date.fromisoformat(str(contract.identity["contract_start_date"]))
    return (start + timedelta(days=round(cashflow_time * 365.0))).isoformat()


def _barrier_events(
    monitors: Mapping[str, Any],
    path: PricePath,
    completed_sessions: tuple[str, ...],
    *,
    cutoff_time: float | None,
) -> list[dict[str, Any]]:
    candidates: list[tuple[float, str]] = []
    for name, event_type in (("tau_in", "knock_in"),):
        event_time = _finite_monitor_time(monitors.get(name))
        if event_time is not None and (
            cutoff_time is None or event_time <= cutoff_time + 1e-12
        ):
            candidates.append((event_time, event_type))
    out_times = [
        value
        for name in ("tau_out", "tau_out_1", "tau_out_2")
        if (value := _finite_monitor_time(monitors.get(name))) is not None
    ]
    if out_times:
        event_time = min(out_times)
        if cutoff_time is None or event_time <= cutoff_time + 1e-12:
            candidates.append((event_time, "knock_out"))
    events: list[dict[str, Any]] = []
    for event_time, event_type in sorted(candidates, key=lambda item: (item[0], item[1])):
        event_date, stage = _event_position(event_time, path, completed_sessions)
        events.append({
            "event_type": event_type,
            "event_date": event_date,
            "observation_stage": stage,
        })
    return events


def _event_position(
    event_time: float,
    path: PricePath,
    completed_sessions: tuple[str, ...],
) -> tuple[str, int]:
    matches = [
        index for index, value in enumerate(path.times)
        if isclose(float(value), event_time, rel_tol=0.0, abs_tol=1e-12)
    ]
    if len(matches) != 1 or path.dates is None:
        raise ValueError("历史monitor事件无法唯一映射至冻结交易日")
    event_date = str(pd.Timestamp(path.dates[matches[0]]).date())
    try:
        return event_date, completed_sessions.index(event_date)
    except ValueError as error:
        raise ValueError("历史monitor事件落在非交易日起始锚点，不能作为观察事实") from error


def _finite_monitor_time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"历史monitor.{label}必须为非负整数")
    result = float(value)
    if not isfinite(result) or result < 0.0 or not result.is_integer():
        raise ValueError(f"历史monitor.{label}必须为非负整数")
    return int(result)


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"历史monitor.{label}必须为非负有限数")
    result = float(value)
    if not isfinite(result) or result < 0.0:
        raise ValueError(f"历史monitor.{label}必须为非负有限数")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _nonnegative_number(value, label)
    if result <= 0.0:
        raise ValueError(f"{label}必须严格为正")
    return result


def _event_sort_key(value: Mapping[str, Any]) -> tuple[str, int, str]:
    priorities = {"knock_in": 0, "knock_out": 1, "touch": 1, "hedge": 1}
    event_type = str(value["event_type"])
    return str(value["event_date"]), priorities.get(event_type, 2), event_type


__all__ = ("rebuild_observed_state_from_verified_history",)
