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
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping
from uuid import uuid4

import pandas as pd

from runtime.adapters.local_store import LocalDataStore, StoreError
from runtime.bootstrap import bootstrap_runtime

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
from .providers.base import ProviderError, ProviderQuotaExceeded, ProviderUnauthorized, ProviderUnavailable
from .quality_validator import DataQualityError, validate_daily_history
from .request_validator import (
    RequestValidationError,
    cache_identity,
    latest_observable_market_date,
    request_fingerprint,
    validate_request,
)


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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _quota_limit(request: DataRequest | CalendarRequest, config: DataFetcherConfig) -> int:
    return request.quota_limit if request.quota_limit is not None else config.max_provider_units


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
    if calendar_evidence is None:
        sessions = sorted(str(value) for value in frame["date"].dropna().unique())
        calendar_id = "UNVERIFIED-" + "+".join(exchanges)
        calendar_revision = "unverified"
    else:
        sessions = sorted({session for values in calendar_evidence.dates_by_asset.values() for session in values})
        calendar_id = calendar_evidence.calendar_id
        calendar_revision = calendar_evidence.calendar_revision
    coverage = {
        # 正式覆盖只声明已观测数据；请求区间另存，不能把未验证日期伪装成覆盖。
        "start_date": str(frame["date"].min()),
        "end_date": str(frame["date"].max()),
        "requested_start_date": request.start_date,
        "requested_end_date": request.end_date,
        "sessions": sessions,
        "calendar_id": calendar_id,
        "calendar_revision": calendar_revision,
        "by_asset": by_asset,
    }
    if calendar_evidence is not None and calendar_evidence.calendar_ref:
        coverage["calendar_ref"] = dict(calendar_evidence.calendar_ref)
    return coverage


