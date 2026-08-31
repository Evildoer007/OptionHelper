"""Model execution boundary. This file never embeds provider credentials."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any, Callable

from ..errors import UnavailableCapabilityError
from ..identity.session_identity import SessionIdentity
from ..secrets.secret_provider import SecretProvider
from ..settings.settings_models import (
    MULTI_AGENT_LEGACY_ROLE_ALIASES,
    MULTI_AGENT_RECOMMENDATION_ROLES,
    ModelSelection,
    ModelServiceSettings,
    SettingsSnapshot,
)
from ..settings.settings_service import SettingsService
from .provider_registry import ProviderRegistry
from .request_control import ModelRequestControl
from .capability_probe import probe_provider_capabilities


SETTINGS_MODEL_CAPABILITY_PROBE_SCHEMA_ID = "optionhelper.settings-model-capability-probe"


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

    def complete_messages_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> str:
        """Complete an Agent turn without flattening role or tool boundaries."""

        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.complete(
            model,
            secret_ref,
            _validated_messages(messages),
            request_control=request_control,
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

    def complete_messages_with_metadata_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.complete_with_metadata(
            model,
            secret_ref,
            _validated_messages(messages),
            request_control=request_control,
        )

    def stream_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Stream one configured model response through the provider registry."""

        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.stream(
            model,
            secret_ref,
            [{"role": "user", "content": message}],
            request_control=request_control,
        )

    def stream_messages_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        selection: ModelSelection | None = None,
        request_control: ModelRequestControl | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Stream an Agent turn while preserving assistant and tool messages."""

        del task_id
        model, secret_ref = self._configured(identity, selection)
        return self._providers.stream(
            model,
            secret_ref,
            _validated_messages(messages),
            request_control=request_control,
            tools=list(tools or ()),
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
            MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role): selection
            for role, selection in configured.items()
            if MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role) in MULTI_AGENT_RECOMMENDATION_ROLES
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
        catalog_model = self._selected_catalog_model(identity, selection)
        return {
            "model_id": model.model_name or model.provider_name,
            "structured_output": True,
            "input_modalities": list(catalog_model.input_modalities) if catalog_model is not None else ["text"],
            "context_window": catalog_model.context_window if catalog_model is not None else None,
            "max_output_tokens": catalog_model.max_output_tokens if catalog_model is not None else None,
            "reasoning_support": catalog_model.reasoning_support if catalog_model is not None else False,
            "tool_calling": catalog_model.tool_calling if catalog_model is not None else False,
            "multi_agent": False,
            "max_parallel_agents": 1,
        }

    def probe_capabilities_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        selection: ModelSelection | None = None,
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any]:
        """Actively probe a configured Provider without exposing its credential."""

        del task_id
        model, secret_ref = self._configured(identity, selection)

        def invoke(messages, tools, control):
            return self._providers.stream(
                model,
                secret_ref,
                [dict(item) for item in messages],
                request_control=control,
                tools=[dict(item) for item in tools],
            )

        return probe_provider_capabilities(
            invoke,
            timeout_seconds=timeout_seconds,
            bounded_request_control=self._providers.supports_bounded_request_control(model.provider_name),
        )

    def probe_settings_capabilities_for(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        selection: ModelSelection | None = None,
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any]:
        """Return the stable, non-secret capability contract consumed by Settings.

        Declared catalog metadata and actively observed behavior remain
        separate.  Settings must not advertise tool-capable multi-Agent use
        unless both sources agree and the bounded request probe succeeds.
        """

        declared = self.capability_for(identity, selection=selection)
        observed = self.probe_capabilities_for(
            identity,
            task_id,
            selection=selection,
            timeout_seconds=timeout_seconds,
        )
        binding = self.model_binding_for(identity, selection=selection)
        effective = {
            "streaming": bool(observed.get("streaming")),
            "tool_calling": bool(declared.get("tool_calling")) and bool(observed.get("tool_calling")),
            "tool_result_continuation": bool(observed.get("tool_result_continuation")),
            "reasoning": bool(declared.get("reasoning_support")) and bool(observed.get("reasoning")),
            "cancellation": bool(observed.get("cancellation")),
            "bounded_timeout": bool(observed.get("bounded_timeout")),
        }
        verified = bool(observed.get("verified")) and all(
            effective[key]
            for key in ("streaming", "tool_calling", "tool_result_continuation", "cancellation", "bounded_timeout")
        )
        return {
            "schema": SETTINGS_MODEL_CAPABILITY_PROBE_SCHEMA_ID,
            "status": "verified" if verified else "unverified",
            "detected": True,
            "initialized": bool(observed.get("streaming")),
            "verified": verified,
            "model": dict(binding),
            "declared": dict(declared),
            "observed": dict(observed),
            "effective": effective,
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

    def _selected_catalog_model(
        self, identity: SessionIdentity, selection: ModelSelection | None,
    ) -> Any | None:
        """Find the exact enabled catalog entry behind a selected model.

        Legacy single-model connections do not have a capability declaration;
        their conservative text-only fallback remains intentional.
        """

        settings = self._load(identity)
        selected = selection or settings.default_model_selection
        if selected is None:
            return None
        provider = next(
            (item for item in settings.model_providers if item.provider_id == selected.provider_id),
            None,
        )
        if provider is None:
            return None
        return next(
            (item for item in provider.models if item.model_id == selected.model_id and item.enabled),
            None,
        )


def _validated_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(messages, (str, bytes)) or not messages:
        raise ValueError("messages must be a non-empty sequence")
    allowed_roles = {"system", "user", "assistant", "tool"}
    result: list[dict[str, Any]] = []
    total_image_bytes = 0
    for item in messages:
        if not isinstance(item, Mapping):
            raise ValueError("each message must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in allowed_roles or not isinstance(content, (str, list)):
            raise ValueError("each message must contain a valid role and content")
        if isinstance(content, list):
            if role != "user" or not content or len(content) > 24:
                raise ValueError("multimodal content is only valid for user messages")
            parts: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") not in {"text", "image_url"}:
                    raise ValueError("multimodal content block is invalid")
                if block.get("type") == "text":
                    text = block.get("text")
                    if not isinstance(text, str) or len(text) > 128_000:
                        raise ValueError("multimodal text block is invalid")
                    parts.append({"type": "text", "text": text})
                    continue
                image_url = block.get("image_url")
                url = image_url.get("url") if isinstance(image_url, Mapping) else None
                if (
                    not isinstance(url, str)
                    or len(url) > 6 * 1024 * 1024
                    or not re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+", url)
                ):
                    raise ValueError("multimodal image block is invalid")
                encoded = url.split(",", 1)[1]
                total_image_bytes += len(encoded.rstrip("=")) * 3 // 4
                if total_image_bytes > 32 * 1024 * 1024:
                    raise ValueError("multimodal image input exceeds the request limit")
                parts.append({"type": "image_url", "image_url": {"url": url}})
            content = parts
        message: dict[str, Any] = {"role": str(role), "content": content}
        if role == "assistant" and item.get("tool_calls") is not None:
            tool_calls = item.get("tool_calls")
            if not isinstance(tool_calls, list):
                raise ValueError("assistant tool_calls must be an array")
            message["tool_calls"] = [dict(call) for call in tool_calls if isinstance(call, Mapping)]
        if role == "tool":
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                raise ValueError("tool messages require tool_call_id")
            message["tool_call_id"] = tool_call_id.strip()
        result.append(message)
    return result
