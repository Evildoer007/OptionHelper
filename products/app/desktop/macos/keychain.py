"""Native macOS Keychain adapter for the local App.

``security add-generic-password -w`` is intentionally interactive: it reads
from a terminal and asks for the value twice.  A background WebView backend
cannot safely automate that prompt through ``stdin``.  This adapter therefore
uses the Security framework directly.  Credential bytes never enter a command
line, settings document, log or HTTP response.
"""

from __future__ import annotations

import ctypes
from ctypes import c_char_p, c_int32, c_uint32, c_void_p
from typing import Protocol

from backend.errors import UnavailableCapabilityError, ValidationError
from backend.secrets.secret_ref import SecretRef


_ERR_SEC_SUCCESS = 0
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_AUTH_FAILED = -25293
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308


class KeychainBackend(Protocol):
    """Small injectable boundary around Security.framework for safe tests."""

    def store(self, service: str, account: str, value: bytes) -> int: ...

    def resolve(self, service: str, account: str) -> tuple[int, bytes | None]: ...

    def delete(self, service: str, account: str) -> int: ...


class _SecurityFrameworkBackend:
    """Minimal generic-password binding with no subprocess or terminal I/O."""

    def __init__(self) -> None:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        self._add = security.SecKeychainAddGenericPassword
        self._add.argtypes = [
            c_void_p, c_uint32, c_char_p, c_uint32, c_char_p, c_uint32, c_void_p, ctypes.POINTER(c_void_p),
        ]
        self._add.restype = c_int32

        self._find = security.SecKeychainFindGenericPassword
        self._find.argtypes = [
            c_void_p, c_uint32, c_char_p, c_uint32, c_char_p,
            ctypes.POINTER(c_uint32), ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p),
        ]
        self._find.restype = c_int32

        self._modify = security.SecKeychainItemModifyAttributesAndData
        self._modify.argtypes = [c_void_p, c_void_p, c_uint32, c_void_p]
        self._modify.restype = c_int32

        self._free_content = security.SecKeychainItemFreeContent
        self._free_content.argtypes = [c_void_p, c_void_p]
        self._free_content.restype = c_int32

        self._delete = security.SecKeychainItemDelete
        self._delete.argtypes = [c_void_p]
        self._delete.restype = c_int32

        self._release = core_foundation.CFRelease
        self._release.argtypes = [c_void_p]
        self._release.restype = None

    @staticmethod
    def _encoded(service: str, account: str) -> tuple[bytes, bytes]:
        return service.encode("utf-8"), account.encode("utf-8")

    @staticmethod
    def _buffer(value: bytes) -> ctypes.Array[ctypes.c_char]:
        # create_string_buffer preserves the supplied byte count while keeping
        # the native memory alive for the duration of the Security call.
        return ctypes.create_string_buffer(value)

    def store(self, service: str, account: str, value: bytes) -> int:
        service_bytes, account_bytes = self._encoded(service, account)
        buffer = self._buffer(value)
        item = c_void_p()
        status = int(self._add(
            None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
            len(value), ctypes.cast(buffer, c_void_p), ctypes.byref(item),
        ))
        if status == _ERR_SEC_DUPLICATE_ITEM:
            found = c_void_p()
            status = int(self._find(
                None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                None, None, ctypes.byref(found),
            ))
            if status == _ERR_SEC_SUCCESS:
                try:
                    status = int(self._modify(found, None, len(value), ctypes.cast(buffer, c_void_p)))
                finally:
                    self._release(found)
        elif item.value:
            self._release(item)
        return status

    def resolve(self, service: str, account: str) -> tuple[int, bytes | None]:
        service_bytes, account_bytes = self._encoded(service, account)
        length = c_uint32()
        value = c_void_p()
        status = int(self._find(
            None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
            ctypes.byref(length), ctypes.byref(value), None,
        ))
        if status != _ERR_SEC_SUCCESS:
            return status, None
        try:
            return status, ctypes.string_at(value, length.value)
        finally:
            self._free_content(None, value)

    def delete(self, service: str, account: str) -> int:
        service_bytes, account_bytes = self._encoded(service, account)
        item = c_void_p()
        status = int(self._find(
            None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
            None, None, ctypes.byref(item),
        ))
        if status != _ERR_SEC_SUCCESS:
            return status
        try:
            return int(self._delete(item))
        finally:
            self._release(item)


class MacOSKeychain:
    """Stores OptionHelper local credentials in the user's login keychain."""

    service_name = "OptionHelper"

    def __init__(self, backend: KeychainBackend | None = None) -> None:
        try:
            self._backend = backend or _SecurityFrameworkBackend()
        except OSError as error:
            raise UnavailableCapabilityError("macOS钥匙串", "系统Security.framework不可用") from error

    def store(self, secret_ref: SecretRef, value: str) -> None:
        self._validate_reference(secret_ref)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("凭据不能为空")
        self._require_success("store", self._backend.store(self.service_name, secret_ref.key, value.encode("utf-8")))

    def resolve(self, secret_ref: SecretRef) -> str:
        self._validate_reference(secret_ref)
        status, value = self._backend.resolve(self.service_name, secret_ref.key)
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            raise UnavailableCapabilityError("本机凭据缺失", "未找到已保存的凭据，请在设置中心重新粘贴并保存")
        self._require_success("resolve", status)
        if not value:
            raise UnavailableCapabilityError("本机凭据缺失", "凭据为空，请在设置中心重新粘贴并保存")
        try:
            result = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "凭据格式无效，请在设置中心重新保存") from error
        if not result.strip():
            raise UnavailableCapabilityError("本机凭据缺失", "凭据为空，请在设置中心重新粘贴并保存")
        return result

    def delete(self, secret_ref: SecretRef) -> None:
        self._validate_reference(secret_ref)
        status = self._backend.delete(self.service_name, secret_ref.key)
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return
        self._require_success("delete", status)

    @staticmethod
    def _validate_reference(secret_ref: SecretRef) -> None:
        if secret_ref.provider != "keychain":
            raise ValidationError("macOS本机凭据必须使用keychain")

    @staticmethod
    def _require_success(operation: str, status: int) -> None:
        if status == _ERR_SEC_SUCCESS:
            return
        if status in {_ERR_SEC_AUTH_FAILED, _ERR_SEC_INTERACTION_NOT_ALLOWED}:
            raise UnavailableCapabilityError(
                "macOS钥匙串",
                "无法访问登录钥匙串。请解锁Mac后重新打开OptionHelper，再保存或测试凭据。",
            )
        raise UnavailableCapabilityError(
            "macOS钥匙串",
            f"本机钥匙串{operation}未完成。请检查登录钥匙串状态后重试。",
        )
