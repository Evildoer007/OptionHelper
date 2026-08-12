"""Model execution boundary. This file never embeds provider credentials."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from ..errors import UnavailableCapabilityError
from ..identity.session_identity import SessionIdentity
from ..secrets.secret_provider import SecretProvider
from ..settings.settings_models import SettingsSnapshot
from ..settings.settings_service import SettingsService
from .provider_registry import ProviderRegistry


class ModelGateway:
    def __init__(self, settings: SettingsService | Callable[[SessionIdentity], SettingsSnapshot], providers: ProviderRegistry, secrets: SecretProvider) -> None:
        self._settings = settings
        self._providers = providers
        self._secrets = secrets

    def complete_for(self, identity: SessionIdentity, task_id: str, message: str) -> str:
        del task_id
        settings, secret_ref = self._configured(identity)
        return self._providers.complete(settings.model_service, secret_ref, [{"role": "user", "content": message}])

    def decide_for(self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Provider-neutral structured decision boundary for the App Agent."""
        del task_id
        if not isinstance(context, Mapping):
            raise ValueError("Agent decision context must be an object")
        settings, secret_ref = self._configured(identity)
        return self._providers.decide(settings.model_service, secret_ref, dict(context))

    def capability_for(self, identity: SessionIdentity) -> dict[str, Any]:
        """Expose only non-secret provider capabilities to App workflows."""
        settings = self._load(identity)
        model = settings.model_service
        if model.provider_name == "unconfigured":
            raise UnavailableCapabilityError(
                "ModelGateway",
                "请由管理员在设置中心配置模型服务后重试。",
            )
        self._secrets.require_reference(model.secret_ref, "模型服务凭据")
        return {
            "model_id": model.provider_name,
            "structured_output": True,
            "tool_calling": True,
            "multi_agent": False,
            "max_parallel_agents": 1,
        }

    def _configured(self, identity: SessionIdentity) -> tuple[Any, Any]:
        settings = self._load(identity)
        model = settings.model_service
        if model.provider_name == "unconfigured":
            raise UnavailableCapabilityError(
                "ModelGateway",
                "请由管理员在设置中心配置模型服务后重试。",
            )
        secret_ref = self._secrets.require_reference(model.secret_ref, "模型服务凭据")
        return settings, secret_ref

    def _load(self, identity: SessionIdentity) -> SettingsSnapshot:
        if callable(self._settings):
            return self._settings(identity)
        return self._settings.load(identity.principal_id)
