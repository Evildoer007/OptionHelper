"""Settings data that may contain references but never secret material."""

from dataclasses import dataclass
from typing import Any, Literal

from ..secrets.secret_ref import SecretRef


RoleName = Literal["sales", "admin"]


@dataclass(frozen=True)
class ModelServiceSettings:
    provider_name: str
    endpoint: str
    secret_ref: SecretRef | None = None
    model_name: str = ""


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


def serialize_settings(snapshot: SettingsSnapshot) -> dict[str, Any]:
    return {
        "role": snapshot.role,
        "model_service": {
            "provider_name": snapshot.model_service.provider_name,
            "endpoint": snapshot.model_service.endpoint,
            "model_name": snapshot.model_service.model_name,
            "secret_ref": snapshot.model_service.secret_ref.redacted() if snapshot.model_service.secret_ref else None,
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
    role = value.get("role", "sales")
    if role not in ("sales", "admin"):
        raise ValueError("role must be sales or admin")
    return SettingsSnapshot(
        role=role,
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
    )
