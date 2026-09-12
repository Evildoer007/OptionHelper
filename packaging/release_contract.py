"""Independent Skill and App identities for the current release train."""

from __future__ import annotations

from datetime import datetime

SKILL_VERSION = "v0.3.0"
APP_VERSION = "v0.1.0"
# Signed knowledge and Capability releases follow the Skill version.
RELEASE_VERSION = SKILL_VERSION
DEVELOPMENT_ID = "development"
PROTOCOL_ID = "optionhelper.module-host"
CAPABILITY_MANIFEST_SCHEMA = "optionhelper.capability-manifest"
HASH_SPEC_ID = "content-tree-sha256-nfc"
RELEASE_VERSION_FIELDS = (
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
    expected = {field: APP_VERSION if field == "app_version" else SKILL_VERSION for field in RELEASE_VERSION_FIELDS}
    return [f"{field}必须为{version}" for field, version in expected.items()
            if field in manifest and manifest[field] != version]



def skill_archive_name() -> str:
    """Version the download, while the installed Skill remains option-helper."""
    return f"option-helper-{SKILL_VERSION}.zip"


def require_app_version(version: str) -> str:
    if version != APP_VERSION:
        raise ValueError(f"当前App发行只接受{APP_VERSION}")
    return version


def app_platform_versions(version: str) -> tuple[str, str]:
    """Map the public prerelease label to native numeric/Apple bundle versions."""
    import re
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)(?:[.-](alpha|beta|rc)(?:[.-]?(\d+))?)?", version)
    if not match:
        raise ValueError("App版本格式无效")
    base, stage, serial = match.groups()
    suffix = {"alpha": "a", "beta": "b", "rc": "fc"}.get(stage, "")
    return base, base + (suffix + (serial or "1") if suffix else "")
