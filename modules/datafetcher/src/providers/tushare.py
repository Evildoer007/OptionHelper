"""Explicit Tushare-compatible daily data; never switch credential destinations.

Tinyshare transport is injected by the host-approved client factory. Financial
normalization is shared, but provider identity, secrets and caches stay separate.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pandas as pd

from ..config import DataFetcherConfig, SecretPortUnavailable, resolve_secret
from ..market_conventions import china_market_convention
from ..models import CalendarRequest, DataRequest
from .base import (ProviderAccountPermissionDenied, ProviderInputError, ProviderNoData,
                   ProviderQuotaExceeded, ProviderUnauthorized, ProviderUnavailable)

OHLC = ("open", "high", "low", "close")
ENDPOINTS = {"stock": "daily", "etf": "fund_daily", "index": "index_daily"}


def _provider_failure(error: Exception) -> Exception:
    # Use server text only for classification. Never propagate credential echoes.
    message = str(error).lower()
    if any(word in message for word in ("token", "授权码", "认证", "鉴权")):
        return ProviderUnauthorized("数据Token验证失败，请检查所选服务与Token是否匹配")
    if any(word in message for word in ("频次", "频率", "每分钟", "quota", "rate limit")):
        return ProviderQuotaExceeded("数据服务调用额度或频次受限")
    if any(word in message for word in ("权限", "积分", "permission", "forbidden")):
        return ProviderAccountPermissionDenied("当前数据账号无权访问该接口")
    return ProviderUnavailable("数据服务请求未完成，请检查网络与服务状态")


def _dates(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        raise ProviderInputError("数据返回缺少日期字段")
    try:
        values = frame[column].astype(str)
        parsed = pd.to_datetime(values, format="%Y%m%d", errors="raise")
        if not values.str.fullmatch(r"\d{8}").all():
            raise ValueError
        return parsed.dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        raise ProviderInputError("数据返回日期格式无效") from None


class TushareProvider:
    network = True

    def __init__(self, name: str = "tushare"):
        if name not in {"tinyshare", "tushare"}:
            raise ValueError("Unsupported Tushare-compatible implementation")
        self.name = name

    @staticmethod
    def estimate_quota(request: DataRequest) -> int:
        return sum(2 if china_market_convention(a, request.adjustment)["effective_adjustment"] == "forward" else 1
                   for a in request.asset_ids)

    def _client(self, config: DataFetcherConfig) -> Any:
        if config.token_provider is not None and config.token_provider != self.name:
            raise ProviderUnauthorized("Token与所选数据服务不匹配")
        try:
            secret = resolve_secret(config.token_secret_ref, self.name.upper() + "_TOKEN",
                                    host_secret_port=config.token_secret_port)
        except SecretPortUnavailable:
            raise ProviderUnavailable("无法读取数据Token", reason_code="credential_unavailable") from None
        if not secret:
            raise ProviderUnauthorized("尚未配置数据Token")
        try:
            record = json.loads(secret)
        except ValueError:
            record = {"token": secret}
        if not isinstance(record, dict) or not isinstance(record.get("token"), str) or not record["token"].strip():
            raise ProviderUnauthorized("数据Token格式无效")
        from .tushare_transport import make_client
        return make_client(self.name, record["token"].strip(), config.timeout_seconds)

    @staticmethod
    def _query(client: Any, endpoint: str, **params: Any) -> pd.DataFrame:
        try:
            frame = client.query(endpoint, **params)
        except (ProviderUnavailable, ProviderUnauthorized, ProviderAccountPermissionDenied, ProviderQuotaExceeded):
            raise
        except Exception as error:
            raise _provider_failure(error) from None
        if not isinstance(frame, pd.DataFrame):
            raise ProviderInputError("数据服务返回格式无效")
        if frame.empty:
            raise ProviderNoData("数据服务未返回所请求记录")
        return frame.copy()

    def fetch(self, request: DataRequest, config: DataFetcherConfig) -> pd.DataFrame:
        client = self._client(config)
        frames = []
        for asset_id in request.asset_ids:
            convention = china_market_convention(asset_id, request.adjustment)
            endpoint = ENDPOINTS[str(convention["asset_class"])]
            params = dict(ts_code=asset_id, start_date=request.start_date.replace("-", ""), end_date=request.end_date.replace("-", ""))
            raw = self._query(client, endpoint, **params, fields="ts_code,trade_date,open,high,low,close,vol")
            required = {"ts_code", "trade_date", *OHLC, "vol"}
            if not required.issubset(raw) or not raw.ts_code.eq(asset_id).all():
                raise ProviderInputError("行情字段或标的与请求不一致")
            result = pd.DataFrame({"date": _dates(raw, "trade_date"), "asset_id": asset_id})
            if not result.date.between(request.start_date, request.end_date).all() or result.date.duplicated().any():
                raise ProviderInputError("行情日期越界或重复")
            for name in OHLC:
                result[name] = pd.to_numeric(raw[name], errors="coerce")
            # Tushare daily/fund_daily/index_daily all document volume in lots.
            result["volume"] = pd.to_numeric(raw.vol, errors="coerce") * 100
            if result[[*OHLC, "volume"]].isna().any().any() or not ((result[[*OHLC, "volume"]] > -float("inf")) & (result[[*OHLC, "volume"]] < float("inf"))).all().all() or (result[list(OHLC)] <= 0).any().any() or (result.volume < 0).any():
                raise ProviderInputError("行情价格或成交量无效")
            ratio = pd.Series(1.0, index=result.index)
            if convention["effective_adjustment"] == "forward":
                factor_api = "fund_adj" if convention["asset_class"] == "etf" else "adj_factor"
                factors = self._query(client, factor_api, **params, fields="ts_code,trade_date,adj_factor")
                if not {"ts_code", "trade_date", "adj_factor"}.issubset(factors) or not factors.ts_code.eq(asset_id).all():
                    raise ProviderInputError("复权因子字段或标的不一致")
                factors["date"] = _dates(factors, "trade_date")
                factors["adj_factor"] = pd.to_numeric(factors.adj_factor, errors="coerce")
                if factors.date.duplicated().any() or factors.adj_factor.isna().any() or not ((factors.adj_factor > 0) & (factors.adj_factor < float('inf'))).all():
                    raise ProviderInputError("复权因子缺失、重复或无效")
                joined = result[["date"]].merge(factors[["date", "adj_factor"]], on="date", how="left", validate="one_to_one")
                if joined.adj_factor.isna().any():
                    raise ProviderInputError("复权因子未覆盖全部行情日期，不能用原始价格替代")
                anchor = joined.sort_values("date").adj_factor.iloc[-1]
                ratio = joined.adj_factor.set_axis(result.index) / anchor
            for name in OHLC:
                result["adj_" + name] = result[name] * ratio
            frames.append(result)
        return pd.concat(frames, ignore_index=True).sort_values(["asset_id", "date"]).reset_index(drop=True)

    def fetch_calendar(self, request: CalendarRequest, config: DataFetcherConfig) -> dict[str, tuple[str, ...]]:
        client = self._client(config)
        result = {}
        expected = set(pd.date_range(request.start_date, request.end_date).strftime("%Y-%m-%d"))
        for exchange in sorted({str(china_market_convention(a)["exchange"]) for a in request.asset_ids}):
            frame = self._query(client, "trade_cal", exchange=exchange,
                                start_date=request.start_date.replace("-", ""), end_date=request.end_date.replace("-", ""),
                                fields="exchange,cal_date,is_open")
            if not {"exchange", "cal_date", "is_open"}.issubset(frame) or not frame.exchange.eq(exchange).all():
                raise ProviderInputError("交易日历字段或交易所不一致")
            dates = _dates(frame, "cal_date")
            opened = frame.is_open.astype(str)
            if dates.duplicated().any() or set(dates) != expected or not opened.isin({"0", "1"}).all():
                raise ProviderInputError("交易日历没有完整覆盖请求，不能推测缺失交易日")
            result[exchange] = tuple(sorted(dates[opened.eq("1")]))
        return result

    def test_connection(self, config: DataFetcherConfig) -> None:
        # An actual endpoint probe, not merely accepting the token's shape.
        end = date.today() - timedelta(days=1)
        self.fetch_calendar(CalendarRequest(asset_ids=("000300.SH",), start_date=(end-timedelta(days=6)).isoformat(), end_date=end.isoformat()), config)
