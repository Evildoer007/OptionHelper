"""Formal local persistence for non-sensitive App settings."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
from typing import Any

from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import SettingsSnapshot, deserialize_settings, serialize_settings
from . import _LocalDocumentStore


class SettingsStore(ABC):
    @abstractmethod
    def load(self, principal_id: str) -> SettingsSnapshot:
        raise NotImplementedError

    @abstractmethod
    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        raise NotImplementedError


class LocalSettingsStore(SettingsStore):
    """One per-user settings document backed by the App-owned state store."""

    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def load(self, principal_id: str) -> SettingsSnapshot:
        raw = self._state.read("settings").get(principal_id)
        if not isinstance(raw, dict):
            raise KeyError(principal_id)
        return deserialize_settings(raw)

    def save(self, principal_id: str, settings: SettingsSnapshot) -> None:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            value[principal_id] = serialize_settings(settings)
            return value

        self._state.update("settings", update)

    def items(self) -> tuple[tuple[str, SettingsSnapshot], ...]:
        """Return a stable snapshot for one-time host-owned migrations."""

        values = self._state.read("settings")
        return tuple(
            (principal_id, deserialize_settings(raw))
            for principal_id, raw in values.items()
            if isinstance(principal_id, str)
            and not principal_id.startswith("data-verification:")
            and not principal_id.startswith("model-verification:")
            and isinstance(raw, dict)
        )

    def save_data_verification(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef,
        available: bool,
    ) -> None:
        """Persist only Host verification metadata bound to one SecretRef revision."""

        key = _data_verification_key(tenant_id, principal_id)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            if not available:
                value.pop(key, None)
                return value
            if secret_ref.revision is None:
                raise ValueError("Verified data credentials require a SecretRef revision")
            value[key] = {
                "tenant_hash": hashlib.sha256(tenant_id.encode("utf-8")).hexdigest(),
                "principal_hash": hashlib.sha256(principal_id.encode("utf-8")).hexdigest(),
                "secret_ref_hash": _secret_ref_hash(secret_ref),
                "secret_ref_revision": secret_ref.revision,
                "status": "available",
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            return value

        self._state.update("settings", update)

    def save_data_verification_for_current_reference(
        self,
        *,
        settings_key: str,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef,
        available: bool,
    ) -> bool:
        """Atomically bind a connection-test result to the still-current revision."""

        verification_key = _data_verification_key(tenant_id, principal_id)
        matched = False

        def update(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal matched
            settings = value.get(settings_key)
            data = settings.get("data_interface") if isinstance(settings, dict) else None
            current = data.get("secret_ref") if isinstance(data, dict) else None
            expected = secret_ref.redacted()
            if current != expected:
                return value
            matched = True
            if not available:
                value.pop(verification_key, None)
                return value
            if secret_ref.revision is None:
                raise ValueError("Verified data credentials require a SecretRef revision")
            value[verification_key] = {
                "tenant_hash": hashlib.sha256(tenant_id.encode("utf-8")).hexdigest(),
                "principal_hash": hashlib.sha256(principal_id.encode("utf-8")).hexdigest(),
                "secret_ref_hash": _secret_ref_hash(secret_ref),
                "secret_ref_revision": secret_ref.revision,
                "status": "available",
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            return value

        self._state.update("settings", update)
        return matched

    def data_verification_available(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef | None,
    ) -> bool:
        return self.data_verification_status(
            tenant_id=tenant_id,
            principal_id=principal_id,
            secret_ref=secret_ref,
        )["verification_state"] == "verified"

    def data_verification_status(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef | None,
    ) -> dict[str, Any]:
        """Return a browser-safe status bound to the current credential revision."""

        configured = secret_ref is not None
        if secret_ref is None or secret_ref.revision is None:
            return {
                "configured": configured,
                "verification_state": "configured_unverified" if configured else "not_configured",
                "verified_at": None,
                "current_revision_match": False,
            }
        record = self._state.read("settings").get(_data_verification_key(tenant_id, principal_id))
        revision_match = bool(
            isinstance(record, dict)
            and record.get("tenant_hash") == hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
            and record.get("principal_hash") == hashlib.sha256(principal_id.encode("utf-8")).hexdigest()
            and record.get("secret_ref_revision") == secret_ref.revision
            and record.get("secret_ref_hash") == _secret_ref_hash(secret_ref)
        )
        verified = bool(revision_match and record.get("status") == "available")
        temporarily_unavailable = bool(
            revision_match and record.get("status") == "temporarily_unavailable"
        )
        return {
            "configured": True,
            "verification_state": (
                "verified" if verified
                else "temporarily_unavailable" if temporarily_unavailable
                else "configured_unverified"
            ),
            "verified_at": record.get("verified_at") if verified else None,
            "current_revision_match": revision_match,
        }

    def revoke_data_verification(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef,
    ) -> None:
        """Revoke only the credential revision rejected by the provider."""

        key = _data_verification_key(tenant_id, principal_id)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            record = value.get(key)
            if (
                isinstance(record, dict)
                and record.get("secret_ref_revision") == secret_ref.revision
                and record.get("secret_ref_hash") == _secret_ref_hash(secret_ref)
            ):
                value.pop(key, None)
            return value

        self._state.update("settings", update)

    def mark_data_verification_temporarily_unavailable(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        secret_ref: SecretRef,
    ) -> None:
        """Retain the exact credential binding while blocking use until retest."""

        key = _data_verification_key(tenant_id, principal_id)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            record = value.get(key)
            if (
                isinstance(record, dict)
                and record.get("secret_ref_revision") == secret_ref.revision
                and record.get("secret_ref_hash") == _secret_ref_hash(secret_ref)
            ):
                record = dict(record)
                record["status"] = "temporarily_unavailable"
                record["verified_at"] = None
                value[key] = record
            return value

        self._state.update("settings", update)

    def save_model_verification_for_current_reference(
        self,
        *,
        settings_key: str,
        tenant_id: str,
        principal_id: str,
        provider_id: str,
        model_id: str,
        endpoint: str,
        secret_ref: SecretRef,
        verified: bool,
        connected: bool = False,
        capabilities: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically bind one active probe to the exact model configuration."""

        verification_key = _model_verification_key(tenant_id, principal_id, provider_id, model_id)
        matched = False

        def update(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal matched
            settings = value.get(settings_key)
            providers = settings.get("model_providers") if isinstance(settings, dict) else None
            provider = next((
                item for item in providers
                if isinstance(item, dict) and item.get("provider_id") == provider_id
            ), None) if isinstance(providers, list) else None
            models = provider.get("models") if isinstance(provider, dict) else None
            model = next((
                item for item in models
                if isinstance(item, dict) and item.get("model_id") == model_id and item.get("enabled") is True
            ), None) if isinstance(models, list) else None
            if (
                not isinstance(provider, dict)
                or model is None
                or provider.get("endpoint") != endpoint
                or provider.get("secret_ref") != secret_ref.redacted()
            ):
                return value
            matched = True
            if not verified and not connected:
                value.pop(verification_key, None)
                return value
            if secret_ref.revision is None:
                raise ValueError("Verified model credentials require a SecretRef revision")
            value[verification_key] = {
                "tenant_hash": hashlib.sha256(tenant_id.encode("utf-8")).hexdigest(),
                "principal_hash": hashlib.sha256(principal_id.encode("utf-8")).hexdigest(),
                "provider_id_hash": hashlib.sha256(provider_id.encode("utf-8")).hexdigest(),
                "model_id_hash": hashlib.sha256(model_id.encode("utf-8")).hexdigest(),
                "endpoint_hash": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
                "secret_ref_hash": _secret_ref_hash(secret_ref),
                "secret_ref_revision": secret_ref.revision,
                "status": "verified" if verified else "connected",
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "capabilities": _safe_model_capabilities(capabilities),
            }
            return value

        self._state.update("settings", update)
        return matched

    def model_verification_status(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        provider_id: str,
        model_id: str,
        endpoint: str,
        secret_ref: SecretRef | None,
    ) -> dict[str, Any]:
        """Return non-secret probe state for one exact model binding."""

        configured = secret_ref is not None
        if secret_ref is None or secret_ref.revision is None:
            return {
                "verification_state": "configured_unverified" if configured else "not_configured",
                "verified_at": None,
                "current_revision_match": False,
                "verified_capabilities": {},
            }
        record = self._state.read("settings").get(
            _model_verification_key(tenant_id, principal_id, provider_id, model_id)
        )
        revision_match = bool(
            isinstance(record, dict)
            and record.get("tenant_hash") == hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
            and record.get("principal_hash") == hashlib.sha256(principal_id.encode("utf-8")).hexdigest()
            and record.get("provider_id_hash") == hashlib.sha256(provider_id.encode("utf-8")).hexdigest()
            and record.get("model_id_hash") == hashlib.sha256(model_id.encode("utf-8")).hexdigest()
            and record.get("endpoint_hash") == hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
            and record.get("secret_ref_revision") == secret_ref.revision
            and record.get("secret_ref_hash") == _secret_ref_hash(secret_ref)
        )
        verified = bool(revision_match and record.get("status") == "verified")
        return {
            "verification_state": "verified" if verified else "connected" if revision_match and record.get("status") == "connected" else "configured_unverified",
            "verified_at": record.get("verified_at") if verified else None,
            "current_revision_match": revision_match,
            "verified_capabilities": dict(record.get("capabilities", {})) if verified else {},
        }


def _data_verification_key(tenant_id: str, principal_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{principal_id}".encode("utf-8")).hexdigest()
    return f"data-verification:{digest}"


def _secret_ref_hash(secret_ref: SecretRef) -> str:
    material = f"{secret_ref.provider}\0{secret_ref.key}\0{secret_ref.revision or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _model_verification_key(tenant_id: str, principal_id: str, provider_id: str, model_id: str) -> str:
    material = f"{tenant_id}\0{principal_id}\0{provider_id}\0{model_id}"
    return f"model-verification:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _safe_model_capabilities(value: dict[str, Any] | None) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {
        key: bool(source.get(key))
        for key in ("streaming", "tool_calling", "tool_result_continuation", "reasoning", "cancellation", "bounded_timeout")
    }
