"""Desk-only daily dashboard. Derived observations never become pricing inputs.

Every panel retains its own observation date and source. Provider adapters return
normalized rows; this module owns deterministic calculations, not provider names.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping
import math

import pandas as pd

from .market_conventions import china_market_convention

SECTIONS = ("prices", "valuation", "etf", "futures", "options")
HV_WINDOWS = (20, 60, 122, 244)
ANNUALIZATION = 244


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def panel(status: str, message: str = "", **content: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **content}


def rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON has nulls, never NaN/Infinity. Dates are canonical ISO strings."""
    return [{str(k): (None if pd.isna(v) or isinstance(v, (float, int)) and not math.isfinite(v) else v.item() if hasattr(v, "item") else v)
             for k, v in row.items()} for row in frame.to_dict("records")]


def percentile(values: pd.Series, latest: float | None, *, positive: bool = False) -> dict[str, Any]:
    series = pd.to_numeric(values, errors="coerce").replace([math.inf, -math.inf], math.nan).dropna()
    if positive:
        series = series[series > 0]
    if latest is None or (positive and latest <= 0) or len(series) < 20:
        return {"value": None, "observations": len(series)}
    # Midrank handles flat series and ties without implying a 100th percentile.
    rank = ((series < latest).sum() + .5 * (series == latest).sum()) / len(series)
    return {"value": float(rank), "observations": len(series)}


def research_statistics(price: pd.Series, daily: pd.Series, view: pd.DataFrame) -> dict[str, Any]:
    """Observed history only: no forecasts, inferred flows or synthetic returns."""
    sample = daily.dropna()
    edges = [-math.inf, -.05, -.03, -.02, -.01, 0, .01, .02, .03, .05, math.inf]
    labels = ["低于-5%", "-5%至-3%", "-3%至-2%", "-2%至-1%", "-1%至0%",
              "0%至1%", "1%至2%", "2%至3%", "3%至5%", "5%及以上"]
    counts = pd.cut(sample, edges, right=False, labels=labels).value_counts(sort=False)
    recent_volume = view.volume.iloc[-20:]
    average_volume = number(recent_volume.mean()) if len(recent_volume) == 20 and recent_volume.notna().all() else None
    current = number(price.iloc[-1])
    moving = {f"ma{window}": number(price.rolling(window, min_periods=window).mean().iloc[-1]) for window in (20, 60)}
    enough = len(sample) >= 20
    return {
        "observations": len(sample), "missing_prices": int(price.isna().sum()),
        "up_day_share": float((sample > 0).mean()) if enough else None,
        "return_p05": number(sample.quantile(.05)) if enough else None,
        "return_median": number(sample.median()) if enough else None,
        "return_p95": number(sample.quantile(.95)) if enough else None,
        "worst_day": number(sample.min()) if enough else None,
        "best_day": number(sample.max()) if enough else None,
        "current_drawdown": number(view.drawdown.iloc[-1]),
        "max_drawdown": number(view.drawdown.min()),
        "volume_20d": average_volume,
        "volume_ratio": number(view.volume.iloc[-1] / average_volume) if average_volume and average_volume > 0 else None,
        "ma20_distance": current / moving["ma20"] - 1 if current is not None and moving["ma20"] else None,
        "ma60_distance": current / moving["ma60"] - 1 if current is not None and moving["ma60"] else None,
        "volatility_windows": [{"window": window, "value": number(view[f"hv{window}"].iloc[-1])} for window in HV_WINDOWS],
        "distribution": [{"label": label, "count": int(counts[label])} for label in labels],
    }


