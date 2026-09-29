"""iFinD daily research panels, separate from settlement-price requests.

Only field bindings with an inspected source are enabled. Contract metadata
and option-IV bindings must be verified before enabling those panels;
unknown indicator names must never be guessed at runtime.
"""
from __future__ import annotations
from threading import Lock
from datetime import date, timedelta
import re
from typing import Any, Callable, Mapping

from .ifind_http import IFindHttpProvider, _post, get_access_token
from ..dashboard import panel, valuation_panel, etf_panel, number

BASIC_URL = "https://quantapi.51ifind.com/api/v1/basic_data_service"
SERIES_URL = "https://quantapi.51ifind.com/api/v1/date_sequence"
# Bindings inspected in ResearchHelper/core/selection.py and universe.py.
# Transport shape follows iFinD's official HTTP examples. Account acceptance
# is a separate test; neither a source mapping nor an HTTP200 proves coverage.
VALUATION_FIELDS = {"pe": "ths_pe_index", "pb": "ths_pb_index", "ps": "ths_ps_index"}
# Official DateSerial example: 100,100 and historical constituents.
# HTTPS includes the initial date slot; verified on four indices, 2026-09-18.
INDEX_PARAMS = {field: ["", "100", "100"] for field in VALUATION_FIELDS}
# Official Super Command, inspected 2026-09-18: date_sequence, no parameters.
STOCK_FIELDS = {"pe": "ths_pe_ttm_stock", "pb": "ths_pb_mrq_stock",
                "dividend_yield": "ths_dividend_yield_ttm_ex_sd_stock",
                "total_shares": "ths_total_shares_stock", "market_value": "ths_market_value_stock",
                "turnover_ratio": "ths_turnover_ratio_stock"}
# The initial empty argument is the date slot populated by date_sequence.
STOCK_PARAMS = {field: (["", "100"] if field == "pe" else [""]) for field in STOCK_FIELDS}
ETF_FIELDS = {"nav": "ths_unit_nvnf_fund", "shares": "ths_fund_shares_fund"}
TRACKING_FIELD = "ths_tracking_index_code_fund"
INDEX_NAMES = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000", "000016.SH": "上证50"}
FUTURES_FAMILIES = {"000300.SH": "IF", "000905.SH": "IC", "000852.SH": "IM", "000016.SH": "IH"}


