"""Local settings orchestration and secret-safe validation."""

from dataclasses import replace

from ..errors import ValidationError
from .settings_models import MULTI_AGENT_RECOMMENDATION_ROLES, SettingsSnapshot
from ..stores.settings_store import SettingsStore


class SettingsService:
    def __init__(self, store: SettingsStore) -> None:
        self._store = store

    def load(self, principal_id: str) -> SettingsSnapshot:
        return self._normalize_multi_agent_roles(self._store.load(principal_id))

    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        settings = self._normalize_multi_agent_roles(settings)
        self._assert_non_sensitive(settings)

        self._store.save(principal_id, settings)

    @staticmethod
    def _normalize_multi_agent_roles(settings: SettingsSnapshot) -> SettingsSnapshot:
        return replace(
            settings,
            multi_agent_preset_role_models={
                preset_id: {
                    role: selection for role, selection in role_models.items()
                    if role in MULTI_AGENT_RECOMMENDATION_ROLES
                }
                for preset_id, role_models in settings.multi_agent_preset_role_models.items()
            },
        )

    @staticmethod
    def _assert_non_sensitive(settings: SettingsSnapshot) -> None:
        references = [settings.model_service.secret_ref, settings.data_interface.secret_ref]
        references.extend(connection.model_service.secret_ref for connection in settings.model_connections)
        references.extend(provider.secret_ref for provider in settings.model_providers)
        for reference in references:
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
        if settings.preferences.theme not in {"light", "dark", "auto"}:
            raise ValidationError("theme must be light, dark, or auto")
        allowed_presets = {"sequential-deliberation", "independent-council"}
        if settings.multi_agent_recommendation_preset_id not in allowed_presets:
            raise ValidationError("MultiAgent默认预设无效")
        allowed_roles = MULTI_AGENT_RECOMMENDATION_ROLES
        enabled = {
            (provider.provider_id, model.model_id)
            for provider in settings.model_providers
            for model in provider.models
            if model.enabled
        }
        for preset_id, role_models in settings.multi_agent_preset_role_models.items():
            if preset_id not in allowed_presets or not isinstance(role_models, dict):
                raise ValidationError("MultiAgent预设角色模型映射无效")
            for role, selection in role_models.items():
                if role not in allowed_roles or (selection.provider_id, selection.model_id) not in enabled:
                    raise ValidationError("MultiAgent角色模型必须引用当前已启用模型")
