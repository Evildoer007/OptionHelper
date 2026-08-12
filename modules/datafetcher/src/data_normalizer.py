"""Provider返回日线数据后的唯一标准化入口。"""

from __future__ import annotations

import hashlib

import pandas as pd

from .market_conventions import market_conventions
from .models import DataRequest


_ALIASES = {
    "trade_date": "date",
    "datetime": "date",
    "time": "date",
    "ts_code": "asset_id",
    "thscode": "asset_id",
    "code": "asset_id",
    "adjusted_open": "adj_open",
    "adjusted_high": "adj_high",
    "adjusted_low": "adj_low",
    "adjusted_close": "adj_close",
    "vol": "volume",
}


class DataNormalizationError(ValueError):
    code = "normalization_error"


def daily_content_hash(frame: pd.DataFrame) -> str:
    """返回规范日线CSV的内容地址，缓存和DataAssetRef共用同一算法。"""

    return hashlib.sha256(daily_csv_bytes(frame)).hexdigest()


def daily_csv_bytes(frame: pd.DataFrame) -> bytes:
    """生成唯一的规范CSV字节，缓存校验与DataStore写入不得各自序列化。"""

    if not {"asset_id", "date"}.issubset(frame.columns):
        raise DataNormalizationError("数据缺少asset_id或date，无法序列化")
    canonical = frame.sort_values(["asset_id", "date"]).to_csv(index=False, lineterminator="\n", float_format="%.12g")
    return canonical.encode("utf-8")


def normalize_daily_history(frame: pd.DataFrame, request: DataRequest) -> pd.DataFrame:
    """将Provider原始列转为``date + asset_id + requested fields``。"""

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataNormalizationError("Provider未返回可用日线记录")
    result = frame.copy()
    result.columns = [str(column).strip().lower() for column in result.columns]
    result = result.rename(columns={source: target for source, target in _ALIASES.items() if source in result.columns})
    if "date" not in result.columns:
        raise DataNormalizationError("Provider数据缺少date字段")
    if "asset_id" not in result.columns:
        if len(request.asset_ids) != 1:
            raise DataNormalizationError("多标的Provider数据必须包含asset_id字段")
        result["asset_id"] = request.asset_id

    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result["asset_id"] = result["asset_id"].astype(str).str.strip().str.upper()
    if "close" not in result.columns:
        raise DataNormalizationError("Provider数据缺少请求字段：close")
    for asset_id, convention in market_conventions(request.asset_ids, request.adjustment).items():
        if convention["effective_adjustment"] == "none":
            result.loc[result["asset_id"].eq(asset_id), "adj_close"] = result.loc[result["asset_id"].eq(asset_id), "close"]
    required = ["date", "asset_id", *request.fields]
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise DataNormalizationError(f"Provider数据缺少请求字段：{','.join(missing)}")
    for column in request.fields:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.loc[:, required]
    result = result[result["asset_id"].isin(request.asset_ids)]
    result = result[(result["date"] >= request.start_date) & (result["date"] <= request.end_date)]
    if result.empty:
        raise DataNormalizationError("Provider返回数据不覆盖请求日期区间")
    missing_assets = sorted(set(request.asset_ids).difference(result["asset_id"].unique()))
    if missing_assets:
        raise DataNormalizationError(f"Provider返回未覆盖请求标的：{','.join(missing_assets)}")
    return result.sort_values(["asset_id", "date"]).reset_index(drop=True)


def merge_daily_history(*frames: pd.DataFrame) -> pd.DataFrame:
    """合并缓存与新增区间；相同日期的不同数值必须显式失败。"""

    usable = [frame for frame in frames if frame is not None and not frame.empty]
    if not usable:
        return pd.DataFrame()
    combined = pd.concat(usable, ignore_index=True)
    keys = ["asset_id", "date"]
    duplicated = combined.duplicated(keys, keep=False)
    if duplicated.any():
        for _, group in combined.loc[duplicated].groupby(keys, sort=False):
            if len(group.drop_duplicates()) != 1:
                raise DataNormalizationError("缓存与Provider在同一交易日存在冲突数值")
    return combined.drop_duplicates(keys, keep="first").sort_values(keys).reset_index(drop=True)
