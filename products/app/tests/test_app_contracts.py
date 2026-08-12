"""Source-stage App contract tests; no external service is contacted."""

import sys
import tempfile
import unittest
from shutil import copytree
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.authorization.policy import AuthorizationPolicy
from backend.authorization.roles import Role
from backend.errors import CapabilityIntegrityError
from backend.page_registry import PageRegistry
from backend.secrets.secret_ref import SecretRef
from backend.settings.settings_models import (
    DataInterfaceSettings,
    ModelServiceSettings,
    PreferenceSettings,
    SettingsSnapshot,
    StorageExportSettings,
)
from desktop.common.paths import AppPaths
from desktop.common.webview_app import WebViewApp
from desktop.macos.app_window import MacOSAppWindow
from capability_fixture import capability_root


class AppContractTests(unittest.TestCase):
    def test_role_boundary(self) -> None:
        policy = AuthorizationPolicy()
        self.assertTrue(policy.allows(Role.SALES, "optchat"))
        self.assertFalse(policy.allows(Role.SALES, "optdesk"))
        self.assertTrue(policy.allows(Role.ADMIN, "optdesk"))

    def test_page_registry_points_to_capability_assets(self) -> None:
        pages = PageRegistry(capability_root()).all()
        self.assertEqual({page.module_name for page in pages}, {"datafetcher", "payoffer", "pricer", "backtester", "reporter"})
        self.assertTrue(all(page.capability_asset.startswith("assets/pages/") for page in pages))

    def test_page_registry_requires_an_explicit_capability_root(self) -> None:
        with self.assertRaises(CapabilityIntegrityError):
            PageRegistry()

    def test_execution_gate_rechecks_capability_tree_after_startup(self) -> None:
        """A writable post-startup Capability copy cannot execute as verified."""
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "capability"
            copytree(capability_root(), copied)
            registry = PageRegistry(copied)
            tool_entry = copied / "scripts" / "tool_entry.py"
            tool_entry.write_bytes(tool_entry.read_bytes() + b"\n# test mutation\n")
            with self.assertRaises(CapabilityIntegrityError):
                registry.assert_execution_integrity()

    def test_settings_can_only_carry_secret_reference(self) -> None:
        ref = SecretRef(provider="keychain", key="model/default", version="1")
        snapshot = SettingsSnapshot(
            role="admin",
            model_service=ModelServiceSettings("configured_provider", "https://example.invalid", ref),
            data_interface=DataInterfaceSettings("ifind_http", ref),
            storage_export=StorageExportSettings(),
            preferences=PreferenceSettings(),
        )
        self.assertEqual(snapshot.model_service.secret_ref.redacted()["key"], "model/default")
        self.assertFalse(hasattr(ref, "value"))

    def test_macos_window_contract_has_no_unavailable_placeholder(self) -> None:
        command = MacOSAppWindow(Path("/tmp/OptionHelper"), "http://127.0.0.1:4181").command()
        self.assertEqual(command, ("/tmp/OptionHelper",))
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "OptionHelper.app" / "Contents" / "MacOS" / "OptionHelper"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            self.assertEqual(WebViewApp(executable.parents[2]).executable(), executable)

    def test_app_server_can_start_with_explicit_local_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            try:
                url = app.start_background()
                self.assertTrue(url.startswith("http://127.0.0.1:"))
            finally:
                app.shutdown()

    def test_verified_embedded_capability_starts_by_default_and_invalid_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            self.assertTrue(app.registry.integrity["release_ready"])
            app.shutdown()
            with self.assertRaises(CapabilityIntegrityError):
                AppServer(app_data_dir=Path(temporary), capability_root=Path(temporary) / "invalid-capability")

    def test_platform_paths_are_platform_specific(self) -> None:
        paths = AppPaths()
        self.assertIn("Library/Application Support/OptionHelper", paths.user_data_dir("darwin").as_posix())
        self.assertIn("AppData/Local/OptionHelper", paths.user_data_dir("win32").as_posix())
        with self.assertRaises(ValueError):
            paths.user_data_dir("linux")


if __name__ == "__main__":
    unittest.main()
