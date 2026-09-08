"""Settings data that may contain references but never secret material."""

from dataclasses import dataclass, field
from typing import Any, Literal

from ..secrets.secret_ref import SecretRef


RoleName = Literal["sales", "admin"]
MULTI_AGENT_RECOMMENDATION_PRESET_ROLES = {
    "sequential-deliberation": frozenset({"Interpreter", "Selector", "Reviewer"}),
    "product-trader-loop": frozenset({"Structurer", "Trader", "Reviewer"}),
    "independent-council": frozenset({"Framer", "Matcher", "Hedger", "Moderator"}),
    "constraint-ranking": frozenset({"Specifier", "Generator", "Evaluator", "Reviewer"}),
}
MULTI_AGENT_LEGACY_ROLE_ALIASES: dict[str, str] = {
    "Intent": "Interpreter",
    "Research": "Selector",
    "Critic": "Reviewer",
}
MULTI_AGENT_RECOMMENDATION_ROLES = frozenset().union(*MULTI_AGENT_RECOMMENDATION_PRESET_ROLES.values())
MULTI_AGENT_ENABLED_RECOMMENDATION_PRESET_IDS = frozenset(MULTI_AGENT_RECOMMENDATION_PRESET_ROLES)
MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID = "standard-review"
MULTI_AGENT_ENABLED_REVIEW_POLICY_IDS = frozenset({MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID})
MULTI_AGENT_REVIEW_POLICY_ROLES = {
    MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID: frozenset(),
}


@dataclass(frozen=True)
class ModelServiceSettings:
    provider_name: str
    endpoint: str
    secret_ref: SecretRef | None = None
    model_name: str = ""


