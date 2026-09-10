"""DataFetcher唯一服务入口。

所有Tool和页面请求都经由``fetch_data``完成校验、缓存、Provider、标准化、
质量检查、DataAssetRef与DataFetchRun登记；页面不会直接访问数据源。
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import base64
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

import pandas as pd

from runtime.adapters.local_store import LocalDataStore, StoreError
from runtime.bootstrap import bootstrap_runtime
from runtime.contracts.contract_types import deep_thaw

from .cache_resolver import CacheIndexError, CacheMatch, LocalCache
from .config import DataFetcherConfig
from .data_normalizer import DataNormalizationError, daily_content_hash, daily_csv_bytes, merge_daily_history, normalize_daily_history
from .calendar_service import (
    CalendarCache,
    CalendarValidationError,
    VerifiedCalendarEvidence,
    calendar_evidence_for_history,
    fetch_calendar_asset,
    validate_calendar_request,
)
from .models import CalendarRequest, CallerContext, DataAssetRef, DataFetchResult, DataFetchRun, DataRequest, SecretRef
from .market_conventions import hv_input_requirements, market_conventions
from .providers import IFindHttpProvider, IFindSdkProvider, LocalCsvProvider, WindProvider
from .providers.base import (
    ProviderAccountPermissionDenied,
    ProviderDeviceLimitExceeded,
    ProviderError,
    ProviderFieldPermissionDenied,
    ProviderNoData,
    ProviderQuotaExceeded,
    ProviderUnauthorized,
    ProviderUnavailable,
)
from .quality_validator import DataQualityError, validate_daily_history
from .request_validator import (
    RequestValidationError,
    cache_identity,
    latest_completed_daily_market_date,
    latest_observable_market_date,
    request_fingerprint,
    validate_request,
)
from .volatile_store import list_volatile_assets, read_volatile_asset, store_volatile_asset


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PAGE_DIR = RUNTIME_PATHS.module_page_dir("datafetcher")
PAGE = PAGE_DIR / "datafetcher.html"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_DATAFETCHER_PORT", "4279"))


class DataFetcherError(RuntimeError):
    code = "failed"


class OfflineMiss(DataFetcherError):
    code = "offline_miss"


class CacheMiss(DataFetcherError):
    code = "cache_miss"


CHART_SERIES_MAX_POINTS = 180
_VERIFIED_CONNECTIONS: set[str] = set()
_VERIFIED_CONNECTIONS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _quota_limit(request: DataRequest | CalendarRequest, config: DataFetcherConfig) -> int:
    return min(config.max_provider_units, request.quota_limit) if request.quota_limit is not None else config.max_provider_units


def _providers() -> dict[str, Any]:
    providers = (IFindHttpProvider(), LocalCsvProvider(), WindProvider(), IFindSdkProvider())
    return {provider.name: provider for provider in providers}


def _asset_ref(value: Mapping[str, Any]) -> DataAssetRef:
    return DataAssetRef(
        data_asset_id=str(value["data_asset_id"]),
        storage_ref=str(value["storage_ref"]),
        media_type=str(value["media_type"]),
        schema_id=str(value["schema_id"]),
        asset_ids=tuple(value["asset_ids"]),
        normalized_fields=tuple(value["normalized_fields"]),
        coverage=dict(value["coverage"]),
        row_count=int(value["row_count"]),
        price_convention=dict(value["price_convention"]),
        content_hash=str(value["content_hash"]),
        lineage=dict(value["lineage"]),
        tenant_id=str(value.get("tenant_id", "local")),
        created_by=str(value.get("created_by", "local")),
        access_scope=tuple(value.get("access_scope", ("read",))),
        partition_spec=dict(value.get("partition_spec", {})),
    )


def _coverage(
    frame: pd.DataFrame,
    request: DataRequest,
    calendar_evidence: VerifiedCalendarEvidence | None,
    *,
    provider_confirmed_end_date: str | None = None,
) -> dict[str, Any]:
    by_asset = {
        asset_id: {
            "start_date": group["date"].min(),
            "end_date": group["date"].max(),
            "row_count": int(len(group)),
        }
        for asset_id, group in frame.groupby("asset_id", sort=True)
    }
    conventions = market_conventions(request.asset_ids, request.adjustment)
    exchanges = sorted({str(value["exchange"]) for value in conventions.values()})
    # Market-history coverage describes rows that actually exist in this
    # immutable asset. A verified exchange calendar may extend beyond the
    # latest completed daily bar; those dates belong to the calendar asset.
    sessions = sorted(str(value) for value in frame["date"].dropna().unique())
    if calendar_evidence is None:
        calendar_id = "UNVERIFIED-" + "+".join(exchanges)
        calendar_revision = "unverified"
    else:
        calendar_id = calendar_evidence.calendar_id
        calendar_revision = calendar_evidence.calendar_revision
    coverage = {
        # 正式覆盖只声明已观测数据；请求区间另存，不能把未验证日期伪装成覆盖。
        "start_date": str(frame["date"].min()),
        "end_date": str(frame["date"].max()),
        # Backtester的正式DataAssetRef协议以此字段确认日历声明不越过
        # 实际观察到的最后一条日线；不要用请求截止日替代。
        "calendar_coverage_end": str(frame["date"].max()),
        "requested_start_date": request.start_date,
        "requested_end_date": request.end_date,
        "sessions": sessions,
        "calendar_id": calendar_id,
        "calendar_revision": calendar_revision,
        "by_asset": by_asset,
    }
    if provider_confirmed_end_date is not None:
        coverage["provider_confirmed_end_date"] = provider_confirmed_end_date
    if calendar_evidence is not None and calendar_evidence.calendar_ref:
        coverage["calendar_ref"] = dict(calendar_evidence.calendar_ref)
    return coverage


def _asset_coverage_matches_frame(asset_ref: DataAssetRef, frame: pd.DataFrame) -> bool:
    """Reject cached metadata that claims rows absent from the cached CSV."""

    declared_sessions = asset_ref.coverage.get("sessions")
    if not isinstance(declared_sessions, (list, tuple)):
        return False
    actual_sessions = tuple(sorted(str(value) for value in frame["date"].dropna().unique()))
    if tuple(str(value) for value in declared_sessions) != actual_sessions:
        return False
    if asset_ref.row_count != len(frame):
        return False
    actual_assets = {str(value) for value in frame["asset_id"].dropna().unique()}
    return set(asset_ref.asset_ids) == actual_assets


VOLATILITY_WINDOWS = (5, 10, 20, 30, 60, 120, 252)


def _price_history(frame: pd.DataFrame, request: DataRequest, asset_id: str) -> tuple[str, pd.DataFrame]:
    """保留无效观测为空值，避免收益率跨过缺失价格。"""
    asset = frame.loc[frame["asset_id"].astype(str).str.upper().eq(asset_id)].sort_values("date")
    fields = ("close", "adj_close") if request.adjustment == "none" else ("adj_close", "close")
    for field in fields:
        if field not in asset:
            continue
        values = pd.to_numeric(asset[field], errors="coerce")
        valid = values.map(lambda value: pd.notna(value) and math.isfinite(float(value)) and float(value) > 0)
        if valid.any():
            return field, pd.DataFrame({"date": asset["date"], "value": values.where(valid)})
    return "", pd.DataFrame(columns=["date", "value"])


def _sample_chart_points(history: pd.DataFrame, max_points: int) -> tuple[Mapping[str, Any], ...]:
    if len(history) > max_points:
        indices = sorted({round(index * (len(history) - 1) / (max_points - 1)) for index in range(max_points)})
        history = history.iloc[indices]
    return tuple({"date": str(row["date"]), "value": float(row["value"])} for row in history.to_dict(orient="records"))


def _chart_series(frame: pd.DataFrame, request: DataRequest, *, max_points: int = CHART_SERIES_MAX_POINTS) -> tuple[Mapping[str, Any], ...]:
    """完整请求区间的轻量行情走势，不替代正式数据资产。"""
    if frame.empty or max_points < 2:
        return ()
    series = []
    for asset_id in request.asset_ids:
        field, history = _price_history(frame, request, asset_id)
        if field:
            series.append({"asset_id": asset_id, "field": field, "points": _sample_chart_points(history.dropna(), max_points)})
    return tuple(series)


def _volatility_series(frame: pd.DataFrame, request: DataRequest, *, max_points: int = CHART_SERIES_MAX_POINTS) -> tuple[Mapping[str, Any], ...]:
    """完整日线对数收益的滚动样本标准差，按252个交易日年化后抽样。"""
    if frame.empty or max_points < 2 or request.frequency != "1d":
        return ()
    series = []
    for asset_id in request.asset_ids:
        field, history = _price_history(frame, request, asset_id)
        if not field:
            continue
        ratios = history["value"] / history["value"].shift(1)
        returns = ratios.map(lambda value: math.log(value) if pd.notna(value) and math.isfinite(value) and value > 0 else math.nan)
        for window in VOLATILITY_WINDOWS:
            volatility = returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(252)
            observations = pd.DataFrame({"date": history["date"], "value": volatility}).dropna()
            series.append({"asset_id": asset_id, "field": field, "window": window, "annualization": 252,
                           "observation_count": len(observations), "points": _sample_chart_points(observations, max_points)})
    return tuple(series)


def _expected_trading_dates(
    request: DataRequest,
    config: DataFetcherConfig,
    caller: CallerContext,
    *,
    fallback_before: str | None = None,
) -> VerifiedCalendarEvidence | None:
    """只接受带身份、版本和完整覆盖声明的显式交易所日历。"""

    if config.trading_calendar_ref is not None:
        return calendar_evidence_for_history(
            config.trading_calendar_ref,
            request,
            caller,
            config,
            fallback_before=fallback_before,
        )
    conventions = market_conventions(request.asset_ids)
    validated_by_exchange: dict[str, tuple[str, ...]] = {}
    calendar_ids: list[str] = []
    calendar_revisions: list[str] = []
    fully_covered = True
    for convention in conventions.values():
        exchange = str(convention["exchange"])
        if exchange in validated_by_exchange:
            continue
        configured = config.trading_calendar_sessions.get(exchange, ())
        metadata = configured if isinstance(configured, Mapping) else None
        raw_sessions = metadata.get("sessions", ()) if metadata is not None else configured
        if isinstance(raw_sessions, (str, bytes)) or not isinstance(raw_sessions, (tuple, list)):
            raise DataQualityError(f"交易日历{exchange} sessions必须为YYYY-MM-DD序列")
        normalized: list[str] = []
        for session in raw_sessions:
            try:
                canonical = datetime.strptime(session, "%Y-%m-%d").date().isoformat()
            except (TypeError, ValueError) as error:
                raise DataQualityError(f"交易日历{exchange} sessions必须为YYYY-MM-DD") from error
            if canonical != session:
                raise DataQualityError(f"交易日历{exchange} sessions必须为YYYY-MM-DD")
            normalized.append(canonical)
        if len(set(normalized)) != len(normalized) or normalized != sorted(normalized):
            raise DataQualityError(f"交易日历{exchange} sessions必须严格递增且不重复")
        validated_by_exchange[exchange] = tuple(normalized)
        if metadata is None:
            # 旧式裸sessions没有来源版本或覆盖声明，不能充当完整交易日历。
            fully_covered = False
            continue
        calendar_id = metadata.get("calendar_id")
        calendar_revision = metadata.get("calendar_revision")
        coverage_start = metadata.get("coverage_start_date")
        coverage_end = metadata.get("coverage_end_date")
        if not all(isinstance(value, str) and value.strip() for value in (calendar_id, calendar_revision, coverage_start, coverage_end)):
            raise DataQualityError(f"交易日历{exchange}缺少calendar_id、calendar_revision或覆盖声明")
        try:
            canonical_start = datetime.strptime(str(coverage_start), "%Y-%m-%d").date().isoformat()
            canonical_end = datetime.strptime(str(coverage_end), "%Y-%m-%d").date().isoformat()
        except ValueError as error:
            raise DataQualityError(f"交易日历{exchange}覆盖日期必须为YYYY-MM-DD") from error
        if canonical_start != coverage_start or canonical_end != coverage_end or canonical_start > canonical_end:
            raise DataQualityError(f"交易日历{exchange}覆盖日期无效")
        if canonical_start > request.start_date or canonical_end < request.end_date:
            fully_covered = False
        calendar_ids.append(str(calendar_id))
        calendar_revisions.append(str(calendar_revision))
    expected = {
        asset_id: tuple(
            session for session in validated_by_exchange[str(convention["exchange"])]
            if session < fallback_before
        ) if fallback_before is not None else tuple(
            session for session in validated_by_exchange[str(convention["exchange"])]
            if request.start_date <= session <= request.end_date
        )
        for asset_id, convention in conventions.items()
    }
    if not fully_covered or not expected or not all(dates for dates in expected.values()):
        return None
    return VerifiedCalendarEvidence(
        dates_by_asset=expected,
        calendar_id="+".join(sorted(dict.fromkeys(calendar_ids))),
        calendar_revision="+".join(sorted(dict.fromkeys(calendar_revisions))),
        calendar_ref={},
    )


def _asset_uses_calendar_evidence(
    ref: DataAssetRef,
    evidence: VerifiedCalendarEvidence | None,
) -> bool:
    """缓存原始CSV可复用，但DataAssetRef必须与本次日历质量证据一致。"""

    coverage = dict(ref.coverage)
    lineage = dict(ref.lineage)
    if evidence is None:
        return (
            coverage.get("calendar_revision") == "unverified"
            and "calendar_ref" not in coverage
            and "trading_calendar_ref" not in lineage
        )
    if coverage.get("calendar_id") != evidence.calendar_id or coverage.get("calendar_revision") != evidence.calendar_revision:
        return False
    if evidence.calendar_ref:
        return (
            coverage.get("calendar_ref") == dict(evidence.calendar_ref)
            and lineage.get("trading_calendar_ref") == dict(evidence.calendar_ref)
        )
    return "calendar_ref" not in coverage and "trading_calendar_ref" not in lineage


def _data_asset_ref_key(caller: CallerContext, request: DataRequest) -> str:
    """同一原始缓存内按主体和完整请求选择语义正确的DataAssetRef。"""

    evidence = request.calendar_evidence_identity or "unverified"
    return f"{_owner_partition(caller.principal_id)}:{evidence}:{request_fingerprint(request)}"


def _existing_data_asset(store: LocalDataStore, *, asset_id: str, caller: CallerContext, content_hash: str) -> DataAssetRef:
    tenant_root = store.root if caller.tenant_id == "local" else store.root / "tenants" / caller.tenant_id
    manifest_path = tenant_root / "assets" / asset_id / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reference = _asset_ref({**manifest["metadata"], "storage_ref": manifest["storage_ref"]})
        if manifest.get("storage_ref") != reference.storage_ref or reference.data_asset_id != asset_id or reference.content_hash != content_hash:
            raise ValueError("DataAsset清单与请求内容不一致")
        _assert_asset_readable(reference, caller)
        store.resolve(reference, tenant_id=caller.tenant_id)
        return reference
    except (FileNotFoundError, OSError, StoreError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise DataFetcherError("已有DataAsset无法通过完整性校验") from error


def _asset_reference(
    frame: pd.DataFrame,
    request: DataRequest,
    config: DataFetcherConfig,
    provider: str,
    request_hash: str,
    cache_decision: str,
    provider_calls: list[Mapping[str, Any]],
    caller: CallerContext,
    calendar_evidence: VerifiedCalendarEvidence | None,
    latest_completed_date: str,
) -> DataAssetRef:
    content_hash = daily_content_hash(frame)
    conventions = market_conventions(request.asset_ids, request.adjustment)
    provider_confirmed_end_date: str | None = None
    if provider != "local" and cache_decision in {"cache_miss_fetched", "cache_extended"}:
        provider_confirmed_end_date = min(request.end_date, latest_completed_date)
        if calendar_evidence is not None:
            provider_confirmed_end_date = min(
                provider_confirmed_end_date,
                str(frame["date"].max()),
            )
    coverage = _coverage(
        frame,
        request,
        calendar_evidence,
        provider_confirmed_end_date=provider_confirmed_end_date,
    )
    adjustment = {
        asset_id: {
            field: (
                "unadjusted" if not field.startswith("adj_") else
                "not_applicable_alias_of_raw" if convention["asset_class"] == "index" else
                "unadjusted_alias_of_raw" if convention["effective_adjustment"] == "none" else
                f"{convention['effective_adjustment']}_adjusted"
            )
            for field in request.fields
        }
        for asset_id, convention in conventions.items()
    }
    price_convention = {
        "frequency": request.frequency,
        "requested_adjustment": request.adjustment,
        "field_adjustment_by_asset": adjustment,
        "timezone": "Asia/Shanghai",
        "as_of": frame["date"].max(),
        "valuation_timestamp": f"{frame['date'].max()}T15:00:00+08:00",
        "market_close_status": "historical_or_cached",
        "calendar_id": coverage["calendar_id"],
        "calendar_revision": coverage["calendar_revision"],
        "asset_market_conventions": conventions,
        "hv_input_requirements_by_asset": {
            asset_id: hv_input_requirements(convention)
            for asset_id, convention in conventions.items()
        },
    }
    # LocalDataStore将全部DataAsset元数据纳入storage_ref认证。ID同样必须绑定会
    # 改变资产语义的内容、请求、覆盖与口径，不能以相同CSV静默复用旧lineage。
    identity = hashlib.sha256(json.dumps({
        "schema_id": "market-history",
        "content_hash": content_hash,
        "provider": provider,
        "request_hash": request_hash,
        "coverage": coverage,
        "normalized_fields": ("date", "asset_id", *request.fields),
        "price_convention": price_convention,
        "tenant_id": caller.tenant_id,
        "owner": _owner_partition(caller.principal_id),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    data_asset_id = f"data-{identity}-{provider}-o-{_owner_partition(caller.principal_id)}"
    lineage: dict[str, Any] = {
        "provider": provider,
        "request_hash": request_hash,
        "cache_decision": cache_decision,
        "provider_calls": [dict(item) for item in provider_calls],
        "local_source_fingerprint": request.local_source_fingerprint if provider == "local" else None,
        "normalizer_version": "market-history",
        "fetched_at": _now(),
        "persistence_mode": request.persistence_mode,
    }
    if calendar_evidence is not None and calendar_evidence.calendar_ref:
        lineage["trading_calendar_ref"] = dict(calendar_evidence.calendar_ref)
    payload = daily_csv_bytes(frame)
    if request.persistence_mode == "volatile":
        return store_volatile_asset(
            caller=caller,
            data_asset_id=data_asset_id,
            payload=payload,
            media_type="text/csv",
            schema_id="market-history",
            asset_ids=tuple(request.asset_ids),
            normalized_fields=("date", "asset_id", *request.fields),
            coverage=coverage,
            row_count=int(len(frame)),
            partition_spec={"date_column": "date", "frequency": request.frequency, "asset_partitioned": True},
            price_convention=price_convention,
            lineage=lineage,
        )
    store = LocalDataStore(Path(config.data_root or RUNTIME_PATHS.data_root))
    try:
        return store.put_bytes(
            tenant_id=caller.tenant_id,
            data_asset_id=data_asset_id,
            payload=payload,
            media_type="text/csv",
            schema_id="market-history",
            asset_ids=tuple(request.asset_ids),
            normalized_fields=("date", "asset_id", *request.fields),
            coverage=coverage,
            row_count=int(len(frame)),
            partition_spec={"date_column": "date", "frequency": request.frequency, "asset_partitioned": True},
            price_convention=price_convention,
            lineage=lineage,
            created_by=caller.principal_id,
            access_scope=("read",),
        )
    except FileExistsError:
        return _existing_data_asset(store, asset_id=data_asset_id, caller=caller, content_hash=content_hash)


def _safe_task_id(value: Any) -> str:
    task_id = str(value or "local").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", task_id):
        raise RequestValidationError("task_id只能包含字母、数字、下划线和连字符")
    return task_id


def _tenant_partition(tenant_id: str) -> str:
    value = str(tenant_id).strip()
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value) else hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _owner_partition(principal_id: str) -> str:
    """DataAssetId与缓存必须绑定创建主体，避免同租户越权复用。"""

    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:12]


def _assert_data_read(caller: CallerContext) -> None:
    if "data:read" not in caller.capabilities:
        raise PermissionError("CallerContext无data:read权限")


def _assert_asset_readable(ref: DataAssetRef, caller: CallerContext) -> None:
    _assert_data_read(caller)
    if ref.tenant_id != caller.tenant_id:
        raise PermissionError("DataAssetRef租户与CallerContext不一致")
    if ref.created_by != caller.principal_id:
        raise PermissionError("DataAssetRef创建主体与CallerContext不一致")
    if "read" not in ref.access_scope:
        raise PermissionError("DataAssetRef未授予read权限")


def _persist_run(config: DataFetcherConfig, request: DataRequest | CalendarRequest, run: DataFetchRun, quality: Mapping[str, Any] | None) -> DataFetchRun:
    """写入不可覆盖的本机DataFetchRun，不把路径或凭据写进公开对象。"""

    root = Path(config.result_root or RUNTIME_PATHS.result_root) / "output_datafetch" / "tenants" / _tenant_partition(run.tenant_id) / run.task_id
    target = root / run.data_fetch_run_id
    root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise DataFetcherError("DataFetchRun已存在，拒绝覆盖")
    staging = Path(tempfile.mkdtemp(prefix=f".{run.data_fetch_run_id}.", dir=root))
    try:
        artifacts: dict[str, Any] = {
            "input_snapshot.json": request.public_dict(),
            "provider_calls.json": list(run.provider_calls),
            "quota_usage.json": dict(run.quota_usage),
        }
        if run.data_asset_ref is not None:
            artifacts["data_asset_ref.json"] = deep_thaw(asdict(run.data_asset_ref))
        if quality is not None:
            artifacts["quality_report.json"] = dict(quality)
        if run.error is not None:
            artifacts["error.json"] = dict(run.error)
        for filename, value in artifacts.items():
            (staging / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        file_hashes = {
            filename: hashlib.sha256((staging / filename).read_bytes()).hexdigest()
            for filename in sorted(artifacts)
        }
        manifest_payload = json.dumps({
            "data_fetch_run_id": run.data_fetch_run_id,
            "task_id": run.task_id,
            "tenant_id": run.tenant_id,
            "module": "datafetcher",
            "status": run.status,
            "created_at": _now(),
            "request_hash": run.request_hash,
            "cache_decision": run.cache_decision,
            "complete": True,
            "file_hashes": file_hashes,
        }, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        (staging / "manifest.json").write_bytes(manifest_payload)
        staging.replace(target)
        return replace(
            run,
            expected_manifest_hash=hashlib.sha256(manifest_payload).hexdigest(),
            expected_data_asset_hash=run.data_asset_ref.content_hash if run.data_asset_ref is not None else None,
        )
    except Exception:
        # 不删除未知目录；本次暂存目录可由受控清理流程回收。
        raise


def _provider_order(request: DataRequest, cached_provider: str | None = None) -> tuple[str, ...]:
    if cached_provider:
        return (cached_provider,)
    return request.source_priority


def _provider_call(provider: str, outcome: str, quota_units: int, error: ProviderError | None = None) -> dict[str, Any]:
    """生成不含Provider原文或凭据的调用审计记录。"""

    call: dict[str, Any] = {"provider": provider, "outcome": outcome, "quota_units": quota_units}
    if error is not None:
        if error.reason_code:
            call["reason_code"] = error.reason_code
        if error.provider_code:
            call["provider_error_code"] = error.provider_code
        if error.http_status is not None:
            call["http_status"] = error.http_status
    return call


def _fetch_provider(
    request: DataRequest,
    config: DataFetcherConfig,
    names: tuple[str, ...],
    calls: list[dict[str, Any]],
    quota_used: int,
    *,
    expected_trading_dates: Mapping[str, tuple[str, ...]] | None = None,
    latest_observable_date: str | None = None,
    latest_pending_session: str | None = None,
) -> tuple[str, pd.DataFrame, int]:
    catalog = _providers()
    limit = min(config.max_provider_units, request.quota_limit) if request.quota_limit is not None else config.max_provider_units
    last_error: Exception | None = None
    protected_error: Exception | None = None
    for name in names:
        provider = catalog[name]
        if (config.offline or request.offline) and provider.network:
            calls.append({"provider": name, "outcome": "skipped_offline", "quota_units": 0})
            continue
        units = int(provider.estimate_quota(request))
        if quota_used + units > limit:
            calls.append(_provider_call(name, "quota_exceeded", units))
            last_error = ProviderQuotaExceeded("请求超过DataFetcher额度保护上限")
            if protected_error is None:
                protected_error = last_error
            continue
        charged = False
        try:
            frame = provider.fetch(request, config)
            quota_used += units
            charged = True
            normalized = normalize_daily_history(
                frame,
                request,
                latest_observable_date=latest_observable_date or latest_observable_market_date(config),
                retain_source_history=name == "local",
            )
            if name == "local":
                # 保留已读取文件的完整价格快照，资产输出和日历校验仍使用请求视图。
                validate_daily_history(normalized, request.fields, latest_observable_date=latest_observable_date)
            request_view = _request_view(normalized, request)
            # Provider优先级只有在当前Provider按标的、价格字段及已验证交易日历
            # 完整覆盖本次区间后才可终止。本地文件缺少中间交易日时应继续尝试
            # 后续API，不能先记录成功再在Provider选择结束后失败。
            validate_daily_history(
                request_view,
                request.fields,
                start_date=request.start_date,
                end_date=request.end_date,
                expected_trading_dates=expected_trading_dates,
                latest_observable_date=latest_observable_date,
                latest_pending_session=latest_pending_session,
            )
            calls.append(_provider_call(name, "succeeded", units))
            return name, normalized, quota_used
        except ProviderError as error:
            if not charged:
                quota_used += units
            calls.append(_provider_call(name, error.code, units, error))
            last_error = error
            if protected_error is None and isinstance(error, (ProviderUnauthorized, ProviderQuotaExceeded)):
                protected_error = error
        except (DataNormalizationError, DataQualityError) as error:
            if not charged:
                quota_used += units
            calls.append({"provider": name, "outcome": getattr(error, "code", "normalization_error"), "quota_units": units})
            last_error = error
    if config.offline or request.offline:
        raise OfflineMiss("离线模式下缓存或本地CSV未满足请求")
    if protected_error is not None:
        raise protected_error
    if last_error is not None:
        if isinstance(last_error, (ProviderError, DataQualityError)):
            raise last_error
        raise ProviderUnavailable("没有可用Provider满足本次请求") from last_error
    raise ProviderUnavailable("没有可用Provider满足本次请求")


def _interval_requests(request: DataRequest, match: CacheMatch | None) -> tuple[tuple[DataRequest, ...], pd.DataFrame | None, str | None]:
    if match is None or match.frame is None:
        return (request,), None, None
    groups: dict[tuple[str, str], list[str]] = {}
    for asset_id, intervals in match.missing_by_asset.items():
        for start_date, end_date in intervals:
            groups.setdefault((start_date, end_date), []).append(asset_id)
    requests = tuple(
        replace(request, asset_ids=tuple(asset_ids), start_date=start_date, end_date=end_date)
        for (start_date, end_date), asset_ids in groups.items()
    )
    return requests, match.frame, match.provider


def _interval_expected_trading_dates(
    expected_trading_dates: Mapping[str, tuple[str, ...]] | None,
    request: DataRequest,
) -> Mapping[str, tuple[str, ...]] | None:
    """将整段日历证据裁剪到一个缺口请求，保持逐资产完整性校验。"""

    if expected_trading_dates is None:
        return None
    return {
        asset_id: tuple(
            date
            for date in expected_trading_dates.get(asset_id, ())
            if request.start_date <= date <= request.end_date
        )
        for asset_id in request.asset_ids
    }


def _completed_calendar_sessions(
    evidence: VerifiedCalendarEvidence | None,
    *,
    latest_completed_date: str,
) -> Mapping[str, tuple[str, ...]] | None:
    if evidence is None:
        return None
    return {
        asset_id: tuple(session for session in sessions if session <= latest_completed_date)
        for asset_id, sessions in evidence.dates_by_asset.items()
    }


def _latest_pending_session(
    expected_trading_dates: Mapping[str, tuple[str, ...]] | None,
    latest_completed_date: str,
) -> str | None:
    """只允许Host最新已完成自然日恰为共同交易Session时进入待发布态。"""

    if not expected_trading_dates:
        return None
    if all(sessions and max(sessions) == latest_completed_date for sessions in expected_trading_dates.values()):
        return latest_completed_date
    return None


def _previous_verified_session_request(
    request: DataRequest,
    config: DataFetcherConfig,
    caller: CallerContext,
) -> DataRequest | None:
    """单日空行情只沿已验证交易所日历回退，不推断工作日。"""

    if request.start_date != request.end_date:
        return None
    evidence = _expected_trading_dates(
        request,
        config,
        caller,
        fallback_before=request.end_date,
    )
    if evidence is None:
        return None
    common = set.intersection(*(set(values) for values in evidence.dates_by_asset.values()))
    if not common:
        return None
    session = max(common)
    return replace(request, start_date=session, end_date=session)


def _request_view(frame: pd.DataFrame, request: DataRequest) -> pd.DataFrame:
    """从宽缓存生成本次请求视图，避免改变缓存本体覆盖。"""

    assets = frame["asset_id"].astype(str).str.upper()
    dates = frame["date"].astype(str)
    view = frame.loc[
        assets.isin(request.asset_ids) & dates.between(request.start_date, request.end_date)
    ].copy()
    observed_assets = set(view["asset_id"].astype(str).str.upper())
    missing_assets = sorted(set(request.asset_ids).difference(observed_assets))
    if missing_assets:
        raise DataQualityError(f"缓存请求视图未覆盖标的：{','.join(missing_assets)}")
    return view.sort_values(["asset_id", "date"]).reset_index(drop=True)


def _resolve_data(
    cache: LocalCache,
    request: DataRequest,
    config: DataFetcherConfig,
    request_hash: str,
    calls: list[dict[str, Any]],
    quota_usage: dict[str, int],
    caller: CallerContext,
) -> tuple[DataAssetRef, Mapping[str, Any], str, pd.DataFrame]:
    """在同一缓存身份锁内完成查找、补齐和写入，避免并发重复取数。"""

    identities = sorted({
        cache_identity(
            request,
            provider,
            tenant_id=caller.tenant_id,
            principal_id=caller.principal_id,
        )
        for provider in request.source_priority
    })
    with ExitStack() as locks:
        for identity in identities:
            locks.enter_context(cache.fetch_lock(identity))

        calendar_evidence = _expected_trading_dates(request, config, caller)
        latest_observable_date = latest_observable_market_date(config)
        latest_completed_date = latest_completed_daily_market_date(config)
        expected_trading_dates = _completed_calendar_sessions(
            calendar_evidence, latest_completed_date=latest_completed_date,
        )
        latest_pending_session = _latest_pending_session(expected_trading_dates, latest_completed_date)
        data_asset_ref_key = _data_asset_ref_key(caller, request)
        matches = [] if request.cache_policy == "force_refresh" else [
            cache.lookup(
                request,
                name,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                expected_trading_dates=expected_trading_dates,
                latest_completed_date=latest_completed_date,
                data_asset_ref_key=data_asset_ref_key,
            )
            for name in request.source_priority
        ]
        full_match = next((match for match in matches if match.complete and match.frame is not None and match.metadata), None)
        if full_match is not None:
            request_frame = _request_view(full_match.frame, request)
            reference = full_match.metadata.get("data_asset_ref")
            asset_ref: DataAssetRef | None = _asset_ref(reference) if isinstance(reference, Mapping) else None
            if asset_ref is not None:
                try:
                    if asset_ref.storage_ref.startswith("volatile:"):
                        volatile = read_volatile_asset(asset_ref.data_asset_id, caller)
                        if volatile is None or volatile[0].content_hash != asset_ref.content_hash:
                            raise FileNotFoundError("volatile asset is unavailable")
                    else:
                        LocalDataStore(Path(config.data_root or RUNTIME_PATHS.data_root)).resolve(
                            asset_ref,
                            tenant_id=caller.tenant_id,
                        )
                except (FileNotFoundError, PermissionError, StoreError):
                    asset_ref = None
                    invalid_match = full_match
                else:
                    invalid_match = None
            else:
                invalid_match = None
            quality = validate_daily_history(
                request_frame, request.fields, start_date=request.start_date, end_date=request.end_date,
                expected_trading_dates=expected_trading_dates,
                latest_observable_date=latest_observable_date,
                latest_pending_session=latest_pending_session,
            )
            if asset_ref is not None:
                same_owner = asset_ref.created_by == caller.principal_id and "read" in asset_ref.access_scope
                if (
                    same_owner
                    and _asset_uses_calendar_evidence(asset_ref, calendar_evidence)
                    and _asset_coverage_matches_frame(asset_ref, request_frame)
                ):
                    return asset_ref, quality, "cache_hit", request_frame
            references = full_match.metadata.get("data_asset_refs")
            owner = _owner_partition(caller.principal_id)
            evidence = request.calendar_evidence_identity or "unverified"
            same_evidence_prefix = f"{owner}:{evidence}:"
            owner_prefix = f"{owner}:"
            reference_keys = tuple(references) if isinstance(references, Mapping) else ()
            if any(str(key).startswith(same_evidence_prefix) for key in reference_keys):
                rebound_decision = "cache_hit"
            elif any(str(key).startswith(owner_prefix) for key in reference_keys):
                rebound_decision = "cache_revalidated"
            else:
                rebound_decision = "cache_rebound"
            rebound_ref = _asset_reference(
                request_frame,
                request,
                config,
                full_match.provider,
                request_hash,
                rebound_decision,
                calls,
                caller,
                calendar_evidence,
                latest_completed_date,
            )
            if request.persistence_mode == "library":
                cache.save(
                    cache_identity(
                        request,
                        full_match.provider,
                        tenant_id=caller.tenant_id,
                        principal_id=caller.principal_id,
                    ),
                    full_match.frame,
                    {
                        "content_hash": daily_content_hash(full_match.frame),
                        "provider": full_match.provider,
                        "provider_confirmed_dates": full_match.metadata.get("provider_confirmed_dates", {}),
                        "data_asset_ref": deep_thaw(asdict(rebound_ref)),
                        "updated_at": _now(),
                        "tenant_id": caller.tenant_id,
                        "principal_id": caller.principal_id,
                    },
                    data_asset_ref_key=data_asset_ref_key,
                )
            else:
                cache.save_volatile(
                    cache_identity(
                        request,
                        full_match.provider,
                        tenant_id=caller.tenant_id,
                        principal_id=caller.principal_id,
                    ),
                    full_match.frame,
                    {
                        "content_hash": daily_content_hash(full_match.frame),
                        "provider": full_match.provider,
                        "provider_confirmed_dates": full_match.metadata.get("provider_confirmed_dates", {}),
                        "data_asset_ref": deep_thaw(asdict(rebound_ref)),
                        "updated_at": _now(),
                        "tenant_id": caller.tenant_id,
                        "principal_id": caller.principal_id,
                    },
                    data_asset_ref_key=data_asset_ref_key,
                )
            return rebound_ref, quality, rebound_decision, request_frame
        else:
            invalid_match = None

        partial_match = next((match for match in matches if match is not invalid_match and match.frame is not None), None)
        if request.cache_policy == "reuse":
            raise CacheMiss("缓存覆盖不足且cache_policy=reuse禁止下载")
        interval_requests, cached_frame, cached_provider = _interval_requests(request, partial_match)
        if not interval_requests:
            raise CacheMiss("缓存覆盖不足但没有可补齐区间")
        fetched_frames: list[pd.DataFrame] = []
        selected_provider: str | None = cached_provider
        latest_session_pending_no_data = False
        for interval in interval_requests:
            interval_expected_dates = _interval_expected_trading_dates(expected_trading_dates, interval)
            try:
                provider, frame, quota_usage["used"] = _fetch_provider(
                    interval,
                    config,
                    _provider_order(interval, cached_provider),
                    calls,
                    quota_usage["used"],
                    expected_trading_dates=interval_expected_dates,
                    latest_observable_date=latest_observable_date,
                    latest_pending_session=(
                        latest_pending_session
                        if latest_pending_session is not None
                        and interval.start_date <= latest_pending_session <= interval.end_date
                        else None
                    ),
                )
            except ProviderNoData:
                if (
                    cached_frame is not None
                    and interval.start_date == interval.end_date == latest_pending_session
                ):
                    latest_session_pending_no_data = True
                    continue
                raise
            if selected_provider is not None and provider != selected_provider:
                raise DataFetcherError("不同Provider数据不得自动混合")
            selected_provider = provider
            fetched_frames.append(frame)
        cache_frame = merge_daily_history(cached_frame, *fetched_frames)
        frame = _request_view(cache_frame, request)
        quality = validate_daily_history(
            frame, request.fields, start_date=request.start_date, end_date=request.end_date,
            expected_trading_dates=expected_trading_dates,
            latest_observable_date=latest_observable_date,
            latest_pending_session=latest_pending_session,
        )
        cache_decision = (
            "latest_session_pending"
            if latest_session_pending_no_data
            else "cache_extended" if cached_frame is not None else "cache_miss_fetched"
        )
        asset_ref = _asset_reference(
            frame,
            request,
            config,
            selected_provider or "local",
            request_hash,
            cache_decision,
            calls,
            caller,
            calendar_evidence,
            latest_completed_date,
        )
        if request.persistence_mode == "library":
            cache.save(
                cache_identity(
                    request,
                    selected_provider or "local",
                    tenant_id=caller.tenant_id,
                    principal_id=caller.principal_id,
                ),
                cache_frame,
                {
                    "content_hash": daily_content_hash(cache_frame),
                    "provider": selected_provider,
                    "provider_confirmed_dates": (partial_match.metadata or {}).get("provider_confirmed_dates", {}) if partial_match else {},
                    "data_asset_ref": deep_thaw(asdict(asset_ref)),
                    "updated_at": _now(),
                    "tenant_id": caller.tenant_id,
                    "principal_id": caller.principal_id,
                },
                data_asset_ref_key=data_asset_ref_key,
                reset_coverage=request.cache_policy == "force_refresh",
            )
        else:
            cache.save_volatile(
                cache_identity(
                    request,
                    selected_provider or "local",
                    tenant_id=caller.tenant_id,
                    principal_id=caller.principal_id,
                ),
                cache_frame,
                {
                    "content_hash": daily_content_hash(cache_frame),
                    "provider": selected_provider,
                    "provider_confirmed_dates": (partial_match.metadata or {}).get("provider_confirmed_dates", {}) if partial_match else {},
                    "data_asset_ref": deep_thaw(asdict(asset_ref)),
                    "updated_at": _now(),
                    "tenant_id": caller.tenant_id,
                    "principal_id": caller.principal_id,
                },
                data_asset_ref_key=data_asset_ref_key,
                reset_coverage=request.cache_policy == "force_refresh",
            )
        return asset_ref, quality, cache_decision, frame


def _failure_result(
    config: DataFetcherConfig,
    request: DataRequest | CalendarRequest,
    task_id: str,
    run_id: str,
    request_hash: str,
    cache_decision: str,
    calls: list[dict[str, Any]],
    quota_used: int,
    error: Exception,
    caller: CallerContext,
) -> DataFetchResult:
    code = getattr(error, "code", "failed")
    status = "quota_exceeded" if code == "quota_exceeded" else "unauthorized" if code == "unauthorized" else "failed"
    public_error = {
        "code": code,
        "message": str(error) if isinstance(error, (RequestValidationError, CalendarValidationError, ProviderError, DataFetcherError, DataQualityError, CacheIndexError)) else "DataFetcher内部错误",
    }
    if isinstance(error, ProviderError):
        if error.reason_code:
            public_error["reason_code"] = error.reason_code
        if error.provider_code:
            public_error["provider_error_code"] = error.provider_code
        if error.http_status is not None:
            public_error["http_status"] = error.http_status
    audited_quota_used = max(
        quota_used,
        sum(
            int(item.get("quota_units", 0))
            for item in calls
            if item.get("outcome") not in {"quota_exceeded", "skipped_offline"}
        ),
    )
    run = DataFetchRun(
        data_fetch_run_id=run_id,
        task_id=task_id,
        status=status,
        request_hash=request_hash,
        cache_decision=cache_decision,
        provider_calls=tuple(calls),
        quota_usage={"used": audited_quota_used, "limit": _quota_limit(request, config)},
        error=public_error,
        tenant_id=caller.tenant_id,
    )
    if request.persistence_mode == "library":
        run = _persist_run(config, request, run, None)
    return DataFetchResult(ok=False, run=run)


def fetch_data(
    request: DataRequest | Mapping[str, Any],
    caller: CallerContext | None = None,
    *,
    config: DataFetcherConfig | None = None,
    task_id: str = "local",
) -> DataFetchResult:
    """统一数据服务正式入口。"""

    effective_config = (config or DataFetcherConfig.from_runtime()).resolved()
    caller = caller or CallerContext(
        tenant_id="local",
        principal_id="local-user",
        role="local",
        capabilities=("data:read", "data:force_refresh"),
        session_id="local",
        audience="local",
    )
    run_id = f"datafetch-{uuid4().hex}"
    calls: list[dict[str, Any]] = []
    quota_usage = {"used": 0}
    raw_request: DataRequest | None = None
    warnings: list[str] = []
    try:
        task_id = _safe_task_id(task_id)
        if isinstance(request, DataRequest):
            raw_request = request
        elif isinstance(request, Mapping):
            try:
                raw_request = DataRequest.from_mapping(request)
            except ValueError as error:
                raise RequestValidationError(str(error)) from error
        else:
            raise RequestValidationError("DataRequest必须为对象")
        validated = validate_request(raw_request, effective_config, caller)
        request_hash = request_fingerprint(validated)
        cache = LocalCache(Path(effective_config.cache_root or Path(effective_config.data_root or RUNTIME_PATHS.data_root) / "datafetcher-cache"))
        try:
            asset_ref, quality, cache_decision, frame = _resolve_data(
                cache, validated, effective_config, request_hash, calls, quota_usage, caller,
            )
        except ProviderNoData:
            fallback = _previous_verified_session_request(validated, effective_config, caller)
            if fallback is None:
                raise
            asset_ref, quality, cache_decision, frame = _resolve_data(
                cache, fallback, effective_config, request_hash, calls, quota_usage, caller,
            )
            warnings.append(f"请求日期无可用日线，已按验证交易日历使用最近交易日{fallback.end_date}")
        if quality.get("calendar_completeness") == "latest_session_pending":
            warnings.append(
                "latest_session_pending：最后一个已验证交易日的日线尚未发布，"
                f"本次资产仅使用截至{asset_ref.coverage['end_date']}的完整行情"
            )
        run = DataFetchRun(
            data_fetch_run_id=run_id,
            task_id=task_id,
            status="complete",
            request_hash=request_hash,
            cache_decision=cache_decision,
            provider_calls=tuple(calls),
            quota_usage={"used": quota_usage["used"], "limit": _quota_limit(validated, effective_config)},
            warnings=tuple(warnings),
            data_asset_ref=asset_ref,
            tenant_id=caller.tenant_id,
        )
        if validated.persistence_mode == "library":
            run = _persist_run(effective_config, validated, run, quality)
        preview = tuple(
            {column: (None if pd.isna(value) else value) for column, value in row.items()}
            for row in frame.head(12).to_dict(orient="records")
        )
        chart_series = _chart_series(frame, validated)
        return DataFetchResult(
            ok=True,
            run=run,
            quality_report=quality,
            data_preview=preview,
            chart_series=chart_series,
            volatility_series=_volatility_series(frame, validated),
        )
    except Exception as error:
        fallback = raw_request or DataRequest(asset_ids=(), start_date="", end_date="", fields=())
        try:
            request_hash = request_fingerprint(fallback)
        except Exception:
            request_hash = "invalid-request"
        return _failure_result(effective_config, fallback, task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(task_id or "")) else "local", run_id, request_hash, "failed", calls, quota_usage["used"], error, caller)


def fetch_calendar_data(
    request: CalendarRequest | Mapping[str, Any],
    caller: CallerContext | None = None,
    *,
    config: DataFetcherConfig | None = None,
    task_id: str = "local",
) -> DataFetchResult:
    """获取独立中国交易日历资产，不进入历史行情字段质量门禁。"""

    effective_config = (config or DataFetcherConfig.from_runtime()).resolved()
    effective_caller = caller or CallerContext(
        tenant_id="local",
        principal_id="local-user",
        role="local",
        capabilities=("data:read", "data:force_refresh"),
        session_id="local",
        audience="local",
    )
    run_id = f"datafetch-{uuid4().hex}"
    raw_request: CalendarRequest | None = None
    calls: list[dict[str, Any]] = []
    try:
        safe_task = _safe_task_id(task_id)
        raw_request = validate_calendar_request(request, effective_config)
        raw_request, ref, quality, cache_decision, provider_calls = fetch_calendar_asset(
            raw_request, effective_caller, effective_config,
        )
        calls.extend(dict(item) for item in provider_calls)
        request_hash = hashlib.sha256(
            json.dumps(raw_request.public_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run = DataFetchRun(
            data_fetch_run_id=run_id,
            task_id=safe_task,
            status="complete",
            request_hash=request_hash,
            cache_decision=cache_decision,
            provider_calls=tuple(calls),
            quota_usage={"used": sum(int(item.get("quota_units", 0)) for item in calls), "limit": _quota_limit(raw_request, effective_config)},
            data_asset_ref=ref,
            tenant_id=effective_caller.tenant_id,
        )
        if raw_request.persistence_mode == "library":
            run = _persist_run(effective_config, raw_request, run, quality)
        preview = tuple({"session": value} for value in tuple(ref.coverage.get("sessions", ()))[:12])
        return DataFetchResult(ok=True, run=run, quality_report=quality, data_preview=preview)
    except Exception as error:
        attempted_calls = getattr(error, "provider_calls", ())
        if isinstance(attempted_calls, (tuple, list)):
            calls.extend(dict(item) for item in attempted_calls if isinstance(item, Mapping))
        fallback = raw_request or CalendarRequest(asset_ids=(), start_date="", end_date="")
        request_hash = hashlib.sha256(
            json.dumps(fallback.public_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        safe_task = task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(task_id or "")) else "local"
        return _failure_result(
            effective_config,
            fallback,
            str(safe_task),
            run_id,
            request_hash,
            "failed",
            calls,
            sum(int(item.get("quota_units", 0)) for item in calls),
            error,
            effective_caller,
        )


def capability(*, app_configured: bool | None = None, app_verified: bool = False) -> dict[str, Any]:
    """只报告可调用能力，不探测或输出凭据。"""

    result: dict[str, Any] = {
        "ok": True,
        "module": "datafetcher",
        "status": "available",
        "entrypoint": "fetch_data(DataRequest, CallerContext)",
        "provider_priority": ["ifind_http"],
        "credentials": "environment_or_secret_ref_only",
        "credential_status": {
            "ifind_http": "SecretRef或环境变量",
            "wind": "受控启用后再探测",
        },
        "offline_supported": True,
        "actions": ["status", "catalog", "list_assets", "fetch", "fetch_calendar"],
        "data_schemas": ["market-history", "trading-calendar"],
    }
    if app_configured is not None:
        verified = bool(app_configured and app_verified)
        result.update({
            "configured": app_configured,
            "credentials": "app_host_secret_ref_only",
            "credential_status": {
                "configured": app_configured,
                "state": "verified" if verified else "configured_unverified" if app_configured else "not_configured",
                "source": "app_host_secret_ref" if app_configured else "none",
                "remote_provider": "available" if verified else "not_checked" if app_configured else "not_configured",
            },
        })
    return result


def _runtime_cache() -> tuple[DataFetcherConfig, LocalCache]:
    config = DataFetcherConfig.from_runtime().resolved()
    return config, LocalCache(Path(config.cache_root or Path(config.data_root) / "datafetcher-cache"))


def list_data_assets(*, caller: CallerContext | None = None, limit: int = 100) -> list[Mapping[str, Any]]:
    """读取本模块缓存索引中的当前租户资产，绝不扫描任意目录。"""

    effective_caller = caller or CallerContext("local", "local-user", "local", ("data:read",), "local", "local")
    _assert_data_read(effective_caller)
    config, cache = _runtime_cache()
    history = cache.list_assets(
        tenant_id=effective_caller.tenant_id,
        principal_id=effective_caller.principal_id,
        limit=limit,
    )
    calendars = CalendarCache(Path(config.cache_root)).list_assets(
        tenant_id=effective_caller.tenant_id,
        principal_id=effective_caller.principal_id,
        limit=limit,
    )
    volatile = [
        {
            "data_asset_ref": deep_thaw(asdict(reference)),
            "provider": reference.lineage.get("provider"),
            "updated_at": reference.lineage.get("fetched_at"),
        }
        for reference in list_volatile_assets(effective_caller, limit=limit)
    ]
    combined: list[Mapping[str, Any]] = []
    store: LocalDataStore | None = None
    for item in history + calendars + volatile:
        reference = item.get("data_asset_ref") if isinstance(item, Mapping) else None
        if not isinstance(reference, Mapping):
            continue
        try:
            ref = _asset_ref(reference)
            _assert_asset_readable(ref, effective_caller)
            if ref.storage_ref.startswith("volatile:"):
                in_memory = read_volatile_asset(ref.data_asset_id, effective_caller)
                if in_memory is None or in_memory[0].content_hash != ref.content_hash:
                    continue
            else:
                store = store or LocalDataStore(Path(config.data_root))
                store.resolve(ref, tenant_id=effective_caller.tenant_id)
        except (FileNotFoundError, StoreError, KeyError, TypeError, ValueError, PermissionError):
            continue
        combined.append(item)
    safe_limit = max(1, min(int(limit), 100))
    deduplicated = {
        str(item["data_asset_ref"]["data_asset_id"]): item
        for item in combined
        if isinstance(item.get("data_asset_ref"), Mapping)
    }
    return sorted(
        deduplicated.values(),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )[:safe_limit]


def read_data_asset(data_asset_id: str, *, caller: CallerContext | None = None) -> tuple[DataAssetRef, bytes]:
    """按缓存索引中的DataAssetRef读取受控数据本体，供本机页面下载。"""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", data_asset_id):
        raise DataFetcherError("DataAsset标识非法")
    effective_caller = caller or CallerContext("local", "local-user", "local", ("data:read",), "local", "local")
    _assert_data_read(effective_caller)
    volatile = read_volatile_asset(data_asset_id, effective_caller)
    if volatile is not None:
        return volatile
    config, cache = _runtime_cache()
    item = cache.find_asset(
        tenant_id=effective_caller.tenant_id,
        principal_id=effective_caller.principal_id,
        data_asset_id=data_asset_id,
    )
    if item is not None:
        ref = _asset_ref(item["data_asset_ref"])
        _assert_asset_readable(ref, effective_caller)
        try:
            return ref, LocalDataStore(Path(config.data_root)).read_bytes(ref, tenant_id=effective_caller.tenant_id)
        except (FileNotFoundError, StoreError) as error:
            raise FileNotFoundError("DataAsset不存在或完整性校验失败") from error
    calendar_item = CalendarCache(Path(config.cache_root)).find_asset(
        tenant_id=effective_caller.tenant_id,
        principal_id=effective_caller.principal_id,
        data_asset_id=data_asset_id,
    )
    if calendar_item is not None:
        ref = _asset_ref(calendar_item["data_asset_ref"])
        _assert_asset_readable(ref, effective_caller)
        try:
            return ref, LocalDataStore(Path(config.data_root)).read_bytes(ref, tenant_id=effective_caller.tenant_id)
        except (FileNotFoundError, StoreError) as error:
            raise FileNotFoundError("DataAsset不存在或完整性校验失败") from error
    raise FileNotFoundError("DataAsset不存在或无访问权限")


def _business_request_source(payload: dict[str, Any]) -> Mapping[str, Any]:
    """提取唯一业务请求，并拒绝信封层夹带模块外身份。"""

    wrappers = tuple(field for field in ("request", "data_request") if field in payload)
    if len(wrappers) > 1:
        raise RequestValidationError("DataFetcher请求只能使用一个DataRequest入口")
    if not wrappers:
        return payload
    source = payload.pop(wrappers[0])
    if payload:
        raise RequestValidationError(
            "DataFetcher调用信封含未知字段：" + "、".join(sorted(payload))
        )
    if not isinstance(source, Mapping):
        raise RequestValidationError("DataRequest必须为对象")
    return source


def call_tool(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Tool薄适配器；不信任调用方自行声明管理员权限。"""

    payload = dict(request)
    action = str(payload.pop("action", "fetch")).strip().lower()
    if action == "status":
        return capability()
    if action == "catalog":
        return {**capability(), "providers": ["ifind_http", "local", "wind", "ifind_sdk"], "wind": WindProvider.capability(enabled=False)}
    if action == "list_assets":
        return {**capability(), "assets": list_data_assets()}
    if action not in {"fetch", "fetch_calendar"}:
        return {"ok": False, "module": "datafetcher", "status": "failed", "error": {"code": "unsupported_action", "message": "DataFetcher不支持该action"}}
    task_id = payload.pop("task_id", "local")
    try:
        source = _business_request_source(payload)
    except RequestValidationError as error:
        return {
            "ok": False,
            "module": "datafetcher",
            "status": "failed",
            "error": {"code": "validation_error", "message": str(error)},
        }
    if action == "fetch_calendar":
        return fetch_calendar_data(source, task_id=str(task_id)).to_dict()
    return fetch_data(source, task_id=str(task_id)).to_dict()


