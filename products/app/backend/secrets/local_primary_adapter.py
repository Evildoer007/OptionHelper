"""OptionHelper-local credential storage with one-release system-vault migration."""

from __future__ import annotations

import secrets
from hashlib import sha256
from collections.abc import Iterable

from ..errors import UnavailableCapabilityError, ValidationError
from .local_secret_store import LocalSecretStore
from .secret_provider import SecretAdapter
from .secret_ref import SecretRef


class LocalPrimarySecretAdapter:
    """Keep new credentials local and remove matching legacy copies safely."""

    provider_name = "local-secret"

    def __init__(
        self,
        store: LocalSecretStore,
        *,
        legacy_provider: str | None = None,
        legacy_adapter: SecretAdapter | None = None,
        legacy_local_stores: Iterable[LocalSecretStore] = (),
    ) -> None:
        if (legacy_provider is None) != (legacy_adapter is None):
            raise ValidationError("系统凭据迁移配置不完整")
        if legacy_provider not in {None, "keychain", "credential-manager"}:
            raise ValidationError("系统凭据迁移Provider无效")
        self._store = store
        self._legacy_provider = legacy_provider
        self._legacy_adapter = legacy_adapter
        self._legacy_local_stores = tuple(legacy_local_stores)

    def store(self, secret_ref: SecretRef, value: str) -> None:
        self._validate_local(secret_ref)
        self._stage_and_verify(secret_ref, value)
        try:
            self._delete_legacy_copies(secret_ref)
            self._store.promote_staged(secret_ref)
        except Exception:
            # The verified candidate remains staged for a safe retry.  The
            # currently active local credential, if any, is left untouched.
            raise

    def resolve(self, secret_ref: SecretRef) -> str:
        self._validate_local(secret_ref)
        return self._store.resolve(secret_ref)

    def delete(self, secret_ref: SecretRef) -> None:
        self._validate_local(secret_ref)
        self._delete_legacy_copies(secret_ref)
        self._store.delete(secret_ref)
        self._store.discard_staged(secret_ref)

    def migrate_legacy(self, secret_ref: SecretRef) -> SecretRef:
        """Migrate one referenced system-vault item and return its local ref."""

        if self._legacy_provider is None or self._legacy_adapter is None:
            raise UnavailableCapabilityError("凭据迁移", "当前设备没有可迁移的系统凭据存储")
        if secret_ref.provider != self._legacy_provider:
            raise ValidationError("旧凭据引用与系统凭据Provider不一致")
        revision = secret_ref.revision or "migrated-" + sha256(
            f"{secret_ref.provider}\0{secret_ref.key}".encode("utf-8")
        ).hexdigest()
        local_ref = SecretRef("local-secret", secret_ref.key, revision)
        try:
            value = self._legacy_adapter.resolve(secret_ref)
        except UnavailableCapabilityError as error:
            if error.capability != "本机凭据缺失":
                raise
            return self._recover_after_legacy_removal(local_ref, error)
        self._stage_and_verify(local_ref, value)
        self._delete_and_confirm_system(secret_ref)
        self._delete_legacy_local_copies(local_ref)
        self._store.promote_staged(local_ref)
        return local_ref

    def _recover_after_legacy_removal(
        self, local_ref: SecretRef, missing_error: UnavailableCapabilityError,
    ) -> SecretRef:
        try:
            self._store.resolve(local_ref)
            self._delete_legacy_local_copies(local_ref)
            return local_ref
        except UnavailableCapabilityError as local_error:
            if local_error.capability != "本机凭据缺失":
                raise
        try:
            self._store.resolve_staged(local_ref)
        except UnavailableCapabilityError as staged_error:
            if staged_error.capability != "本机凭据缺失":
                raise
        else:
            self._delete_legacy_local_copies(local_ref)
            self._store.promote_staged(local_ref)
            return local_ref
        for legacy_store in self._legacy_local_stores:
            try:
                value = legacy_store.resolve(local_ref)
            except UnavailableCapabilityError as legacy_error:
                if legacy_error.capability == "本机凭据缺失":
                    continue
                raise
            self._stage_and_verify(local_ref, value)
            legacy_store.delete(local_ref)
            self._store.promote_staged(local_ref)
            return local_ref
        raise missing_error

    def _stage_and_verify(self, local_ref: SecretRef, value: str) -> None:
        self._store.stage(local_ref, value)
        try:
            staged = self._store.resolve_staged(local_ref)
        except Exception:
            self._store.discard_staged(local_ref)
            raise
        if not secrets.compare_digest(staged.encode("utf-8"), value.encode("utf-8")):
            self._store.discard_staged(local_ref)
            raise ValidationError("本机凭据迁移回读校验失败")

    def _delete_legacy_copies(self, local_ref: SecretRef) -> None:
        if self._legacy_provider is not None and self._legacy_adapter is not None:
            self._delete_and_confirm_system(
                SecretRef(self._legacy_provider, local_ref.key, local_ref.revision),
            )
        self._delete_legacy_local_copies(local_ref)

    def _delete_and_confirm_system(self, legacy_ref: SecretRef) -> None:
        assert self._legacy_adapter is not None
        self._legacy_adapter.delete(legacy_ref)
        try:
            self._legacy_adapter.resolve(legacy_ref)
        except UnavailableCapabilityError as error:
            if error.capability == "本机凭据缺失":
                return
            raise
        raise UnavailableCapabilityError("凭据迁移", "系统凭据删除后仍可读取")

    def _delete_legacy_local_copies(self, local_ref: SecretRef) -> None:
        for legacy_store in self._legacy_local_stores:
            legacy_store.delete(local_ref)

    @staticmethod
    def _validate_local(secret_ref: SecretRef) -> None:
        if secret_ref.provider != "local-secret":
            raise ValidationError("OptionHelper本地凭据必须使用local-secret")


__all__ = ("LocalPrimarySecretAdapter",)
