"""iFind中国交易日历资产的获取、校验与受控缓存。"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping, Sequence

from runtime.adapters.local_store import LocalDataStore, StoreError
from runtime.contracts.contract_types import deep_thaw
from runtime.protocol.models import CallerContext, DataAssetRef

from .cache_resolver import _process_lock, _temporary_lock_path, missing_intervals
from .config import DataFetcherConfig
from .market_conventions import UnsupportedChinaAsset, china_market_convention
from .models import CalendarRequest, DataRequest
from .providers import IFindHttpProvider
from .providers.base import (
    ProviderError,
    ProviderFieldPermissionDenied,
    ProviderInputError,
    ProviderMarketPermissionDenied,
    ProviderQuotaExceeded,
    ProviderUnauthorized,
    ProviderUnavailable,
)
from .volatile_store import read_volatile_asset, store_volatile_asset


CALENDAR_SCHEMA = "trading-calendar"
_INDEX_LOCKS: dict[str, threading.RLock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()
_VOLATILE_CALENDARS: dict[tuple[str, str], dict[str, Any]] = {}
_VOLATILE_CALENDARS_LOCK = threading.RLock()


class CalendarValidationError(ValueError):
    code = "calendar_validation_error"


@dataclass(frozen=True)
class VerifiedCalendarEvidence:
    """历史行情质量使用的已登记日历证据，不复制Core DataAssetRef。"""

    dates_by_asset: Mapping[str, tuple[str, ...]]
    calendar_id: str
    calendar_revision: str
    calendar_ref: Mapping[str, str]


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
    if request.persistence_mode not in {"volatile", "library"}:
        raise CalendarValidationError("交易日历persistence_mode必须为volatile或library")
    try:
        for asset_id in request.asset_ids:
            china_market_convention(asset_id)
    except UnsupportedChinaAsset as error:
        raise CalendarValidationError(str(error)) from error
    return request


def _validate_exchange_sessions(
    values: tuple[str, ...],
    *,
    exchange: str,
    start_date: str,
    end_date: str,
    allow_empty: bool = False,
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
    if not result and not allow_empty:
        raise CalendarValidationError(f"{exchange}交易日历为空")
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise CalendarValidationError(f"{exchange}交易日必须严格递增且不重复")
    return result


def _coverage_dates(value: Mapping[str, Any]) -> tuple[str, str]:
    try:
        start = date.fromisoformat(str(value["start_date"])).isoformat()
        end = date.fromisoformat(str(value["end_date"])).isoformat()
    except (KeyError, TypeError, ValueError) as error:
        raise CalendarValidationError("交易日历DataAssetRef覆盖日期无效") from error
    if start > end:
        raise CalendarValidationError("交易日历DataAssetRef覆盖日期无效")
    return start, end


def calendar_evidence_for_history(
    ref: DataAssetRef,
    request: DataRequest,
    caller: CallerContext,
    config: DataFetcherConfig,
    *,
    fallback_before: str | None = None,
) -> VerifiedCalendarEvidence:
    """从受控日历资产读取可消费的历史质量证据。

    只接受与当前租户、主体、标的和请求区间一致的Core DataAssetRef；没有该证据
    时历史行情保持unverified，绝不以工作日代替交易所日历。
    """

    if not isinstance(ref, DataAssetRef):
        raise CalendarValidationError("trading_calendar_ref必须是受控DataAssetRef")
    if ref.schema_id != CALENDAR_SCHEMA or ref.media_type != "application/json" or ref.normalized_fields != ("session",):
        raise CalendarValidationError("trading_calendar_ref必须是trading-calendar application/json")
    if ref.tenant_id != caller.tenant_id or ref.created_by != caller.principal_id or "read" not in ref.access_scope:
        raise CalendarValidationError("trading_calendar_ref无当前CallerContext读取权限")
    if set(ref.asset_ids) != set(request.asset_ids):
        raise CalendarValidationError("trading_calendar_ref必须逐一覆盖历史行情标的")
    coverage = dict(ref.coverage)
    coverage_start, coverage_end = _coverage_dates(coverage)
    if coverage_start > request.start_date or coverage_end < request.end_date:
        raise CalendarValidationError("trading_calendar_ref未完整覆盖历史行情请求区间")
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    if not isinstance(calendar_id, str) or not calendar_id.startswith("CN-") or not isinstance(calendar_revision, str) or not calendar_revision:
        raise CalendarValidationError("trading_calendar_ref缺少已验证中国交易所日历身份")
    if dict(ref.price_convention).get("contains_market_prices") is not False:
        raise CalendarValidationError("trading_calendar_ref不得包含市场价格")
    volatile = read_volatile_asset(ref.data_asset_id, caller)
    try:
        encoded = volatile[1] if volatile is not None else LocalDataStore(Path(config.data_root)).read_bytes(ref, tenant_id=caller.tenant_id)
    except (FileNotFoundError, PermissionError, StoreError) as error:
        raise CalendarValidationError("trading_calendar_ref无法通过DataStore完整性校验") from error
    if hashlib.sha256(encoded).hexdigest() != ref.content_hash:
        raise CalendarValidationError("trading_calendar_ref内容哈希不一致")
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CalendarValidationError("trading_calendar_ref内容不是有效JSON") from error
    if not isinstance(payload, Mapping) or payload.get("schema_id") != CALENDAR_SCHEMA:
        raise CalendarValidationError("trading_calendar_ref内容协议无效")
    if set(payload.get("asset_ids", ())) != set(request.asset_ids):
        raise CalendarValidationError("trading_calendar_ref内容标的与请求不一致")
    exchanges = {
        asset_id: str(china_market_convention(asset_id)["exchange"])
        for asset_id in request.asset_ids
    }
    payload_exchange = payload.get("asset_exchange")
    raw_by_exchange = payload.get("sessions_by_exchange")
    if not isinstance(payload_exchange, Mapping) or not isinstance(raw_by_exchange, Mapping):
        raise CalendarValidationError("trading_calendar_ref缺少交易所session映射")
    if {str(key): str(value) for key, value in payload_exchange.items()} != exchanges:
        raise CalendarValidationError("trading_calendar_ref交易所映射与标的不一致")
    verified_by_exchange: dict[str, tuple[str, ...]] = {}
    for exchange in sorted(set(exchanges.values())):
        values = raw_by_exchange.get(exchange)
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise CalendarValidationError("trading_calendar_ref交易所session映射无效")
        verified_by_exchange[exchange] = _validate_exchange_sessions(
            tuple(values), exchange=exchange, start_date=coverage_start, end_date=coverage_end,
        )
    expected_intersection = tuple(sorted(set.intersection(*(set(values) for values in verified_by_exchange.values()))))
    payload_sessions = payload.get("sessions")
    coverage_sessions = coverage.get("sessions")
    if not isinstance(payload_sessions, Sequence) or isinstance(payload_sessions, (str, bytes)):
        raise CalendarValidationError("trading_calendar_ref缺少sessions")
    if (
        tuple(payload_sessions) != expected_intersection
        or isinstance(coverage_sessions, (str, bytes))
        or not isinstance(coverage_sessions, Sequence)
        or tuple(coverage_sessions) != expected_intersection
    ):
        raise CalendarValidationError("trading_calendar_ref交易日交集与覆盖声明不一致")
    dates_by_asset = {
        asset_id: tuple(
            value for value in verified_by_exchange[exchange]
            if value < fallback_before
        ) if fallback_before is not None else tuple(
            value for value in verified_by_exchange[exchange]
            if request.start_date <= value <= request.end_date
        )
        for asset_id, exchange in exchanges.items()
    }
    if not all(dates_by_asset.values()):
        raise CalendarValidationError("trading_calendar_ref在历史行情请求区间没有交易日")
    return VerifiedCalendarEvidence(
        dates_by_asset=dates_by_asset,
        calendar_id=calendar_id,
        calendar_revision=calendar_revision,
        calendar_ref={
            "data_asset_id": ref.data_asset_id,
            "content_hash": ref.content_hash,
            "schema_id": ref.schema_id,
            "media_type": ref.media_type,
        },
    )


def _canonical_payload(
    request: CalendarRequest,
    sessions_by_exchange: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, Any], bytes, str]:
    exchanges = tuple(sorted(sessions_by_exchange))
    intersection = tuple(sorted(set.intersection(*(set(sessions_by_exchange[item]) for item in exchanges))))
    if not intersection and any(sessions_by_exchange[item] for item in exchanges):
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


def _calendar_identity(request: CalendarRequest, tenant_id: str, principal_id: str) -> str:
    exchanges = sorted({str(china_market_convention(item)["exchange"]) for item in request.asset_ids})
    encoded = json.dumps({
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "exchanges": exchanges,
        "schema_id": CALENDAR_SCHEMA,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exchange_lock_identity(caller: CallerContext, exchange: str) -> str:
    return hashlib.sha256(
        f"{caller.tenant_id}:{caller.principal_id}:{CALENDAR_SCHEMA}:{exchange}".encode("utf-8")
    ).hexdigest()


def _date_sequence(start_date: str, end_date: str) -> tuple[str, ...]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    values: list[str] = []
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(values)


@dataclass(frozen=True)
class CalendarCoverage:
    sessions_by_exchange: Mapping[str, tuple[str, ...]]
    covered_dates_by_exchange: Mapping[str, frozenset[str]]
    exact: tuple[DataAssetRef, bytes] | None = None

    @property
    def has_local_coverage(self) -> bool:
        return any(self.covered_dates_by_exchange.values())


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
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
            with _process_lock(_temporary_lock_path("calendar-index", str(self.root.resolve()))):
                yield

    @contextmanager
    def fetch_lock(self, identity: str):
        """以日历请求身份串行Provider、缓存查找和写入。"""

        with _INDEX_LOCKS_GUARD:
            lock = _INDEX_LOCKS.setdefault(
                f"{self.root.resolve()}:{identity}",
                threading.RLock(),
            )
        with lock:
            with _process_lock(
                _temporary_lock_path("calendar-request", f"{self.root.resolve()}:{identity}")
            ):
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
        identity = _calendar_identity(request, caller.tenant_id, caller.principal_id)
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
                "data_asset_ref": deep_thaw(asdict(ref)),
                "tenant_id": caller.tenant_id,
                "principal_id": caller.principal_id,
                "exchanges": sorted({str(china_market_convention(item)["exchange"]) for item in request.asset_ids}),
                "provider": "ifind_http",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(self.index_path, index)

    def save_volatile(
        self,
        request: CalendarRequest,
        caller: CallerContext,
        payload: bytes,
        ref: DataAssetRef,
    ) -> None:
        """Register calendar coverage in process memory only."""

        identity = _calendar_identity(request, caller.tenant_id, caller.principal_id)
        key = (str(self.root.resolve()), f"{identity}:{ref.content_hash}")
        with _VOLATILE_CALENDARS_LOCK:
            _VOLATILE_CALENDARS[key] = {
                "calendar_identity": identity,
                "payload": bytes(payload),
                "data_asset_ref": deep_thaw(asdict(ref)),
                "tenant_id": caller.tenant_id,
                "principal_id": caller.principal_id,
                "exchanges": sorted({str(china_market_convention(item)["exchange"]) for item in request.asset_ids}),
                "provider": "ifind_http",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def list_assets(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._guard():
            records = list(self._read_index()["entries"].values())
        assets: list[Mapping[str, Any]] = []
        for record in records:
            if (
                not isinstance(record, Mapping)
                or record.get("tenant_id") != tenant_id
                or (principal_id is not None and record.get("principal_id") not in {None, principal_id})
            ):
                continue
            reference = record.get("data_asset_ref")
            if isinstance(reference, Mapping):
                assets.append({
                    "data_asset_ref": dict(reference),
                    "provider": "ifind_http",
                    "updated_at": record.get("updated_at"),
                })
        return sorted(assets, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:safe_limit]

    def find_asset(
        self,
        *,
        tenant_id: str,
        data_asset_id: str,
        principal_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        with self._guard():
            records = list(self._read_index()["entries"].values())
        for record in records:
            if (
                not isinstance(record, Mapping)
                or record.get("tenant_id") != tenant_id
            ):
                continue
            reference = record.get("data_asset_ref")
            if isinstance(reference, Mapping) and reference.get("data_asset_id") == data_asset_id:
                return {
                    "data_asset_ref": dict(reference),
                    "provider": "ifind_http",
                    "updated_at": record.get("updated_at"),
                }
        return None

    def _records(self, caller: CallerContext) -> list[Mapping[str, Any]]:
        records: list[Mapping[str, Any]] = []
        if self.index_path.exists():
            with self._guard():
                records.extend(self._read_index()["entries"].values())
        root_key = str(self.root.resolve())
        with _VOLATILE_CALENDARS_LOCK:
            records.extend(
                dict(record)
                for (record_root, _key), record in _VOLATILE_CALENDARS.items()
                if record_root == root_key
            )
        return [
            record
            for record in records
            if isinstance(record, Mapping)
            and record.get("tenant_id") == caller.tenant_id
            and record.get("principal_id") in {None, caller.principal_id}
        ]

    def _resolve_record(
        self,
        record: Mapping[str, Any],
        caller: CallerContext,
        store: LocalDataStore | None,
    ) -> tuple[DataAssetRef, bytes, Mapping[str, Any]] | None:
        reference = record.get("data_asset_ref")
        relative = record.get("payload_path")
        inline = record.get("payload")
        if not isinstance(reference, Mapping):
            return None
        try:
            ref = _ref_from_mapping(reference)
            if isinstance(inline, bytes):
                payload = bytes(inline)
            elif isinstance(relative, str):
                path = (self.root / relative).resolve()
                if self.root.resolve() not in path.parents or not path.is_file():
                    return None
                payload = path.read_bytes()
            else:
                return None
            document = json.loads(payload)
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            return None
        if (
            ref.tenant_id != caller.tenant_id
            or ref.created_by != caller.principal_id
            or "read" not in ref.access_scope
        ):
            return None
        if ref.schema_id != CALENDAR_SCHEMA or hashlib.sha256(payload).hexdigest() != ref.content_hash:
            return None
        if not isinstance(document, Mapping) or document.get("schema_id") != CALENDAR_SCHEMA:
            return None
        if ref.storage_ref.startswith("volatile:"):
            in_memory = read_volatile_asset(ref.data_asset_id, caller)
            if in_memory is None or in_memory[0].content_hash != ref.content_hash:
                return None
        else:
            if store is None:
                return None
            try:
                store.resolve(ref, tenant_id=caller.tenant_id)
            except (FileNotFoundError, PermissionError, StoreError):
                return None
        return ref, payload, document

    def coverage(
        self,
        request: CalendarRequest,
        caller: CallerContext,
        store: LocalDataStore | None,
    ) -> CalendarCoverage:
        expected_exchanges = {
            str(china_market_convention(asset_id)["exchange"])
            for asset_id in request.asset_ids
        }
        sessions: dict[str, set[str]] = {exchange: set() for exchange in expected_exchanges}
        covered: dict[str, set[str]] = {exchange: set() for exchange in expected_exchanges}
        exact: tuple[DataAssetRef, bytes] | None = None
        records = sorted(
            self._records(caller),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )
        for record in records:
            resolved = self._resolve_record(record, caller, store)
            if resolved is None:
                continue
            ref, payload, document = resolved
            try:
                start_date = date.fromisoformat(str(document["requested_start_date"])).isoformat()
                end_date = date.fromisoformat(str(document["requested_end_date"])).isoformat()
                raw_by_exchange = document["sessions_by_exchange"]
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(raw_by_exchange, Mapping):
                continue
            for exchange in expected_exchanges.intersection(str(item) for item in raw_by_exchange):
                raw_sessions = raw_by_exchange.get(exchange)
                if not isinstance(raw_sessions, list):
                    continue
                try:
                    validated = _validate_exchange_sessions(
                        tuple(raw_sessions),
                        exchange=exchange,
                        start_date=start_date,
                        end_date=end_date,
                        allow_empty=True,
                    )
                except CalendarValidationError:
                    continue
                sessions[exchange].update(validated)
                covered[exchange].update(_date_sequence(start_date, end_date))
            coverage = dict(ref.coverage)
            if (
                exact is None
                and set(ref.asset_ids) == set(request.asset_ids)
                and coverage.get("start_date") <= request.start_date
                and coverage.get("end_date") >= request.end_date
                and ref.lineage.get("persistence_mode") == request.persistence_mode
            ):
                exact = (ref, payload)
        return CalendarCoverage(
            sessions_by_exchange={key: tuple(sorted(values)) for key, values in sessions.items()},
            covered_dates_by_exchange={key: frozenset(values) for key, values in covered.items()},
            exact=exact,
        )

    def lookup(
        self,
        request: CalendarRequest,
        caller: CallerContext,
        store: LocalDataStore | None,
    ) -> tuple[DataAssetRef, bytes] | None:
        local = self.coverage(request, caller, store)
        requested_dates = set(_date_sequence(request.start_date, request.end_date))
        if local.exact is not None and all(
            requested_dates.issubset(local.covered_dates_by_exchange.get(exchange, frozenset()))
            for exchange in local.covered_dates_by_exchange
        ):
            return local.exact
        return None


def _store_reference(
    request: CalendarRequest,
    caller: CallerContext,
    config: DataFetcherConfig,
    payload: Mapping[str, Any],
    encoded: bytes,
    content_hash: str,
) -> DataAssetRef:
    sessions = tuple(str(item) for item in payload["sessions"])
    by_exchange: dict[str, dict[str, Any]] = {}
    for exchange, raw in dict(payload["sessions_by_exchange"]).items():
        values = tuple(str(item) for item in raw)
        by_exchange[exchange] = {
            "start_date": values[0] if values else request.start_date,
            "end_date": values[-1] if values else request.end_date,
            "row_count": len(values),
        }
    coverage = {
        "start_date": request.start_date,
        "end_date": request.end_date,
        "sessions": list(sessions),
        "calendar_id": "CN-" + "+".join(payload["exchanges"]),
        "calendar_revision": content_hash[:16],
        "by_exchange": by_exchange,
    }
    owner = hashlib.sha256(
        f"{caller.tenant_id}\0{caller.principal_id}".encode("utf-8")
    ).hexdigest()[:12]
    data_asset_id = f"calendar-{content_hash}-ifind-o-{owner}"
    lineage = {
        "provider": "ifind_http",
        "endpoint": "get_trade_dates",
        "request_hash": hashlib.sha256(json.dumps(request.public_dict(), sort_keys=True).encode("utf-8")).hexdigest(),
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "persistence_mode": request.persistence_mode,
    }
    if request.persistence_mode == "volatile":
        return store_volatile_asset(
            caller=caller,
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
            lineage=lineage,
        )
    store = LocalDataStore(Path(config.data_root))
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
            lineage=lineage,
            created_by=caller.principal_id,
            access_scope=("read",),
        )
    except FileExistsError:
        tenant_root = store.root if caller.tenant_id == "local" else store.root / "tenants" / caller.tenant_id
        manifest = json.loads((tenant_root / "assets" / data_asset_id / "manifest.json").read_text(encoding="utf-8"))
        ref = _ref_from_mapping({**manifest["metadata"], "storage_ref": manifest["storage_ref"]})
        store.resolve(ref, tenant_id=caller.tenant_id)
        return ref


def _cached_reference(cached: tuple[DataAssetRef, bytes]) -> tuple[DataAssetRef, Mapping[str, Any]]:
    """解析已由CalendarCache按主体和完整性验证过的缓存日历。"""

    ref, encoded = cached
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CalendarValidationError("交易日历缓存内容无效") from error
    if not isinstance(payload, Mapping):
        raise CalendarValidationError("交易日历缓存内容无效")
    return ref, payload


def _attach_provider_calls(error: Exception, calls: Sequence[Mapping[str, Any]]) -> Exception:
    """把已经发生的日历Provider尝试交给统一Run持久化，不暴露异常成因。"""

    setattr(error, "provider_calls", tuple(dict(item) for item in calls))
    return error


def fetch_calendar_asset(
    request_value: CalendarRequest | Mapping[str, Any],
    caller: CallerContext,
    config: DataFetcherConfig,
) -> tuple[CalendarRequest, DataAssetRef, Mapping[str, Any], str, tuple[Mapping[str, Any], ...]]:
    """Combine user-scoped local coverage first and fetch only exact gaps."""

    if "data:read" not in caller.capabilities:
        raise CalendarValidationError("当前CallerContext无data:read权限")
    request = validate_calendar_request(request_value, config)
    cache = CalendarCache(Path(config.cache_root))
    store = (
        LocalDataStore(Path(config.data_root))
        if request.persistence_mode == "library" or cache.index_path.exists()
        else None
    )
    calls: list[Mapping[str, Any]] = []
    asset_exchange = {
        asset_id: str(china_market_convention(asset_id)["exchange"])
        for asset_id in request.asset_ids
    }
    expected = tuple(sorted(set(asset_exchange.values())))
    lock_identities = tuple(_exchange_lock_identity(caller, exchange) for exchange in expected)
    requested_dates = _date_sequence(request.start_date, request.end_date)

    with ExitStack() as locks:
        for identity in lock_identities:
            locks.enter_context(cache.fetch_lock(identity))

        local = cache.coverage(request, caller, store)
        missing_by_exchange = {
            exchange: missing_intervals(
                requested_dates,
                local.covered_dates_by_exchange.get(exchange, frozenset()),
            )
            for exchange in expected
        }
        missing_by_exchange = {
            exchange: intervals
            for exchange, intervals in missing_by_exchange.items()
            if intervals
        }

        if not missing_by_exchange and local.exact is not None:
            ref, encoded = local.exact
            ref, payload = _cached_reference((ref, encoded))
            decision = "verified_cache_reused"
        else:
            combined_sessions = {
                exchange: set(local.sessions_by_exchange.get(exchange, ()))
                for exchange in expected
            }
            grouped_gaps: dict[tuple[str, str], list[str]] = {}
            for exchange, intervals in missing_by_exchange.items():
                for start_date, end_date in intervals:
                    grouped_gaps.setdefault((start_date, end_date), []).append(exchange)

            quota_used = 0
            for (start_date, end_date), exchanges in sorted(grouped_gaps.items()):
                selected_assets = tuple(
                    next(asset_id for asset_id, asset_market in asset_exchange.items() if asset_market == exchange)
                    for exchange in sorted(exchanges)
                )
                gap_request = replace(
                    request,
                    asset_ids=selected_assets,
                    start_date=start_date,
                    end_date=end_date,
                )
                estimated_units = len(exchanges)
                limit = min(config.max_provider_units, request.quota_limit) if request.quota_limit is not None else config.max_provider_units
                attempted = False
                try:
                    if config.offline:
                        raise ProviderUnavailable("离线模式下交易日历覆盖不足，禁止远程补齐", reason_code="offline_miss")
                    if quota_used + estimated_units > limit:
                        raise ProviderQuotaExceeded("交易日历请求超过DataFetcher额度保护上限")
                    attempted = True
                    raw_by_exchange = IFindHttpProvider().fetch_calendar(gap_request, config)
                    if not isinstance(raw_by_exchange, Mapping):
                        raise CalendarValidationError("iFind交易日历返回结构无效")
                    validated = {
                        exchange: _validate_exchange_sessions(
                            tuple(values)
                            if isinstance(values, (tuple, list)) and not isinstance(values, (str, bytes))
                            else (),
                            exchange=exchange,
                            start_date=start_date,
                            end_date=end_date,
                            allow_empty=True,
                        )
                        for exchange, values in raw_by_exchange.items()
                        if isinstance(exchange, str)
                        and isinstance(values, (tuple, list))
                        and not isinstance(values, (str, bytes))
                    }
                    if len(validated) != len(raw_by_exchange):
                        raise CalendarValidationError("iFind交易日历返回结构无效")
                    if sorted(validated) != sorted(exchanges):
                        raise CalendarValidationError("iFind交易日历未覆盖请求的全部交易所")
                    for exchange, sessions in validated.items():
                        combined_sessions[exchange].update(sessions)
                    quota_used += estimated_units
                    calls.append({
                        "provider": "ifind_http",
                        "endpoint": "get_trade_dates",
                        "outcome": "succeeded",
                        "quota_units": estimated_units,
                        "start_date": start_date,
                        "end_date": end_date,
                        "exchanges": sorted(exchanges),
                    })
                except (ProviderError, CalendarValidationError) as error:
                    calls.append({
                        "provider": "ifind_http",
                        "endpoint": "get_trade_dates",
                        "outcome": error.code,
                        "quota_units": estimated_units if attempted else 0,
                        "start_date": start_date,
                        "end_date": end_date,
                        "exchanges": sorted(exchanges),
                    })
                    if config.offline or getattr(error, "reason_code", None) == "credential_unavailable":
                        raise _attach_provider_calls(error, calls)
                    if isinstance(error, (
                        CalendarValidationError,
                        ProviderFieldPermissionDenied,
                        ProviderInputError,
                        ProviderMarketPermissionDenied,
                        ProviderQuotaExceeded,
                        ProviderUnauthorized,
                    )):
                        raise _attach_provider_calls(error, calls)
                    unavailable = ProviderUnavailable("交易日历暂不可用，且本地资产未完整覆盖请求区间")
                    raise _attach_provider_calls(unavailable, calls) from error

            normalized_sessions = {
                exchange: tuple(
                    value
                    for value in sorted(combined_sessions[exchange])
                    if request.start_date <= value <= request.end_date
                )
                for exchange in expected
            }
            payload, encoded, content_hash = _canonical_payload(request, normalized_sessions)
            ref = _store_reference(request, caller, config, payload, encoded, content_hash)
            if request.persistence_mode == "library":
                cache.save(request, caller, encoded, ref)
            else:
                cache.save_volatile(request, caller, encoded, ref)
            if missing_by_exchange:
                decision = "cache_extended" if local.has_local_coverage else "provider_fetched"
            else:
                decision = "cache_rebound"
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
    "VerifiedCalendarEvidence",
    "calendar_evidence_for_history",
    "fetch_calendar_asset",
    "validate_calendar_request",
)
