"""仅负责65个结构的专属指标，不重复计算公共统计。"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from runtime.contracts.contract_types import semantic_hash

from .common_metrics import event_happened, monitor_values, number_summary, paired_monitor_values
from .metric_profile_map import MetricProfileSpec, metric_profile_for


def profile_for_product(product_id: str) -> MetricProfileSpec:
    return metric_profile_for(product_id)


@lru_cache(maxsize=None)
def metric_profile_hash(profile_spec: MetricProfileSpec) -> str:
    """绑定专属指标定义、映射和公共统计实现，供执行指纹审计。"""
    source_hashes = {
        filename: sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()
        for filename in ("common_metrics.py", "metric_profile_map.py", "metric_profiles.py")
    }
    return semantic_hash({"profile": asdict(profile_spec), "source_hashes": source_hashes})


def specialized_metrics(
    profile_spec: MetricProfileSpec,
    trades: Sequence[Any],
    *,
    terms: Mapping[str, Any],
    event_summary: Mapping[str, Any],
    monitor_summary: Mapping[str, Any],
    outcome_summary: Sequence[Mapping[str, Any]],
    three_outcome_summary: Sequence[Mapping[str, Any]],
    conditional_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """仅从逐笔账本和通用统计派生结构独有观察。"""
    terminal = np.asarray([trade.terminal_performance for trade in trades if trade.terminal_performance is not None], dtype=float)
    result: dict[str, Any] = {"profile_id": profile_spec.profile_id}
    if profile_spec.profile_id == "terminal_payoff":
        result.update({
            "terminal_performance": number_summary(terminal),
            "terminal_return_sign": _terminal_return_sign(terminal),
            "terminal_segments": list(outcome_summary),
        })
    elif profile_spec.profile_id in {"dual_knock_autocall", "coupon_autocall"}:
        result.update({
            "three_outcome_summary": list(three_outcome_summary),
            "conditional_summary": dict(conditional_summary),
            "events": _select_events(event_summary, ("tau_in", "tau_out", "tau_out_1", "tau_out_2", "tau_reset", "tau_hedge")),
        })
        if profile_spec.profile_id == "coupon_autocall":
            result["coupon_observations"] = _select_monitors(monitor_summary, ("n_coupon",))
            result["coupon_payment"] = _coupon_payment(trades)
    elif profile_spec.profile_id in {"single_knock_out", "single_knock_out_autocall"}:
        result.update({
            "events": _select_events(event_summary, ("tau_out", "tau_out_1", "tau_out_2")),
            "trigger_vs_untriggered": _event_outcomes(trades, ("tau_out", "tau_out_1", "tau_out_2")),
        })
    elif profile_spec.profile_id == "single_knock_in":
        result.update({"events": _select_events(event_summary, ("tau_in",)), "knock_in_outcomes": _knock_in_outcomes(trades)})
    elif profile_spec.profile_id == "touch_binary":
        result.update({
            "events": _select_events(event_summary, ("tau_touch",)),
            "touch_vs_untouched": _event_outcomes(trades, ("tau_touch",)),
        })
    elif profile_spec.profile_id == "airbag":
        result.update({
            "events": _select_events(event_summary, ("tau_in",)),
            "buffer_outcomes": _buffer_outcomes(trades),
            "knock_in_outcomes": _knock_in_outcomes(trades),
        })
    elif profile_spec.profile_id == "accumulator":
        result.update({
            "events": _select_events(event_summary, ("tau_out",)),
            "accumulated_quantity": _select_monitors(monitor_summary, ("Q_acc", "n_out")),
            "pnl_per_accumulated_unit": _pnl_per_accumulated_unit(trades),
            "contract_purchase_price": _finite_term(terms, "K"),
            "quantity_multiplier": _finite_term(terms, "m"),
            "periodic_purchase_cashflows": {"status": "not_exposed_by_shared_contract_core"},
        })
    elif profile_spec.profile_id == "variance_swap":
        realized = monitor_values(trades, "sigma_realized")
        strike = _finite_term(terms, "Ksig")
        result.update({
            "realized_volatility": _select_monitors(monitor_summary, ("sigma_realized",)),
            "realized_variance": number_summary((realized / 100.0) ** 2),
            "realized_volatility_vs_strike": {
                "strike_volatility": strike,
                "spread": number_summary(realized - strike) if strike is not None else number_summary([]),
            },
            "volatility_buckets": _volatility_buckets(realized, strike),
            "variance_pnl": number_summary([trade.pnl for trade in trades]),
        })
    elif profile_spec.profile_id == "range_accrual":
        n_in, n_obs = paired_monitor_values(trades, "n_in", "n_obs_actual")
        ratio = n_in / n_obs if len(n_in) and np.all(n_obs > 0) else np.asarray([], dtype=float)
        result.update({
            "range_observations": _select_monitors(monitor_summary, ("n_in", "n_obs_actual")),
            "in_range_observation_ratio": number_summary(ratio),
            "range_accrual_pnl": number_summary([trade.pnl for trade in trades]),
        })
    elif profile_spec.profile_id == "shark_fin":
        result.update({
            "events": _select_events(event_summary, ("tau_out", "tau_out_1", "tau_out_2")),
            "trigger_vs_untriggered": _event_outcomes(trades, ("tau_out", "tau_out_1", "tau_out_2")),
            "terminal_performance": number_summary(terminal),
        })
    gaps = _metric_gaps(profile_spec.profile_id, terms)
    result["metric_coverage"] = {
        "status": "partial" if gaps else "complete",
        "gaps": gaps,
    }
    return result


def _select_events(source: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {name: source[name] for name in names if name in source}


def _select_monitors(source: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {name: source[name] for name in names if name in source}


def _terminal_return_sign(values: np.ndarray) -> dict[str, Any]:
    total = len(values)
    return {
        "positive_count": int(np.sum(values > 0)),
        "flat_count": int(np.sum(np.isclose(values, 0.0))),
        "negative_count": int(np.sum(values < 0)),
        "positive_rate": float(np.mean(values > 0)) if total else None,
        "negative_rate": float(np.mean(values < 0)) if total else None,
    }


def _coupon_payment(trades: Sequence[Any]) -> dict[str, Any]:
    paid: list[float] = []
    scheduled: list[float] = []
    rates: list[float] = []
    unpaid: list[float] = []
    for trade in trades:
        count = trade.events.get("n_coupon")
        schedule = trade.historical_contract.resolved_schedules.get("O_c", {})
        dates = schedule.get("dates", ()) if isinstance(schedule, Mapping) else ()
        if not isinstance(count, (int, float, np.number)) or not np.isfinite(float(count)) or not dates:
            continue
        paid_count, scheduled_count = float(count), float(len(dates))
        paid.append(paid_count)
        scheduled.append(scheduled_count)
        rates.append(paid_count / scheduled_count)
        unpaid.append(max(0.0, scheduled_count - paid_count))
    return {
        "paid_observation_count": number_summary(paid),
        "scheduled_observation_count": number_summary(scheduled),
        "payment_rate": number_summary(rates),
        "unpaid_observation_count": number_summary(unpaid),
    }


def _knock_in_outcomes(trades: Sequence[Any]) -> dict[str, Any]:
    knocked = [trade for trade in trades if event_happened(trade.events.get("tau_in"))]
    not_knocked = [trade for trade in trades if not event_happened(trade.events.get("tau_in"))]
    return {
        "knock_in_pnl": number_summary([trade.pnl for trade in knocked]),
        "not_knock_in_pnl": number_summary([trade.pnl for trade in not_knocked]),
        "knock_in_terminal_performance": number_summary([trade.terminal_performance for trade in knocked if trade.terminal_performance is not None]),
    }


def _buffer_outcomes(trades: Sequence[Any]) -> dict[str, Any]:
    values = np.asarray([trade.terminal_performance for trade in trades if trade.terminal_performance is not None], dtype=float)
    return {
        "terminal_performance": number_summary(values),
        "non_negative_terminal_rate": float(np.mean(values >= 0)) if len(values) else None,
        "negative_terminal_rate": float(np.mean(values < 0)) if len(values) else None,
    }


def _pnl_per_accumulated_unit(trades: Sequence[Any]) -> dict[str, float | None]:
    values = [
        float(trade.pnl) / float(quantity)
        for trade in trades
        if isinstance((quantity := trade.events.get("Q_acc")), (int, float, np.number)) and float(quantity) > 0
    ]
    return number_summary(values)


def _finite_term(terms: Mapping[str, Any], key: str) -> float | None:
    value = terms.get(key)
    return float(value) if isinstance(value, (int, float, np.number)) and np.isfinite(float(value)) else None


def _volatility_buckets(values: np.ndarray, strike: float | None) -> dict[str, Any]:
    if strike is None or not len(values):
        return {"below_strike_count": 0, "at_or_above_strike_count": 0, "below_strike_rate": None}
    below = int(np.sum(values < strike))
    return {
        "below_strike_count": below,
        "at_or_above_strike_count": int(len(values) - below),
        "below_strike_rate": below / len(values),
    }


def _event_outcomes(trades: Sequence[Any], event_names: Sequence[str]) -> dict[str, Any]:
    triggered = [
        trade for trade in trades
        if any(event_happened(trade.events.get(name)) for name in event_names)
    ]
    triggered_ids = {id(trade) for trade in triggered}
    untriggered = [trade for trade in trades if id(trade) not in triggered_ids]
    total = len(trades)
    return {
        "triggered_count": len(triggered),
        "untriggered_count": total - len(triggered),
        "triggered_rate": len(triggered) / total if total else None,
        "triggered_pnl": number_summary([trade.pnl for trade in triggered]),
        "untriggered_pnl": number_summary([trade.pnl for trade in untriggered]),
    }


def _metric_gaps(profile_id: str, terms: Mapping[str, Any]) -> list[str]:
    gaps = {
        "terminal_payoff": ["break_even_not_derived_from_shared_contract"],
        "accumulator": ["periodic_purchase_cashflows_not_exposed_by_shared_contract_core"],
        "airbag": ["participation_and_floor_cashflow_decomposition_not_exposed"],
        "shark_fin": ["participation_and_floor_cashflow_decomposition_not_exposed"],
    }.get(profile_id, []).copy()
    if "S0Vec" in terms:
        gaps.append("synchronous_multi_underlying_coverage_not_estimated_from_single_sample")
    return gaps
