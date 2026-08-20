"""Model execution boundary. This file never embeds provider credentials."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from ..errors import UnavailableCapabilityError
from ..identity.session_identity import SessionIdentity
from ..secrets.secret_provider import SecretProvider
from ..settings.settings_models import ModelSelection, ModelServiceSettings, SettingsSnapshot
from ..settings.settings_service import SettingsService
from .provider_registry import ProviderRegistry


class ModelGateway:
    def __init__(self, settings: SettingsService | Callable[[SessionIdentity], SettingsSnapshot], providers: ProviderRegistry, secrets: SecretProvider) -> None:
        self._settings = settings
        self._providers = providers
        self._secrets = secrets

    def complete_for(self, identity: SessionIdentity, task_id: str, message: str, *, selection: ModelSelection | None = None) -> str:
        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.complete(model, secret_ref, [{"role": "user", "content": message}])

    def decide_for(self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any], *, selection: ModelSelection | None = None) -> Mapping[str, Any]:
        """Provider-neutral structured decision boundary for the App Agent."""
        del task_id
        if not isinstance(context, Mapping):
            raise ValueError("Agent decision context must be an object")
        model, secret_ref = self._configured(identity, selection)
        return self._providers.decide(model, secret_ref, dict(context))

    def capability_for(self, identity: SessionIdentity, *, selection: ModelSelection | None = None) -> dict[str, Any]:
        """Expose only non-secret provider capabilities to App workflows."""
        model, _secret_ref = self._configured(identity, selection)
        return {
            "model_id": model.model_name or model.provider_name,
            "structured_output": True,
            "tool_calling": True,
            "multi_agent": False,
            "max_parallel_agents": 1,
        }

    def _configured(self, identity: SessionIdentity, selection: ModelSelection | None = None) -> tuple[ModelServiceSettings, Any]:
        settings = self._load(identity)
        selected = selection or settings.default_model_selection
        model = settings.model_service
        if selected is not None:
            provider = next((item for item in settings.model_providers if item.provider_id == selected.provider_id), None)
            catalog_model = next(
                (item for item in provider.models if item.model_id == selected.model_id and item.enabled),
                None,
            ) if provider is not None else None
            if provider is None or catalog_model is None or provider.secret_ref is None:
                raise UnavailableCapabilityError(
                    "模型服务",
                    "所选模型未配置、已停用或已更新，请在设置中心选择可用模型后重试。",
                )
            model = ModelServiceSettings("openai-compatible", provider.endpoint, provider.secret_ref, catalog_model.model_id)
        if model.provider_name == "unconfigured":
            raise UnavailableCapabilityError(
                "ModelGateway",
                "请由管理员在设置中心配置模型服务后重试。",
            )
        secret_ref = self._secrets.require_reference(model.secret_ref, "模型服务凭据")
        return model, secret_ref

    def _load(self, identity: SessionIdentity) -> SettingsSnapshot:
        if callable(self._settings):
            return self._settings(identity)
        return self._settings.load(identity.principal_id)
