"""Single registry for approved model provider adapters."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..errors import UnavailableCapabilityError
from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import ModelServiceSettings
from .request_control import ModelRequestControl


ModelCompletion = Callable[..., str]
ModelCompletionWithMetadata = Callable[..., Mapping[str, Any]]
ModelDecision = Callable[..., Mapping[str, Any]]
ModelStream = Callable[..., Iterable[Mapping[str, Any]]]


class ProviderRegistry:
    """Routes configured providers without owning any credential material."""

    _COMPATIBLE_ALIASES = {
        "deepseek-compatible": "openai-compatible",
        "kimi-compatible": "openai-compatible",
    }

    def __init__(self) -> None:
        self._providers: dict[str, ModelCompletion] = {}
        self._decision_providers: dict[str, ModelDecision] = {}
        self._metadata_providers: dict[str, ModelCompletionWithMetadata] = {}
        self._stream_providers: dict[str, ModelStream] = {}
        self._bounded_request_control: dict[str, bool] = {}

    def register(
        self,
        provider_name: str,
        completion: ModelCompletion,
        *,
        decision: ModelDecision | None = None,
        completion_with_metadata: ModelCompletionWithMetadata | None = None,
        stream: ModelStream | None = None,
        bounded_request_control: bool = False,
    ) -> None:
        name = provider_name.strip()
        if not name or not callable(completion):
            raise ValueError("provider_name and completion adapter are required")
        self._providers[name] = completion
        self._bounded_request_control[name] = bool(bounded_request_control)
        if decision is not None:
            if not callable(decision):
                raise ValueError("decision adapter must be callable")
            self._decision_providers[name] = decision
        if completion_with_metadata is not None:
            if not callable(completion_with_metadata):
                raise ValueError("completion_with_metadata adapter must be callable")
            self._metadata_providers[name] = completion_with_metadata
        if stream is not None:
            if not callable(stream):
                raise ValueError("stream adapter must be callable")
            self._stream_providers[name] = stream

    def complete(
        self, settings: ModelServiceSettings, secret_ref: SecretRef, messages: list[dict[str, str]], *,
        request_control: ModelRequestControl | None = None,
    ) -> str:
        completion = self._providers.get(self._canonical_name(settings.provider_name))
        if completion is None:
            raise UnavailableCapabilityError(
                f"model provider {settings.provider_name or 'unconfigured'}",
                "register an approved provider adapter; no external model call was made",
            )
        provider_name = self._canonical_name(settings.provider_name)
        return _invoke_adapter(
            completion, settings, secret_ref, messages, request_control=request_control,
            bounded_request_control=self._bounded_request_control.get(provider_name, False),
        )

    def decide(
        self, settings: ModelServiceSettings, secret_ref: SecretRef, context: Mapping[str, Any], *,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        """Ask a provider for one structured decision without provider leakage.

        Providers may register a native structured-output adapter.  The small
        fallback keeps the public gateway usable for compatible providers while
        still rejecting non-JSON output rather than guessing an action.
        """

        adapter = self._decision_providers.get(self._canonical_name(settings.provider_name))
        if adapter is not None:
            provider_name = self._canonical_name(settings.provider_name)
            result = _invoke_adapter(
                adapter, settings, secret_ref, context, request_control=request_control,
                bounded_request_control=self._bounded_request_control.get(provider_name, False),
            )
            if not isinstance(result, Mapping):
                raise ValueError("model decision adapter must return an object")
            return dict(result)
        fixed_step = context.get("operation") == "recommender_fixed_step"
        instruction = (
            "Return exactly one JSON object {action:'final',result:{...}} for the supplied fixed Recommender step. "
            if fixed_step else
            "Return exactly one JSON object for the supplied structured decision protocol. "
        )
        instruction += "Do not include markdown, hidden reasoning, credentials or financial calculations."
        raw = self.complete(settings, secret_ref, [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), default=str)},
        ], request_control=request_control)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("model provider did not return a structured JSON decision") from error
        if not isinstance(value, Mapping):
            raise ValueError("model provider decision must be an object")
        return dict(value)

    def complete_with_metadata(
        self,
        settings: ModelServiceSettings,
        secret_ref: SecretRef,
        messages: list[dict[str, str]],
        *,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        provider_name = self._canonical_name(settings.provider_name)
        adapter = self._metadata_providers.get(provider_name)
        if adapter is None:
            return {"text": self.complete(
                settings, secret_ref, messages, request_control=request_control,
            ), "usage": None}
        value = _invoke_adapter(
            adapter,
            settings,
            secret_ref,
            messages,
            request_control=request_control,
            bounded_request_control=self._bounded_request_control.get(provider_name, False),
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("text"), str):
            raise ValueError("model metadata adapter must return text and optional usage")
        usage = value.get("usage")
        if usage is not None and not isinstance(usage, Mapping):
            raise ValueError("model metadata usage must be an object when present")
        return {"text": str(value["text"]), "usage": dict(usage) if isinstance(usage, Mapping) else None}

    def stream(
        self,
        settings: ModelServiceSettings,
        secret_ref: SecretRef,
        messages: list[dict[str, Any]],
        *,
        request_control: ModelRequestControl | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Return the provider's normalized stream without owning secrets.

        Streaming is optional.  Providers that only implement the existing
        completion contract remain fully compatible and fail explicitly when a
        caller requests streaming instead of silently falling back to a
        partial or fabricated stream.
        """

        provider_name = self._canonical_name(settings.provider_name)
        adapter = self._stream_providers.get(provider_name)
        if adapter is None:
            raise UnavailableCapabilityError(
                "模型流式输出",
                "当前Provider未注册流式适配器，请继续使用非流式模型调用。",
            )
        adapter_kwargs: dict[str, Any] = {}
        if tools:
            parameters = inspect.signature(adapter).parameters.values()
            if not any(item.name == "tools" or item.kind == item.VAR_KEYWORD for item in parameters):
                raise UnavailableCapabilityError(
                    "模型工具调用",
                    "当前Provider流式适配器未声明工具Schema支持。",
                )
            adapter_kwargs["tools"] = tools
        value = _invoke_adapter(
            adapter,
            settings,
            secret_ref,
            messages,
            request_control=request_control,
            bounded_request_control=self._bounded_request_control.get(provider_name, False),
            adapter_kwargs=adapter_kwargs,
        )
        if isinstance(value, (str, bytes)) or value is None:
            raise ValueError("model stream adapter must return an iterable of events")
        try:
            iter(value)
        except TypeError as error:
            raise ValueError("model stream adapter must return an iterable of events") from error
        return value

    @classmethod
    def _canonical_name(cls, provider_name: str) -> str:
        name = provider_name.strip()
        return cls._COMPATIBLE_ALIASES.get(name, name)

    def supports_bounded_request_control(self, provider_name: str) -> bool:
        return self._bounded_request_control.get(self._canonical_name(provider_name), False)


def _invoke_adapter(
    adapter: Callable[..., Any],
    *args: Any,
    request_control: ModelRequestControl | None,
    bounded_request_control: bool = False,
    adapter_kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Use only the capability declared at provider registration."""

    if request_control is not None and not bounded_request_control:
        raise UnavailableCapabilityError(
            "bounded_request_control",
            "当前Provider未声明受控请求期限，不能用于多Agent子运行。",
        )
    if bounded_request_control:
        return adapter(*args, request_control=request_control, **dict(adapter_kwargs or {}))
    return adapter(*args, **dict(adapter_kwargs or {}))
