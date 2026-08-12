"""Cross-platform offline and iFinD HTTP market-data providers."""

from __future__ import annotations

from datetime import date, datetime
import os
from typing import Any, Mapping, Protocol

import requests

from .models import DailyBar


TOKEN_URL = "https://quantapi.51ifind.com/api/v1/get_access_token"
HISTORY_URL = "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"


class MarketDataError(RuntimeError):
    pass


class MarketDataProvider(Protocol):
    provider_name: str

    def history_quotes(
        self,
        code: str,
        start_date: date,
        end_date: date,
        *,
        asset_type: str,
    ) -> tuple[DailyBar, ...]: ...


class OfflineProvider:
    provider_name = "OFFLINE"

    def __init__(self, bars_by_code: Mapping[str, tuple[DailyBar, ...] | list[DailyBar]]):
        normalized: dict[str, tuple[DailyBar, ...]] = {}
        for code, bars in bars_by_code.items():
            if not isinstance(code, str) or not code.strip():
                raise ValueError("OfflineProvider代码必须为非空字符串")
            ordered = tuple(sorted(tuple(bars), key=lambda item: item.trading_date))
            if any(item.code != code for item in ordered):
                raise ValueError(f"OfflineProvider的{code}含其他证券代码")
            if len({item.trading_date for item in ordered}) != len(ordered):
                raise ValueError(f"OfflineProvider的{code}含重复交易日")
            normalized[code] = ordered
        self._bars_by_code = normalized

    def history_quotes(
        self,
        code: str,
        start_date: date,
        end_date: date,
        *,
        asset_type: str,
    ) -> tuple[DailyBar, ...]:
        if start_date > end_date:
            raise ValueError("start_date不得晚于end_date")
        bars = self._bars_by_code.get(code)
        if bars is None:
            raise MarketDataError(f"离线数据中不存在证券{code}")
        selected = tuple(
            bar for bar in bars if start_date <= bar.trading_date <= end_date
        )
        if not selected:
            raise MarketDataError(f"离线数据在请求日期内没有{code}行情")
        return selected


