"""中国市场资产标识、交易所与价格口径的唯一判定处。"""

from __future__ import annotations

import re
from typing import Any, Mapping


_EXCHANGES = {
    "SH": "SSE", "SZ": "SZSE", "CFE": "CFFEX", "SHF": "SHFE", "DCE": "DCE",
    "CZC": "CZCE", "INE": "INE", "HK": "HKEX",
}

_SUPPORTED_SUFFIXES = {"SH", "SZ"}
_INDEX_PREFIXES = {"SH": ("000",), "SZ": ("399",)}
_ETF_PREFIXES = {
    "SH": (
        "510", "511", "512", "513", "515", "516", "517", "518",
        "520", "560", "561", "562", "563", "588", "589",
    ),
    "SZ": ("159",),
}
_A_SHARE_PREFIXES = {
    "SH": ("600", "601", "603", "605", "688"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
}
_HV_WINDOWS = (10, 20, 60, 122)


class UnsupportedChinaAsset(ValueError):
    """当前DataFetcher尚未验证的中国市场资产类别。"""


def _supported_asset_class(code: str, suffix: str) -> str:
    if code.startswith(_INDEX_PREFIXES[suffix]):
        return "index"
    if code.startswith(_ETF_PREFIXES[suffix]):
        return "etf"
    if code.startswith(_A_SHARE_PREFIXES[suffix]):
        return "stock"
    raise UnsupportedChinaAsset(f"当前DataFetcher仅支持中国A股、ETF和指数日线：{code}.{suffix}")


def hv_input_requirements(convention: Mapping[str, Any]) -> Mapping[str, Any]:
    """声明交给Pricer计算HV前必须满足的数据口径，不在此重复计算。"""

    return {
        "windows_trading_days": list(_HV_WINDOWS),
        "return_price_field": convention["historical_return_field"],
        "return_type": "log_return",
        "minimum_close_observations": {str(window): window + 1 for window in _HV_WINDOWS},
        "annualization_trading_days": 244,
        "required_frequency": "1d",
        "required_quality": "observed_rows=valid; calendar_completeness=complete for verified HV input",
        "calculator_owner": "pricer",
    }


def china_market_convention(asset_id: str, adjustment: str = "auto") -> Mapping[str, Any]:
    """返回资产的交易所、币种、时区与收盘价口径。"""

    code, _, suffix = asset_id.strip().upper().partition(".")
    if suffix not in _SUPPORTED_SUFFIXES or not re.fullmatch(r"\d{6}", code):
        raise UnsupportedChinaAsset(f"当前DataFetcher仅支持中国A股、ETF和指数日线：{asset_id}")
    exchange = _EXCHANGES[suffix]
    asset_class = _supported_asset_class(code, suffix)
    requested_adjustment = adjustment.lower()
    effective_adjustment = (
        "none" if asset_class == "index" else
        "forward" if requested_adjustment == "auto" else
        requested_adjustment
    )
    unadjusted = effective_adjustment == "none"
    adjustment_label = (
        "unadjusted_adj_fields_alias_raw" if unadjusted else
        "forward_adjusted_for_adj_fields"
    )
    return {
        "exchange": exchange,
        "asset_class": asset_class,
        "currency": "CNY",
        "timezone": "Asia/Shanghai",
        "close_convention": "unadjusted_index_close" if asset_class == "index" else "unadjusted_close",
        "historical_return_field": "close" if asset_class == "index" or unadjusted else "adj_close",
        "raw_close_retained": True,
        "corporate_action_adjustment": (
            "not_applicable" if asset_class == "index" else
            adjustment_label
        ),
        "requested_adjustment": requested_adjustment,
        "effective_adjustment": effective_adjustment,
    }


def market_conventions(asset_ids: tuple[str, ...], adjustment: str = "auto") -> Mapping[str, Mapping[str, Any]]:
    return {asset_id: china_market_convention(asset_id, adjustment) for asset_id in asset_ids}