def price_panel(frame: pd.DataFrame, asset_id: str, convention: Mapping[str, Any], *, sessions=()) -> dict[str, Any]:
    history = frame.loc[frame.asset_id.eq(asset_id)].copy().sort_values("date")
    if history.empty:
        return panel("unavailable", "所选标的没有行情记录。")
    if history.date.duplicated().any():
        raise ValueError("行情存在重复日期，不能计算Dashboard指标。")
    history["date"] = pd.to_datetime(history.date, errors="raise").dt.strftime("%Y-%m-%d")
    first, last = history.date.iloc[0], history.date.iloc[-1]
    if sessions:
        complete = [day for day in sessions if first <= day <= last]
        history = history.set_index("date").reindex(complete).rename_axis("date").reset_index()
        history["asset_id"] = asset_id
    field = str(convention.get("historical_return_field", "close"))
    if field not in history:
        return panel("unavailable", "缺少约定复权口径的价格，未改用其他价格代替。")
    price = pd.to_numeric(history[field], errors="coerce").replace([math.inf, -math.inf], math.nan).where(lambda value: value > 0)
    raw = pd.to_numeric(history.close, errors="coerce").replace([math.inf, -math.inf], math.nan).where(lambda value: value > 0)
    returns = (price / price.shift(1)).map(lambda x: math.log(x) if pd.notna(x) and x > 0 else math.nan)
    view = pd.DataFrame({"date": history.date, "close": raw, "return_price": price,
                         "volume": pd.to_numeric(history.get("volume", pd.Series(index=history.index, dtype=float)), errors="coerce"),
                         "amount": pd.to_numeric(history.get("amount", pd.Series(index=history.index, dtype=float)), errors="coerce")})
    simple_returns = (price / price.shift(1) - 1).replace([math.inf, -math.inf], math.nan)
    view["daily_return"] = simple_returns
    view["ma20"] = price.rolling(20, min_periods=20).mean()
    view["ma60"] = price.rolling(60, min_periods=60).mean()
    view["drawdown"] = price / price.cummax() - 1
    for window in HV_WINDOWS:
        view[f"hv{window}"] = returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(ANNUALIZATION)
    def change(window):
        sample = price.iloc[-(window + 1):]
        return float(sample.iloc[-1] / sample.iloc[0] - 1) if len(sample) == window + 1 and sample.notna().all() else None
    year_start = (date.fromisoformat(last) - timedelta(days=365)).isoformat()
    recent = price[history.date.ge(year_start)]
    complete_year = bool(first <= year_start and recent.notna().all())
    yearly_drawdown = recent / recent.cummax() - 1
    summary = {"close": number(raw.iloc[-1]), "change_1d": change(1), "change_20d": change(20),
               "hv20": number(view.hv20.iloc[-1]),
               "drawdown_1y": number(yearly_drawdown.min()) if complete_year else None,
               "amount_20d": number(view.amount.iloc[-20:].mean()) if len(view) >= 20 and view.amount.iloc[-20:].notna().all() else None}
    gaps = int(price.isna().sum())
    return panel("partial" if gaps or not sessions else "available",
                 "行情日历未验证，波动率仅供研究展示。" if not sessions else "存在缺失交易日，未跨过缺失值计算收益。" if gaps else "",
                 as_of=last, start_date=first, annualization=ANNUALIZATION, price_field=field,
                 research=research_statistics(price, simple_returns, view),
                 summary=summary, hv_percentile=percentile(view.hv20, summary["hv20"]),
                 returns={str(window): change(window) for window in (1, 5, 20, 60, 122, 244)},
                 series=rows(view), records=rows(history), year_coverage_complete=complete_year)


