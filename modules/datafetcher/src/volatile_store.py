"""Process-local DataAsset bytes for non-persistent DataFetcher requests."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import threading
from typing import Any, Mapping
from uuid import uuid4

from runtime.protocol.models import CallerContext, DataAssetRef


_LOCK = threading.RLock()
_ASSETS: dict[tuple[str, str, str], tuple[DataAssetRef, bytes]] = {}


def store_volatile_asset(
    *,
    caller: CallerContext,
    data_asset_id: str,
    payload: bytes,
    media_type: str,
    schema_id: str,
    asset_ids: tuple[str, ...],
    normalized_fields: tuple[str, ...],
    coverage: Mapping[str, Any],
    row_count: int,
    partition_spec: Mapping[str, Any],
    price_convention: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> DataAssetRef:
    """Create an opaque reference whose bytes only live in this process."""

    content = bytes(payload)
    enriched_lineage = {**dict(lineage), "persistence_mode": "volatile"}
    reference = DataAssetRef(
        data_asset_id=data_asset_id,
        storage_ref=f"volatile:{uuid4().hex}",
        media_type=media_type,
        schema_id=schema_id,
        asset_ids=asset_ids,
        normalized_fields=normalized_fields,
        coverage=dict(coverage),
        row_count=row_count,
        price_convention=dict(price_convention),
        content_hash=sha256(content).hexdigest(),
        lineage=enriched_lineage,
        tenant_id=caller.tenant_id,
        created_by=caller.principal_id,
        access_scope=("read",),
        partition_spec=dict(partition_spec),
    )
    key = (caller.tenant_id, caller.principal_id, data_asset_id)
    with _LOCK:
        _ASSETS[key] = (reference, content)
    return reference


def read_volatile_asset(data_asset_id: str, caller: CallerContext) -> tuple[DataAssetRef, bytes] | None:
    key = (caller.tenant_id, caller.principal_id, data_asset_id)
    with _LOCK:
        value = _ASSETS.get(key)
    if value is None:
        return None
    reference, content = value
    return replace(reference), bytes(content)


__all__ = ("read_volatile_asset", "store_volatile_asset")
