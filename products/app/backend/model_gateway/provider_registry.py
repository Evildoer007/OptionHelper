"""Single registry for approved model provider adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ..errors import UnavailableCapabilityError
from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import ModelServiceSettings


ModelCompletion = Callable[[ModelServiceSettings, SecretRef, list[dict[str, str]]], str]
ModelDecision = Callable[[ModelServiceSettings, SecretRef, Mapping[str, Any]], Mapping[str, Any]]


class ProviderRegistry:
    """Routes configured providers without owning any credential material."""

    _COMPATIBLE_ALIASES = {
        "deepseek-compatible": "openai-compatible",
        "kimi-compatible": "openai-compatible",
    }

    def __init__(self) -> None:
        self._providers: dict[str, ModelCompletion] = {}
        self._decision_providers: dict[str, ModelDecision] = {}

    def register(self, provider_name: str, completion: ModelCompletion, *, decision: ModelDecision | None = None) -> None:
        name = provider_name.strip()
        if not name or not callable(completion):
            raise ValueError("provider_name and completion adapter are required")
        self._providers[name] = completion
        if decision is not None:
            if not callable(decision):
                raise ValueError("decision adapter must be callable")
            self._decision_providers[name] = decision

    def complete(self, settings: ModelServiceSettings, secret_ref: SecretRef, messages: list[dict[str, str]]) -> str:
        completion = self._providers.get(self._canonical_name(settings.provider_name))
        if completion is None:
            raise UnavailableCapabilityError(
                f"model provider {settings.provider_name or 'unconfigured'}",
                "register an approved provider adapter; no external model call was made",
            )
        return completion(settings, secret_ref, messages)

    def decide(self, settings: ModelServiceSettings, secret_ref: SecretRef, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Ask a provider for one structured decision without provider leakage.

        Providers may register a native structured-output adapter.  The small
        fallback keeps the public gateway usable for compatible providers while
        still rejecting non-JSON output rather than guessing an action.
        """

        adapter = self._decision_providers.get(self._canonical_name(settings.provider_name))
        if adapter is not None:
            result = adapter(settings, secret_ref, context)
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
        ])
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("model provider did not return a structured JSON decision") from error
        if not isinstance(value, Mapping):
            raise ValueError("model provider decision must be an object")
        return dict(value)

    @classmethod
    def _canonical_name(cls, provider_name: str) -> str:
        name = provider_name.strip()
        return cls._COMPATIBLE_ALIASES.get(name, name)
