"""Secret-reference boundary for App integrations.

The settings layer stores only :class:`SecretRef` metadata.  Credential
material is resolved by one explicitly registered local or managed adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..errors import UnavailableCapabilityError, ValidationError
from .secret_ref import SecretRef


class SecretAdapter(Protocol):
    def store(self, secret_ref: SecretRef, value: str) -> None: ...

    def resolve(self, secret_ref: SecretRef) -> str: ...

    def delete(self, secret_ref: SecretRef) -> None: ...


class SecretProvider:
    """Validates opaque references and delegates only to approved stores."""

    _SUPPORTED_REFERENCE_PROVIDERS = frozenset({"local-secret", "keychain", "credential-manager", "managed-secret"})

    def __init__(self, adapters: Mapping[str, SecretAdapter] | None = None) -> None:
        self._adapters = dict(adapters or {})
        self._allowed: set[tuple[str, str, str]] = set()

    def supports(self, provider: str) -> bool:
        """Return whether this Host has an adapter for one reference provider."""

        return provider in self._adapters

    def allow_reference(self, reference: SecretRef, purpose: str) -> None:
        """Allow one exact Host-issued reference for one credential purpose."""

        if not purpose.strip():
            raise ValidationError("Credential purpose is required")
        if reference.provider not in self._SUPPORTED_REFERENCE_PROVIDERS:
            raise ValidationError(f"Unsupported SecretRef provider: {reference.provider}")
        self._allowed.add((purpose, reference.provider, reference.key))

    def require_reference(self, reference: SecretRef | None, purpose: str) -> SecretRef:
        if reference is None:
            raise UnavailableCapabilityError(purpose, "请在设置中心保存对应服务的凭据后重试。")
        if reference.provider not in self._SUPPORTED_REFERENCE_PROVIDERS:
            raise ValidationError(f"Unsupported SecretRef provider: {reference.provider}")
        if (purpose, reference.provider, reference.key) not in self._allowed:
            raise ValidationError("SecretRef is not approved for this credential purpose")
        return reference

    def store(self, reference: SecretRef, value: str, purpose: str) -> None:
        reference = self.require_reference(reference, purpose)
        self._adapter_for(reference, purpose).store(reference, value)

    def resolve(self, reference: SecretRef, purpose: str) -> str:
        reference = self.require_reference(reference, purpose)
        try:
            return self._adapter_for(reference, purpose).resolve(reference)
        except UnavailableCapabilityError as error:
            # Platform adapters deliberately do not know which product service
            # asked for a credential.  Preserve the platform failure, but make
            # a missing record actionable in the calling service's language.
            if error.capability == "本机凭据缺失":
                raise UnavailableCapabilityError(
                    purpose,
                    "未找到已保存的凭据，请在设置中心重新粘贴并保存后再测试连接。",
                ) from error
            raise

    def delete(self, reference: SecretRef, purpose: str) -> None:
        reference = self.require_reference(reference, purpose)
        self._adapter_for(reference, purpose).delete(reference)

    def migrate_to_local(self, reference: SecretRef, purpose: str) -> SecretRef:
        """Migrate one approved legacy reference through the local adapter."""

        reference = self.require_reference(reference, purpose)
        if reference.provider == "local-secret":
            return reference
        local_adapter = self._adapters.get("local-secret")
        migrate = getattr(local_adapter, "migrate_legacy", None)
        if not callable(migrate):
            raise UnavailableCapabilityError("凭据迁移", "当前设备没有OptionHelper本地凭据迁移能力")
        local_reference = migrate(reference)
        if not isinstance(local_reference, SecretRef) or local_reference.provider != "local-secret":
            raise ValidationError("凭据迁移未返回有效的OptionHelper本地引用")
        self.allow_reference(local_reference, purpose)
        return local_reference

    def _adapter_for(self, reference: SecretRef, purpose: str) -> SecretAdapter:
        adapter = self._adapters.get(reference.provider)
        if adapter is None:
            raise UnavailableCapabilityError(purpose, "当前设备没有可用的安全凭据存储，请检查系统支持后重试。")
        return adapter
