"""Backtester公共指标的唯一计算文件。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


_EVENT_LABELS = {
    "tau_out": "敲出", "tau_out_1": "第一敲出", "tau_out_2": "第二敲出", "tau_in": "敲入",
    "tau_touch": "触碰", "tau_up": "上行障碍触发", "tau_down": "下行障碍触发",
    "tau_reset": "重置", "tau_hedge": "避险触发", "tau_hedge_raw": "原始避险触发",
}
_MONITOR_LABELS = {
    "Q_acc": "累计数量", "n_coupon": "累计派息期数", "n_in": "区间内观察次数",
    "n_obs_actual": "实际观察次数", "sigma_realized": "实现波动率", "S_out": "触发时价格",
}
_KNOCK_OUT_EVENTS = ("tau_out", "tau_out_1", "tau_out_2")


def summarize_common_metrics(
    trades: Sequence[Any],
    contract: Any,
    *,
    include_annual: bool,
) -> dict[str, Any]:
    """由唯一逐笔账本派生所有非产品专属统计。"""
    pnl = np.asarray([trade.pnl for trade in trades], dtype=float)
    returns = np.asarray([trade.return_value for trade in trades if trade.return_value is not None], dtype=float)
    common = {
        "sample_count": len(trades),
        "win_rate": float(np.mean(pnl > 0)) if len(pnl) else None,
        "average_pnl": float(pnl.mean()) if len(pnl) else None,
        "median_pnl": float(np.median(pnl)) if len(pnl) else None,
        "minimum_pnl": float(pnl.min()) if len(pnl) else None,
        "maximum_pnl": float(pnl.max()) if len(pnl) else None,
        # ``minimum_pnl`` is signed.  ``max_loss`` is a loss amount, so an
        # all-profitable sample has no loss rather than a positive “loss”.
        "max_loss": max(0.0, -float(pnl.min())) if len(pnl) else None,
        "average_return": float(returns.mean()) if len(returns) else None,
        "median_return": float(np.median(returns)) if len(returns) else None,
        "minimum_return": float(returns.min()) if len(returns) else None,
        "maximum_return": float(returns.max()) if len(returns) else None,
        "return_not_applicable_count": sum(trade.return_value is None for trade in trades),
        "return_distribution": distribution(returns),
    }
    tenor_years = float(contract.terms.get("T", 0.0))
    return {
        "common_metrics": common,
        "underlying_performance": underlying_performance_summary(trades),
        "event_summary": event_summary(trades, tenor_years),
        "monitor_summary": monitor_summary(trades),
        "outcome_summary": outcome_summary(trades, contract),
        "three_outcome_summary": three_outcome_summary(trades),
        "conditional_summary": conditional_summary(trades),
        "annual_summary": annual_summary(trades, contract) if include_annual else [],
    }


def number_summary(values: np.ndarray | Sequence[float]) -> dict[str, float | None]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return {
        "average": float(values.mean()) if len(values) else None,
        "median": float(np.median(values)) if len(values) else None,
        "minimum": float(values.min()) if len(values) else None,
        "maximum": float(values.max()) if len(values) else None,
    }


def monitor_values(trades: Sequence[Any], name: str) -> np.ndarray:
    return np.asarray([
        float(value) for trade in trades
        if isinstance((value := getattr(trade, "events", {}).get(name)), (int, float, np.number))
        and not isinstance(value, (bool, np.bool_)) and np.isfinite(float(value))
    ], dtype=float)


def paired_monitor_values(trades: Sequence[Any], left: str, right: str) -> tuple[np.ndarray, np.ndarray]:
    pairs = [
        (float(events[left]), float(events[right]))
        for trade in trades
        if isinstance((events := getattr(trade, "events", {})).get(left), (int, float, np.number))
        and isinstance(events.get(right), (int, float, np.number))
        and not isinstance(events[left], (bool, np.bool_)) and not isinstance(events[right], (bool, np.bool_))
        and np.isfinite(float(events[left])) and np.isfinite(float(events[right]))
    ]
    if not pairs:
        return np.asarray([], dtype=float), np.asarray([], dtype=float)
    return np.asarray([pair[0] for pair in pairs], dtype=float), np.asarray([pair[1] for pair in pairs], dtype=float)


def underlying_performance_summary(trades: Sequence[Any]) -> dict[str, Any]:
    assets = sorted({asset for trade in trades for asset in getattr(trade, "underlying_performances", {})})
    by_asset = {
        asset: number_summary([float(trade.underlying_performances[asset]) for trade in trades if asset in trade.underlying_performances])
        for asset in assets
    }
    worst = [min(trade.underlying_performances.values()) for trade in trades if trade.underlying_performances]
    dispersion = [
        max(trade.underlying_performances.values()) - min(trade.underlying_performances.values())
        for trade in trades if len(trade.underlying_performances) > 1
    ]
    return {
        "by_asset": by_asset,
        "worst_of_per_trade": number_summary(worst),
        "cross_underlying_dispersion": number_summary(dispersion),
    }


def event_happened(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("date") is not None


def has_knock_out(trade: Any) -> bool:
    return any(event_happened(getattr(trade, "events", {}).get(name)) for name in _KNOCK_OUT_EVENTS)


def event_summary(trades: Sequence[Any], tenor_years: float) -> dict[str, dict[str, Any]]:
    names = sorted({name for trade in trades for name in trade.events if name.startswith("tau_")})
    summary: dict[str, dict[str, Any]] = {}
    for name in names:
        values = [trade.events[name] for trade in trades if name in trade.events]
        happened = [value for value in values if event_happened(value)]
        days = np.asarray([float(value["time"]) * 365.0 for value in happened], dtype=float)
        summary[name] = {
            "name": name,
            "label": _EVENT_LABELS.get(name, name),
            "sample_count": len(values),
            "trigger_count": len(happened),
            "trigger_rate": len(happened) / len(values) if values else None,
            "average_days": float(days.mean()) if len(days) else None,
            "median_days": float(np.median(days)) if len(days) else None,
            "monthly_distribution": monthly_distribution(days, tenor_years, len(values)),
        }
    return summary


def monitor_summary(trades: Sequence[Any]) -> dict[str, dict[str, Any]]:
    names = sorted({name for trade in trades for name in trade.events if not name.startswith("tau_")})
    summary: dict[str, dict[str, Any]] = {}
    for name in names:
        raw = [trade.events[name] for trade in trades if name in trade.events]
        if raw and all(isinstance(value, (bool, np.bool_)) for value in raw):
            true_count = sum(bool(value) for value in raw)
            summary[name] = {
                "name": name,
                "label": _MONITOR_LABELS.get(name, name),
                "sample_count": len(raw),
                "true_count": true_count,
                "false_count": len(raw) - true_count,
                "true_rate": true_count / len(raw),
            }
            continue
        values = monitor_values(trades, name)
        if len(values):
            summary[name] = {"name": name, "label": _MONITOR_LABELS.get(name, name), **number_summary(values), "sample_count": len(values)}
    return summary


def outcome_summary(trades: Sequence[Any], contract: Any) -> list[dict[str, Any]]:
    definitions = {
        (path_index, case_index): {"condition": str(path["condition"]), "domain": str(case["domain"])}
        for path_index, path in enumerate(contract.paths)
        for case_index, case in enumerate(path["cases"])
    }
    total = len(trades)
    rows: list[dict[str, Any]] = []
    for (path_index, case_index), definition in definitions.items():
        selected = [trade for trade in trades if trade.path_id == path_index and trade.case_id == case_index]
        pnl = np.asarray([trade.pnl for trade in selected], dtype=float)
        rows.append({
            "code": f"path_{path_index + 1}_case_{case_index + 1}",
            "label": f"路径{path_index + 1}·情形{case_index + 1}",
            "condition": definition["condition"],
            "domain": definition["domain"],
            "count": len(selected),
            "rate": len(selected) / total if total else None,
            "average_pnl": float(pnl.mean()) if len(pnl) else None,
        })
    return rows


def three_outcome_summary(trades: Sequence[Any]) -> list[dict[str, Any]]:
    total = len(trades)
    definitions = (
        ("ko", "敲出KO", has_knock_out),
        ("no_ki_no_ko", "未敲入未敲出", lambda trade: not event_happened(trade.events.get("tau_in")) and not has_knock_out(trade)),
        ("ki_no_ko", "敲入未敲出", lambda trade: event_happened(trade.events.get("tau_in")) and not has_knock_out(trade)),
    )
    return [{"code": code, "label": label, "count": sum(rule(trade) for trade in trades), "rate": sum(rule(trade) for trade in trades) / total if total else None} for code, label, rule in definitions]


def conditional_summary(trades: Sequence[Any]) -> dict[str, Any]:
    ki_trades = [trade for trade in trades if event_happened(trade.events.get("tau_in"))]
    ki_count = len(ki_trades)
    ki_no_ko = sum(not has_knock_out(trade) for trade in ki_trades)
    return {
        "knock_in_count": ki_count,
        "knock_in_then_no_knock_out_count": ki_no_ko,
        "knock_in_then_no_knock_out_rate": ki_no_ko / ki_count if ki_count else None,
        "knock_in_then_knock_out_count": ki_count - ki_no_ko,
        "knock_in_then_knock_out_rate": (ki_count - ki_no_ko) / ki_count if ki_count else None,
    }


def annual_summary(trades: Sequence[Any], contract: Any) -> list[dict[str, Any]]:
    grouped: dict[int, list[Any]] = {}
    for trade in trades:
        grouped.setdefault(pd.Timestamp(trade.entry_date).year, []).append(trade)
    rows: list[dict[str, Any]] = []
    for year, items in sorted(grouped.items()):
        pnl = np.asarray([trade.pnl for trade in items], dtype=float)
        returns = np.asarray([trade.return_value for trade in items if trade.return_value is not None], dtype=float)
        rows.append({
            "year": year,
            "sample_count": len(items),
            "win_rate": float(np.mean(pnl > 0)) if len(pnl) else None,
            "average_pnl": float(pnl.mean()) if len(pnl) else None,
            "average_return": float(returns.mean()) if len(returns) else None,
            "outcome_summary": outcome_summary(items, contract),
            "three_outcome_summary": three_outcome_summary(items),
            "event_rates": {name: value["trigger_rate"] for name, value in event_summary(items, 0.0).items()},
        })
    return rows


def distribution(values: np.ndarray) -> dict[str, int]:
    if not len(values):
        return {}
    labels = ["<=-20%", "-20%~-5%", "-5%~0", "0~5%", "5%~20%", ">20%"]
    bucket = pd.cut(values, bins=[-np.inf, -0.2, -0.05, 0.0, 0.05, 0.2, np.inf], labels=labels, include_lowest=True)
    return {str(key): int(value) for key, value in bucket.value_counts().sort_index().items()}


def monthly_distribution(days: np.ndarray, tenor_years: float, sample_count: int) -> list[dict[str, Any]]:
    max_month = max(1, int(np.ceil(max(float(tenor_years), 0.0) * 12.0)))
    counts = np.zeros(max_month, dtype=int)
    for value in days:
        month = max(1, int(np.ceil(float(value) / (365.0 / 12.0))))
        counts[min(month, max_month) - 1] += 1
    triggered = int(counts.sum())
    return [{
        "month": index + 1,
        "count": int(count),
        "rate_of_samples": int(count) / sample_count if sample_count else None,
        "rate_of_events": int(count) / triggered if triggered else None,
    } for index, count in enumerate(counts)]
