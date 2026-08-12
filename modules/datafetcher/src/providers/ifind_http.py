#!/usr/bin/env python3
"""通过iFind HTTPS下载并标准化日线历史行情。

用户和Host只配置Refresh Token。短期Access Token由Provider在请求前交换获得，
不接受外部持久化配置，也不写入结果。凭据只由环境变量或DataFetcherConfig中的
SecretRef解析。
字段口径：open、high、low、close用于原始市场尺度；对应adj_open、adj_high、adj_low、
adj_close按请求口径提供；不复权时它是close的审计别名。
普通价格指数不适用证券复权，四个adj_*字段分别等于原始OHLC。
股票和ETF会额外请求CPS=2前复权OHLC。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pandas as pd
import requests

from ..config import DataFetcherConfig, SecretPortUnavailable, resolve_secret
from ..market_conventions import china_market_convention
from ..models import CalendarRequest, DataRequest
from .base import ProviderQuotaExceeded, ProviderUnauthorized, ProviderUnavailable


TOKEN_URL = "https://quantapi.51ifind.com/api/v1/get_access_token"
HISTORY_URL = "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"
CALENDAR_URL = "https://quantapi.51ifind.com/api/v1/get_trade_dates"
CALENDAR_MARKET_CODES = {"SSE": "212001", "SZSE": "212100"}
RAW_OHLC_COLUMNS = ("open", "high", "low", "close")
ADJ_OHLC_COLUMNS = ("adj_open", "adj_high", "adj_low", "adj_close")
CANONICAL_COLUMNS = ("date", "asset_id", *RAW_OHLC_COLUMNS, *ADJ_OHLC_COLUMNS)


class IFindDownloadError(RuntimeError):
    """iFind鉴权、取数或返回格式不符合预期。"""

    def __init__(self, message: str, *, quota_exceeded: bool = False, unauthorized: bool = False) -> None:
        super().__init__(message)
        self.quota_exceeded = quota_exceeded
        self.unauthorized = unauthorized


def _post(url: str, *, headers: Mapping[str, str], payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = requests.post(url, headers=dict(headers), json=payload, timeout=60)
    except requests.RequestException as error:
        raise IFindDownloadError("iFind HTTPS连接失败") from error
    if not response.ok:
        # Provider可能回显请求头或令牌；结果和错误只保留状态码。
        raise IFindDownloadError(
            f"iFind数据请求失败，状态码={response.status_code}",
            quota_exceeded=response.status_code == 429,
            unauthorized=response.status_code in {401, 403},
        )
    try:
        result = response.json()
    except ValueError as error:
        raise IFindDownloadError("iFind返回的不是JSON") from error
    if not isinstance(result, dict):
        raise IFindDownloadError("iFind返回结构不是JSON对象")
    try:
        error_code = int(result.get("errorcode", 0))
    except (TypeError, ValueError) as error:
        raise IFindDownloadError("iFind返回错误码无效") from error
    if error_code != 0:
        # errmsg可能回显凭据或请求信息，只把额度分类保留在内部错误类型中。
        detail = str(result.get("errmsg") or "").lower()
        raise IFindDownloadError(
            "iFind数据请求被拒绝",
            quota_exceeded=("额度" in detail or "quota" in detail),
            unauthorized=any(marker in detail for marker in ("token", "auth", "鉴权", "认证", "权限")),
        )
    return result


def get_access_token(refresh_token: str) -> str:
    result = _post(TOKEN_URL, headers={"Content-Type": "application/json", "refresh_token": refresh_token})
    token = result.get("data", {}).get("access_token") if isinstance(result.get("data"), Mapping) else None
    if not isinstance(token, str) or not token:
        raise IFindDownloadError("iFind未返回有效access_token")
    return token


def history_response(access_token: str, *, code: str, indicators: str, start_date: str, end_date: str, cps: int) -> dict[str, Any]:
    payload = {
        "codes": code,
        "indicators": indicators,
        "startdate": start_date,
        "enddate": end_date,
        "functionpara": {"Interval": "D", "CPS": str(cps), "Fill": "Omit"},
    }
    return _post(
        HISTORY_URL,
        headers={"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"},
        payload=payload,
    )


def calendar_response(access_token: str, *, exchange: str, start_date: str, end_date: str) -> dict[str, Any]:
    """调用iFind独立交易日历接口，不携带任何行情字段或复权参数。"""

    market_code = CALENDAR_MARKET_CODES.get(exchange)
    if market_code is None:
        raise IFindDownloadError(f"iFind交易日历暂不支持交易所{exchange}")
    return _post(
        CALENDAR_URL,
        headers={"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"},
        payload={
            "marketcode": market_code,
            "functionpara": {
                "mode": "1",
                "dateType": "0",
                "period": "D",
                "dateFormat": "0",
            },
            "startdate": start_date,
            "enddate": end_date,
        },
    )


def calendar_dates(payload: Mapping[str, Any], *, exchange: str) -> tuple[str, ...]:
    """解析DateQuery返回的日期列，并拒绝交易所回显错配。"""

    expected_code = CALENDAR_MARKET_CODES.get(exchange)
    input_params = payload.get("inputParams", payload.get("inputparams"))
    if isinstance(input_params, Mapping):
        echoed = input_params.get("marketcode", input_params.get("marketCode"))
        if echoed is not None and str(echoed) not in {str(expected_code), exchange}:
            raise IFindDownloadError("iFind交易日历返回的交易所与请求不一致")
    values: list[Any] = []
    tables = payload.get("tables")
    table_items = [tables] if isinstance(tables, Mapping) else tables
    if isinstance(table_items, list):
        for item in table_items:
            if not isinstance(item, Mapping):
                continue
            item_values = _as_list(item.get("time"))
            table = item.get("table", item.get("data"))
            if not item_values and isinstance(table, Mapping):
                for key in ("sequencedate", "sequenceDate", "date", "time"):
                    item_values = _as_list(table.get(key))
                    if item_values:
                        break
            elif not item_values and isinstance(table, (list, tuple)):
                item_values = list(table)
            values.extend(item_values)
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in ("sequencedate", "sequenceDate", "date", "time"):
            values.extend(_as_list(data.get(key)))
    elif isinstance(data, (list, tuple)):
        values.extend(data)
    normalized: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("sequencedate", value.get("date", value.get("time")))
        if value is None:
            continue
        text = str(value).strip()[:10]
        if text:
            normalized.append(text)
    if not normalized:
        raise IFindDownloadError("iFind交易日历未返回有效交易日")
    return tuple(normalized)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _table_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """将THS_HQ的tables格式转换为逐日记录，兼容常见HTTP返回层级。"""
    rows: list[dict[str, Any]] = []
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise IFindDownloadError("iFind历史行情未返回tables")
    for item in tables:
        if not isinstance(item, Mapping):
            continue
        values = item.get("table", item.get("data", {}))
        if not isinstance(values, Mapping):
            continue
        times = _as_list(item.get("time")) or _as_list(values.get("time"))
        code = item.get("thscode") or values.get("thscode")
        if not times or not isinstance(code, str):
            continue
        columns = {str(key).lower(): _as_list(value) for key, value in values.items()}
        for index, timestamp in enumerate(times):
            row = {"date": timestamp, "asset_id": code}
            for key, series in columns.items():
                if key not in {"time", "thscode"} and index < len(series):
                    row[key] = series[index]
            rows.append(row)
    if not rows:
        raise IFindDownloadError("iFind历史行情未解析出有效日线记录")
    return rows


def _numeric(data: pd.DataFrame, column: str, *, required: bool) -> pd.Series:
    if column not in data.columns:
        if required:
            raise IFindDownloadError(f"iFind返回缺少{column}字段")
        return pd.Series(index=data.index, dtype=float)
    return pd.to_numeric(data[column], errors="coerce")


def _canonical_raw(payload: Mapping[str, Any], *, include_volume: bool = False) -> pd.DataFrame:
    raw = pd.DataFrame(_table_rows(payload))
    result = pd.DataFrame({
        "date": pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d"),
        "asset_id": raw["asset_id"].astype(str).str.strip(),
        "open": _numeric(raw, "open", required=True),
        "high": _numeric(raw, "high", required=True),
        "low": _numeric(raw, "low", required=True),
        "close": _numeric(raw, "close", required=True),
    })
    if include_volume:
        result["volume"] = _numeric(raw, "volume", required=True)
    invalid = result[["date", "asset_id", "open", "high", "low", "close"]].isna().any(axis=1)
    invalid |= result["asset_id"].eq("") | (result[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if include_volume:
        invalid |= result["volume"] < 0
    if invalid.any() or result.duplicated(["date", "asset_id"]).any():
        raise IFindDownloadError("iFind原始OHLC含空值、非正值或重复交易日")
    return result.sort_values(["date", "asset_id"]).reset_index(drop=True)


def _adjusted_ohlc_from_cps_2(payload: Mapping[str, Any]) -> pd.DataFrame:
    raw = pd.DataFrame(_table_rows(payload))
    result = pd.DataFrame({"date": pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d"), "asset_id": raw["asset_id"].astype(str).str.strip()})
    for raw_name, adjusted_name in zip(RAW_OHLC_COLUMNS, ADJ_OHLC_COLUMNS, strict=True):
        result[adjusted_name] = _numeric(raw, raw_name, required=True)
    if result.isna().any(axis=None) or (result[list(ADJ_OHLC_COLUMNS)] <= 0).any(axis=None) or result.duplicated(["date", "asset_id"]).any():
        raise IFindDownloadError("iFind前复权OHLC含无效或重复记录")
    return result


def download_history(
    *, access_token: str, code: str, start_date: str, end_date: str,
    asset_type: str, adjustment: str, include_volume: bool = False,
) -> pd.DataFrame:
    raw_indicators = "open,high,low,close,volume" if include_volume else "open,high,low,close"
    raw = _canonical_raw(
        history_response(access_token, code=code, indicators=raw_indicators, start_date=start_date, end_date=end_date, cps=1),
        include_volume=include_volume,
    )
    if asset_type in {"stock", "etf"} and adjustment in {"forward", "both"}:
        adjusted = _adjusted_ohlc_from_cps_2(
            history_response(access_token, code=code, indicators="open,high,low,close", start_date=start_date, end_date=end_date, cps=2)
        )
        result = raw.merge(adjusted, on=["date", "asset_id"], how="inner", validate="one_to_one")
        if len(result) != len(raw):
            raise IFindDownloadError("不复权与前复权历史交易日未能一一对应")
    else:
        result = raw.copy()
        for raw_name, adjusted_name in zip(RAW_OHLC_COLUMNS, ADJ_OHLC_COLUMNS, strict=True):
            result[adjusted_name] = result[raw_name]
    columns = (*CANONICAL_COLUMNS, "volume") if include_volume else CANONICAL_COLUMNS
    return result.loc[:, columns]


class IFindHttpProvider:
    """复用现有HTTP下载器的Service适配，不写CSV、不打印令牌。"""

    name = "ifind_http"
    network = True

    @staticmethod
    def estimate_quota(request: DataRequest) -> int:
        # 只有股票/ETF确实需要前复权字段时才额外请求CPS=2。
        multiplier = sum(
            2 if any(field.startswith("adj_") for field in request.fields)
            and china_market_convention(asset_id, request.adjustment)["effective_adjustment"] in {"forward", "both"} else 1
            for asset_id in request.asset_ids
        )
        return multiplier

    def fetch(self, request: DataRequest, config: DataFetcherConfig) -> pd.DataFrame:
        refresh_token = self._refresh_token(config)
        try:
            access_token = get_access_token(refresh_token)
        except IFindDownloadError as error:
            self._raise_access_error(error)
        try:
            frames = [
                download_history(
                    access_token=access_token,
                    code=asset_id,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    asset_type=str(china_market_convention(asset_id, request.adjustment)["asset_class"]),
                    adjustment=str(china_market_convention(asset_id, request.adjustment)["effective_adjustment"]),
                    include_volume="volume" in request.fields,
                )
                for asset_id in request.asset_ids
            ]
        except IFindDownloadError as error:
            if error.quota_exceeded:
                raise ProviderQuotaExceeded("iFind额度不足") from error
            if error.unauthorized:
                raise ProviderUnauthorized("iFind鉴权失败") from error
            raise ProviderUnavailable("iFind数据请求失败") from error
        return pd.concat(frames, ignore_index=True)

    def fetch_calendar(self, request: CalendarRequest, config: DataFetcherConfig) -> Mapping[str, tuple[str, ...]]:
        """一次鉴权后按交易所获取日历；不请求或解析任何未来价格。"""

        refresh_token = self._refresh_token(config)
        try:
            access_token = get_access_token(refresh_token)
        except IFindDownloadError as error:
            self._raise_access_error(error)
        exchanges = sorted({
            str(china_market_convention(asset_id)["exchange"])
            for asset_id in request.asset_ids
        })
        try:
            return {
                exchange: calendar_dates(
                    calendar_response(
                        access_token,
                        exchange=exchange,
                        start_date=request.start_date,
                        end_date=request.end_date,
                    ),
                    exchange=exchange,
                )
                for exchange in exchanges
            }
        except IFindDownloadError as error:
            if error.quota_exceeded:
                raise ProviderQuotaExceeded("iFind额度不足") from error
            if error.unauthorized:
                raise ProviderUnauthorized("iFind鉴权失败") from error
            raise ProviderUnavailable("iFind交易日历请求失败") from error

    def test_connection(self, config: DataFetcherConfig) -> None:
        """以同一受控凭据边界完成最小鉴权，不请求市场数据。"""

        refresh_token = self._refresh_token(config)
        try:
            get_access_token(refresh_token)
        except IFindDownloadError as error:
            self._raise_access_error(error)

    @staticmethod
    def _raise_access_error(error: IFindDownloadError) -> None:
        if error.quota_exceeded:
            raise ProviderQuotaExceeded("iFind额度不足") from error
        if error.unauthorized:
            raise ProviderUnauthorized("iFind鉴权失败") from error
        raise ProviderUnavailable("iFind连接暂不可用") from error

    @staticmethod
    def _refresh_token(config: DataFetcherConfig) -> str:
        try:
            secret_value = resolve_secret(
                config.ifind_secret_ref,
                "IFIND_REFRESH_TOKEN",
                host_secret_port=config.ifind_secret_port,
            )
        except SecretPortUnavailable as error:
            raise ProviderUnavailable("iFind凭据服务暂不可用") from error
        if not secret_value:
            raise ProviderUnauthorized("iFind Refresh Token未配置")
        if config.ifind_secret_ref is not None:
            try:
                record = json.loads(secret_value)
            except (TypeError, ValueError):
                return secret_value
            if isinstance(record, Mapping):
                if "access_token" in record:
                    raise ProviderUnauthorized("iFind仅接受Refresh Token配置")
                refresh_token = record.get("refresh_token")
                if isinstance(refresh_token, str) and refresh_token.strip():
                    return refresh_token.strip()
                raise ProviderUnauthorized("iFind Refresh Token未配置")
            raise ProviderUnauthorized("iFind凭据格式无效")
        return secret_value
