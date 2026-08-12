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
from .secret_ref import SecretRef


class LocalSecretStore:
    """Persist local-App credentials with owner-only filesystem permissions."""

    provider_name = "local-secret"
    _MAX_SECRET_BYTES = 1024 * 1024

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
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
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
                os.chmod(target, 0o600)
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

    def _path(self, secret_ref: SecretRef) -> Path:
        material = f"{secret_ref.provider}\0{secret_ref.key}".encode("utf-8")
        return self._root / f"{hashlib.sha256(material).hexdigest()}.secret"

    @classmethod
    def _validate_reference(cls, secret_ref: SecretRef) -> None:
        if secret_ref.provider != cls.provider_name:
            raise ValidationError("OptionHelper本机凭据必须使用local-secret")
        if not secret_ref.key.strip() or "\x00" in secret_ref.key or len(secret_ref.key) > 512:
            raise ValidationError("本机凭据引用无效")
