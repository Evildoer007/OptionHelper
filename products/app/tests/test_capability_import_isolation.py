"""Regression coverage for App Capability import isolation."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT / "core" / "src", APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.capability_service import (
    CapabilityServiceCaller,
    capability_import_scope,
    load_verified_capability_service,
)
from backend.page_registry import PageRegistry
from backend.errors import CapabilityIntegrityError, UnavailableCapabilityError
from capability_fixture import capability_root
from runtime.bootstrap import bootstrap_runtime
from runtime.capability_import import CAPABILITY_IMPORT_LOCK
from runtime.protocol.models import CallerContext, SecretRef


class CapabilityImportIsolationTests(unittest.TestCase):
    def test_import_lock_is_shared_across_legacy_app_import_prefixes(self) -> None:
        import products.app.backend.capability_service as package_service
        from evals.runner import EvalRuntime

        self.assertIs(package_service.CAPABILITY_IMPORT_LOCK, CAPABILITY_IMPORT_LOCK)
        self.assertIs(EvalRuntime._shared_import_lock(), CAPABILITY_IMPORT_LOCK)

    def test_capability_import_scopes_serialize_across_threads(self) -> None:
        scripts_root = capability_root() / "scripts"
        first_entered = Event()
        release_first = Event()
        second_entered = Event()
        errors: list[BaseException] = []

        def first() -> None:
            try:
                with capability_import_scope(scripts_root):
                    first_entered.set()
                    release_first.wait(timeout=3)
            except BaseException as error:  # Keep the worker evidence for the test thread.
                errors.append(error)

        def second() -> None:
            try:
                with capability_import_scope(scripts_root):
                    second_entered.set()
            except BaseException as error:
                errors.append(error)

        first_thread = Thread(target=first, daemon=True)
        second_thread = Thread(target=second, daemon=True)
        first_thread.start()
        self.assertTrue(first_entered.wait(timeout=3))
        second_thread.start()
        self.assertFalse(second_entered.wait(timeout=0.1))
        release_first.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(second_entered.is_set())

    def test_runtime_gate_rejects_unverified_shared_source_without_reloading_runtime(self) -> None:
        scripts_root = capability_root() / "scripts"
        name = "runtime.unverified_probe"
        prior = sys.modules.get(name)
        probe = ModuleType(name)
        probe.__file__ = __file__
        sys.modules[name] = probe
        try:
            with self.assertRaises(CapabilityIntegrityError):
                with capability_import_scope(scripts_root):
                    self.fail("unverified runtime source must reject the Capability call")
        finally:
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior

    def test_runtime_gate_rejects_shared_module_without_source(self) -> None:
        scripts_root = capability_root() / "scripts"
        name = "runtime.no_source_probe"
        prior = sys.modules.get(name)
        sys.modules[name] = ModuleType(name)
        try:
            with self.assertRaises(CapabilityIntegrityError):
                with capability_import_scope(scripts_root):
                    self.fail("runtime module without a source must reject the Capability call")
        finally:
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior

    def test_verified_service_source_wins_over_development_module_cache(self) -> None:
        """A preloaded development service cannot satisfy a Capability call."""

        with tempfile.TemporaryDirectory(prefix="optionhelper-capability-source-") as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {
                "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
                "OPTIONHELPER_DATA_ROOT": str(root / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(root / "result"),
            }, clear=False):
                bootstrap_runtime(ROOT)
                development = importlib.import_module("modules.pricer.service")
                development_source = Path(str(development.__file__)).resolve()
                self.assertTrue(str(development_source).startswith(str(ROOT / "modules")))

                scripts_root = capability_root() / "scripts"
                caller_type = CallerContext
                with capability_import_scope(scripts_root):
                    verified = load_verified_capability_service("pricer", scripts_root)
                    verified_source = Path(str(verified.__file__)).resolve()
                    self.assertTrue(verified_source.is_relative_to(scripts_root.resolve()))
                    self.assertNotEqual(verified_source, development_source)
                    from runtime.protocol.models import CallerContext as scoped_caller_type

                    self.assertIs(scoped_caller_type, caller_type)

                self.assertIs(sys.modules["modules.pricer.service"], development)

    def test_app_runtime_scope_is_private_and_does_not_mutate_environment(self) -> None:
        """Embedded services use App-owned stores without leaking process env."""

        scripts_root = capability_root() / "scripts"
        names = (
            "OPTIONHELPER_RUNTIME_ROOT",
            "OPTIONHELPER_DATA_ROOT",
            "OPTIONHELPER_RESULT_ROOT",
        )
        before = {name: os.environ.get(name) for name in names}
        with tempfile.TemporaryDirectory(prefix="optionhelper-app-runtime-") as temporary:
            runtime_root = Path(temporary) / "runtime"
            (runtime_root / "data").mkdir(parents=True)
            (runtime_root / "result").mkdir(parents=True)
            with capability_import_scope(scripts_root, runtime_root=runtime_root):
                service = load_verified_capability_service("payoffer", scripts_root)
                paths = bootstrap_runtime(Path(str(service.__file__)))
                self.assertEqual(paths.mode, "release")
                self.assertEqual(paths.data_root, (runtime_root / "data").resolve())
                self.assertEqual(paths.result_root, (runtime_root / "result").resolve())
                self.assertEqual({name: os.environ.get(name) for name in names}, before)
        self.assertEqual({name: os.environ.get(name) for name in names}, before)

    def test_datafetcher_store_failure_is_not_misclassified_as_missing_app_port(self) -> None:
        """The verified App entry exists; a downstream Store failure stays datafetcher."""

        registry = PageRegistry(capability_root())
        scripts_root = registry.capability_root / "scripts"
        previous_path = list(sys.path)
        previous_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "modules" or name.startswith("modules.")
        }
        with tempfile.TemporaryDirectory(prefix="optionhelper-datafetch-store-failure-") as temporary:
            root = Path(temporary)
            blocked_result_root = root / "result-file"
            blocked_result_root.write_text("not a directory", encoding="utf-8")
            with patch.dict(os.environ, {
                "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
                "OPTIONHELPER_DATA_ROOT": str(root / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(blocked_result_root),
            }, clear=False):
                with capability_import_scope(scripts_root):
                    service = load_verified_capability_service("datafetcher", scripts_root)
                    self.assertTrue(callable(getattr(service, "call_tool_from_app", None)))
                with self.assertRaises(UnavailableCapabilityError) as raised:
                    CapabilityServiceCaller(registry).call_app_datafetcher(
                        {
                            "action": "fetch",
                            "task_id": "store-failure",
                            "request": {"asset_id": "000905.SH"},
                        },
                        CallerContext(
                            tenant_id="tenant-a",
                            principal_id="analyst-a",
                            role="admin",
                            capabilities=("data:read",),
                            session_id="session-a",
                            audience="app",
                            request_id="store-failure",
                        ),
                        SecretRef("keychain", "ifind/analyst-a"),
                    )
            self.assertEqual(raised.exception.capability, "datafetcher")
        self.assertEqual(sys.path, previous_path)
        self.assertEqual(
            {
                name: module
                for name, module in sys.modules.items()
                if name == "modules" or name.startswith("modules.")
            },
            previous_modules,
        )

    def test_eval_execution_restores_development_environment_and_module_namespace(self) -> None:
        from evals.runner import EvalRuntime

        names = (
            "OPTIONHELPER_PROJECT_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT",
            "OPTIONHELPER_DATAFETCHER_OFFLINE", "OPTIONHELPER_MODEL_GATEWAY_URL",
            "OPTIONHELPER_KNOWLEDGER_URL", "OPTIONHELPER_TOOL_GATEWAY_URL",
        )
        previous_environment = {name: os.environ.get(name) for name in names}
        previous_path = list(sys.path)
        previous_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "modules" or name.startswith("modules.")
        }
        previous_dont_write_bytecode = sys.dont_write_bytecode

        runtime = EvalRuntime()
        self.assertEqual({name: os.environ.get(name) for name in names}, previous_environment)
        self.assertEqual(sys.path, previous_path)
        try:
            result = runtime.execute("tool.call", {"module": "pricer", "request": {"action": "catalog"}})
            self.assertTrue(result["ok"])
        finally:
            runtime.close()

        self.assertEqual({name: os.environ.get(name) for name in names}, previous_environment)
        self.assertEqual(sys.path, previous_path)
        self.assertEqual(sys.dont_write_bytecode, previous_dont_write_bytecode)
        self.assertEqual(
            {
                name: module
                for name, module in sys.modules.items()
                if name == "modules" or name.startswith("modules.")
            },
            previous_modules,
        )

    def test_eval_pricer_catalog_then_real_http_pricer_uses_verified_capability(self) -> None:
        """The historical Eval->App sequence must stay valid in one interpreter."""

        from evals.runner import run
        from test_compute_http_loopback import ComputeHttpLoopbackTest

        outcome = run("pricer.catalog.65")
        self.assertEqual(outcome["failed"], 0, outcome)

        case = ComputeHttpLoopbackTest("test_payoffer_pricer_backtester_share_one_host_contract_and_commit_once")
        try:
            case.setUp()
        except PermissionError as error:
            self.skipTest(f"当前执行器禁止127.0.0.1监听：{error}")
        try:
            case.test_payoffer_pricer_backtester_share_one_host_contract_and_commit_once()
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