_APP_RESERVED_KEYS = {"app_context", "caller_context", "tenant_id", "principal_id", "secret_ref", "credential_ref"}
_PLAINTEXT_SECRET_KEYS = {"password", "token", "access_token", "refresh_token", "api_key", "secret", "secret_value", "private_key"}
_HOST_SECRET_PROVIDERS = {"local-secret", "keychain", "credential-manager", "managed-secret"}


def _validate_app_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _APP_RESERVED_KEYS or normalized in _PLAINTEXT_SECRET_KEYS:
                raise RequestValidationError("App请求不得携带调用上下文或明文凭据")
            _validate_app_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_app_payload(item)


def _app_action(request: Mapping[str, Any]) -> str:
    value = request.get("action", "fetch")
    if not isinstance(value, str):
        raise RequestValidationError("App DataFetcher action必须为字符串")
    return value.strip().lower()


def _is_host_secret_ref(value: object) -> bool:
    return isinstance(value, SecretRef) and value.provider.strip().lower() in _HOST_SECRET_PROVIDERS


def _verification_key(caller: CallerContext, secret_ref: SecretRef | None) -> str | None:
    if not _is_host_secret_ref(secret_ref):
        return None
    payload = "\0".join((
        caller.tenant_id,
        caller.principal_id,
        secret_ref.provider,
        secret_ref.key,
        secret_ref.revision or "",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_host_secret_ref(value: object) -> SecretRef:
    if not isinstance(value, SecretRef):
        raise RequestValidationError("App DataFetcher缺少Core SecretRef")
    if not _is_host_secret_ref(value):
        raise RequestValidationError("App SecretRef必须由Host受控凭据系统管理")
    return value


def _validate_host_secret_port(value: object) -> Callable[[SecretRef], str]:
    if not callable(value):
        raise RequestValidationError("App Host未注入受控凭据端口")
    return value


def _app_fetch_requires_secret(source: Mapping[str, Any]) -> bool:
    """仅iFind DataRequest需要Host凭据注入；Local仍由受控路径门禁保护。"""

    try:
        request = DataRequest.from_mapping(source)
    except ValueError as error:
        raise RequestValidationError(str(error)) from error
    providers = tuple(item.strip().lower() for item in request.source_priority if item and item.strip())
    if request.provider and request.provider.strip().lower() not in providers:
        providers = (request.provider.strip().lower(), *providers)
    return not providers or any(provider in {"ifind_http", "ifind_sdk"} for provider in providers)


def call_tool_from_app(
    request: Mapping[str, Any],
    *,
    caller_context: CallerContext,
    secret_ref: SecretRef | None = None,
    secret_port: Callable[[SecretRef], str] | None = None,
    trading_calendar_ref: DataAssetRef | None = None,
) -> Mapping[str, Any]:
    """App薄适配入口；Host可注入已验证交易日历，不进入页面请求。"""

    if not isinstance(request, Mapping):
        raise RequestValidationError("App DataFetcher请求必须为对象")
    if not isinstance(caller_context, CallerContext):
        raise RequestValidationError("App DataFetcher缺少Core CallerContext")
    if not all((caller_context.tenant_id.strip(), caller_context.principal_id.strip(), caller_context.session_id.strip(), caller_context.audience.strip())):
        raise RequestValidationError("App CallerContext不完整")
    if "data:read" not in caller_context.capabilities:
        raise RequestValidationError("App CallerContext无data:read权限")
    _validate_app_payload(request)
    if trading_calendar_ref is not None and not isinstance(trading_calendar_ref, DataAssetRef):
        raise RequestValidationError("App交易日历必须由Host以Core DataAssetRef注入")

    payload = dict(request)
    action = _app_action(payload)
    payload.pop("action", None)
    configured = _is_host_secret_ref(secret_ref) and callable(secret_port)
    verification_key = _verification_key(caller_context, secret_ref)
    with _VERIFIED_CONNECTIONS_LOCK:
        verified = verification_key in _VERIFIED_CONNECTIONS if verification_key is not None else False
    status = capability(app_configured=configured, app_verified=verified)
    if action == "status":
        return status
    if action == "catalog":
        return {**status, "providers": ["ifind_http", "local", "wind", "ifind_sdk"], "wind": WindProvider.capability(enabled=False)}
    if action == "list_assets":
        return {**status, "assets": list_data_assets(caller=caller_context)}
    if action not in {"fetch", "fetch_calendar", "test_connection"}:
        return {"ok": False, "module": "datafetcher", "status": "failed", "error": {"code": "unsupported_action", "message": "DataFetcher不支持该action"}}
    task_id = payload.pop("task_id", "local")
    if action == "test_connection":
        if payload:
            raise RequestValidationError("DataFetcher连接测试不接受数据请求字段")
        source: Mapping[str, Any] = {}
    else:
        source = _business_request_source(payload)
    requires_secret = action in {"fetch_calendar", "test_connection"} or (
        action == "fetch" and _app_fetch_requires_secret(source)
    )
    base_config = DataFetcherConfig.from_runtime()
    if requires_secret:
        config = replace(
            base_config,
            ifind_secret_ref=_validate_host_secret_ref(secret_ref),
            ifind_secret_port=_validate_host_secret_port(secret_port),
            trading_calendar_ref=trading_calendar_ref,
        )
    else:
        config = replace(base_config, trading_calendar_ref=trading_calendar_ref)
    if action == "test_connection":
        def failed_connection(error: ProviderError, status_name: str, detail: str) -> dict[str, Any]:
            if verification_key is not None:
                with _VERIFIED_CONNECTIONS_LOCK:
                    _VERIFIED_CONNECTIONS.discard(verification_key)
            connection: dict[str, Any] = {
                "provider_name": "ifind-http",
                "status": status_name,
                "reason_code": error.reason_code,
                "detail": detail,
            }
            if error.provider_code:
                connection["provider_error_code"] = error.provider_code
            if error.http_status is not None:
                connection["http_status"] = error.http_status
            return {
                **capability(app_configured=True, app_verified=False),
                "connection": connection,
            }

        try:
            IFindHttpProvider().test_connection(config)
        except ProviderDeviceLimitExceeded as error:
            return failed_connection(
                error,
                "device_limit_exceeded",
                "iFind设备或IP使用数量受限，请在iFind侧释放旧设备或更新授权后重试。",
            )
        except ProviderAccountPermissionDenied as error:
            return failed_connection(
                error,
                "account_permission_denied",
                "iFind账号无权完成Token验证，请确认账号状态和QuantAPI权限。",
            )
        except ProviderFieldPermissionDenied as error:
            return failed_connection(
                error,
                "field_permission_denied",
                "iFind账号缺少连接验证所需权限，请联系数据管理员确认授权。",
            )
        except ProviderUnauthorized as error:
            return failed_connection(
                error,
                "unauthorized",
                "iFind Refresh Token无效或已失效，请重新保存当前Refresh Token后重试。",
            )
        except ProviderQuotaExceeded as error:
            return failed_connection(error, "quota_exceeded", "iFind调用额度或频次受限，请稍后重试。")
        except ProviderUnavailable as error:
            return failed_connection(error, "unavailable", "iFind连接暂不可用，请检查网络或服务状态后重试。")
        if verification_key is not None:
            with _VERIFIED_CONNECTIONS_LOCK:
                _VERIFIED_CONNECTIONS.add(verification_key)
        return {
            **capability(app_configured=True, app_verified=True),
            "connection": {
                "provider_name": "ifind-http",
                "status": "available",
                "detail": "iFind凭据验证通过，可用于数据请求。",
            },
        }
    result = (
        fetch_calendar_data(source, caller_context, config=config, task_id=str(task_id))
        if action == "fetch_calendar"
        else fetch_data(source, caller_context, config=config, task_id=str(task_id))
    ).to_dict()
    reference = result.get("data_asset_ref")
    if result.get("ok") is True and isinstance(reference, Mapping) and dict(reference.get("lineage", {})).get("persistence_mode") == "volatile":
        volatile = read_volatile_asset(str(reference.get("data_asset_id", "")), caller_context)
        if volatile is not None:
            result["_volatile_payload_b64"] = base64.b64encode(volatile[1]).decode("ascii")
    return result
