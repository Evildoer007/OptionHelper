"""Local settings orchestration and secret-safe validation."""

from dataclasses import replace

from ..errors import ValidationError
from .settings_models import (
    MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID,
    MULTI_AGENT_ENABLED_RECOMMENDATION_PRESET_IDS,
    MULTI_AGENT_ENABLED_REVIEW_POLICY_IDS,
    MULTI_AGENT_RECOMMENDATION_PRESET_ROLES,
    MULTI_AGENT_REVIEW_POLICY_ROLES,
    SettingsSnapshot,
)
from ..stores.settings_store import SettingsStore


class SettingsService:
    def __init__(self, store: SettingsStore) -> None:
        self._store = store

    def load(self, principal_id: str) -> SettingsSnapshot:
        loaded = self._store.load(principal_id)
        normalized = self._normalize_multi_agent_roles(loaded)
        if normalized != loaded:
            self._store.save(principal_id, normalized)
        return normalized

    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        settings = self._normalize_multi_agent_roles(settings)
        self._assert_non_sensitive(settings)

        self._store.save(principal_id, settings)

    @staticmethod
    def _normalize_multi_agent_roles(settings: SettingsSnapshot) -> SettingsSnapshot:
        preset_id = (
            "sequential-deliberation"
            if settings.multi_agent_recommendation_preset_id == "adversarial-review"
            else settings.multi_agent_recommendation_preset_id
        )
        preset_role_models = {
            configured_preset_id: {
                role: selection for role, selection in role_models.items()
                if role in MULTI_AGENT_RECOMMENDATION_PRESET_ROLES.get(configured_preset_id, frozenset())
            }
            for configured_preset_id, role_models in settings.multi_agent_preset_role_models.items()
            if configured_preset_id in MULTI_AGENT_ENABLED_RECOMMENDATION_PRESET_IDS
        }
        if settings.multi_agent_recommendation_preset_id == "adversarial-review":
            legacy_roles = settings.multi_agent_preset_role_models.get("adversarial-review", {})
            if legacy_roles and "sequential-deliberation" not in preset_role_models:
                preset_role_models["sequential-deliberation"] = {
                    role: selection for role, selection in legacy_roles.items()
                    if role in MULTI_AGENT_RECOMMENDATION_PRESET_ROLES["sequential-deliberation"]
                }
        return replace(
            settings,
            multi_agent_preset_role_models=preset_role_models,
            multi_agent_recommendation_preset_id=preset_id,
            multi_agent_review_policy_id=(
                MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID
                if settings.multi_agent_review_policy_id == "adversarial-review"
                else settings.multi_agent_review_policy_id
            ),
            multi_agent_review_policy_role_models={
                policy_id: {
                    role: selection for role, selection in role_models.items()
                    if role in MULTI_AGENT_REVIEW_POLICY_ROLES.get(policy_id, frozenset())
                }
                for policy_id, role_models in settings.multi_agent_review_policy_role_models.items()
                if policy_id in MULTI_AGENT_ENABLED_REVIEW_POLICY_IDS
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
        allowed_presets = MULTI_AGENT_ENABLED_RECOMMENDATION_PRESET_IDS
        if settings.multi_agent_recommendation_preset_id not in allowed_presets:
            raise ValidationError("MultiAgent默认预设无效")
        if settings.multi_agent_review_policy_id not in MULTI_AGENT_ENABLED_REVIEW_POLICY_IDS:
            raise ValidationError("MultiAgent复核策略无效")
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
                if role not in MULTI_AGENT_RECOMMENDATION_PRESET_ROLES[preset_id] or (selection.provider_id, selection.model_id) not in enabled:
                    raise ValidationError("MultiAgent角色模型必须引用当前已启用模型")
        for policy_id, role_models in settings.multi_agent_review_policy_role_models.items():
            if policy_id not in MULTI_AGENT_ENABLED_REVIEW_POLICY_IDS or not isinstance(role_models, dict):
                raise ValidationError("MultiAgent复核策略角色模型映射无效")
            for role, selection in role_models.items():
                if role not in MULTI_AGENT_REVIEW_POLICY_ROLES[policy_id] or (selection.provider_id, selection.model_id) not in enabled:
                    raise ValidationError("MultiAgent复核角色模型必须引用当前已启用模型")
