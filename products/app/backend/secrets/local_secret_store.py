"""App-owned local credential persistence without system password prompts.

Credential values are kept outside settings, conversations and reports.  Each
record is stored in a private file whose name is derived from the opaque
``SecretRef``; the directory is owner-only and writes are atomic.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from threading import RLock

from ..errors import UnavailableCapabilityError, ValidationError
from ..file_permissions import protect_private_path
from .secret_ref import SecretRef


class LocalSecretStore:
    """Persist local-App credentials with owner-only filesystem permissions."""

    provider_name = "local-secret"
    _MAX_SECRET_BYTES = 1024 * 1024

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(self._root)
        self._lock = RLock()

    def store(self, secret_ref: SecretRef, value: str) -> None:
        self._validate_reference(secret_ref)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("凭据不能为空")
        encoded = value.encode("utf-8")
        if len(encoded) > self._MAX_SECRET_BYTES:
            raise ValidationError("凭据内容过大")
        target = self._path(secret_ref)
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=self._root)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    protect_private_path(Path(temporary_name))
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
                protect_private_path(target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def resolve(self, secret_ref: SecretRef) -> str:
        self._validate_reference(secret_ref)
        path = self._path(secret_ref)
        with self._lock:
            try:
                metadata = path.lstat()
            except FileNotFoundError as error:
                raise UnavailableCapabilityError(
                    "本机凭据缺失",
                    "未找到已保存的凭据，请在设置中心重新粘贴并保存。",
                ) from error
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise UnavailableCapabilityError("本机凭据存储", "本机凭据文件状态异常，请重新保存凭据。")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
                try:
                    value = os.read(descriptor, self._MAX_SECRET_BYTES + 1)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise UnavailableCapabilityError("本机凭据存储", "无法读取已保存凭据，请检查本机数据目录权限。") from error
        if not value or len(value) > self._MAX_SECRET_BYTES:
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据为空或无效，请重新保存。")
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据格式无效，请重新保存。") from error
        if not decoded.strip():
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据为空，请重新保存。")
        return decoded

    def delete(self, secret_ref: SecretRef) -> None:
        self._validate_reference(secret_ref)
        with self._lock:
            try:
                self._path(secret_ref).unlink()
            except FileNotFoundError:
                return

    def stage(self, secret_ref: SecretRef, value: str) -> None:
        """Write a migration candidate without making it runtime-visible."""

        self._validate_reference(secret_ref)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("凭据不能为空")
        encoded = value.encode("utf-8")
        if len(encoded) > self._MAX_SECRET_BYTES:
            raise ValidationError("凭据内容过大")
        migration_root = self._migration_root()
        target = self._staged_path(secret_ref)
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".credential-migration-", suffix=".tmp", dir=migration_root,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    protect_private_path(Path(temporary_name))
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
                protect_private_path(target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def resolve_staged(self, secret_ref: SecretRef) -> str:
        """Read a migration candidate using the same file-safety checks."""

        self._validate_reference(secret_ref)
        return self._read_path(self._staged_path(secret_ref))

    def promote_staged(self, secret_ref: SecretRef) -> None:
        """Atomically make a verified migration candidate authoritative."""

        self._validate_reference(secret_ref)
        with self._lock:
            source = self._staged_path(secret_ref)
            if not source.is_file() or source.is_symlink():
                raise UnavailableCapabilityError("本机凭据缺失", "未找到已验证的凭据迁移候选")
            os.replace(source, self._path(secret_ref))
            protect_private_path(self._path(secret_ref))

    def discard_staged(self, secret_ref: SecretRef) -> None:
        self._validate_reference(secret_ref)
        with self._lock:
            try:
                self._staged_path(secret_ref).unlink()
            except FileNotFoundError:
                return

    def _path(self, secret_ref: SecretRef) -> Path:
        material = f"{secret_ref.provider}\0{secret_ref.key}".encode("utf-8")
        return self._root / f"{hashlib.sha256(material).hexdigest()}.secret"

    def _staged_path(self, secret_ref: SecretRef) -> Path:
        material = f"{secret_ref.provider}\0{secret_ref.key}".encode("utf-8")
        return self._migration_root() / f"{hashlib.sha256(material).hexdigest()}.candidate"

    def _migration_root(self) -> Path:
        root = self._root / ".migration"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(root)
        return root

    def _read_path(self, path: Path) -> str:
        with self._lock:
            try:
                metadata = path.lstat()
            except FileNotFoundError as error:
                raise UnavailableCapabilityError("本机凭据缺失", "未找到本机凭据") from error
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise UnavailableCapabilityError("本机凭据存储", "本机凭据文件状态异常")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                value = os.read(descriptor, self._MAX_SECRET_BYTES + 1)
            finally:
                os.close(descriptor)
        if not value or len(value) > self._MAX_SECRET_BYTES:
            raise UnavailableCapabilityError("本机凭据缺失", "本机凭据为空或无效")
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "本机凭据格式无效") from error
        if not decoded.strip():
            raise UnavailableCapabilityError("本机凭据缺失", "本机凭据为空")
        return decoded

    @classmethod
    def _validate_reference(cls, secret_ref: SecretRef) -> None:
        if secret_ref.provider != cls.provider_name:
            raise ValidationError("OptionHelper本机凭据必须使用local-secret")
        if not secret_ref.key.strip() or "\x00" in secret_ref.key or len(secret_ref.key) > 512:
            raise ValidationError("本机凭据引用无效")
