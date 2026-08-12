"""Local settings orchestration and secret-safe validation."""

from ..errors import ValidationError
from .settings_models import SettingsSnapshot
from ..stores.settings_store import SettingsStore


class SettingsService:
    def __init__(self, store: SettingsStore) -> None:
        self._store = store

    def load(self, principal_id: str) -> SettingsSnapshot:
        return self._store.load(principal_id)

    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        self._assert_non_sensitive(settings)

        self._store.save(principal_id, settings)

    @staticmethod
    def _assert_non_sensitive(settings: SettingsSnapshot) -> None:
        for reference in (settings.model_service.secret_ref, settings.data_interface.secret_ref):
            if reference is not None and not reference.provider.strip():
                raise ValidationError("SecretRef provider is required")
        data_ref = settings.data_interface.secret_ref
        if data_ref is not None and data_ref.provider not in {"local-secret", "keychain", "credential-manager", "managed-secret"}:
            raise ValidationError("Data Interface SecretRef provider must be Host-managed")
        export_location_ref = settings.storage_export.export_location_ref
        if export_location_ref is not None and (
            not export_location_ref.strip()
            or "\x00" in export_location_ref
            or "/" in export_location_ref
            or "\\" in export_location_ref
        ):
            raise ValidationError("export_location_ref must be a controlled opaque reference")
        if settings.preferences.html_report_layout != "continuous":
            raise ValidationError("html_report_layout must be continuous")
        if settings.preferences.theme not in {"light", "dark", "auto"}:
            raise ValidationError("theme must be light, dark, or auto")
