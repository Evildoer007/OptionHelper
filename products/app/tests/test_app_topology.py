"""12.1 App source-topology regression tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.agent_runtime.tool_dispatcher import ToolDispatcher
from backend.app_server import AppServer
from backend.identity.identity_provider import IdentityProvider, LocalAuthProvider
from backend.model_gateway.provider_registry import ProviderRegistry
from backend.secrets.secret_provider import SecretProvider
from backend.stores.data_store import DataStore
from backend.stores.result_store import ResultStore
from backend.stores.settings_store import LocalSettingsStore, SettingsStore
from backend.task_runtime.job_runner import JobRunner
from capability_fixture import capability_root


class AppTopologyTests(unittest.TestCase):
    def test_blueprint_formal_boundaries_are_importable_and_live(self) -> None:
        self.assertTrue(issubclass(LocalAuthProvider, IdentityProvider))
        self.assertTrue(issubclass(LocalSettingsStore, SettingsStore))
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            self.assertIsInstance(app.identity_provider, LocalAuthProvider)
            self.assertIsInstance(app.secret_provider, SecretProvider)
            self.assertIsInstance(app.provider_registry, ProviderRegistry)
            self.assertIsInstance(app.settings._store, LocalSettingsStore)
            self.assertIsInstance(app.data_assets, DataStore)
            self.assertIsInstance(app.results, ResultStore)
            self.assertIsInstance(app.tool_dispatcher, ToolDispatcher)
            self.assertIsInstance(app.tool_dispatcher._jobs, JobRunner)

    def test_obsolete_parallel_identity_source_is_absent(self) -> None:
        self.assertFalse((APP_ROOT / "backend" / "identity" / "local_auth_provider.py").exists())


if __name__ == "__main__":
    unittest.main()