def normalize_index(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if re.fullmatch(r"(?:\d{6}|H\d{5})\.(?:SH|SZ|CSI)", text):
        return text
    if re.fullmatch(r"\d{6}", text):
        suffix = "SH" if text.startswith("000") else "SZ" if text.startswith(("399", "980")) else "CSI"
        return f"{text}.{suffix}"
    if re.fullmatch(r"H\d{5}", text):
        return f"{text}.CSI"
    return None


def table_rows(payload: Mapping[str, Any], code: str, fields: Mapping[str, str]) -> list[dict[str, Any]]:
    output = []
    for table in payload.get("tables", []):
        if str(table.get("thscode", "")).upper() != code.upper():
            continue
        values = table.get("table", {})
        for index, day in enumerate(table.get("time", [])):
            record = {"date": str(day)[:10]}
            for field, indicator in fields.items():
                series = values.get(indicator, [])
                record[field] = series[index] if isinstance(series, list) and index < len(series) else None
            output.append(record)
    return output


class IFindDashboardProvider:
    def __init__(self, config, *, cancelled: Callable[[], bool] | None = None):
        self.config = config
        self.cancelled = cancelled or (lambda: False)
        self._token = None
        self._lock = Lock()

    def check_cancelled(self):
        if self.cancelled():
            raise InterruptedError("Dashboard请求已停止。")

    def headers(self):
        self.check_cancelled()
        with self._lock:
            if self._token is None:
                refresh = IFindHttpProvider._refresh_token(self.config)
                self._token = get_access_token(refresh, timeout_seconds=min(10, self.config.timeout_seconds))
        self.check_cancelled()
        return {"Content-Type": "application/json", "access_token": self._token, "ifindlang": "cn"}

    def request(self, endpoint, payload):
        result = _post(endpoint, headers=self.headers(), payload=payload, timeout_seconds=min(10, self.config.timeout_seconds))
        self.check_cancelled()
        return result

    def tracking_index(self, code: str) -> str | None:
        result = self.request(BASIC_URL, {"codes": code, "indipara": [{"indicator": TRACKING_FIELD, "indiparams": []}]})
        for table in result.get("tables", []):
            if str(table.get("thscode", "")).upper() == code:
                values = table.get("table", {}).get(TRACKING_FIELD, [])
                return normalize_index(values[0]) if values else None
        return None

    def valuation(self, code: str, asset_class: str, start: str, end: str):
        # A current ETF tracking relationship cannot be applied to a historical
        # valuation date without effective-dated reference evidence.
        if asset_class == "etf":
            return panel("unavailable", "ETF历史估值需要按日期核实跟踪指数关系，尚未接入该资料。", code="mapping_unverified")
        fields = STOCK_FIELDS if asset_class == "stock" else VALUATION_FIELDS
        observations = self.daily_series(code, start, end, fields,
                                         parameters=STOCK_PARAMS if asset_class == "stock" else INDEX_PARAMS,
                                         function_parameters=None if asset_class == "stock" else {"block":"history"})
        if asset_class == "stock":
            for row in observations:
                for field in ("dividend_yield", "turnover_ratio"):
                    value = number(row.get(field))
                    row[field] = value / 100 if value is not None else None
        content = valuation_panel(observations, subject_id=code, as_of=end, asset_class=asset_class)
        labels = {"pe": "PE（TTM）", "pb": "PB（MRQ）", "dividend_yield": "股息率（TTM）", "total_shares": "总股本（股）", "market_value": "总市值（元）", "turnover_ratio": "换手率"} if asset_class == "stock" else {"pe": "PE", "pb": "PB", "ps": "PS"}
        missing = ["financial_statements", "corporate_events"] if asset_class == "stock" else ["dividend_yield", "constituents", "industry_weights"]
        return {**content, "source": "iFinD", "fields": dict(fields), "labels": labels, "missing": missing}

    def daily_series(self, code: str, start: str, end: str, fields: Mapping[str, str], *, parameters=None, function_parameters=None):
        observations = []
        cursor, final = date.fromisoformat(start), date.fromisoformat(end)
        # Official FAQ: some index date-sequence queries allow at most a year.
        while cursor <= final:
            through = min(cursor + timedelta(days=364), final)
            result = self.request(SERIES_URL, {"codes": code, "startdate": cursor.isoformat(), "enddate": through.isoformat(),
                "functionpara": {"Fill": "Blank", **(function_parameters or {})},
                "indipara": [{"indicator": indicator, "indiparams": (parameters or {}).get(field, [""])} for field, indicator in fields.items()]})
            observations.extend(table_rows(result, code, fields))
            cursor = through + timedelta(days=1)
        return observations

    def etf(self, code: str, start: str, end: str, prices):
        observations = self.daily_series(code, start, end, ETF_FIELDS)
        content = etf_panel(observations, prices, as_of=end)
        return {**content, "source": "iFinD", "fields": dict(ETF_FIELDS),
                "missing": ["net_assets", "tracking_index_history"],
                "fill_policy": "Blank"}

    def section(self, name: str, code: str, asset_class: str, start: str, end: str, *, prices=()):
        self.check_cancelled()
        if name == "valuation":
            return self.valuation(code, asset_class, start, end)
        if name == "etf" and asset_class != "etf":
            return panel("not_applicable", "当前标的不是ETF，不适用基金净值与份额指标。")
        if name == "etf":
            return self.etf(code, start, end, prices)
        if asset_class == "stock" and name in {"futures", "options"}:
            return panel("not_applicable", "本版A股个股研究不提供直接对应的上市期货、期权面板。")
        if name == "futures" and asset_class == "index" and code not in FUTURES_FAMILIES:
            return panel("not_applicable", "当前未登记与该指数直接对应的股指期货。")
        messages = {
            "futures": "期货合约及到期日数据尚未核实，当前不生成升贴水曲线。",
            "options": "期权合约关联和IV字段尚未核实，当前不生成隐含波动率曲线。",
        }
        return panel("unavailable", messages[name], code="mapping_unverified")
