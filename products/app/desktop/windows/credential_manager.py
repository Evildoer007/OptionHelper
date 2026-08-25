"""Native Windows Credential Manager adapter for OptionHelper."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Protocol

from backend.errors import UnavailableCapabilityError, ValidationError
from backend.secrets.secret_ref import SecretRef


_ERROR_SUCCESS = 0
_ERROR_ACCESS_DENIED = 5
_ERROR_NOT_FOUND = 1168
_ERROR_NO_SUCH_LOGON_SESSION = 1312
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_MAX_CREDENTIAL_BYTES = 5 * 512


class CredentialBackend(Protocol):
    def store(self, target: str, value: bytes) -> int: ...

    def resolve(self, target: str) -> tuple[int, bytes | None]: ...

    def delete(self, target: str) -> int: ...


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _Win32CredentialBackend:
    def __init__(self) -> None:
        try:
            library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except (AttributeError, OSError) as error:
            raise OSError("Advapi32.dll不可用") from error
        self._write = library.CredWriteW
        self._write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = library.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        self._read.restype = wintypes.BOOL
        self._delete = library.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = library.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def store(self, target: str, value: bytes) -> int:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "OptionHelper"
        if self._write(ctypes.byref(credential), 0):
            return _ERROR_SUCCESS
        return int(ctypes.get_last_error())

    def resolve(self, target: str) -> tuple[int, bytes | None]:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return int(ctypes.get_last_error()), None
        try:
            credential = pointer.contents
            value = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return _ERROR_SUCCESS, value
        finally:
            self._free(pointer)

    def delete(self, target: str) -> int:
        if self._delete(target, _CRED_TYPE_GENERIC, 0):
            return _ERROR_SUCCESS
        return int(ctypes.get_last_error())


class WindowsCredentialManager:
    """Store OptionHelper secrets as Generic Credentials for the current user."""

    target_prefix = "OptionHelper/"

    def __init__(self, backend: CredentialBackend | None = None) -> None:
        try:
            self._backend = backend or _Win32CredentialBackend()
        except OSError as error:
            raise UnavailableCapabilityError(
                "Windows凭据管理器", "系统Credential Manager接口不可用，请重新启动Windows后重试。",
            ) from error

    def store(self, secret_ref: SecretRef, value: str) -> None:
        self._validate_reference(secret_ref)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("凭据不能为空")
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_CREDENTIAL_BYTES:
            raise ValidationError("凭据内容超过Windows凭据管理器限制")
        self._require_success("保存", self._backend.store(self._target(secret_ref), encoded))

    def resolve(self, secret_ref: SecretRef) -> str:
        self._validate_reference(secret_ref)
        status, value = self._backend.resolve(self._target(secret_ref))
        if status == _ERROR_NOT_FOUND:
            raise UnavailableCapabilityError(
                "本机凭据缺失", "未找到已保存的凭据，请在设置中心重新粘贴并保存。",
            )
        self._require_success("读取", status)
        if not value:
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据为空，请重新保存。")
        try:
            result = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据格式无效，请重新保存。") from error
        if not result.strip():
            raise UnavailableCapabilityError("本机凭据缺失", "已保存凭据为空，请重新保存。")
        return result

    def delete(self, secret_ref: SecretRef) -> None:
        self._validate_reference(secret_ref)
        status = self._backend.delete(self._target(secret_ref))
        if status == _ERROR_NOT_FOUND:
            return
        self._require_success("删除", status)

    @classmethod
    def _target(cls, secret_ref: SecretRef) -> str:
        return cls.target_prefix + secret_ref.key

    @staticmethod
    def _validate_reference(secret_ref: SecretRef) -> None:
        if secret_ref.provider != "credential-manager":
            raise ValidationError("Windows本机凭据必须使用credential-manager")
        if not secret_ref.key.strip() or "\x00" in secret_ref.key or len(secret_ref.key) > 512:
            raise ValidationError("Windows凭据引用无效")

    @staticmethod
    def _require_success(operation: str, status: int) -> None:
        if status == _ERROR_SUCCESS:
            return
        if status in {_ERROR_ACCESS_DENIED, _ERROR_NO_SUCH_LOGON_SESSION}:
            raise UnavailableCapabilityError(
                "Windows凭据管理器",
                "当前Windows登录会话无法访问凭据管理器，请重新登录系统后再试。",
            )
        raise UnavailableCapabilityError(
            "Windows凭据管理器",
            f"系统凭据{operation}未完成，请检查Windows凭据管理器后重试。",
        )