class IFindHTTPProvider:
    provider_name = "IFIND_HTTP"

    def __init__(self, *, access_token: str, session: Any = None, timeout_seconds: float = 60.0):
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("access_token必须为非空字符串")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds必须为正数")
        self._access_token = access_token.strip()
        self._session = requests if session is None else session
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_environment(cls, *, session: Any = None, timeout_seconds: float = 60.0):
        transport = requests if session is None else session
        refresh_token = os.environ.get("IFIND_REFRESH_TOKEN", "").strip()
        if not refresh_token:
            raise ValueError("请配置IFIND_REFRESH_TOKEN")
        payload = cls._post_json(
            transport,
            TOKEN_URL,
            headers={"Content-Type": "application/json", "refresh_token": refresh_token},
            body=None,
            timeout_seconds=timeout_seconds,
        )
        data = payload.get("data")
        access_token = data.get("access_token", "") if isinstance(data, Mapping) else ""
        if not isinstance(access_token, str) or not access_token.strip():
            raise MarketDataError("iFinD未返回有效access_token")
        return cls(
            access_token=access_token,
            session=transport,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _post_json(
        session: Any,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            response = session.post(
                url,
                headers=dict(headers),
                json=body,
                timeout=timeout_seconds,
            )
        except requests.RequestException as error:
            raise MarketDataError(f"iFinD HTTPS连接失败：{error}") from error
        if not getattr(response, "ok", False):
            status = getattr(response, "status_code", "unknown")
            raise MarketDataError(f"iFinD HTTP状态异常：{status}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise MarketDataError("iFinD返回的不是JSON对象") from error
        if not isinstance(payload, dict):
            raise MarketDataError("iFinD返回结构不是JSON对象")
        try:
            error_code = int(payload.get("errorcode", 0))
        except (TypeError, ValueError) as error:
            raise MarketDataError("iFinD errorcode格式无效") from error
        if error_code != 0:
            message = payload.get("errmsg") or f"errorcode={error_code}"
            raise MarketDataError(f"iFinD接口错误：{message}")
        return payload

    @staticmethod
    def _series(value: Any, label: str) -> list[Any]:
        if not isinstance(value, (list, tuple)):
            raise MarketDataError(f"iFinD历史行情字段{label}不是序列")
        return list(value)

    @classmethod
    def _parse_ohlc(cls, payload: Mapping[str, Any], requested_code: str):
        tables = payload.get("tables")
        if not isinstance(tables, list) or not tables:
            raise MarketDataError("iFinD历史行情未返回tables")
        rows: list[tuple[date, str, float, float, float, float]] = []
        for item in tables:
            if not isinstance(item, Mapping):
                continue
            table = item.get("table", item.get("data"))
            if not isinstance(table, Mapping):
                continue
            code = item.get("thscode") or table.get("thscode")
            times = item.get("time") or table.get("time")
            if not isinstance(code, str) or code != requested_code:
                continue
            times = cls._series(times, "time")
            series = {
                name: cls._series(table.get(name), name)
                for name in ("open", "high", "low", "close")
            }
            if any(len(values) != len(times) for values in series.values()):
                raise MarketDataError("iFinD历史行情OHLC长度不一致")
            for index, timestamp in enumerate(times):
                try:
                    trading_date = datetime.fromisoformat(str(timestamp)[:10]).date()
                    values = tuple(float(series[name][index]) for name in ("open", "high", "low", "close"))
                except (TypeError, ValueError) as error:
                    raise MarketDataError("iFinD历史行情含无效日期或OHLC") from error
                rows.append((trading_date, code, *values))
        rows.sort(key=lambda item: item[0])
        if not rows:
            raise MarketDataError("iFinD历史行情未解析出有效记录")
        if len({row[0] for row in rows}) != len(rows):
            raise MarketDataError("iFinD历史行情含重复交易日")
        return rows

    def _history_payload(self, code: str, start_date: date, end_date: date, cps: int):
        body = {
            "codes": code,
            "indicators": "open,high,low,close",
            "startdate": start_date.isoformat(),
            "enddate": end_date.isoformat(),
            "functionpara": {"Interval": "D", "CPS": str(cps), "Fill": "Omit"},
        }
        return self._post_json(
            self._session,
            HISTORY_URL,
            headers={
                "Content-Type": "application/json",
                "access_token": self._access_token,
                "ifindlang": "cn",
            },
            body=body,
            timeout_seconds=self._timeout_seconds,
        )

    def history_quotes(
        self,
        code: str,
        start_date: date,
        end_date: date,
        *,
        asset_type: str,
    ) -> tuple[DailyBar, ...]:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code必须为非空字符串")
        if start_date > end_date:
            raise ValueError("start_date不得晚于end_date")
        normalized_type = asset_type.upper()
        if normalized_type not in {"INDEX", "STOCK", "ETF"}:
            raise ValueError("asset_type必须为INDEX、STOCK或ETF")
        raw = self._parse_ohlc(
            self._history_payload(code, start_date, end_date, 1), code
        )
        adjusted = (
            self._parse_ohlc(
                self._history_payload(code, start_date, end_date, 2), code
            )
            if normalized_type in {"STOCK", "ETF"}
            else raw
        )
        if [row[0] for row in raw] != [row[0] for row in adjusted]:
            raise MarketDataError("iFinD不复权与前复权交易日不能一一对应")
        return tuple(
            DailyBar(
                trading_date=raw_row[0],
                code=code,
                open=raw_row[2],
                high=raw_row[3],
                low=raw_row[4],
                close=raw_row[5],
                adjusted_open=adjusted_row[2],
                adjusted_high=adjusted_row[3],
                adjusted_low=adjusted_row[4],
                adjusted_close=adjusted_row[5],
            )
            for raw_row, adjusted_row in zip(raw, adjusted, strict=True)
        )