def _expected_trading_dates(
    request: DataRequest,
    config: DataFetcherConfig,
    caller: CallerContext,
) -> VerifiedCalendarEvidence | None:
    """只接受带身份、版本和完整覆盖声明的显式交易所日历。"""

    if config.trading_calendar_ref is not None:
        return calendar_evidence_for_history(config.trading_calendar_ref, request, caller, config)
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
    """同一原始缓存内按主体和日历证据选择语义正确的DataAssetRef。"""

    evidence = request.calendar_evidence_identity or "unverified"
    return f"{_owner_partition(caller.principal_id)}:{evidence}"


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
) -> DataAssetRef:
    content_hash = daily_content_hash(frame)
    conventions = market_conventions(request.asset_ids, request.adjustment)
    coverage = _coverage(frame, request, calendar_evidence)
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
        "owner": _owner_partition(caller.principal_id),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    data_asset_id = f"data-{identity}-{provider}-o-{_owner_partition(caller.principal_id)}"
    store = LocalDataStore(Path(config.data_root or RUNTIME_PATHS.data_root))
    lineage: dict[str, Any] = {
        "provider": provider,
        "request_hash": request_hash,
        "cache_decision": cache_decision,
        "provider_calls": [dict(item) for item in provider_calls],
        "local_source_fingerprint": request.local_source_fingerprint if provider == "local" else None,
        "normalizer_version": "market-history",
        "fetched_at": _now(),
    }
    if calendar_evidence is not None and calendar_evidence.calendar_ref:
        lineage["trading_calendar_ref"] = dict(calendar_evidence.calendar_ref)
    try:
        return store.put_bytes(
            tenant_id=caller.tenant_id,
            data_asset_id=data_asset_id,
            payload=daily_csv_bytes(frame),
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
            artifacts["data_asset_ref.json"] = asdict(run.data_asset_ref)
        if quality is not None:
            artifacts["quality_report.json"] = dict(quality)
        if run.error is not None:
            artifacts["error.json"] = dict(run.error)
        for filename, value in artifacts.items():
            (staging / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _fetch_provider(
    request: DataRequest,
    config: DataFetcherConfig,
    names: tuple[str, ...],
    calls: list[dict[str, Any]],
    quota_used: int,
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
            calls.append({"provider": name, "outcome": "quota_exceeded", "quota_units": units})
            last_error = ProviderQuotaExceeded("请求超过DataFetcher额度保护上限")
            if protected_error is None:
                protected_error = last_error
            continue
        try:
            frame = provider.fetch(request, config)
            quota_used += units
            normalized = normalize_daily_history(
                frame,
                request,
                latest_observable_date=latest_observable_market_date(config),
            )
            calls.append({"provider": name, "outcome": "succeeded", "quota_units": units})
            return name, normalized, quota_used
        except ProviderError as error:
            quota_used += units
            calls.append({"provider": name, "outcome": error.code, "quota_units": units})
            last_error = error
            if protected_error is None and isinstance(error, (ProviderUnauthorized, ProviderQuotaExceeded)):
                protected_error = error
        except (DataNormalizationError, DataQualityError) as error:
            calls.append({"provider": name, "outcome": getattr(error, "code", "normalization_error"), "quota_units": units})
            last_error = error
    if config.offline or request.offline:
        raise OfflineMiss("离线模式下缓存或本地CSV未满足请求")
    if protected_error is not None:
        raise protected_error
    if last_error is not None:
        if isinstance(last_error, ProviderError):
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

    identities = sorted({cache_identity(request, provider, tenant_id=caller.tenant_id) for provider in request.source_priority})
    with ExitStack() as locks:
        for identity in identities:
            locks.enter_context(cache.fetch_lock(identity))

        calendar_evidence = _expected_trading_dates(request, config, caller)
        expected_trading_dates = calendar_evidence.dates_by_asset if calendar_evidence is not None else None
        latest_observable_date = latest_observable_market_date(config)
        data_asset_ref_key = _data_asset_ref_key(caller, request)
        matches = [] if request.cache_policy == "force_refresh" else [
            cache.lookup(
                request,
                name,
                tenant_id=caller.tenant_id,
                expected_trading_dates=expected_trading_dates,
                data_asset_ref_key=data_asset_ref_key,
            )
            for name in request.source_priority
        ]
        full_match = next((match for match in matches if match.complete and match.frame is not None and match.metadata), None)
        if full_match is not None:
            reference = full_match.metadata.get("data_asset_ref")
            asset_ref: DataAssetRef | None = _asset_ref(reference) if isinstance(reference, Mapping) else None
            if asset_ref is not None:
                try:
                    LocalDataStore(Path(config.data_root or RUNTIME_PATHS.data_root)).resolve(asset_ref, tenant_id=caller.tenant_id)
                except (FileNotFoundError, PermissionError, StoreError):
                    asset_ref = None
                    invalid_match = full_match
                else:
                    invalid_match = None
            else:
                invalid_match = None
            quality = validate_daily_history(
                full_match.frame, request.fields, start_date=request.start_date, end_date=request.end_date,
                expected_trading_dates=expected_trading_dates,
                latest_observable_date=latest_observable_date,
            )
            if asset_ref is not None:
                same_owner = asset_ref.created_by == caller.principal_id and "read" in asset_ref.access_scope
                if same_owner and _asset_uses_calendar_evidence(asset_ref, calendar_evidence):
                    return asset_ref, quality, "cache_hit", full_match.frame
            rebound_decision = "cache_rebound" if asset_ref is None or asset_ref.created_by != caller.principal_id else "cache_revalidated"
            rebound_ref = _asset_reference(
                full_match.frame,
                request,
                config,
                full_match.provider,
                request_hash,
                rebound_decision,
                calls,
                caller,
                calendar_evidence,
            )
            cache.save(
                cache_identity(request, full_match.provider, tenant_id=caller.tenant_id),
                full_match.frame,
                {
                    "content_hash": rebound_ref.content_hash,
                    "provider": full_match.provider,
                    "data_asset_ref": asdict(rebound_ref),
                    "updated_at": _now(),
                    "tenant_id": caller.tenant_id,
                },
                data_asset_ref_key=data_asset_ref_key,
            )
            return rebound_ref, quality, rebound_decision, full_match.frame
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
        for interval in interval_requests:
            provider, frame, quota_usage["used"] = _fetch_provider(
                interval,
                config,
                _provider_order(interval, cached_provider),
                calls,
                quota_usage["used"],
            )
            if selected_provider is not None and provider != selected_provider:
                raise DataFetcherError("不同Provider数据不得自动混合")
            selected_provider = provider
            fetched_frames.append(frame)
        frame = merge_daily_history(cached_frame, *fetched_frames)
        quality = validate_daily_history(
            frame, request.fields, start_date=request.start_date, end_date=request.end_date,
            expected_trading_dates=expected_trading_dates,
            latest_observable_date=latest_observable_date,
        )
        cache_decision = "cache_extended" if cached_frame is not None else "cache_miss_fetched"
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
        )
        cache.save(
            cache_identity(request, selected_provider or "local", tenant_id=caller.tenant_id),
            frame,
            {
                "content_hash": asset_ref.content_hash,
                "provider": selected_provider,
                "data_asset_ref": asdict(asset_ref),
                "updated_at": _now(),
                "tenant_id": caller.tenant_id,
            },
            data_asset_ref_key=data_asset_ref_key,
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
        error={"code": code, "message": str(error) if isinstance(error, (RequestValidationError, CalendarValidationError, ProviderError, DataFetcherError, DataQualityError, CacheIndexError)) else "DataFetcher内部错误"},
        tenant_id=caller.tenant_id,
    )
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
        asset_ref, quality, cache_decision, frame = _resolve_data(cache, validated, effective_config, request_hash, calls, quota_usage, caller)
        run = DataFetchRun(
            data_fetch_run_id=run_id,
            task_id=task_id,
            status="complete",
            request_hash=request_hash,
            cache_decision=cache_decision,
            provider_calls=tuple(calls),
            quota_usage={"used": quota_usage["used"], "limit": _quota_limit(validated, effective_config)},
            data_asset_ref=asset_ref,
            tenant_id=caller.tenant_id,
        )
        run = _persist_run(effective_config, validated, run, quality)
        preview = tuple(
            {column: (None if pd.isna(value) else value) for column, value in row.items()}
            for row in frame.head(12).to_dict(orient="records")
        )
        return DataFetchResult(ok=True, run=run, quality_report=quality, data_preview=preview)
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


def capability(*, app_configured: bool | None = None) -> dict[str, Any]:
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
        result.update({
            "configured": app_configured,
            "credentials": "app_host_secret_ref_only",
            "credential_status": {
                "configured": app_configured,
                "state": "configured_unverified" if app_configured else "not_configured",
                "source": "app_host_secret_ref" if app_configured else "none",
                "remote_provider": "not_checked" if app_configured else "not_configured",
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
    store = LocalDataStore(Path(config.data_root))
    history = cache.list_assets(tenant_id=effective_caller.tenant_id, limit=limit)
    calendars = CalendarCache(Path(config.cache_root)).list_assets(
        tenant_id=effective_caller.tenant_id, limit=limit,
    )
    combined = []
    for item in history + calendars:
        reference = item.get("data_asset_ref") if isinstance(item, Mapping) else None
        if not isinstance(reference, Mapping):
            continue
        try:
            ref = _asset_ref(reference)
            _assert_asset_readable(ref, effective_caller)
            store.resolve(ref, tenant_id=effective_caller.tenant_id)
        except (FileNotFoundError, StoreError, KeyError, TypeError, ValueError, PermissionError):
            continue
        combined.append(item)
    safe_limit = max(1, min(int(limit), 100))
    return sorted(combined, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:safe_limit]


def read_data_asset(data_asset_id: str, *, caller: CallerContext | None = None) -> tuple[DataAssetRef, bytes]:
    """按缓存索引中的DataAssetRef读取受控数据本体，供本机页面下载。"""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", data_asset_id):
        raise DataFetcherError("DataAsset标识非法")
    effective_caller = caller or CallerContext("local", "local-user", "local", ("data:read",), "local", "local")
    _assert_data_read(effective_caller)
    config, cache = _runtime_cache()
    item = cache.find_asset(tenant_id=effective_caller.tenant_id, data_asset_id=data_asset_id)
    if item is not None:
        ref = _asset_ref(item["data_asset_ref"])
        _assert_asset_readable(ref, effective_caller)
        try:
            return ref, LocalDataStore(Path(config.data_root)).read_bytes(ref, tenant_id=effective_caller.tenant_id)
        except (FileNotFoundError, StoreError) as error:
            raise FileNotFoundError("DataAsset不存在或完整性校验失败") from error
    calendar_item = CalendarCache(Path(config.cache_root)).find_asset(
        tenant_id=effective_caller.tenant_id,
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
    source = payload.pop("request", payload.pop("data_request", payload))
    if not isinstance(source, Mapping):
        return {"ok": False, "module": "datafetcher", "status": "failed", "error": {"code": "validation_error", "message": "DataRequest必须为对象"}}
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


def call_tool_from_app(
    request: Mapping[str, Any],
    *,
    caller_context: CallerContext,
    secret_ref: SecretRef | None = None,
    secret_port: Callable[[SecretRef], str] | None = None,
) -> Mapping[str, Any]:
    """App薄适配入口；只读动作不要求凭据，fetch必须使用Host SecretRef。"""

    if not isinstance(request, Mapping):
        raise RequestValidationError("App DataFetcher请求必须为对象")
    if not isinstance(caller_context, CallerContext):
        raise RequestValidationError("App DataFetcher缺少Core CallerContext")
    if not all((caller_context.tenant_id.strip(), caller_context.principal_id.strip(), caller_context.session_id.strip(), caller_context.audience.strip())):
        raise RequestValidationError("App CallerContext不完整")
    if "data:read" not in caller_context.capabilities:
        raise RequestValidationError("App CallerContext无data:read权限")
    _validate_app_payload(request)

    payload = dict(request)
    action = _app_action(payload)
    payload.pop("action", None)
    configured = _is_host_secret_ref(secret_ref) and callable(secret_port)
    status = capability(app_configured=configured)
    if action == "status":
        return status
    if action == "catalog":
        return {**status, "providers": ["ifind_http", "local", "wind", "ifind_sdk"], "wind": WindProvider.capability(enabled=False)}
    if action == "list_assets":
        return {**status, "assets": list_data_assets(caller=caller_context)}
    if action not in {"fetch", "fetch_calendar", "test_connection"}:
        return {"ok": False, "module": "datafetcher", "status": "failed", "error": {"code": "unsupported_action", "message": "DataFetcher不支持该action"}}
    managed_secret_ref = _validate_host_secret_ref(secret_ref)
    managed_secret_port = _validate_host_secret_port(secret_port)
    config = replace(
        DataFetcherConfig.from_runtime(),
        ifind_secret_ref=managed_secret_ref,
        ifind_secret_port=managed_secret_port,
    )
    if action == "test_connection":
        try:
            IFindHttpProvider().test_connection(config)
        except ProviderUnauthorized:
            return {
                **status,
                "connection": {
                    "provider_name": "ifind-http",
                    "status": "unauthorized",
                    "detail": "iFind凭据验证失败，请在设置中心重新保存Refresh Token后重试。",
                },
            }
        except ProviderUnavailable:
            return {
                **status,
                "connection": {
                    "provider_name": "ifind-http",
                    "status": "unavailable",
                    "detail": "iFind连接暂不可用，请检查网络后重试。",
                },
            }
        return {
            **status,
            "connection": {
                "provider_name": "ifind-http",
                "status": "available",
                "detail": "iFind凭据验证通过，可用于数据请求。",
            },
        }
    task_id = payload.pop("task_id", "local")
    source = payload.pop("request", payload.pop("data_request", payload))
    if not isinstance(source, Mapping):
        raise RequestValidationError("DataRequest必须为对象")
    if action == "fetch_calendar":
        return fetch_calendar_data(source, caller_context, config=config, task_id=str(task_id)).to_dict()
    return fetch_data(source, caller_context, config=config, task_id=str(task_id)).to_dict()