def valuation_panel(data: list[Mapping[str, Any]], *, subject_id: str, as_of: str, asset_class: str = "index") -> dict[str, Any]:
    frame = pd.DataFrame(data)
    if frame.empty or "date" not in frame:
        return panel("unavailable", "未取得估值历史。")
    frame = frame.loc[frame.date.le(as_of)].sort_values("date")
    if frame.empty or frame.date.duplicated().any():
        return panel("unavailable", "估值日期无效或重复。")
    indicators = {}
    for field in ("pe", "pb", "ps", "dividend_yield", "total_shares", "market_value", "turnover_ratio"):
        if field not in frame:
            continue
        values = pd.to_numeric(frame[field], errors="coerce").replace([math.inf, -math.inf], math.nan)
        frame[field] = values
        valid = values.dropna()
        latest = number(valid.iloc[-1]) if not valid.empty else None
        indicators[field] = {"value": latest, "as_of": frame.loc[valid.index[-1], "date"] if not valid.empty else None,
                             "percentile": percentile(values, latest, positive=field in {"pe", "pb"})}
    if not any(item["value"] is not None for item in indicators.values()):
        return panel("unavailable", "当前请求未返回有效估值数据。", subject_id=subject_id, indicators=indicators, series=rows(frame))
    required = ("pe", "pb", "dividend_yield") if asset_class == "stock" else ("pe", "pb")
    message = "个股PE采用TTM、PB采用MRQ；股息率为TTM，不作为未来分红预测。" if asset_class == "stock" else "估值按数据源的指数整体口径；未获取的字段不参与计算。"
    return panel("partial" if any(indicators.get(key, {}).get("value") is None or indicators.get(key, {}).get("as_of") != as_of for key in required) else "available",
                 message, subject_id=subject_id,
                 as_of=frame.date.iloc[-1], start_date=frame.date.iloc[0], indicators=indicators, series=rows(frame))


def etf_panel(data: list[Mapping[str, Any]], prices: list[Mapping[str, Any]], *, as_of: str) -> dict[str, Any]:
    if not data:
        return panel("unavailable", "未取得ETF净值与份额。")
    frame = pd.DataFrame(data)
    if "date" not in frame or frame.date.duplicated().any():
        raise ValueError("ETF资料日期无效或重复。")
    frame = frame.loc[frame.date.le(as_of)].sort_values("date")
    close = pd.DataFrame(prices, columns=["date", "close"])
    merged = frame.merge(close, on="date", how="left", validate="one_to_one")
    if "nav" in merged:
        nav = pd.to_numeric(merged.nav, errors="coerce").where(lambda x: x > 0)
        merged["nav"] = nav
        merged["premium"] = merged.close / nav - 1
    if "shares" in merged:
        shares = pd.to_numeric(merged.shares, errors="coerce").where(lambda x: x >= 0)
        merged["shares"] = shares
        merged["shares_change"] = shares.diff()
    complete = not merged.empty and merged.date.iloc[-1] == as_of and all(
        key in merged and number(merged[key].iloc[-1]) is not None for key in ("nav", "shares", "premium")
    )
    latest = merged.iloc[-1] if not merged.empty else {}
    share_window = merged["shares"].iloc[-21:] if "shares" in merged else pd.Series(dtype=float)
    share_change_20d = float(share_window.iloc[-1] / share_window.iloc[0] - 1) if len(share_window) == 21 and share_window.notna().all() and share_window.iloc[0] > 0 else None
    summary = {field: number(latest.get(field)) for field in ("nav", "shares", "shares_change", "premium")}
    summary["shares_change_20d"] = share_change_20d
    premium_history = merged["premium"] if "premium" in merged else pd.Series(dtype=float)
    return panel("available" if complete else "partial",
                 "折溢价仅匹配同日净值；份额增减不等于资金净流入。",
                 summary=summary, premium_percentile=percentile(premium_history, summary["premium"]),
                 as_of=merged.date.iloc[-1] if not merged.empty else None, series=rows(merged))


def futures_panel(data: list[Mapping[str, Any]], *, index_id: str, spots: Mapping[str, float], as_of: str) -> dict[str, Any]:
    output = []
    for item in data:
        if item.get("underlying_id") != index_id or item.get("date") != as_of:
            continue
        future = number(item.get("close")); spot = number(spots.get(as_of))
        if future is None or spot is None or future <= 0 or spot <= 0:
            continue
        expiry = date.fromisoformat(str(item["expiry"]))
        days = (expiry - date.fromisoformat(as_of)).days
        if days < 0:
            continue
        output.append({**item, "days": days, "basis": future - spot,
                       "basis_rate": future / spot - 1,
                       "annualized_basis": (future / spot - 1) * 365 / days if days else None})
    output.sort(key=lambda item: (item["expiry"], item["contract_id"]))
    return panel("available" if output else "unavailable", "基差对比同日指数收盘价；年化采用365个自然日，不作期限外推。",
                 subject_id=index_id, as_of=as_of, contracts=output)


