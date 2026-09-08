"""Tenant-scoped metadata store for DataAsset references.

Data bytes remain behind a controlled ``storage_ref``.  This App store never
accepts an arbitrary filesystem path or any provider credential.
"""

from __future__ import annotations

from copy import deepcopy
import json
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping

from ..errors import AuthorizationError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


class DataStore:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state
        self._volatile: dict[str, dict[str, Any]] = {}
        self._volatile_lock = RLock()

    def _records(self) -> dict[str, Any]:
        with self._volatile_lock:
            volatile = deepcopy(self._volatile)
        return {**self._state.read("data_assets"), **volatile}

    def register(self, identity: SessionIdentity, asset: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(asset, dict):
            raise ValidationError("DataAssetRef must be an object")
        storage_ref = asset.get("storage_ref")
        if not isinstance(storage_ref, str) or not storage_ref.strip() or "/" in storage_ref or "\\" in storage_ref:
            raise ValidationError("DataAsset storage_ref must be a controlled opaque reference")
        asset_id = asset.get("data_asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValidationError("DataAssetRef data_asset_id is required")
        claimed_tenant = asset.get("tenant_id")
        if claimed_tenant is not None and claimed_tenant != identity.tenant_id:
            raise ValidationError("DataAssetRef tenant_id does not match App caller")
        claimed_owner = asset.get("created_by")
        if claimed_owner is not None and claimed_owner != identity.principal_id:
            raise ValidationError("DataAssetRef created_by does not match App caller")
        asset_ids = _string_list(asset.get("asset_ids"), "asset_ids")
        normalized_fields = _string_list(asset.get("normalized_fields"), "normalized_fields")
        coverage = _mapping(asset.get("coverage"), "coverage")
        price_convention = _mapping(asset.get("price_convention"), "price_convention")
        lineage = _mapping(asset.get("lineage"), "lineage")
        partition_spec = _mapping(asset.get("partition_spec", {}), "partition_spec")
        access_scope = _string_list(asset.get("access_scope", ["read"]), "access_scope")
        row_count = asset.get("row_count")
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            raise ValidationError("DataAssetRef row_count must be a non-negative integer")
        for key in ("media_type", "schema_id", "content_hash"):
            if not isinstance(asset.get(key), str) or not str(asset[key]).strip():
                raise ValidationError(f"DataAssetRef {key} is required")
        record = {
            "data_asset_id": asset_id,
            "tenant_id": identity.tenant_id,
            "storage_ref": storage_ref,
            "media_type": asset["media_type"],
            "schema_id": asset["schema_id"],
            "asset_ids": asset_ids,
            "normalized_fields": normalized_fields,
            "coverage": coverage,
            "row_count": row_count,
            "price_convention": price_convention,
            "content_hash": asset["content_hash"],
            "lineage": lineage,
            "created_by": identity.principal_id,
            "access_scope": access_scope,
            "partition_spec": partition_spec,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        record["metadata_hash"] = sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

        key = _asset_key(identity.tenant_id, asset_id)
        if lineage.get("persistence_mode") == "volatile":
            with self._volatile_lock:
                existing = self._volatile.get(key)
                if existing is not None and not all(existing.get(name) == record.get(name) for name in _PROTOCOL_FIELDS):
                    raise ValidationError("DataAsset references are immutable; create a new data_asset_id")
                if existing is None:
                    self._volatile[key] = record
            return self.get(identity, asset_id)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            if key in value:
                existing = value[key]
                if isinstance(existing, dict) and all(existing.get(name) == record.get(name) for name in _PROTOCOL_FIELDS):
                    return value
                raise ValidationError("DataAsset references are immutable; create a new data_asset_id")
            value[key] = record
            return value

        self._state.update("data_assets", update)
        return self.get(identity, asset_id)

    def resolve_for_compute(
        self,
        identity: SessionIdentity,
        requested: object,
        *,
        asset_ids: tuple[str, ...],
        schema_id: str | None = None,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve an opaque App reference to the exact Core DataAssetRef."""
        requested_id = _requested_asset_id(requested)
        if requested_id:
            record = self.get(identity, requested_id)
            if isinstance(requested, dict) and requested.get("content_hash") not in {None, record.get("content_hash")}:
                raise ValidationError("DataAssetRef.content_hash与App登记记录不一致")
            if schema_id == "market-history" and not _market_history_metadata_consistent(record):
                raise UserActionError(
                    "market_history_metadata_inconsistent",
                    "当前行情资产的覆盖声明与实际行数不一致，请重新获取行情后重试。",
                    stage="data",
                    next_step="请重新获取当前标的所需区间的日频行情。",
                )
        else:
            candidates = [
                item for item in self._records().values()
                if isinstance(item, dict)
                and item.get("tenant_id") == identity.tenant_id
                and item.get("created_by") == identity.principal_id
                and "read" in item.get("access_scope", [])
                and set(asset_ids) == set(item.get("asset_ids", []))
                and (schema_id is None or item.get("schema_id") == schema_id)
                and (schema_id != "market-history" or _market_history_metadata_consistent(item))
            ]
            if not candidates:
                if optional:
                    return None
                labels = "、".join(str(item) for item in asset_ids)
                if schema_id == "trading-calendar":
                    message = f"本地资料库没有覆盖{labels}所需区间的交易日历，系统将尝试通过iFind自动补齐。"
                else:
                    message = f"本地资料库没有覆盖{labels}所需区间的行情，系统将尝试通过iFind自动补齐。"
                raise UserActionError(
                    "market_data_required",
                    message,
                    stage="data",
                    next_step="如自动获取失败，请检查iFind连接、标的代码和日期范围。",
                )
            record = max(candidates, key=lambda item: (str(item.get("registered_at", "")), str(item.get("data_asset_id", ""))))
        if record.get("created_by") != identity.principal_id:
            raise AuthorizationError("data.read", "data asset is not owned by current caller")
        if set(asset_ids) != set(record.get("asset_ids", [])):
            raise ValidationError("DataAssetRef标的必须与ResolvedContract完全一致")
        if schema_id is not None and record.get("schema_id") != schema_id:
            raise ValidationError(f"DataAssetRef.schema_id必须为{schema_id}")
        return {key: record[key] for key in _PROTOCOL_FIELDS}

    def resolve_market_history_for_calendar(
        self,
        identity: SessionIdentity,
        *,
        asset_ids: tuple[str, ...],
        calendar_id: str,
        calendar_revision: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any] | None:
        """Prefer history signed by the exact calendar frozen into a contract."""

        try:
            required_start = date.fromisoformat(start_date)
            required_end = date.fromisoformat(end_date)
        except ValueError as error:
            raise ValidationError("历史行情覆盖区间无效") from error
        candidates: list[dict[str, Any]] = []
        for item in self._records().values():
            if not isinstance(item, dict):
                continue
            coverage = item.get("coverage")
            if not isinstance(coverage, dict):
                continue
            if (
                item.get("tenant_id") != identity.tenant_id
                or item.get("created_by") != identity.principal_id
                or "read" not in item.get("access_scope", [])
                or item.get("schema_id") != "market-history"
                or set(asset_ids) != set(item.get("asset_ids", []))
                or not _market_history_metadata_consistent(item)
                or coverage.get("calendar_id") != calendar_id
                or coverage.get("calendar_revision") != calendar_revision
            ):
                continue
            try:
                available_start = date.fromisoformat(str(coverage.get("start_date", coverage.get("date_start"))))
                available_end = date.fromisoformat(str(coverage.get("end_date", coverage.get("date_end"))))
            except ValueError:
                continue
            if available_start <= required_start and available_end >= required_end:
                candidates.append(item)
        if not candidates:
            return None
        record = max(candidates, key=lambda item: (str(item.get("registered_at", "")), str(item.get("data_asset_id", ""))))
        return {key: record[key] for key in _PROTOCOL_FIELDS}

    def resolve_market_history_covering(
        self,
        identity: SessionIdentity,
        *,
        asset_ids: tuple[str, ...],
        start_date: str,
        end_date: str,
        fields: tuple[str, ...],
        frequency: str,
        adjustment: str,
        allow_available_end: bool = False,
        require_verified_calendar: bool = False,
    ) -> dict[str, Any] | None:
        """Choose local history by fitness, never by registration recency alone.

        Only observed common-asset bounds prove coverage. The Host resolves
        civil boundaries against a verified calendar before reusing a shorter
        interval; a provider's previous request dates cannot establish it.
        """

        try:
            required_start = date.fromisoformat(start_date)
            required_end = date.fromisoformat(end_date)
        except ValueError as error:
            raise ValidationError("历史行情覆盖区间无效") from error
        candidates: list[tuple[bool, date, date, dict[str, Any]]] = []
        for item in self._records().values():
            if not isinstance(item, dict) or not _market_history_metadata_consistent(item):
                continue
            if (
                item.get("tenant_id") != identity.tenant_id
                or item.get("created_by") != identity.principal_id
                or "read" not in item.get("access_scope", [])
                or item.get("schema_id") != "market-history"
                or set(item.get("asset_ids", [])) != set(asset_ids)
                or not set(fields).issubset(set(item.get("normalized_fields", [])))
            ):
                continue
            convention = item.get("price_convention")
            coverage = item.get("coverage")
            if not isinstance(convention, dict) or not isinstance(coverage, dict):
                continue
            if convention.get("frequency") != frequency or convention.get("requested_adjustment") != adjustment:
                continue
            if require_verified_calendar and not _history_has_verified_calendar(coverage):
                continue
            bounds = market_history_bounds(item)
            if bounds is None:
                continue
            available_start, available_end = bounds
            exact = available_start <= required_start and available_end >= required_end
            latest_available = (
                allow_available_end
                and available_start <= required_start
                and available_end >= required_start
                and available_end <= required_end
            )
            if exact or latest_available:
                candidates.append((exact, available_end, available_start, item))
        if not candidates:
            return None
        _, _, _, record = max(
            candidates,
            key=lambda candidate: (
                candidate[0],
                candidate[1],
                candidate[2],
                str(candidate[3].get("registered_at", "")),
                str(candidate[3].get("data_asset_id", "")),
            ),
        )
        return {key: record[key] for key in _PROTOCOL_FIELDS}

    def resolve_trading_calendar_covering(
        self,
        identity: SessionIdentity,
        *,
        asset_ids: tuple[str, ...],
        start_date: str,
        end_date: str,
    ) -> dict[str, Any] | None:
        """Return a locally registered calendar that covers an exact interval.

        Calendar assets are immutable snapshots. Registration time therefore
        is not a meaningful proxy for fitness: a newer short-range snapshot
        must not hide an older snapshot that already covers the requested
        interval.
        """

        candidates: list[dict[str, Any]] = []
        for item in self._records().values():
            if not isinstance(item, dict):
                continue
            if (
                item.get("tenant_id") != identity.tenant_id
                or item.get("created_by") != identity.principal_id
                or "read" not in item.get("access_scope", [])
                or item.get("schema_id") != "trading-calendar"
                or set(item.get("asset_ids", [])) != set(asset_ids)
            ):
                continue
            coverage = item.get("coverage")
            if not isinstance(coverage, dict):
                continue
            coverage_start = coverage.get("start_date")
            coverage_end = coverage.get("end_date")
            if (
                isinstance(coverage_start, str)
                and isinstance(coverage_end, str)
                and coverage_start <= start_date
                and coverage_end >= end_date
            ):
                candidates.append(item)
        if not candidates:
            return None
        record = max(
            candidates,
            key=lambda item: (str(item.get("registered_at", "")), str(item.get("data_asset_id", ""))),
        )
        return {key: record[key] for key in _PROTOCOL_FIELDS}

    def get(self, identity: SessionIdentity, data_asset_id: str) -> dict[str, Any]:
        record = self._records().get(_asset_key(identity.tenant_id, data_asset_id))
        if not isinstance(record, dict):
            raise KeyError(data_asset_id)
        if record.get("tenant_id") != identity.tenant_id:
            raise AuthorizationError("data.read", "data asset belongs to another tenant")
        if record.get("created_by") != identity.principal_id or "read" not in record.get("access_scope", []):
            raise AuthorizationError("data.read", "data asset is not owned by current caller")
        return record


def _asset_key(tenant_id: str, data_asset_id: str) -> str:
    return f"{tenant_id}:{data_asset_id}"


_PROTOCOL_FIELDS = (
    "data_asset_id", "storage_ref", "media_type", "schema_id", "asset_ids", "normalized_fields",
    "coverage", "row_count", "price_convention", "content_hash", "lineage", "tenant_id",
    "created_by", "access_scope", "partition_spec",
)


def market_history_bounds(reference: Mapping[str, Any]) -> tuple[date, date] | None:
    """Return actual common-asset bounds, independent of acquisition intent."""

    coverage = reference.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    try:
        starts = [date.fromisoformat(str(coverage["start_date"]))]
        ends = [date.fromisoformat(str(coverage["end_date"]))]
        by_asset = coverage.get("by_asset")
        if isinstance(by_asset, Mapping):
            for asset_id in reference.get("asset_ids", ()):
                starts.append(date.fromisoformat(str(by_asset[asset_id]["start_date"])))
                ends.append(date.fromisoformat(str(by_asset[asset_id]["end_date"])))
        first, last = max(starts), min(ends)
    except (KeyError, TypeError, ValueError):
        return None
    return (first, last) if first <= last else None


def _market_history_metadata_consistent(record: dict[str, Any]) -> bool:
    """Check self-consistency before selecting a history asset for compute.

    Capability code remains responsible for validating the underlying bytes.
    This Host-side check only prevents a known-invalid coverage declaration
    from being selected repeatedly when automatic acquisition can replace it.
    """

    coverage = record.get("coverage")
    if not isinstance(coverage, dict):
        return False
    sessions = coverage.get("sessions")
    if sessions is None:
        return True
    if not isinstance(sessions, (list, tuple)) or not sessions:
        return False
    try:
        parsed_sessions = tuple(date.fromisoformat(str(value)) for value in sessions)
    except ValueError:
        return False
    if tuple(sorted(set(parsed_sessions))) != parsed_sessions:
        return False
    by_asset = coverage.get("by_asset")
    if not isinstance(by_asset, dict):
        return False
    asset_ids = record.get("asset_ids")
    if not isinstance(asset_ids, (list, tuple)) or set(by_asset) != set(asset_ids):
        return False
    counts: list[int] = []
    for asset_id in asset_ids:
        item = by_asset.get(asset_id)
        if not isinstance(item, dict):
            return False
        count = item.get("row_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        counts.append(count)
        if item.get("start_date") not in sessions or item.get("end_date") not in sessions:
            return False
    row_count = record.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or sum(counts) != row_count:
        return False
    if len(asset_ids) == 1 and row_count != len(sessions):
        return False
    declared_start = coverage.get("start_date", coverage.get("start"))
    declared_end = coverage.get("end_date", coverage.get("end"))
    return declared_start == str(sessions[0]) and declared_end == str(sessions[-1])


def _history_has_verified_calendar(coverage: dict[str, Any]) -> bool:
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    sessions = coverage.get("sessions")
    calendar_coverage_end = coverage.get("calendar_coverage_end")
    if (
        not isinstance(calendar_id, str)
        or not calendar_id.startswith("CN-")
        or not isinstance(calendar_revision, str)
        or not calendar_revision.strip()
        or calendar_revision.casefold() in {"unverified", "unknown", "derived", "frame-sessions"}
        or not isinstance(sessions, list)
        or not sessions
        or not isinstance(calendar_coverage_end, str)
    ):
        return False
    try:
        return date.fromisoformat(calendar_coverage_end) <= date.fromisoformat(str(sessions[-1]))
    except ValueError:
        return False


def _requested_asset_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if "/" in value or "\\" in value:
            raise ValidationError("App计算只接受data_asset_id，不接受本地文件路径")
        return value.strip() or None
    if isinstance(value, dict):
        unknown = set(value) - {"data_asset_id", "content_hash"}
        if unknown or not isinstance(value.get("data_asset_id"), str):
            raise ValidationError("页面只能提交data_asset_id或其内容哈希约束")
        return str(value["data_asset_id"])
    raise ValidationError("DataAsset引用必须是data_asset_id")


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"DataAssetRef {name} must be a non-empty string list")
    return list(value)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"DataAssetRef {name} must be an object")
    # DataAsset metadata crosses JSON persistence and HTTP boundaries.
    # Canonicalise nested tuples (for example ``coverage.sessions``) before
    # the immutability comparison. Otherwise registering the same calendar a
    # second time changes only tuple -> list after persistence and is falsely
    # rejected as a different immutable asset.
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ValidationError(f"DataAssetRef {name} must be JSON-compatible") from error
    if not isinstance(normalized, dict):
        raise ValidationError(f"DataAssetRef {name} must be an object")
    return normalized
