"""DataStore业务端口。"""
from typing import Any, Mapping, Protocol
from runtime.protocol.models import DataAssetRef

class DataStorePort(Protocol):
    def put_bytes(self, *, tenant_id: str, data_asset_id: str, payload: bytes, media_type: str, schema_id: str, asset_ids: tuple[str, ...] = (), normalized_fields: tuple[str, ...] = (), coverage: Mapping[str, Any] | None = None, row_count: int = 0, partition_spec: Mapping[str, Any] | None = None, price_convention: Mapping[str, Any] | None = None, lineage: Mapping[str, Any] | None = None, created_by: str = "local", access_scope: tuple[str, ...] = ("read",)) -> DataAssetRef: ...
    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes: ...

class DataStoreReadPort(Protocol):
    """Least-authority slice injected into calculation consumers."""
    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes: ...

__all__ = ("DataStorePort", "DataStoreReadPort")
