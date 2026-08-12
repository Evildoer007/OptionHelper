"""Pricer的输入输出边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from typing import Any, Mapping

from runtime.contracts.contract_api import ResolvedContract
from runtime.protocol.models import DataAssetRef

from .config import PricingConfig
from .observed_state import ObservedContractState
from .engines.pricing_core.optionhelper_core import PricingResult


@dataclass(frozen=True)
class HistoricalData:
    """已加载的受控市场历史，不接受任意文件路径。"""

    source_ref: str
    rows: tuple[Mapping[str, Any], ...]
    content_hash: str = ""
    schema_id: str = "market-history-v1"
    asset_ids: tuple[str, ...] = ()
    normalized_fields: tuple[str, ...] = ("date", "asset_id", "close", "adj_close")
    coverage: Mapping[str, Any] | None = None
    storage_mode: str = "host-injected"

    def __post_init__(self) -> None:
        if not self.source_ref.strip():
            raise ValueError("HistoricalData.source_ref不能为空")
        if not self.rows:
            raise ValueError("HistoricalData.rows不能为空")
        required = {"date", "asset_id", "close", "adj_close"}
        fields = set(self.normalized_fields)
        if not required.issubset(fields):
            raise ValueError("HistoricalData.normalized_fields必须含date、asset_id、close、adj_close")
        if self.schema_id != "market-history-v1":
            raise ValueError("HistoricalData.schema_id必须为market-history-v1")
        row_assets = tuple(sorted({str(row.get("asset_id", "")).strip() for row in self.rows}))
        if not row_assets or "" in row_assets:
            raise ValueError("HistoricalData.rows必须逐行提供asset_id")
        if self.asset_ids and set(self.asset_ids) != set(row_assets):
            raise ValueError("HistoricalData.asset_ids必须与历史行资产完全一致")
        if not self.asset_ids:
            object.__setattr__(self, "asset_ids", row_assets)
        content_hash = self.content_hash or _rows_hash(self.rows)
        if not _is_sha256(content_hash):
            raise ValueError("HistoricalData.content_hash必须为SHA-256")
        object.__setattr__(self, "content_hash", content_hash)
        coverage = dict(self.coverage or {})
        _validate_coverage(coverage, allow_empty=True)
        object.__setattr__(self, "coverage", coverage)
        if self.storage_mode not in {"host-injected", "local-development"}:
            raise ValueError("HistoricalData.storage_mode只能为host-injected或local-development")

    @property
    def trading_calendar(self) -> Mapping[str, Any] | None:
        """资产声明的日历，不从weekday或历史日期猜测。"""
        coverage = dict(self.coverage or {})
        sessions = coverage.get("sessions")
        calendar_id = coverage.get("calendar_id")
        calendar_version = coverage.get("calendar_version")
        if not sessions or not calendar_id or not calendar_version:
            return None
        return {
            "calendar_id": str(calendar_id),
            "calendar_version": str(calendar_version),
            "sessions": tuple(str(value) for value in sessions),
            "verified_cn_sessions": _is_verified_cn_calendar(coverage),
            "source": self.storage_mode,
        }


@dataclass(frozen=True)
class TradingCalendarData:
    """从独立``trading-calendar``资产加载的中国交易日序列。"""

    source_ref: str
    asset_ids: tuple[str, ...]
    sessions: tuple[str, ...]
    sessions_by_exchange: Mapping[str, tuple[str, ...]]
    asset_exchange: Mapping[str, str]
    requested_start_date: str
    requested_end_date: str
    content_hash: str
    schema_id: str = "trading-calendar"
    storage_mode: str = "host-injected"

    def __post_init__(self) -> None:
        if not self.source_ref.strip() or self.schema_id != "trading-calendar":
            raise ValueError("TradingCalendarData必须来自trading-calendar资产")
        if not _is_sha256(self.content_hash):
            raise ValueError("TradingCalendarData.content_hash必须为SHA-256")
        parsed = tuple(_iso_date(value, "TradingCalendarData.sessions").isoformat() for value in self.sessions)
        if not parsed or parsed != tuple(sorted(parsed)) or len(set(parsed)) != len(parsed):
            raise ValueError("TradingCalendarData.sessions必须严格递增且不重复")
        start = _iso_date(self.requested_start_date, "requested_start_date")
        end = _iso_date(self.requested_end_date, "requested_end_date")
        if start > end or any(not start <= _iso_date(value, "session") <= end for value in parsed):
            raise ValueError("TradingCalendarData.sessions必须位于请求区间")
        exchanges = set(self.sessions_by_exchange)
        if not exchanges or set(self.asset_exchange) != set(self.asset_ids):
            raise ValueError("TradingCalendarData必须逐标的声明交易所")
        if any(exchange not in exchanges for exchange in self.asset_exchange.values()):
            raise ValueError("TradingCalendarData标的交易所映射无效")
        for exchange, values in self.sessions_by_exchange.items():
            normalized = tuple(_iso_date(value, f"{exchange}.sessions").isoformat() for value in values)
            if not normalized or normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
                raise ValueError(f"TradingCalendarData.{exchange}交易日必须严格递增且不重复")
        intersection = set.intersection(*(set(values) for values in self.sessions_by_exchange.values()))
        if tuple(sorted(intersection)) != parsed:
            raise ValueError("TradingCalendarData.sessions必须为多交易所交易日交集")
        if self.storage_mode not in {"host-injected", "local-development"}:
            raise ValueError("TradingCalendarData.storage_mode无效")


@dataclass(frozen=True)
class PricingInput:
    contract: ResolvedContract
    pricing_config: PricingConfig
    historical_data: HistoricalData | None = None
    market_data_refs: tuple[DataAssetRef, ...] = ()
    trading_calendar_data: TradingCalendarData | None = None
    trading_calendar_ref: DataAssetRef | None = None
    observed_contract_state: ObservedContractState | Mapping[str, Any] | None = None


def validate_market_data_asset(
    ref: DataAssetRef,
    historical: HistoricalData,
    underlyings: tuple[str, ...],
) -> Mapping[str, Any]:
    """验证Host资产身份、内容、字段、覆盖范围和合同标的映射。"""
    if not isinstance(ref, DataAssetRef):
        raise ValueError("market_data_refs只能传入受控DataAssetRef")
    if ref.media_type != "text/csv" or ref.schema_id != "market-history-v1":
        raise ValueError("DataAssetRef必须是market-history-v1 text/csv")
    if not _is_sha256(ref.content_hash) or ref.content_hash != historical.content_hash:
        raise ValueError("DataAssetRef.content_hash必须与HistoricalData内容哈希一致")
    if ref.storage_ref != historical.source_ref:
        raise ValueError("DataAssetRef.storage_ref必须与HistoricalData.source_ref一致")
    if set(ref.asset_ids) != set(underlyings) or set(historical.asset_ids) != set(underlyings):
        raise ValueError("DataAssetRef、HistoricalData必须逐一覆盖合同标的")
    required = {"date", "asset_id", "close", "adj_close"}
    if not required.issubset(set(ref.normalized_fields)):
        raise ValueError("DataAssetRef.normalized_fields必须含date、asset_id、close、adj_close")
    if tuple(ref.normalized_fields) != tuple(historical.normalized_fields):
        raise ValueError("DataAssetRef与HistoricalData.normalized_fields不一致")
    if int(ref.row_count) != len(historical.rows):
        raise ValueError("DataAssetRef.row_count必须与HistoricalData行数一致")
    coverage = dict(ref.coverage)
    _validate_coverage(coverage, allow_empty=False)
    _validate_by_asset_coverage(
        coverage,
        underlyings,
        historical.rows,
        required=historical.storage_mode == "host-injected",
    )
    if coverage != dict(historical.coverage or {}):
        raise ValueError("DataAssetRef与HistoricalData.coverage不一致")
    _validate_history_sessions(historical.rows, coverage["sessions"])
    return {
        "calendar_id": str(coverage["calendar_id"]),
        "calendar_version": str(coverage["calendar_version"]),
        "sessions": tuple(str(value) for value in coverage["sessions"]),
        "verified_cn_sessions": _is_verified_cn_calendar(coverage),
        "source": "host-injected",
        "content_hash": ref.content_hash,
        "schema_id": ref.schema_id,
        "coverage": coverage,
        "data_asset_id": ref.data_asset_id,
    }


def validate_trading_calendar_asset(
    ref: DataAssetRef,
    calendar: TradingCalendarData,
    underlyings: tuple[str, ...],
) -> Mapping[str, Any]:
    """验证日历引用、内容和交易所交集，不执行任何OHLC字段审计。"""

    if not isinstance(ref, DataAssetRef) or ref.schema_id != "trading-calendar" or ref.media_type != "application/json":
        raise ValueError("trading_calendar_ref必须是trading-calendar application/json")
    if ref.content_hash != calendar.content_hash or ref.storage_ref != calendar.source_ref:
        raise ValueError("trading_calendar_ref与TradingCalendarData内容不一致")
    if set(ref.asset_ids) != set(underlyings) or set(calendar.asset_ids) != set(underlyings):
        raise ValueError("交易日历必须逐一覆盖合同标的")
    if tuple(ref.normalized_fields) != ("session",) or int(ref.row_count) != len(calendar.sessions):
        raise ValueError("交易日历字段或交易日数量与引用不一致")
    coverage = dict(ref.coverage)
    _validate_coverage(coverage, allow_empty=False)
    if tuple(str(value) for value in coverage["sessions"]) != tuple(calendar.sessions):
        raise ValueError("交易日历coverage.sessions与内容不一致")
    if not _is_verified_cn_calendar(coverage):
        raise ValueError("正式路径定价只接受已验证中国交易所日历")
    if dict(ref.price_convention).get("contains_market_prices") is not False:
        raise ValueError("交易日历资产不得包含未来价格")
    return {
        "calendar_id": str(coverage["calendar_id"]),
        "calendar_version": str(coverage["calendar_version"]),
        "sessions": tuple(calendar.sessions),
        "sessions_by_exchange": {key: tuple(value) for key, value in calendar.sessions_by_exchange.items()},
        "asset_exchange": dict(calendar.asset_exchange),
        "verified_cn_sessions": True,
        "source": calendar.storage_mode,
        "content_hash": ref.content_hash,
        "schema_id": ref.schema_id,
        "data_asset_id": ref.data_asset_id,
        "requested_start_date": calendar.requested_start_date,
        "requested_end_date": calendar.requested_end_date,
    }


def _rows_hash(rows: tuple[Mapping[str, Any], ...]) -> str:
    payload = json.dumps([dict(row) for row in rows], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_coverage(coverage: Mapping[str, Any], *, allow_empty: bool) -> None:
    if not coverage:
        if allow_empty:
            return
        raise ValueError("DataAssetRef.coverage必须声明日期覆盖和交易日历")
    required = {"sessions", "calendar_id", "calendar_version"}
    missing = required - set(coverage)
    if missing:
        raise ValueError("DataAssetRef.coverage缺少：" + "、".join(sorted(missing)))
    start_value = coverage.get("start", coverage.get("start_date"))
    end_value = coverage.get("end", coverage.get("end_date"))
    if start_value is None or end_value is None:
        raise ValueError("DataAssetRef.coverage必须含start/end")
    if "start" in coverage and "start_date" in coverage and coverage["start"] != coverage["start_date"]:
        raise ValueError("coverage.start与start_date别名冲突")
    if "end" in coverage and "end_date" in coverage and coverage["end"] != coverage["end_date"]:
        raise ValueError("coverage.end与end_date别名冲突")
    start = _iso_date(start_value, "coverage.start")
    end = _iso_date(end_value, "coverage.end")
    if start > end:
        raise ValueError("DataAssetRef.coverage.start不得晚于end")
    sessions = coverage["sessions"]
    if isinstance(sessions, (str, bytes)) or not isinstance(sessions, (tuple, list)):
        raise ValueError("DataAssetRef.coverage.sessions必须为显式交易日列表")
    parsed = tuple(_iso_date(value, "coverage.sessions") for value in sessions)
    if not parsed or tuple(sorted(parsed)) != parsed or len(set(parsed)) != len(parsed):
        raise ValueError("DataAssetRef.coverage.sessions必须非空、严格递增且不重复")
    if parsed[0] < start or parsed[-1] > end:
        raise ValueError("DataAssetRef.coverage.sessions必须位于声明日期范围")
    for key in ("calendar_id", "calendar_version"):
        if not isinstance(coverage[key], str) or not coverage[key].strip():
            raise ValueError(f"DataAssetRef.coverage.{key}不能为空")


def _validate_history_sessions(rows: tuple[Mapping[str, Any], ...], sessions: object) -> None:
    session_set = {str(value) for value in sessions}
    for row in rows:
        day = _iso_date(row.get("date"), "HistoricalData.rows.date").isoformat()
        if day not in session_set:
            raise ValueError("HistoricalData日期必须包含在DataAssetRef声明的交易sessions中")


def _validate_by_asset_coverage(
    coverage: Mapping[str, Any],
    underlyings: tuple[str, ...],
    rows: tuple[Mapping[str, Any], ...],
    *,
    required: bool,
) -> None:
    by_asset = coverage.get("by_asset")
    if by_asset is None:
        if required:
            raise ValueError("正式DataAssetRef.coverage必须含by_asset逐标的覆盖")
        return
    if not isinstance(by_asset, Mapping) or set(by_asset) != set(underlyings):
        raise ValueError("DataAssetRef.coverage.by_asset必须逐一且只覆盖合同标的")
    top_start = _iso_date(coverage.get("start", coverage.get("start_date")), "coverage.start")
    top_end = _iso_date(coverage.get("end", coverage.get("end_date")), "coverage.end")
    for asset in underlyings:
        item = by_asset[asset]
        if not isinstance(item, Mapping):
            raise ValueError(f"coverage.by_asset.{asset}必须为对象")
        start = _iso_date(item.get("start", item.get("start_date")), f"coverage.by_asset.{asset}.start")
        end = _iso_date(item.get("end", item.get("end_date")), f"coverage.by_asset.{asset}.end")
        if start > end or start < top_start or end > top_end:
            raise ValueError(f"coverage.by_asset.{asset}日期范围无效")
        row_dates = sorted(_iso_date(row.get("date"), "HistoricalData.rows.date") for row in rows if str(row.get("asset_id")) == asset)
        if not row_dates or row_dates[0] != start or row_dates[-1] != end:
            raise ValueError(f"coverage.by_asset.{asset}必须与该标的实际历史行首末日期一致")


def _iso_date(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label}必须为YYYY-MM-DD") from error


def _is_verified_cn_calendar(coverage: Mapping[str, Any]) -> bool:
    """正式报价只认显式版本化的中国交易日历，不根据weekday猜测。"""
    return str(coverage.get("calendar_id", "")).upper().startswith("CN-") and bool(str(coverage.get("calendar_version", "")).strip())


__all__ = (
    "HistoricalData", "PricingInput", "PricingResult", "TradingCalendarData",
    "validate_market_data_asset", "validate_trading_calendar_asset",
)
