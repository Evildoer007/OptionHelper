"""Model execution boundary. This file never embeds provider credentials."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import UnavailableCapabilityError
from ..identity.session_identity import SessionIdentity
from ..secrets.secret_provider import SecretProvider
from ..settings.settings_models import (
    MULTI_AGENT_RECOMMENDATION_ROLES,
    ModelSelection,
    ModelServiceSettings,
    SettingsSnapshot,
)
from ..settings.settings_service import SettingsService
from .provider_registry import ProviderRegistry
from .request_control import ModelRequestControl


@dataclass(frozen=True)
class ResolvedAgentModelRoute:
    """Frozen model identity and dispatch selection for one AgentRun."""

    model_selection: ModelSelection
    dispatch_selection: ModelSelection | None
    source: str


class ModelGateway:
    def __init__(self, settings: SettingsService | Callable[[SessionIdentity], SettingsSnapshot], providers: ProviderRegistry, secrets: SecretProvider) -> None:
        self._settings = settings
        self._providers = providers
        self._secrets = secrets

    def complete_for(
        self, identity: SessionIdentity, task_id: str, message: str, *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> str:
        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.complete(
            model, secret_ref, [{"role": "user", "content": message}], request_control=request_control,
        )

    def complete_with_metadata_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.complete_with_metadata(
            model,
            secret_ref,
            [{"role": "user", "content": message}],
            request_control=request_control,
        )

    def decide_for(
        self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any], *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        """Provider-neutral structured decision boundary for the App Agent."""
        del task_id
        if not isinstance(context, Mapping):
            raise ValueError("Agent decision context must be an object")
        model, secret_ref = self._configured(identity, selection)
        return self._providers.decide(model, secret_ref, dict(context), request_control=request_control)

    def resolve_agent_model_route(
        self,
        identity: SessionIdentity,
        *,
        explicit_selection: ModelSelection | None,
        preset_role_selection: ModelSelection | None,
    ) -> ResolvedAgentModelRoute:
        """Resolve and validate one role route without silent provider fallback."""

        settings = self._load(identity)
        if explicit_selection is not None:
            selected = explicit_selection
            source = "user_explicit"
        elif preset_role_selection is not None:
            selected = preset_role_selection
            source = "preset_role_default"
        else:
            selected = settings.default_model_selection
            source = "session_default"
        model, _secret_ref = self._configured(identity, selected)
        if selected is not None:
            return ResolvedAgentModelRoute(selected, selected, source)
        audit_selection = ModelSelection(
            provider_id=model.provider_name,
            model_id=model.model_name or model.provider_name,
        )
        return ResolvedAgentModelRoute(audit_selection, None, source)

    def multi_agent_role_model_selections_for(
        self, identity: SessionIdentity, preset_id: str,
    ) -> Mapping[str, ModelSelection]:
        settings = self._load(identity)
        configured = settings.multi_agent_preset_role_models.get(str(preset_id), {})
        return {
            role: selection for role, selection in configured.items()
            if role in MULTI_AGENT_RECOMMENDATION_ROLES
        }

    def multi_agent_recommendation_preset_for(self, identity: SessionIdentity) -> str:
        return self._load(identity).multi_agent_recommendation_preset_id

    def multi_agent_review_policy_for(self, identity: SessionIdentity) -> str:
        return self._load(identity).multi_agent_review_policy_id

    def multi_agent_review_policy_role_model_selections_for(
        self, identity: SessionIdentity, policy_id: str,
    ) -> Mapping[str, ModelSelection]:
        """Return the selected policy-local role routes without inheriting preset slots."""

        return dict(self._load(identity).multi_agent_review_policy_role_models.get(str(policy_id), {}))

    def require_bounded_request_control(
        self, identity: SessionIdentity, *, selection: ModelSelection | None,
    ) -> None:
        model, _secret_ref = self._configured(identity, selection)
        if not self._providers.supports_bounded_request_control(model.provider_name):
            raise UnavailableCapabilityError(
                "bounded_request_control",
                "当前模型Provider未声明受控请求期限，不能启动多Agent子运行。",
            )

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

    def model_binding_for(self, identity: SessionIdentity, *, selection: ModelSelection | None = None) -> dict[str, str]:
        """Return the non-secret resolved model identity for one AgentRun audit event."""

        model, _secret_ref = self._configured(identity, selection)
        return {"provider_id": model.provider_name, "model_id": model.model_name or model.provider_name}

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
