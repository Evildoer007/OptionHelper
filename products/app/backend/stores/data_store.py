"""Tenant-scoped metadata store for DataAsset references.

Data bytes remain behind a controlled ``storage_ref``.  This App store never
accepts an arbitrary filesystem path or any provider credential.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any

from ..errors import AuthorizationError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


class DataStore:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

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

        def update(value: dict[str, Any]) -> dict[str, Any]:
            key = _asset_key(identity.tenant_id, asset_id)
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
        else:
            candidates = [
                item for item in self._state.read("data_assets").values()
                if isinstance(item, dict)
                and item.get("tenant_id") == identity.tenant_id
                and item.get("created_by") == identity.principal_id
                and set(asset_ids) == set(item.get("asset_ids", []))
                and (schema_id is None or item.get("schema_id") == schema_id)
            ]
            if not candidates:
                if optional:
                    return None
                labels = "、".join(str(item) for item in asset_ids)
                if schema_id == "trading-calendar":
                    message = f"当前任务尚无可用于{labels}的交易日历；App将尝试通过iFind自动准备，失败时请检查数据接口配置。"
                else:
                    message = f"当前任务尚无可用于{labels}的行情数据；请先在数据获取中获取{labels}日频行情，再运行定价。"
                raise UserActionError(
                    "market_data_required",
                    message,
                    stage="data",
                    next_step="请在数据获取中准备覆盖当前标的和所需区间的日频行情后重试。",
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
        for item in self._state.read("data_assets").values():
            if not isinstance(item, dict):
                continue
            coverage = item.get("coverage")
            if not isinstance(coverage, dict):
                continue
            if (
                item.get("tenant_id") != identity.tenant_id
                or item.get("created_by") != identity.principal_id
                or item.get("schema_id") != "market-history"
                or set(asset_ids) != set(item.get("asset_ids", []))
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

    def resolve_frozen_trading_calendar(
        self,
        identity: SessionIdentity,
        requested: object,
        *,
        asset_ids: tuple[str, ...],
        calendar_id: str,
        calendar_revision: str,
    ) -> dict[str, Any]:
        """Resolve the exact calendar revision frozen into a contract.

        Market-history can safely prefer the newest compatible asset.  An
        observed contract cannot: its schedules were resolved from one
        calendar revision, so selecting a newer revision changes the contract
        after it has been signed.  Keep this lookup separate from the generic
        resolver to make that distinction explicit at the Host boundary.
        """

        requested_id = _requested_asset_id(requested)
        if requested_id:
            record = self.get(identity, requested_id)
            if isinstance(requested, dict) and requested.get("content_hash") not in {None, record.get("content_hash")}:
                raise ValidationError("DataAssetRef.content_hash与App登记记录不一致")
            candidates = [record]
        else:
            candidates = [
                item for item in self._state.read("data_assets").values()
                if isinstance(item, dict)
                and item.get("tenant_id") == identity.tenant_id
                and item.get("created_by") == identity.principal_id
                and item.get("schema_id") == "trading-calendar"
                and set(asset_ids) == set(item.get("asset_ids", []))
                and isinstance(item.get("coverage"), dict)
                and item["coverage"].get("calendar_id") == calendar_id
                and item["coverage"].get("calendar_revision") == calendar_revision
            ]
        if not candidates:
            raise UserActionError(
                "frozen_contract_calendar_missing",
                "当前方案冻结的交易日历不在本机数据中，不能用另一版本替代。请切换产品建立新方案版本后重试。",
                stage="data",
                next_step="请恢复该交易日历，或切换产品建立新的方案版本后重试。",
            )
        record = max(candidates, key=lambda item: (str(item.get("registered_at", "")), str(item.get("data_asset_id", ""))))
        if record.get("created_by") != identity.principal_id:
            raise AuthorizationError("data.read", "data asset is not owned by current caller")
        if set(asset_ids) != set(record.get("asset_ids", [])):
            raise ValidationError("DataAssetRef标的必须与ResolvedContract完全一致")
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

        Calendar assets are immutable snapshots.  Registration time therefore
        is not a meaningful proxy for fitness: a newer short-range snapshot
        must not hide an older snapshot that already covers the full contract
        tenor.  This lookup is deliberately limited to new contracts; a
        frozen contract keeps using ``resolve_frozen_trading_calendar``.
        """

        candidates: list[dict[str, Any]] = []
        for item in self._state.read("data_assets").values():
            if not isinstance(item, dict):
                continue
            if (
                item.get("tenant_id") != identity.tenant_id
                or item.get("created_by") != identity.principal_id
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
        record = self._state.read("data_assets").get(_asset_key(identity.tenant_id, data_asset_id))
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
