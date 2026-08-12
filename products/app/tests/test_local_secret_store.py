"""Regression tests for password-prompt-free local credential persistence."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.errors import UnavailableCapabilityError, ValidationError
from backend.secrets.local_secret_store import LocalSecretStore
from backend.secrets.secret_ref import SecretRef


class LocalSecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "credentials"
        self.reference = SecretRef("local-secret", "optionhelper/model/test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_value_survives_restart_and_uses_owner_only_permissions(self) -> None:
        store = LocalSecretStore(self.root)
        store.store(self.reference, "test-api-key")
        restarted = LocalSecretStore(self.root)
        self.assertEqual(restarted.resolve(self.reference), "test-api-key")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        records = list(self.root.glob("*.secret"))
        self.assertEqual(len(records), 1)
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
        self.assertNotIn("model", records[0].name)

    def test_update_is_atomic_and_delete_is_idempotent(self) -> None:
        store = LocalSecretStore(self.root)
        store.store(self.reference, "first")
        store.store(self.reference, "second")
        self.assertEqual(store.resolve(self.reference), "second")
        store.delete(self.reference)
        store.delete(self.reference)
        with self.assertRaises(UnavailableCapabilityError):
            store.resolve(self.reference)

    def test_wrong_provider_empty_value_and_symlink_are_rejected(self) -> None:
        store = LocalSecretStore(self.root)
        with self.assertRaises(ValidationError):
            store.store(SecretRef("keychain", "optionhelper/model/test"), "value")
        with self.assertRaises(ValidationError):
            store.store(self.reference, "  ")
        target = store._path(self.reference)
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, target)
        with self.assertRaises(UnavailableCapabilityError):
            store.resolve(self.reference)


if __name__ == "__main__":
    unittest.main()
