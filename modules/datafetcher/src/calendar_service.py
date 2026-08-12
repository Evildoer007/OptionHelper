"""iFind中国交易日历资产的获取、校验与受控缓存。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping

from runtime.adapters.local_store import LocalDataStore, StoreError
from runtime.protocol.models import CallerContext, DataAssetRef

from .cache_resolver import _process_lock
from .config import DataFetcherConfig
from .market_conventions import UnsupportedChinaAsset, china_market_convention
from .models import CalendarRequest
from .providers import IFindHttpProvider
from .providers.base import ProviderError, ProviderQuotaExceeded, ProviderUnavailable


CALENDAR_SCHEMA = "trading-calendar"
_INDEX_LOCKS: dict[str, threading.RLock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


class CalendarValidationError(ValueError):
    code = "calendar_validation_error"


def _ref_from_mapping(value: Mapping[str, Any]) -> DataAssetRef:
    return DataAssetRef(
        data_asset_id=str(value["data_asset_id"]),
        storage_ref=str(value["storage_ref"]),
        media_type=str(value["media_type"]),
        schema_id=str(value["schema_id"]),
        asset_ids=tuple(str(item) for item in value["asset_ids"]),
        normalized_fields=tuple(str(item) for item in value["normalized_fields"]),
        coverage=dict(value["coverage"]),
        row_count=int(value["row_count"]),
        price_convention=dict(value["price_convention"]),
        content_hash=str(value["content_hash"]),
        lineage=dict(value["lineage"]),
        tenant_id=str(value.get("tenant_id", "local")),
        created_by=str(value.get("created_by", "local")),
        access_scope=tuple(str(item) for item in value.get("access_scope", ("read",))),
        partition_spec=dict(value.get("partition_spec", {})),
    )


def validate_calendar_request(value: CalendarRequest | Mapping[str, Any], config: DataFetcherConfig) -> CalendarRequest:
    try:
        request = value if isinstance(value, CalendarRequest) else CalendarRequest.from_mapping(value)
    except (TypeError, ValueError) as error:
        raise CalendarValidationError(str(error)) from error
    if not request.asset_ids or len(request.asset_ids) > 8 or any(not item for item in request.asset_ids):
        raise CalendarValidationError("交易日历必须提供1至8个中国市场标的")
    if len(set(request.asset_ids)) != len(request.asset_ids):
        raise CalendarValidationError("交易日历asset_ids不得重复")
    try:
        start = date.fromisoformat(request.start_date)
        end = date.fromisoformat(request.end_date)
    except ValueError as error:
        raise CalendarValidationError("交易日历日期必须为YYYY-MM-DD") from error
    if start.isoformat() != request.start_date or end.isoformat() != request.end_date:
        raise CalendarValidationError("交易日历日期必须为YYYY-MM-DD")
    if end < start:
        raise CalendarValidationError("交易日历start_date不得晚于end_date")
    if (end - start).days > config.max_span_days:
        raise CalendarValidationError("交易日历请求区间超过Host允许上限")
    if request.quota_limit is not None and request.quota_limit < 0:
        raise CalendarValidationError("交易日历quota_limit不能为负数")
    try:
        for asset_id in request.asset_ids:
            china_market_convention(asset_id)
    except UnsupportedChinaAsset as error:
        raise CalendarValidationError(str(error)) from error
    return request


def _validate_exchange_sessions(
    values: tuple[str, ...], *, exchange: str, start_date: str, end_date: str,
) -> tuple[str, ...]:
    parsed: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise CalendarValidationError(f"{exchange}交易日必须为YYYY-MM-DD")
        try:
            canonical = date.fromisoformat(value).isoformat()
        except ValueError as error:
            raise CalendarValidationError(f"{exchange}交易日必须为YYYY-MM-DD") from error
        if canonical != value:
            raise CalendarValidationError(f"{exchange}交易日必须为YYYY-MM-DD")
        if not start_date <= canonical <= end_date:
            raise CalendarValidationError(f"{exchange}交易日超出请求区间")
        parsed.append(canonical)
    result = tuple(parsed)
    if not result:
        raise CalendarValidationError(f"{exchange}交易日历为空")
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise CalendarValidationError(f"{exchange}交易日必须严格递增且不重复")
    return result


def _canonical_payload(
    request: CalendarRequest,
    sessions_by_exchange: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, Any], bytes, str]:
    exchanges = tuple(sorted(sessions_by_exchange))
    intersection = tuple(sorted(set.intersection(*(set(sessions_by_exchange[item]) for item in exchanges))))
    if not intersection:
        raise CalendarValidationError("多交易所交易日交集为空")
    asset_exchange = {
        asset_id: str(china_market_convention(asset_id)["exchange"])
        for asset_id in request.asset_ids
    }
    payload = {
        "schema_id": CALENDAR_SCHEMA,
        "asset_ids": list(request.asset_ids),
        "asset_exchange": asset_exchange,
        "exchanges": list(exchanges),
        "requested_start_date": request.start_date,
        "requested_end_date": request.end_date,
        "sessions": list(intersection),
        "sessions_by_exchange": {key: list(sessions_by_exchange[key]) for key in exchanges},
        "provider": "ifind_http",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, encoded, hashlib.sha256(encoded).hexdigest()


def _calendar_identity(request: CalendarRequest, tenant_id: str) -> str:
    exchanges = sorted({str(china_market_convention(item)["exchange"]) for item in request.asset_ids})
    encoded = json.dumps({
        "tenant_id": tenant_id,
        "asset_ids": sorted(request.asset_ids),
        "exchanges": exchanges,
        "schema_id": CALENDAR_SCHEMA,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


class CalendarCache:
    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / "calendar-index.json"
        with _INDEX_LOCKS_GUARD:
            self._lock = _INDEX_LOCKS.setdefault(str(root.resolve()), threading.RLock())

    @contextmanager
    def _guard(self):
        with self._lock:
            with _process_lock(self.root / ".calendar-index.lock"):
                yield

    @contextmanager
    def fetch_lock(self, identity: str):
        """以日历请求身份串行Provider、缓存查找和写入。"""

        with _INDEX_LOCKS_GUARD:
            lock = _INDEX_LOCKS.setdefault(
                str((self.root / ".calendar-request-locks" / identity).resolve()),
                threading.RLock(),
            )
        with lock:
            with _process_lock(self.root / ".calendar-request-locks" / f"{identity}.lock"):
                yield

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"entries": {}}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CalendarValidationError("交易日历缓存索引损坏") from error
        if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
            raise CalendarValidationError("交易日历缓存索引结构无效")
        return value

    def save(self, request: CalendarRequest, caller: CallerContext, payload: bytes, ref: DataAssetRef) -> None:
        identity = _calendar_identity(request, caller.tenant_id)
        relative = f"calendars/{ref.content_hash}.json"
        with self._guard():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(payload)
            index = self._read_index()
            owner = hashlib.sha256(caller.principal_id.encode("utf-8")).hexdigest()[:12]
            index["entries"][f"{identity}:{ref.content_hash}:{owner}"] = {
                "calendar_identity": identity,
                "payload_path": relative,
                "data_asset_ref": asdict(ref),
                "tenant_id": caller.tenant_id,
                "provider": "ifind_http",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(self.index_path, index)

    def list_assets(self, *, tenant_id: str, limit: int = 100) -> list[Mapping[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._guard():
            records = list(self._read_index()["entries"].values())
        assets: list[Mapping[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping) or record.get("tenant_id") != tenant_id:
                continue
            reference = record.get("data_asset_ref")
            if isinstance(reference, Mapping):
                assets.append({
                    "data_asset_ref": dict(reference),
                    "provider": "ifind_http",
                    "updated_at": record.get("updated_at"),
                })
        return sorted(assets, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:safe_limit]

    def find_asset(self, *, tenant_id: str, data_asset_id: str) -> Mapping[str, Any] | None:
        with self._guard():
            records = list(self._read_index()["entries"].values())
        for record in records:
            if not isinstance(record, Mapping) or record.get("tenant_id") != tenant_id:
                continue
            reference = record.get("data_asset_ref")
            if isinstance(reference, Mapping) and reference.get("data_asset_id") == data_asset_id:
                return {
                    "data_asset_ref": dict(reference),
                    "provider": "ifind_http",
                    "updated_at": record.get("updated_at"),
                }
        return None

    def lookup(self, request: CalendarRequest, caller: CallerContext, store: LocalDataStore) -> tuple[DataAssetRef, bytes] | None:
        identity = _calendar_identity(request, caller.tenant_id)
        with self._guard():
            entries = self._read_index()["entries"]
            records = [
                record
                for key, record in entries.items()
                if isinstance(record, Mapping)
                and record.get("tenant_id") == caller.tenant_id
                and (record.get("calendar_identity") == identity or key == identity)
            ]
            records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
            for record in records:
                resolved = self._resolve_cached_record(record, request, caller, store)
                if resolved is not None:
                    return resolved
            return None

    def _resolve_cached_record(
        self,
        record: Mapping[str, Any],
        request: CalendarRequest,
        caller: CallerContext,
        store: LocalDataStore,
    ) -> tuple[DataAssetRef, bytes] | None:
        reference = record.get("data_asset_ref")
        relative = record.get("payload_path")
        if not isinstance(reference, Mapping) or not isinstance(relative, str):
            return None
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents or not path.is_file():
            return None
        try:
            ref = _ref_from_mapping(reference)
            payload = path.read_bytes()
            document = json.loads(payload)
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            return None
        if ref.schema_id != CALENDAR_SCHEMA or hashlib.sha256(payload).hexdigest() != ref.content_hash:
            return None
        coverage = dict(ref.coverage)
        if coverage.get("start_date") > request.start_date or coverage.get("end_date") < request.end_date:
            return None
        if set(ref.asset_ids) != set(request.asset_ids) or not isinstance(document, Mapping):
            return None
        try:
            store.resolve(ref, tenant_id=caller.tenant_id)
        except (FileNotFoundError, PermissionError, StoreError):
            return None
        return ref, payload


def _store_reference(
    request: CalendarRequest,
    caller: CallerContext,
    config: DataFetcherConfig,
    payload: Mapping[str, Any],
    encoded: bytes,
    content_hash: str,
) -> DataAssetRef:
    sessions = tuple(str(item) for item in payload["sessions"])
    by_exchange = {
        exchange: {
            "start_date": values[0],
            "end_date": values[-1],
            "row_count": len(values),
        }
        for exchange, raw in dict(payload["sessions_by_exchange"]).items()
        if (values := tuple(str(item) for item in raw))
    }
    coverage = {
        "start_date": request.start_date,
        "end_date": request.end_date,
        "sessions": list(sessions),
        "calendar_id": "CN-" + "+".join(payload["exchanges"]),
        "calendar_version": content_hash[:16],
        "by_exchange": by_exchange,
    }
    store = LocalDataStore(Path(config.data_root))
    owner = hashlib.sha256(caller.principal_id.encode("utf-8")).hexdigest()[:12]
    data_asset_id = f"calendar-{content_hash}-ifind-o-{owner}"
    try:
        return store.put_bytes(
            tenant_id=caller.tenant_id,
            data_asset_id=data_asset_id,
            payload=encoded,
            media_type="application/json",
            schema_id=CALENDAR_SCHEMA,
            asset_ids=request.asset_ids,
            normalized_fields=("session",),
            coverage=coverage,
            row_count=len(sessions),
            partition_spec={"date_field": "session", "frequency": "1d", "exchange_intersection": True},
            price_convention={
                "timezone": "Asia/Shanghai",
                "date_type": "trade",
                "period": "D",
                "exchanges": list(payload["exchanges"]),
                "contains_market_prices": False,
            },
            lineage={
                "provider": "ifind_http",
                "endpoint": "get_trade_dates",
                "request_hash": hashlib.sha256(json.dumps(request.public_dict(), sort_keys=True).encode("utf-8")).hexdigest(),
                "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
            created_by=caller.principal_id,
            access_scope=("read",),
        )
    except FileExistsError:
        tenant_root = store.root if caller.tenant_id == "local" else store.root / "tenants" / caller.tenant_id
        manifest = json.loads((tenant_root / "assets" / data_asset_id / "manifest.json").read_text(encoding="utf-8"))
        ref = _ref_from_mapping({**manifest["metadata"], "storage_ref": manifest["storage_ref"]})
        store.resolve(ref, tenant_id=caller.tenant_id)
        return ref


def _cached_reference(
    request: CalendarRequest,
    caller: CallerContext,
    config: DataFetcherConfig,
    cache: CalendarCache,
    cached: tuple[DataAssetRef, bytes],
) -> tuple[DataAssetRef, Mapping[str, Any]]:
    ref, encoded = cached
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CalendarValidationError("交易日历缓存内容无效") from error
    if not isinstance(payload, Mapping):
        raise CalendarValidationError("交易日历缓存内容无效")
    if ref.created_by != caller.principal_id or "read" not in ref.access_scope:
        ref = _store_reference(
            request,
            caller,
            config,
            payload,
            encoded,
            hashlib.sha256(encoded).hexdigest(),
        )
        cache.save(request, caller, encoded, ref)
    return ref, payload


def fetch_calendar_asset(
    request_value: CalendarRequest | Mapping[str, Any],
    caller: CallerContext,
    config: DataFetcherConfig,
) -> tuple[CalendarRequest, DataAssetRef, Mapping[str, Any], str, tuple[Mapping[str, Any], ...]]:
    """优先调用iFind；仅在Provider失败时复用完整覆盖的验证缓存。"""

    if "data:read" not in caller.capabilities:
        raise CalendarValidationError("当前CallerContext无data:read权限")
    request = validate_calendar_request(request_value, config)
    store = LocalDataStore(Path(config.data_root))
    cache = CalendarCache(Path(config.cache_root))
    calls: list[Mapping[str, Any]] = []
    identity = _calendar_identity(request, caller.tenant_id)
    # 默认仍优先向iFind刷新。仅当并发同请求刚刚填充缓存时，等待者复用该结果。
    cached_before = cache.lookup(request, caller, store)
    with cache.fetch_lock(identity):
        cached_after = cache.lookup(request, caller, store)
        if cached_before is None and cached_after is not None:
            ref, payload = _cached_reference(request, caller, config, cache, cached_after)
            decision = "verified_cache_reused"
        else:
            expected = sorted({str(china_market_convention(item)["exchange"]) for item in request.asset_ids})
            estimated_units = len(expected)
            limit = request.quota_limit if request.quota_limit is not None else config.max_provider_units
            try:
                if estimated_units > limit:
                    raise ProviderQuotaExceeded("交易日历请求超过DataFetcher额度保护上限")
                raw_by_exchange = IFindHttpProvider().fetch_calendar(request, config)
                if not isinstance(raw_by_exchange, Mapping):
                    raise CalendarValidationError("iFind交易日历返回结构无效")
                validated = {
                    exchange: _validate_exchange_sessions(
                        tuple(values) if isinstance(values, (tuple, list)) and not isinstance(values, (str, bytes)) else (),
                        exchange=exchange, start_date=request.start_date, end_date=request.end_date,
                    )
                    for exchange, values in raw_by_exchange.items()
                    if isinstance(exchange, str) and isinstance(values, (tuple, list)) and not isinstance(values, (str, bytes))
                }
                if len(validated) != len(raw_by_exchange):
                    raise CalendarValidationError("iFind交易日历返回结构无效")
                if sorted(validated) != expected:
                    raise CalendarValidationError("iFind交易日历未覆盖请求的全部交易所")
                payload, encoded, content_hash = _canonical_payload(request, validated)
                ref = _store_reference(request, caller, config, payload, encoded, content_hash)
                cache.save(request, caller, encoded, ref)
                calls.append({"provider": "ifind_http", "endpoint": "get_trade_dates", "outcome": "succeeded", "quota_units": estimated_units})
                decision = "provider_fetched"
            except (ProviderError, CalendarValidationError) as error:
                calls.append({"provider": "ifind_http", "endpoint": "get_trade_dates", "outcome": error.code, "quota_units": 0})
                cached = cache.lookup(request, caller, store)
                if cached is None:
                    if isinstance(error, CalendarValidationError):
                        raise
                    raise ProviderUnavailable("交易日历暂不可用，且没有完整覆盖本次区间的已验证缓存") from error
                ref, payload = _cached_reference(request, caller, config, cache, cached)
                decision = "verified_cache_fallback"
    quality = {
        "schema_id": CALENDAR_SCHEMA,
        "status": "verified",
        "contains_market_prices": False,
        "strictly_increasing": True,
        "unique": True,
        "within_requested_range": True,
        "exchanges": list(payload["exchanges"]),
        "session_count": len(payload["sessions"]),
    }
    return request, ref, quality, decision, tuple(calls)


__all__ = (
    "CALENDAR_SCHEMA",
    "CalendarCache",
    "CalendarValidationError",
    "fetch_calendar_asset",
    "validate_calendar_request",
)
