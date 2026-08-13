"""唯一正式公开协议版本。"""

from __future__ import annotations


PUBLIC_VERSION = "v1.0.0"
RESOLVED_CONTRACT_SCHEMA_ID = f"optionhelper.resolved-contract/{PUBLIC_VERSION}"
PRICING_CONFIG_SCHEMA_ID = f"optionhelper.pricing-config/{PUBLIC_VERSION}"


def require_public_version(value: object, field: str) -> str:
    if value != PUBLIC_VERSION:
        raise ValueError(f"{field}必须为{PUBLIC_VERSION}")
    return PUBLIC_VERSION


__all__ = (
    "PUBLIC_VERSION", "PRICING_CONFIG_SCHEMA_ID",
    "RESOLVED_CONTRACT_SCHEMA_ID", "require_public_version",
)
