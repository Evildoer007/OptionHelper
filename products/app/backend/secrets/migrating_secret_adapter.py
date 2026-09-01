"""One-way migration from the legacy private-file store to a platform vault."""

from __future__ import annotations

from ..errors import UnavailableCapabilityError, ValidationError
from .local_secret_store import LocalSecretStore
from .secret_provider import SecretAdapter
from .secret_ref import SecretRef


class MigratingSecretAdapter:
    """Use a platform vault and migrate a matching legacy record on first read.

    Migration is deliberately one-way.  A platform-vault failure never falls
    back to plaintext storage; the legacy record is removed only after the
    platform write succeeds.
    """

    def __init__(
        self,
        provider_name: str,
        target: SecretAdapter,
        legacy: LocalSecretStore,
    ) -> None:
        if provider_name not in {"keychain", "credential-manager"}:
            raise ValidationError("系统凭据Provider无效")
        self.provider_name = provider_name
        self._target = target
        self._legacy = legacy

    def store(self, secret_ref: SecretRef, value: str) -> None:
        self._validate(secret_ref)
        self._target.store(secret_ref, value)
        self._legacy.delete(self._legacy_reference(secret_ref))

    def resolve(self, secret_ref: SecretRef) -> str:
        self._validate(secret_ref)
        missing_error: UnavailableCapabilityError | None = None
        try:
            return self._target.resolve(secret_ref)
        except UnavailableCapabilityError as target_error:
            if target_error.capability != "本机凭据缺失":
                raise
            missing_error = target_error
        legacy_reference = self._legacy_reference(secret_ref)
        try:
            value = self._legacy.resolve(legacy_reference)
        except UnavailableCapabilityError as legacy_error:
            if legacy_error.capability == "本机凭据缺失":
                assert missing_error is not None
                raise missing_error from legacy_error
            raise
        self._target.store(secret_ref, value)
        self._legacy.delete(legacy_reference)
        return value

    def delete(self, secret_ref: SecretRef) -> None:
        self._validate(secret_ref)
        self._target.delete(secret_ref)
        self._legacy.delete(self._legacy_reference(secret_ref))

    def _validate(self, secret_ref: SecretRef) -> None:
        if secret_ref.provider != self.provider_name:
            raise ValidationError("系统凭据引用与当前Provider不一致")

    @staticmethod
    def _legacy_reference(secret_ref: SecretRef) -> SecretRef:
        return SecretRef("local-secret", secret_ref.key, secret_ref.revision)