def options_panel(data: list[Mapping[str, Any]], *, underlying_id: str, spot: float, as_of: str) -> dict[str, Any]:
    contracts = []
    seen = set()
    if number(spot) is None or spot <= 0:
        return panel("unavailable", "缺少有效的同日标的价格，不能选择平值合约。")
    for item in data:
        if item.get("underlying_id") != underlying_id or item.get("date") != as_of or item.get("expiry", "") <= as_of:
            continue
        identity = item.get("contract_id")
        if not identity or identity in seen:
            raise ValueError("期权合约标识缺失或重复。")
        seen.add(identity)
        strike, iv, volume, oi = (number(item.get(key)) for key in ("strike", "iv", "volume", "open_interest"))
        if strike is None or strike <= 0 or item.get("side") not in {"call", "put"} or not item.get("iv_method"):
            continue
        if iv is not None and iv <= 0:
            iv = None
        contracts.append({**item, "strike": strike, "iv": iv, "volume": volume, "open_interest": oi})
    # Do not combine adjusted/nonadjusted contracts or different IV conventions.
    groups: dict[tuple, list] = {}
    for item in contracts:
        key = (item["expiry"], item["iv_method"], item.get("contract_series", "standard"))
        groups.setdefault(key, []).append(item)
    expiries = []
    for (expiry, method, series), items in sorted(groups.items()):
        result = {"expiry": expiry, "iv_method": method, "contract_series": series}
        for side in ("call", "put"):
            valid = [item for item in items if item["side"] == side and item["iv"] is not None]
            atm = min(valid, key=lambda item: (abs(item["strike"] - spot), item["strike"])) if valid else None
            result[f"{side}_iv"] = atm["iv"] if atm else None
            result[f"{side}_strike"] = atm["strike"] if atm else None
        for field in ("volume", "open_interest"):
            values = {side: [item[field] for item in items if item["side"] == side] for side in ("call", "put")}
            complete = all(values.values()) and all(v is not None and v >= 0 for entries in values.values() for v in entries)
            result[f"{field}_pcr"] = sum(values["put"]) / sum(values["call"]) if complete and sum(values["call"]) > 0 else None
        expiries.append(result)
    status = "unavailable" if not contracts else "partial" if any(item["iv"] is None for item in contracts) else "available"
    return panel(status, "IV来自数据源，按到期日、合约系列和模型口径分别展示。",
                 subject_id=underlying_id, as_of=as_of, expiries=expiries, contracts=contracts)


def build_dashboard(frame: pd.DataFrame, reference: Mapping[str, Any], *, asset_id: str | None = None, sessions=()) -> dict[str, Any]:
    assets = list(reference["asset_ids"])
    selected = asset_id or assets[0]
    if selected not in assets:
        raise ValueError("所选标的不属于本次行情资产。")
    # The asset's convention, not current UI defaults, governs old data.
    conventions = reference.get("price_convention", {})
    convention = conventions.get("asset_market_conventions", {}).get(selected)
    if not isinstance(convention, Mapping):
        raise ValueError("行情资产未记录逐标的价格口径。")
    # Market-history coverage.sessions are observed dates, not a full calendar.
    # Only the service may supply independently verified exchange sessions.
    prices = price_panel(frame, selected, convention, sessions=sessions)
    kind = convention.get("asset_class") or china_market_convention(selected)["asset_class"]
    return {"schema": "optionhelper.desk-dashboard.v1", "asset_id": selected, "asset_ids": assets,
            "asset_class": kind, "as_of": prices.get("as_of"), "source_ref": reference,
            "available_sections": {
                "stock": ["prices", "valuation", "details"],
                "etf": ["prices", "etf", "details"],
                "index": ["prices", "valuation", "futures", "details"],
            }.get(kind, ["prices", "details"]),
            "sections": {"prices": prices}}
