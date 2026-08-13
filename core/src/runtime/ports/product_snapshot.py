"""Archive lookup boundary for a published ProductVersion."""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class ProductSnapshotProvider(Protocol):
    """Return the exact Registry snapshot that issued one published contract."""

    def load_registry_snapshot(
        self,
        *,
        product_version: str,
        product_id: str,
        registry_snapshot_hash: str,
    ) -> Mapping[str, Any]: ...


def require_product_snapshot_provider(value: object) -> ProductSnapshotProvider:
    if not callable(getattr(value, "load_registry_snapshot", None)):
        raise TypeError("ProductSnapshotProvider必须提供load_registry_snapshot")
    return value  # type: ignore[return-value]


__all__ = ("ProductSnapshotProvider", "require_product_snapshot_provider")
