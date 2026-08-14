"""The one public version identity accepted by the current release train."""

from __future__ import annotations

from datetime import datetime

RELEASE_VERSION = "v1.0.0"
DEVELOPMENT_ID = "development"
PROTOCOL_ID = "optionhelper.module-host"
CAPABILITY_MANIFEST_SCHEMA = "optionhelper.capability-manifest"
HASH_SPEC_ID = "content-tree-sha256-nfc"
RELEASE_VERSION_FIELDS = (
    "product_version",
    "catalog_version",
    "capability_version",
    "app_version",
)


def require_release_version(version: str) -> str:
    """Reject every legacy or implicit version before release work begins."""
    if version != RELEASE_VERSION:
        raise ValueError(f"当前发行只接受{RELEASE_VERSION}")
    return version


def require_published_at(value: str) -> str:
    """Accept one explicit ISO-8601 instant with an offset for publication."""
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("published_at必须是ISO-8601时间") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("published_at必须包含时区")
    return value


def public_version_errors(manifest: dict[str, object]) -> list[str]:
    """Return present public-manifest fields that do not carry the release identity."""
    return [
        f"{field}必须为{RELEASE_VERSION}"
        for field in RELEASE_VERSION_FIELDS
        if field in manifest and manifest[field] != RELEASE_VERSION
    ]
