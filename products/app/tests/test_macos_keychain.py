"""macOS Keychain adapter tests with an in-memory Security.framework seam."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.errors import UnavailableCapabilityError
from backend.secrets.secret_ref import SecretRef
from desktop.macos.keychain import (
    MacOSKeychain,
    _SecurityFrameworkBackend,
    _ERR_SEC_DUPLICATE_ITEM,
    _ERR_SEC_INTERACTION_NOT_ALLOWED,
    _ERR_SEC_ITEM_NOT_FOUND,
    _ERR_SEC_SUCCESS,
)


class MemorySecurityBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}
        self.store_status = _ERR_SEC_SUCCESS
        self.resolve_status: int | None = None

    def store(self, service: str, account: str, value: bytes) -> int:
        if self.store_status == _ERR_SEC_SUCCESS:
            self.values[(service, account)] = bytes(value)
        return self.store_status

    def resolve(self, service: str, account: str) -> tuple[int, bytes | None]:
        if self.resolve_status is not None:
            return self.resolve_status, None
        value = self.values.get((service, account))
        return (_ERR_SEC_SUCCESS, value) if value is not None else (_ERR_SEC_ITEM_NOT_FOUND, None)

    def delete(self, service: str, account: str) -> int:
        self.values.pop((service, account), None)
        return _ERR_SEC_SUCCESS


class MacOSKeychainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MemorySecurityBackend()
        self.keychain = MacOSKeychain(backend=self.backend)
        self.reference = SecretRef("keychain", "model/test")

    def test_store_then_resolve_uses_native_backend_without_command_line_transport(self) -> None:
        self.keychain.store(self.reference, "test-secret-value")
        self.assertEqual(self.keychain.resolve(self.reference), "test-secret-value")
        self.assertEqual(self.backend.values[("OptionHelper", "model/test")], b"test-secret-value")

    def test_missing_record_is_actionable_and_distinct_from_keychain_failure(self) -> None:
        with self.assertRaises(UnavailableCapabilityError) as missing:
            self.keychain.resolve(self.reference)
        self.assertEqual(missing.exception.capability, "本机凭据缺失")
        self.backend.resolve_status = _ERR_SEC_INTERACTION_NOT_ALLOWED
        with self.assertRaises(UnavailableCapabilityError) as unavailable:
            self.keychain.resolve(self.reference)
        self.assertEqual(unavailable.exception.capability, "macOS钥匙串")
        self.assertIn("解锁Mac", unavailable.exception.next_step)

    def test_locked_keychain_store_is_reported_without_fallback(self) -> None:
        self.backend.store_status = _ERR_SEC_INTERACTION_NOT_ALLOWED
        with self.assertRaises(UnavailableCapabilityError) as unavailable:
            self.keychain.store(self.reference, "test-secret")
        self.assertEqual(unavailable.exception.capability, "macOS钥匙串")

    def test_native_duplicate_add_updates_existing_item_and_releases_handle(self) -> None:
        backend = _SecurityFrameworkBackend.__new__(_SecurityFrameworkBackend)
        calls: list[tuple[str, int]] = []

        def add(*args: object) -> int:
            del args
            return _ERR_SEC_DUPLICATE_ITEM

        def find(*args: object) -> int:
            args[-1]._obj.value = 123  # type: ignore[attr-defined]
            calls.append(("find", 123))
            return _ERR_SEC_SUCCESS

        def modify(item: object, _attributes: object, length: int, _value: object) -> int:
            calls.append(("modify", int(item.value)))  # type: ignore[attr-defined]
            self.assertEqual(length, len(b"replacement"))
            return _ERR_SEC_SUCCESS

        def release(item: object) -> None:
            calls.append(("release", int(item.value)))  # type: ignore[attr-defined]

        backend._add = add
        backend._find = find
        backend._modify = modify
        backend._release = release
        self.assertEqual(backend.store("OptionHelper", "model/test", b"replacement"), _ERR_SEC_SUCCESS)
        self.assertEqual(calls, [("find", 123), ("modify", 123), ("release", 123)])


if __name__ == "__main__":
    unittest.main()
