"""Select OptionHelper-local storage with one-release system-vault migration."""

from __future__ import annotations

import sys
from pathlib import Path

from desktop.macos.keychain import MacOSKeychain
from desktop.windows.credential_manager import WindowsCredentialManager

from .local_primary_adapter import LocalPrimarySecretAdapter
from .local_secret_store import LocalSecretStore
from .secret_provider import SecretProvider


def platform_secret_provider(
    app_data_root: Path,
    platform_name: str | None = None,
    *,
    legacy_app_data_root: Path | None = None,
    enable_system_migration: bool = True,
) -> SecretProvider:
    """Return local storage and retain native vaults only for upgrade migration."""

    platform_id = platform_name or sys.platform
    local_store = LocalSecretStore(app_data_root / "credentials")
    legacy_local_stores = ()
    if legacy_app_data_root is not None and legacy_app_data_root.resolve() != app_data_root.resolve():
        legacy_local_stores = (LocalSecretStore(legacy_app_data_root / "credentials"),)
    if not enable_system_migration:
        return SecretProvider({"local-secret": local_store})
    if platform_id == "darwin":
        keychain = MacOSKeychain()
        return SecretProvider({
            "local-secret": LocalPrimarySecretAdapter(
                local_store,
                legacy_provider="keychain",
                legacy_adapter=keychain,
                legacy_local_stores=legacy_local_stores,
            ),
            "keychain": keychain,
        })
    if platform_id == "win32":
        credential_manager = WindowsCredentialManager()
        return SecretProvider({
            "local-secret": LocalPrimarySecretAdapter(
                local_store,
                legacy_provider="credential-manager",
                legacy_adapter=credential_manager,
                legacy_local_stores=legacy_local_stores,
            ),
            "credential-manager": credential_manager,
        })
    return SecretProvider({"local-secret": local_store})
