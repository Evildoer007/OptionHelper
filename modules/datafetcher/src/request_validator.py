"""DataRequest的纯本地校验和稳定指纹。"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .config import DataFetcherConfig
from .market_conventions import UnsupportedChinaAsset, china_market_convention
from .models import CallerContext, DataRequest


class RequestValidationError(ValueError):
    code = "validation_error"


_FIELD_ALIASES = {"adjusted_close": "adj_close", "adjusted_open": "adj_open", "adjusted_high": "adj_high", "adjusted_low": "adj_low"}
_VALID_FREQUENCIES = {"1d", "daily", "d"}
_VALID_ADJUSTMENTS = {"auto", "none", "forward", "raw", "both"}
_VALID_CACHE_POLICIES = {"reuse", "extend_only", "force_refresh"}


def _parse_date(value: str, name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as error:
        raise RequestValidationError(f"{name}必须为YYYY-MM-DD") from error


def _local_csv_root(config: DataFetcherConfig) -> Path:
    return Path(config.local_csv_root or config.data_root or Path.cwd()).resolve()


def controlled_local_csv_path(value: DataRequest, config: DataFetcherConfig) -> Path | None:
    """解析显式本地输入，并拒绝受控DataStore外的文件。"""

    if not value.local_csv:
        return None
    if len(value.asset_ids) != 1:
        raise RequestValidationError("local_csv单次只支持一个asset_id")
    root = _local_csv_root(config)
    candidate = Path(value.local_csv)
    source = (candidate if candidate.is_absolute() else root / candidate).expanduser().resolve()
    if source != root and root not in source.parents:
        raise RequestValidationError("local_csv必须位于受控DataStore目录")
    if not source.is_file():
        raise RequestValidationError("受控local_csv不存在或不可读取")
    return source


def _local_source_fingerprint(value: DataRequest, config: DataFetcherConfig) -> str | None:
    source = controlled_local_csv_path(value, config)
    if source is None:
        return None
    relative = source.relative_to(_local_csv_root(config)).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8") + b"\0")
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request_local_source_fingerprint(value: DataRequest) -> str | None:
    """返回不会泄露路径的本地来源身份。

    服务经过校验后使用文件内容身份；单独调用指纹函数时退回路径哈希，仍能区分
    不同输入而不把物理路径写入结果。
    """

    if value.local_source_fingerprint:
        return value.local_source_fingerprint
    if value.local_csv:
        return hashlib.sha256(value.local_csv.encode("utf-8")).hexdigest()
    return None


def latest_observable_market_date(config: DataFetcherConfig) -> str:
    """返回Host确认的行情截止日，默认使用Asia/Shanghai当日。

    该边界只约束OHLC等历史行情；未来交易日仍必须由独立的CalendarRequest取得。
    """

    value = config.market_data_as_of_date
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if value is None:
        return today.isoformat()
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise RequestValidationError("market_data_as_of_date必须为YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise RequestValidationError("market_data_as_of_date必须为YYYY-MM-DD")
    if parsed > today:
        raise RequestValidationError("market_data_as_of_date不得晚于当前可观测日期")
    return parsed.isoformat()


def _calendar_evidence_identity(config: DataFetcherConfig) -> str | None:
    """将日历证据纳入请求/缓存身份，不把旧质量结论复用于新日历。"""

    reference = config.trading_calendar_ref
    if reference is not None:
        payload: Mapping[str, Any] = {
            "data_asset_id": reference.data_asset_id,
            "content_hash": reference.content_hash,
            "schema_id": reference.schema_id,
        }
    elif config.trading_calendar_sessions:
        payload = {str(key): value for key, value in config.trading_calendar_sessions.items()}
    else:
        return None
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_request(value: DataRequest, config: DataFetcherConfig, caller: CallerContext) -> DataRequest:
    if "data:read" not in caller.capabilities:
        raise RequestValidationError("当前CallerContext无data:read权限")
    asset_ids = tuple(dict.fromkeys(item.strip().upper() for item in value.asset_ids if item and item.strip()))
    if not asset_ids:
        raise RequestValidationError("DataRequest必须包含asset_id或asset_ids")
    try:
        for asset_id in asset_ids:
            china_market_convention(asset_id, value.adjustment)
    except UnsupportedChinaAsset as error:
        raise RequestValidationError(str(error)) from error

    start_date = _parse_date(value.start_date, "start_date")
    end_date = _parse_date(value.end_date, "end_date")
    if start_date > end_date:
        raise RequestValidationError("start_date不得晚于end_date")
    if (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days > config.max_span_days:
        raise RequestValidationError("日期区间超过本机DataFetcher上限")
    if end_date > latest_observable_market_date(config):
        raise RequestValidationError("历史行情不允许请求未来日期；未来交易日请使用fetch_calendar")

    requested_fields = tuple(dict.fromkeys(_FIELD_ALIASES.get(item.strip().lower(), item.strip().lower()) for item in value.fields if item and item.strip()))
    if not requested_fields:
        raise RequestValidationError("DataRequest必须至少指定一个字段")
    unsupported = set(requested_fields).difference(config.allowed_fields)
    if unsupported:
        raise RequestValidationError(f"DataRequest包含未授权字段：{','.join(sorted(unsupported))}")
    selected_fields = {*requested_fields, "close", "adj_close"}
    fields = tuple(field for field in config.allowed_fields if field in selected_fields)
    if not {"close", "adj_close"}.issubset(fields):
        raise RequestValidationError("DataFetcher配置必须授权close和adj_close")

    frequency = value.frequency.strip().lower()
    if frequency not in _VALID_FREQUENCIES:
        raise RequestValidationError("当前DataFetcher仅支持日频数据")
    adjustment = value.adjustment.strip().lower()
    if adjustment not in _VALID_ADJUSTMENTS:
        raise RequestValidationError("adjustment必须为auto、none、raw、forward或both")
    cache_policy = value.cache_policy.strip().lower()
    if cache_policy not in _VALID_CACHE_POLICIES:
        raise RequestValidationError("cache_policy必须为reuse、extend_only或force_refresh")
    if cache_policy == "force_refresh" and "data:force_refresh" not in caller.capabilities:
        raise RequestValidationError("当前CallerContext无force_refresh权限")

    priority = tuple(dict.fromkeys(item.strip().lower() for item in value.source_priority if item and item.strip()))
    if not priority:
        priority = config.provider_priority
    if value.provider and value.provider.strip().lower() not in priority:
        priority = (value.provider.strip().lower(), *priority)
    allowed_providers = {"local", "ifind_http", "ifind_sdk", "wind"}
    unknown = set(priority).difference(allowed_providers)
    if unknown:
        raise RequestValidationError(f"未知Provider：{','.join(sorted(unknown))}")
    if "wind" in priority and not config.wind_enabled:
        raise RequestValidationError("Wind未通过本次运行的受控启用，不能作为默认或回退Provider")
    if value.quota_limit is not None and value.quota_limit < 0:
        raise RequestValidationError("quota_limit不能为负数")

    local_source_fingerprint = _local_source_fingerprint(value, config)

    return DataRequest(
        asset_ids=asset_ids,
        start_date=start_date,
        end_date=end_date,
        fields=fields,
        provider=value.provider.strip().lower() if value.provider else None,
        frequency="1d",
        adjustment=adjustment,
        source_priority=priority,
        cache_policy=cache_policy,
        offline=bool(value.offline),
        local_csv=value.local_csv,
        local_source_fingerprint=local_source_fingerprint,
        calendar_evidence_identity=_calendar_evidence_identity(config),
        quota_limit=value.quota_limit,
    )


def canonical_request_payload(value: DataRequest) -> dict[str, Any]:
    """不会包含本机物理路径或任何凭据的可哈希请求对象。"""

    return {
        "asset_ids": sorted(value.asset_ids),
        "start_date": value.start_date,
        "end_date": value.end_date,
        "fields": list(value.fields),
        "provider": value.provider,
        "frequency": value.frequency,
        "adjustment": value.adjustment,
        "source_priority": list(value.source_priority),
        "cache_policy": value.cache_policy,
        "offline": value.offline,
        "local_source_fingerprint": _request_local_source_fingerprint(value),
        "calendar_evidence_identity": value.calendar_evidence_identity,
        "quota_limit": value.quota_limit,
    }


def request_fingerprint(value: DataRequest) -> str:
    payload = json.dumps(canonical_request_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_identity(
    value: DataRequest,
    provider: str,
    *,
    tenant_id: str = "local",
) -> str:
    """日期范围不进入缓存身份，使extend_only可安全补齐同口径资产。"""

    payload = {
        "schema_id": "market-history-v1",
        "asset_ids": sorted(value.asset_ids),
        "fields": list(value.fields),
        "frequency": value.frequency,
        "adjustment": value.adjustment,
        "provider": provider,
        "tenant_id": tenant_id,
        "local_source_fingerprint": _request_local_source_fingerprint(value) if provider == "local" else None,
    }
    source = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
