"""标准化行情的最小质量检查。"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


class DataQualityError(ValueError):
    code = "quality_error"


def validate_ohlc_relationships(frame: pd.DataFrame, prefix: str = "") -> None:
    columns = tuple(f"{prefix}{name}" for name in ("open", "high", "low", "close"))
    if not set(columns).issubset(frame.columns):
        return
    open_field, high_field, low_field, close_field = columns
    if frame[list(columns)].isna().any(axis=None):
        label = "复权OHLC" if prefix else "原始OHLC"
        raise DataQualityError(f"{label}关系无效：价格字段含空值")
    invalid = (
        frame[high_field].lt(frame[[open_field, close_field]].max(axis=1))
        | frame[low_field].gt(frame[[open_field, close_field]].min(axis=1))
        | frame[high_field].lt(frame[low_field])
    )
    if invalid.any():
        label = "复权OHLC" if prefix else "原始OHLC"
        raise DataQualityError(f"{label}关系无效：最高价或最低价与开收盘价矛盾")


def observed_edge_intervals(
    frame: pd.DataFrame,
    asset_id: str,
    start_date: str,
    end_date: str,
    *,
    latest_completed_date: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """无验证日历时只判断请求内是否已有观测，不推断任何交易日。

    已有观测即可安全复用，并由``calendar_completeness=unverified``明确不声明
    区间完整；主动更新必须使用force_refresh。请求内完全没有观测时才允许获取
    整个区间，不重复拉取缓存锚点。
    """

    completed_end = min(end_date, latest_completed_date or end_date)
    if start_date > completed_end:
        return ()
    dates = frame.loc[
        frame["asset_id"].astype(str).str.upper() == asset_id,
        "date",
    ].astype(str)
    observed_in_request = dates.between(start_date, completed_end).any()
    return () if observed_in_request else ((start_date, completed_end),)


def validate_daily_history(
    frame: pd.DataFrame,
    fields: Iterable[str] = (),
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    expected_trading_dates: Mapping[str, Sequence[str]] | None = None,
    latest_observable_date: str | None = None,
    latest_pending_session: str | None = None,
) -> dict[str, Any]:
    fields = tuple(fields)
    required = {"date", "asset_id", *fields}
    missing = required.difference(frame.columns)
    if missing:
        raise DataQualityError(f"历史行情缺少字段：{','.join(sorted(missing))}")
    if frame.empty:
        raise DataQualityError("历史行情为空")
    if latest_observable_date is not None:
        observed = frame["date"].astype(str)
        if observed.gt(latest_observable_date).any():
            raise DataQualityError("历史行情包含未来日期")
    if frame[["date", "asset_id", *fields]].isna().any(axis=None):
        raise DataQualityError("历史行情含空日期、标的或请求字段")
    if frame.duplicated(["date", "asset_id"]).any():
        raise DataQualityError("历史行情含重复date+asset_id记录")
    for field in fields:
        if not pd.to_numeric(frame[field], errors="coerce").map(math.isfinite).all():
            raise DataQualityError(f"历史行情字段必须为有限数值：{field}")
    price_fields = [field for field in fields if field != "volume"]
    if price_fields and (frame[price_fields] <= 0).any(axis=None):
        raise DataQualityError("价格字段必须为正数")
    if "volume" in fields and (frame["volume"] < 0).any():
        raise DataQualityError("volume不能为负数")
    validate_ohlc_relationships(frame)
    validate_ohlc_relationships(frame, "adj_")
    coverage = {
        asset_id: {
            "start_date": group["date"].min(),
            "end_date": group["date"].max(),
            "row_count": int(len(group)),
        }
        for asset_id, group in frame.groupby("asset_id", sort=True)
    }
    calendar_gaps: dict[str, list[str]] = {}
    if expected_trading_dates is not None:
        expected_assets = {str(asset_id) for asset_id in expected_trading_dates}
        observed_assets = set(coverage)
        if expected_assets != observed_assets:
            missing_assets = sorted(expected_assets.difference(observed_assets))
            unexpected_assets = sorted(observed_assets.difference(expected_assets))
            details = []
            if missing_assets:
                details.append("缺少标的=" + ",".join(missing_assets))
            if unexpected_assets:
                details.append("日历未声明标的=" + ",".join(unexpected_assets))
            raise DataQualityError("显式交易日历与行情标的不一致：" + "；".join(details))
        expected_by_asset: dict[str, set[str]] = {}
        for asset_id in sorted(observed_assets):
            expected = {str(item) for item in expected_trading_dates.get(asset_id, ())}
            expected_by_asset[asset_id] = expected
            observed = set(frame.loc[frame["asset_id"] == asset_id, "date"].astype(str))
            unexpected_dates = sorted(observed.difference(expected))
            if unexpected_dates:
                raise DataQualityError(
                    f"显式交易日历不包含已观测日期：{asset_id}={','.join(unexpected_dates)}"
                )
            missing_dates = sorted(expected.difference(observed))
            if missing_dates:
                calendar_gaps[asset_id] = missing_dates
        if calendar_gaps:
            pending_dates = {item for values in calendar_gaps.values() for item in values}
            latest_session_pending = (
                latest_pending_session is not None
                and len(pending_dates) == 1
                and next(iter(pending_dates)) == latest_pending_session
                and set(calendar_gaps) == observed_assets
                and all(
                    len(calendar_gaps[asset_id]) == 1
                    and bool(expected_by_asset[asset_id])
                    and calendar_gaps[asset_id][0] == max(expected_by_asset[asset_id])
                    for asset_id in observed_assets
                )
            )
            if latest_session_pending:
                calendar_completeness = "latest_session_pending"
            else:
                first_asset = sorted(calendar_gaps)[0]
                raise DataQualityError(
                    f"显式交易日历存在未观测交易日：{first_asset}={','.join(calendar_gaps[first_asset])}"
                )
        else:
            calendar_completeness = "complete"
    else:
        calendar_completeness = "unverified"
    return {
        "status": "passed",
        "row_count": int(len(frame)),
        "asset_count": len(coverage),
        "coverage_by_asset": coverage,
        "duplicate_rows": 0,
        "missing_values": 0,
        "unordered_rows": 0,
        "observed_rows": {"status": "valid", "row_count": int(len(frame)), "coverage_by_asset": coverage},
        "calendar_completeness": calendar_completeness,
        "calendar_missing_dates": calendar_gaps,
    }
