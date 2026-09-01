"""One-time migration of configured model and iFind credentials to local files."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..settings.settings_models import SettingsSnapshot
from ..stores.settings_store import LocalSettingsStore
from .secret_provider import SecretProvider
from .secret_ref import SecretRef


_LEGACY_PROVIDERS = frozenset({"keychain", "credential-manager"})


@dataclass(frozen=True)
class CredentialMigrationSummary:
    migrated_credentials: int = 0
    updated_settings_records: int = 0
    pending_credentials: int = 0
    missing_credentials: int = 0
    settings_write_failures: int = 0

    @property
    def completed(self) -> bool:
        return self.pending_credentials == 0 and self.settings_write_failures == 0


def migrate_configured_credentials(
    settings_store: LocalSettingsStore,
    secret_provider: SecretProvider,
) -> CredentialMigrationSummary:
    """Migrate only references still present in OptionHelper settings."""

    missing_marker = object()
    pending_marker = object()
    cache: dict[tuple[str, str, str, str | None], SecretRef | object] = {}
    migrated = 0
    pending = 0
    missing = 0
    updated = 0
    write_failures = 0

    def migrate(reference: SecretRef | None, purpose: str) -> SecretRef | None:
        nonlocal migrated, pending, missing
        if reference is None or reference.provider not in _LEGACY_PROVIDERS:
            return reference
        cache_key = (purpose, reference.provider, reference.key, reference.revision)
        if cache_key in cache:
            cached = cache[cache_key]
            if cached is missing_marker:
                return None
            if cached is pending_marker:
                return reference
            assert isinstance(cached, SecretRef)
            return cached
        secret_provider.allow_reference(reference, purpose)
        try:
            local_reference = secret_provider.migrate_to_local(reference, purpose)
        except Exception as error:
            if getattr(error, "capability", None) == "本机凭据缺失":
                missing += 1
                cache[cache_key] = missing_marker
                return None
            pending += 1
            cache[cache_key] = pending_marker
            return reference
        cache[cache_key] = local_reference
        migrated += 1
        return local_reference

    for principal_id, snapshot in settings_store.items():
        migrated_snapshot = _migrate_snapshot(snapshot, migrate)
        if migrated_snapshot == snapshot:
            continue
        try:
            settings_store.save(principal_id, migrated_snapshot)
        except Exception:
            write_failures += 1
            continue
        updated += 1

    return CredentialMigrationSummary(
        migrated_credentials=migrated,
        updated_settings_records=updated,
        pending_credentials=pending,
        missing_credentials=missing,
        settings_write_failures=write_failures,
    )


def _migrate_snapshot(snapshot: SettingsSnapshot, migrate) -> SettingsSnapshot:
    model_service = replace(
        snapshot.model_service,
        secret_ref=migrate(snapshot.model_service.secret_ref, "模型服务凭据"),
    )
    model_connections = tuple(
        replace(
            connection,
            model_service=replace(
                connection.model_service,
                secret_ref=migrate(connection.model_service.secret_ref, "模型服务凭据"),
            ),
        )
        for connection in snapshot.model_connections
    )
    model_providers = tuple(
        replace(
            provider,
            secret_ref=migrate(provider.secret_ref, "模型服务凭据"),
        )
        for provider in snapshot.model_providers
    )
    data_interface = replace(
        snapshot.data_interface,
        secret_ref=migrate(snapshot.data_interface.secret_ref, "iFind数据凭据"),
    )
    return replace(
        snapshot,
        model_service=model_service,
        model_connections=model_connections,
        model_providers=model_providers,
        data_interface=data_interface,
    )


__all__ = ("CredentialMigrationSummary", "migrate_configured_credentials")