@dataclass(frozen=True)
class ModelConnectionSettings:
    """One device-owned model profile.

    The model service remains a small value object so existing runtime and
    provider adapters stay unchanged.  A connection only adds a stable local
    id and a display label around it; the credential is still represented by
    an opaque Host-owned reference.
    """

    connection_id: str
    display_name: str
    model_service: ModelServiceSettings


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One selectable model published by a configured provider."""

    model_id: str
    display_name: str = ""
    enabled: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    reasoning_support: bool = False
    tool_calling: bool = True


@dataclass(frozen=True)
class ModelProviderProfile:
    """A local or tenant provider with one credential and many models."""

    provider_id: str
    display_name: str
    endpoint: str
    protocol: str = "openai-chat-completions"
    secret_ref: SecretRef | None = None
    models: tuple[ModelCatalogEntry, ...] = ()


@dataclass(frozen=True)
class ModelSelection:
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class DataInterfaceSettings:
    provider_name: str
    secret_ref: SecretRef | None = None


@dataclass(frozen=True)
class StorageExportSettings:
    export_location_ref: str | None = None
    allow_user_selected_directory: bool = True


@dataclass(frozen=True)
class PreferenceSettings:
    language: str = "zh-CN"
    font_scale: float = 1.0
    theme: Literal["light", "dark", "auto"] = "light"


@dataclass(frozen=True)
class SettingsSnapshot:
    role: RoleName
    model_service: ModelServiceSettings
    data_interface: DataInterfaceSettings
    storage_export: StorageExportSettings
    preferences: PreferenceSettings
    model_connections: tuple[ModelConnectionSettings, ...] = ()
    active_model_connection_id: str | None = None
    model_providers: tuple[ModelProviderProfile, ...] = ()
    default_model_selection: ModelSelection | None = None
    recommendation_execution_mode: str = "single"
    multi_agent_recommendation_preset_id: str = "sequential-deliberation"
    multi_agent_preset_role_models: dict[str, dict[str, ModelSelection]] = field(default_factory=dict)
    multi_agent_preset_agent_instructions: dict[str, dict[str, str]] = field(default_factory=dict)
    multi_agent_review_policy_id: str = MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID
    multi_agent_review_policy_role_models: dict[str, dict[str, ModelSelection]] = field(default_factory=dict)


def serialize_settings(snapshot: SettingsSnapshot) -> dict[str, Any]:
    return {
        "role": snapshot.role,
        "model_service": {
            "provider_name": snapshot.model_service.provider_name,
            "endpoint": snapshot.model_service.endpoint,
            "model_name": snapshot.model_service.model_name,
            "secret_ref": snapshot.model_service.secret_ref.redacted() if snapshot.model_service.secret_ref else None,
        },
        "model_connections": [
            {
                "connection_id": connection.connection_id,
                "display_name": connection.display_name,
                "provider_name": connection.model_service.provider_name,
                "endpoint": connection.model_service.endpoint,
                "model_name": connection.model_service.model_name,
                "secret_ref": connection.model_service.secret_ref.redacted() if connection.model_service.secret_ref else None,
            }
            for connection in snapshot.model_connections
        ],
        "active_model_connection_id": snapshot.active_model_connection_id,
        "model_providers": [
            {
                "provider_id": provider.provider_id,
                "display_name": provider.display_name,
                "endpoint": provider.endpoint,
                "protocol": provider.protocol,
                "secret_ref": provider.secret_ref.redacted() if provider.secret_ref else None,
                "models": [
                    {
                        "model_id": model.model_id,
                        "display_name": model.display_name,
                        "enabled": model.enabled,
                        "context_window": model.context_window,
                        "max_output_tokens": model.max_output_tokens,
                        "input_modalities": list(model.input_modalities),
                        "reasoning_support": model.reasoning_support,
                        "tool_calling": model.tool_calling,
                    }
                    for model in provider.models
                ],
            }
            for provider in snapshot.model_providers
        ],
        "default_model_selection": (
            {"provider_id": snapshot.default_model_selection.provider_id, "model_id": snapshot.default_model_selection.model_id}
            if snapshot.default_model_selection else None
        ),
        "recommendation_execution_mode": snapshot.recommendation_execution_mode,
        "multi_agent_recommendation_preset_id": snapshot.multi_agent_recommendation_preset_id,
        "multi_agent_preset_role_models": {
            preset_id: {
                MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role): {
                    "provider_id": selection.provider_id,
                    "model_id": selection.model_id,
                }
                for role, selection in role_models.items()
                if MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role) in MULTI_AGENT_RECOMMENDATION_ROLES
            }
            for preset_id, role_models in snapshot.multi_agent_preset_role_models.items()
        },
        "multi_agent_preset_agent_instructions": {
            preset_id: {
                MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role): content
                for role, content in role_instructions.items()
                if MULTI_AGENT_LEGACY_ROLE_ALIASES.get(role, role) in MULTI_AGENT_RECOMMENDATION_ROLES
            }
            for preset_id, role_instructions in snapshot.multi_agent_preset_agent_instructions.items()
        },
        "multi_agent_review_policy_id": snapshot.multi_agent_review_policy_id,
        "multi_agent_review_policy_role_models": {
            policy_id: {
                role: {"provider_id": selection.provider_id, "model_id": selection.model_id}
                for role, selection in role_models.items()
            }
            for policy_id, role_models in snapshot.multi_agent_review_policy_role_models.items()
        },
        "data_interface": {
            "provider_name": snapshot.data_interface.provider_name,
            "secret_ref": snapshot.data_interface.secret_ref.redacted() if snapshot.data_interface.secret_ref else None,
        },
        "storage_export": {
            "export_location_ref": snapshot.storage_export.export_location_ref,
            "allow_user_selected_directory": snapshot.storage_export.allow_user_selected_directory,
        },
        "preferences": {
            "language": snapshot.preferences.language,
            "font_scale": snapshot.preferences.font_scale,
            "theme": snapshot.preferences.theme,
        },
    }


def serialize_settings_public(snapshot: SettingsSnapshot) -> dict[str, Any]:
    """Return browser-safe settings without exposing Host-owned references."""

    value = serialize_settings(snapshot)
    model = value["model_service"]
    data = value["data_interface"]
    model["credential_configured"] = model.pop("secret_ref") is not None
    connections = []
    for connection in value.get("model_connections", []):
        public_connection = dict(connection)
        public_connection["credential_configured"] = public_connection.pop("secret_ref") is not None
        connections.append(public_connection)
    value["model_connections"] = connections
    providers = []
    for provider in value.get("model_providers", []):
        public_provider = dict(provider)
        public_provider["credential_configured"] = public_provider.pop("secret_ref") is not None
        providers.append(public_provider)
    value["model_providers"] = providers
    data["credential_configured"] = data.pop("secret_ref") is not None
    return value


def deserialize_settings(value: dict[str, Any]) -> SettingsSnapshot:
    def secret_ref(raw: object) -> SecretRef | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("secret_ref must be an object")
        return SecretRef(provider=str(raw["provider"]), key=str(raw["key"]), revision=raw.get("revision"))

    model = value.get("model_service", {})
    data = value.get("data_interface", {})
    storage = value.get("storage_export", {})
    preferences = value.get("preferences", {})
    raw_connections = value.get("model_connections", [])
    raw_providers = value.get("model_providers", [])
    principal_role = value.get("role", "sales")
    if principal_role not in ("sales", "admin"):
        raise ValueError("role must be sales or admin")
    if not isinstance(raw_connections, list):
        raise ValueError("model_connections must be a list")
    if not isinstance(raw_providers, list):
        raise ValueError("model_providers must be a list")
    connections: list[ModelConnectionSettings] = []
    for raw_connection in raw_connections:
        if not isinstance(raw_connection, dict):
            raise ValueError("model connection must be an object")
        connection_model = raw_connection.get("model_service", raw_connection)
        if not isinstance(connection_model, dict):
            raise ValueError("model connection model must be an object")
        connections.append(ModelConnectionSettings(
            connection_id=str(raw_connection.get("connection_id", "")),
            display_name=str(raw_connection.get("display_name", "")),
            model_service=ModelServiceSettings(
                provider_name=str(connection_model.get("provider_name", "unconfigured")),
                endpoint=str(connection_model.get("endpoint", "")),
                secret_ref=secret_ref(connection_model.get("secret_ref")),
                model_name=str(connection_model.get("model_name", "")),
            ),
        ))
    active_connection = value.get("active_model_connection_id")
    if active_connection is not None:
        active_connection = str(active_connection)
    providers: list[ModelProviderProfile] = []
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            raise ValueError("model provider must be an object")
        raw_models = raw_provider.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError("model provider models must be a list")
        models: list[ModelCatalogEntry] = []
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                raise ValueError("model catalog entry must be an object")
            models.append(ModelCatalogEntry(
                model_id=str(raw_model.get("model_id", raw_model.get("id", ""))),
                display_name=str(raw_model.get("display_name", raw_model.get("name", ""))),
                enabled=bool(raw_model.get("enabled", True)),
                context_window=_positive_int_or_none(raw_model.get("context_window")),
                max_output_tokens=_positive_int_or_none(raw_model.get("max_output_tokens")),
                input_modalities=_input_modalities(raw_model.get("input_modalities")),
                reasoning_support=bool(raw_model.get("reasoning_support", False)),
                tool_calling=bool(raw_model.get("tool_calling", True)),
            ))
        providers.append(ModelProviderProfile(
            provider_id=str(raw_provider.get("provider_id", "")),
            display_name=str(raw_provider.get("display_name", "")),
            endpoint=str(raw_provider.get("endpoint", "")),
            protocol=str(raw_provider.get("protocol", "openai-chat-completions")),
            secret_ref=secret_ref(raw_provider.get("secret_ref")),
            models=tuple(models),
        ))
    raw_selection = value.get("default_model_selection")
    selection = None
    if raw_selection is not None:
        if not isinstance(raw_selection, dict):
            raise ValueError("default_model_selection must be an object")
        selection = ModelSelection(
            provider_id=str(raw_selection.get("provider_id", "")),
            model_id=str(raw_selection.get("model_id", "")),
        )
    raw_role_models = value.get("multi_agent_preset_role_models", {})
    if not isinstance(raw_role_models, dict):
        raise ValueError("multi_agent_preset_role_models must be an object")
    role_models: dict[str, dict[str, ModelSelection]] = {}
    for preset_id, raw_roles in raw_role_models.items():
        if not isinstance(raw_roles, dict):
            raise ValueError("multi-agent preset role models must be an object")
        role_models[str(preset_id)] = {}
        for role_name, raw_role_selection in raw_roles.items():
            role = MULTI_AGENT_LEGACY_ROLE_ALIASES.get(str(role_name), str(role_name))
            if role not in MULTI_AGENT_RECOMMENDATION_ROLES:
                continue
            if not isinstance(raw_role_selection, dict):
                raise ValueError("multi-agent role model selection must be an object")
            role_models[str(preset_id)][role] = ModelSelection(
                provider_id=str(raw_role_selection.get("provider_id", "")),
                model_id=str(raw_role_selection.get("model_id", "")),
            )
    raw_agent_instructions = value.get("multi_agent_preset_agent_instructions", {})
    if not isinstance(raw_agent_instructions, dict):
        raise ValueError("multi_agent_preset_agent_instructions must be an object")
    agent_instructions: dict[str, dict[str, str]] = {}
    for preset_id, raw_roles in raw_agent_instructions.items():
        if not isinstance(raw_roles, dict):
            raise ValueError("multi-agent preset agent instructions must be an object")
        agent_instructions[str(preset_id)] = {}
        for role_name, raw_content in raw_roles.items():
            role = MULTI_AGENT_LEGACY_ROLE_ALIASES.get(str(role_name), str(role_name))
            if role not in MULTI_AGENT_RECOMMENDATION_ROLES:
                continue
            if not isinstance(raw_content, str):
                raise ValueError("multi-agent agent instruction must be a string")
            agent_instructions[str(preset_id)][role] = raw_content
    raw_review_role_models = value.get("multi_agent_review_policy_role_models", {})
    if not isinstance(raw_review_role_models, dict):
        raise ValueError("multi_agent_review_policy_role_models must be an object")
    review_role_models: dict[str, dict[str, ModelSelection]] = {}
    for policy_id, raw_roles in raw_review_role_models.items():
        if not isinstance(raw_roles, dict):
            raise ValueError("multi-agent review policy role models must be an object")
        review_role_models[str(policy_id)] = {}
        for role_name, raw_role_selection in raw_roles.items():
            if not isinstance(role_name, str) or not role_name.strip():
                raise ValueError("multi-agent review policy role must be a non-empty string")
            if not isinstance(raw_role_selection, dict):
                raise ValueError("multi-agent review role model selection must be an object")
            review_role_models[str(policy_id)][role_name] = ModelSelection(
                provider_id=str(raw_role_selection.get("provider_id", "")),
                model_id=str(raw_role_selection.get("model_id", "")),
            )
    preset_id = str(value.get("multi_agent_recommendation_preset_id", "sequential-deliberation"))
    if preset_id == "adversarial-review":
        legacy_models = role_models.pop("adversarial-review", {})
        if legacy_models and "sequential-deliberation" not in role_models:
            role_models["sequential-deliberation"] = legacy_models
        preset_id = "sequential-deliberation"
    review_policy_id = str(value.get("multi_agent_review_policy_id", MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID))
    if review_policy_id == "adversarial-review":
        review_policy_id = MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID
    return SettingsSnapshot(
        role=principal_role,
        model_service=ModelServiceSettings(
            provider_name=str(model.get("provider_name", "unconfigured")),
            endpoint=str(model.get("endpoint", "")),
            secret_ref=secret_ref(model.get("secret_ref")),
            model_name=str(model.get("model_name", "")),
        ),
        data_interface=DataInterfaceSettings(
            provider_name=str(data.get("provider_name", "unconfigured")),
            secret_ref=secret_ref(data.get("secret_ref")),
        ),
        storage_export=StorageExportSettings(
            export_location_ref=storage.get("export_location_ref"),
            allow_user_selected_directory=bool(storage.get("allow_user_selected_directory", True)),
        ),
        preferences=PreferenceSettings(
            language=str(preferences.get("language", "zh-CN")),
            font_scale=float(preferences.get("font_scale", 1.0)),
            theme=preferences.get("theme", "light"),
        ),
        model_connections=tuple(connections),
        active_model_connection_id=active_connection,
        model_providers=tuple(providers),
        default_model_selection=selection,
        recommendation_execution_mode=str(value.get("recommendation_execution_mode", "single")),
        multi_agent_recommendation_preset_id=preset_id,
        multi_agent_preset_role_models=role_models,
        multi_agent_preset_agent_instructions=agent_instructions,
        multi_agent_review_policy_id=review_policy_id,
        multi_agent_review_policy_role_models=review_role_models,
    )


def _positive_int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("model capacity must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("model capacity must be a positive integer")
    return parsed


def _input_modalities(value: object) -> tuple[str, ...]:
    if value is None:
        return ("text",)
    if not isinstance(value, list):
        raise ValueError("input_modalities must be an array")
    modalities = tuple(dict.fromkeys(str(item).strip().lower() for item in value))
    if not modalities or modalities[0] != "text" or any(item not in {"text", "image"} for item in modalities):
        raise ValueError("input_modalities must start with text and may include image")
    return modalities
