"""标准化行情的最小质量检查。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


class DataQualityError(ValueError):
    code = "quality_error"


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
) -> dict[str, Any]:
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
    price_fields = [field for field in fields if field != "volume"]
    if price_fields and (frame[price_fields] <= 0).any(axis=None):
        raise DataQualityError("价格字段必须为正数")
    if "volume" in fields and (frame["volume"] < 0).any():
        raise DataQualityError("volume不能为负数")
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
        for asset_id in coverage:
            expected = {str(item) for item in expected_trading_dates.get(asset_id, ())}
            observed = set(frame.loc[frame["asset_id"] == asset_id, "date"].astype(str))
            unexpected_dates = sorted(observed.difference(expected))
            if unexpected_dates:
                raise DataQualityError(
                    f"显式交易日历不包含已观测日期：{asset_id}={','.join(unexpected_dates)}"
                )
            missing_dates = sorted(expected.difference(observed))
            if missing_dates:
                raise DataQualityError(
                    f"显式交易日历存在未观测交易日：{asset_id}={','.join(missing_dates)}"
                )
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
