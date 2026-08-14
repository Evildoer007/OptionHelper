"""Deterministic close-based market snapshots and historical volatility."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Mapping

from .models import DailyBar, MarketSnapshot, MarketSnapshotRequest
from .providers import MarketDataProvider


SCHEMA_ID = "optionhelper.market-snapshot"


def _historical_volatility(
    closes: tuple[float, ...], window: int, annualization_trading_days: int
) -> float:
    if len(closes) < window + 1:
        raise ValueError(
            f"计算HV{window}至少需要{window + 1}个复权收盘价，当前只有{len(closes)}个"
        )
    selected = closes[-(window + 1):]
    returns = [math.log(selected[index] / selected[index - 1]) for index in range(1, len(selected))]
    return float(statistics.stdev(returns) * math.sqrt(annualization_trading_days))


def _bars_sha256(bars: tuple[DailyBar, ...]) -> str:
    payload = [
        {
            **asdict(bar),
            "trading_date": bar.trading_date.isoformat(),
        }
        for bar in bars
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_market_snapshot(
    provider: MarketDataProvider,
    request: MarketSnapshotRequest,
) -> MarketSnapshot:
    start_date = request.as_of - timedelta(days=request.lookback_calendar_days)
    bars = provider.history_quotes(
        request.code,
        start_date,
        request.as_of,
        asset_type=request.asset_type,
    )
    bars = tuple(sorted((bar for bar in bars if bar.trading_date <= request.as_of), key=lambda bar: bar.trading_date))
    if not bars:
        raise ValueError("估值日及之前没有可用收盘行情")
    if len({bar.trading_date for bar in bars}) != len(bars):
        raise ValueError("市场数据含重复交易日")
    adjusted_closes = tuple(bar.adjusted_close for bar in bars)
    maximum_window = max(request.historical_volatility_windows)
    if len(adjusted_closes) < maximum_window + 1:
        raise ValueError(
            f"计算HV{maximum_window}至少需要{maximum_window + 1}个复权收盘价，"
            f"当前只有{len(adjusted_closes)}个"
        )
    volatility_by_window = tuple(
        (
            window,
            _historical_volatility(
                adjusted_closes, window, request.annualization_trading_days
            ),
        )
        for window in request.historical_volatility_windows
    )
    selected_volatility = dict(volatility_by_window)[request.volatility_window]
    market_date = bars[-1].trading_date
    source = (
        f"{provider.provider_name}:{request.code}:close@{market_date.isoformat()}:"
        f"HV{request.volatility_window}(adjusted_close,{request.annualization_trading_days})"
    )
    return MarketSnapshot(
        schema=SCHEMA_ID,
        code=request.code,
        asset_type=request.asset_type,
        requested_as_of=request.as_of,
        market_date=market_date,
        spot=bars[-1].close,
        volatility=selected_volatility,
        volatility_window=request.volatility_window,
        historical_volatility=volatility_by_window,
        risk_free_rate=float(request.risk_free_rate),
        dividend_yield=float(request.dividend_yield),
        carry=None if request.carry is None else float(request.carry),
        provider=provider.provider_name,
        source=source,
        price_field="close_unadjusted",
        volatility_field="adjusted_close_log_return_sample_std",
        annualization_trading_days=request.annualization_trading_days,
        observation_count=len(bars),
        history_start_date=bars[0].trading_date,
        history_end_date=bars[-1].trading_date,
        data_sha256=_bars_sha256(bars),
    )


def _json_value(snapshot: MarketSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    for name in (
        "requested_as_of", "market_date", "history_start_date", "history_end_date"
    ):
        payload[name] = payload[name].isoformat()
    payload["historical_volatility"] = [list(item) for item in snapshot.historical_volatility]
    return payload


def save_market_snapshot(snapshot: MarketSnapshot, path: Path | str) -> None:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有市场快照：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(snapshot), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_market_snapshot(path: Path | str) -> MarketSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("市场快照必须为JSON对象")
    expected = {field.name for field in __import__("dataclasses").fields(MarketSnapshot)}
    unknown = set(payload) - expected
    missing = expected - set(payload)
    if unknown or missing:
        raise ValueError(
            f"市场快照字段不匹配：missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    values = dict(payload)
    if values["schema"] != SCHEMA_ID:
        raise ValueError(f"不支持的市场快照版本：{values['schema']}")
    for name in (
        "requested_as_of", "market_date", "history_start_date", "history_end_date"
    ):
        values[name] = date.fromisoformat(values[name])
    values["historical_volatility"] = tuple(
        (int(window), float(volatility))
        for window, volatility in values["historical_volatility"]
    )
    return MarketSnapshot(**values)
