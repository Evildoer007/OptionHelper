"""App DataFetcher and Reporter port-contract regression tests."""

from __future__ import annotations

import os
from hashlib import sha256
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.agent_runtime.tool_dispatcher import ToolDispatcher
from backend.app_server import AppServer
from backend.authorization.policy import AuthorizationPolicy
from backend.authorization.roles import Role
from backend.capability_service import CapabilityServiceCaller
from backend.datafetcher_adapter import DataFetcherAdapter
from backend.errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from backend.identity.session_identity import SessionIdentity
from backend.reporter_adapter import ReporterAdapter
from backend.settings.settings_models import DataInterfaceSettings, ModelServiceSettings, PreferenceSettings, SettingsSnapshot, StorageExportSettings
from backend.secrets.secret_ref import SecretRef
from backend.task_runtime.job_runner import JobRunner
from backend.tool_gateway import ToolGateway
from runtime.protocol.models import SecretRef as CoreSecretRef
from runtime.protocol.models import DataAssetRef
from runtime.adapters.local_store import LocalDataStore
from runtime.ports.tool_gateway import ToolGatewayPort
from capability_fixture import capability_root


def _asset(asset_id: str = "data:ifind-001") -> dict[str, object]:
    return {
        "data_asset_id": asset_id,
        "storage_ref": "assetref-001",
        "media_type": "application/json",
        "schema_id": "optionhelper.data-asset/v1",
        "asset_ids": ["000905.SH"],
        "normalized_fields": ["close"],
        "coverage": {"start": "2026/01/01", "end": "2026/01/31"},
        "row_count": 21,
        "price_convention": {"adjustment": "forward"},
        "content_hash": "sha256:asset-001",
        "lineage": {"provider": "ifind-http"},
    }


class _GatewayResponse:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(self._response)


class DataFetcherAdapterTests(unittest.TestCase):
    def test_app_secret_ref_is_the_core_value_object(self) -> None:
        self.assertIs(SecretRef, CoreSecretRef)

    def test_gateway_exposes_a_host_bound_internal_port_without_http(self) -> None:
        identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
        gateway = object.__new__(ToolGateway)
        calls: list[tuple[object, ...]] = []

        def dispatch(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((*args, kwargs))
            return {"ok": True, "status": "queued"}

        gateway.dispatch = dispatch  # type: ignore[method-assign]
        marker = object()
        port = gateway.bind_internal(identity, lambda module, request: marker)
        self.assertIsInstance(port, ToolGatewayPort)
        self.assertEqual(port.call("pricer", {"action": "run"})["status"], "queued")
        self.assertEqual(calls[0][0:3], ("pricer", {"action": "run"}, identity))
        self.assertIs(calls[0][3]["module_context"], marker)
        self.assertTrue(str(calls[0][3]["request_id"]).startswith("internal_"))

    def test_fetch_uses_an_app_aware_service_with_core_caller_and_secret_reference(self) -> None:
        identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
        secret_ref = SecretRef("keychain", "ifind/analyst-a", "v1")
        snapshot = SettingsSnapshot(
            role="admin",
            model_service=ModelServiceSettings("unconfigured", ""),
            data_interface=DataInterfaceSettings("ifind-http", secret_ref),
            storage_export=StorageExportSettings(),
            preferences=PreferenceSettings(),
        )
        calls: list[tuple[dict[str, object], object, object, object]] = []

        class AppAwareService:
            def call_app_datafetcher(self, request: dict[str, object], caller: object, credential: object, secret_port: object) -> dict[str, object]:
                calls.append((request, caller, credential, secret_port))
                return {"ok": True, "status": "completed", "data_asset_ref": _asset()}

        from backend.secrets.secret_provider import SecretProvider

        reply = DataFetcherAdapter(
            lambda _principal: snapshot, AppAwareService(), secret_provider=SecretProvider(),
        ).dispatch(
            {"action": "fetch", "task_id": "task-a", "request": {"asset_id": "000905.SH"}}, identity, request_id="host-request-a",
        )
        self.assertEqual(reply["status"], "completed")
        request, caller, credential, secret_port = calls[0]
        self.assertNotIn("app_context", request)
        self.assertEqual(caller.tenant_id, "tenant-a")
        self.assertEqual(caller.principal_id, "analyst-a")
        self.assertEqual(caller.request_id, "host-request-a")
        self.assertEqual(credential, secret_ref)
        self.assertTrue(callable(secret_port))

    def test_verified_capability_accepts_app_caller_and_secret_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {
                "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
                "OPTIONHELPER_DATA_ROOT": str(root / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(root / "result"),
            }):
                app = AppServer(app_data_dir=root / "app-state", capability_root=capability_root())
                identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
                app.save_settings(identity, SettingsSnapshot(
                    role="admin",
                    model_service=ModelServiceSettings("unconfigured", ""),
                    data_interface=DataInterfaceSettings("ifind-http", SecretRef("keychain", "ifind/analyst-a")),
                    storage_export=StorageExportSettings(),
                    preferences=PreferenceSettings(),
                ), "settings.data.write")
                reply = DataFetcherAdapter(app.settings_for, CapabilityServiceCaller(app.registry)).dispatch(
                    {"action": "status"}, identity, request_id="app-datafetcher-port",
                )
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["module"], "datafetcher")
            self.assertEqual(reply["status"], "available")

    def test_download_port_receives_only_opaque_id_and_host_caller(self) -> None:
        identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
        snapshot = SettingsSnapshot(
            role="admin",
            model_service=ModelServiceSettings("unconfigured", ""),
            data_interface=DataInterfaceSettings("unconfigured", None),
            storage_export=StorageExportSettings(),
            preferences=PreferenceSettings(),
        )
        calls: list[tuple[str, object]] = []

        class AppAwareService:
            def read_app_datafetcher_asset(self, data_asset_id: str, caller: object) -> tuple[dict[str, object], bytes]:
                calls.append((data_asset_id, caller))
                return _asset("data-asset-001"), b"date,asset_id,close\n2026/08/10,000905.SH,100\n"

        reference, content = DataFetcherAdapter(lambda _principal: snapshot, AppAwareService()).read_asset(
            "data-asset-001", identity, request_id="download-1",
        )
        self.assertEqual(reference["data_asset_id"], "data-asset-001")
        self.assertTrue(content.startswith(b"date,"))
        data_asset_id, caller = calls[0]
        self.assertEqual(data_asset_id, "data-asset-001")
        self.assertEqual(caller.tenant_id, "tenant-a")
        self.assertEqual(caller.principal_id, "analyst-a")
        self.assertEqual(caller.request_id, "download-1")
        self.assertFalse(hasattr(caller, "secret_ref"))

    def test_bound_datastore_verifies_tenant_reference_and_actual_bytes(self) -> None:
        identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
        snapshot = SettingsSnapshot(
            role="admin", model_service=ModelServiceSettings("unconfigured", ""),
            data_interface=DataInterfaceSettings("unconfigured", None),
            storage_export=StorageExportSettings(), preferences=PreferenceSettings(),
        )
        payload = b"date,asset_id,close\n2026/08/10,000905.SH,100\n"
        reference = {
            **_asset("data-asset-001"), "tenant_id": "tenant-a", "created_by": "analyst-a",
            "access_scope": ["read"], "partition_spec": {}, "content_hash": sha256(payload).hexdigest(),
        }

        class AppAwareService:
            def read_app_datafetcher_asset(self, data_asset_id: str, caller: object) -> tuple[dict[str, object], bytes]:
                self.data_asset_id = data_asset_id
                self.caller = caller
                return dict(reference), payload

        class HostStore:
            def read_bytes(self, _ref: object, *, tenant_id: str) -> bytes:
                self.tenant_id = tenant_id
                return payload

        service = AppAwareService()
        host_store = HostStore()
        adapter = DataFetcherAdapter(lambda _principal: snapshot, service, host_store)
        port = adapter.bind_data_store(identity)
        ref = DataAssetRef(**reference)
        self.assertEqual(port.read_bytes(ref, tenant_id="tenant-a"), payload)
        self.assertEqual(host_store.tenant_id, "tenant-a")
        with self.assertRaises(PermissionError):
            port.read_bytes(ref, tenant_id="tenant-b")

        other_owner = DataAssetRef(**{**reference, "created_by": "analyst-b"})
        with self.assertRaises(PermissionError):
            port.read_bytes(other_owner, tenant_id="tenant-a")

        reference["content_hash"] = "0" * 64
        forged = DataAssetRef(**reference)
        with self.assertRaisesRegex(ValidationError, "content_hash"):
            port.read_bytes(forged, tenant_id="tenant-a")

    def test_verified_capability_reads_the_exact_host_bound_asset(self) -> None:
        payload = b"date,asset_id,close\n2026/08/10,000905.SH,100\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {
                "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
                "OPTIONHELPER_DATA_ROOT": str(root / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(root / "result"),
            }):
                app = AppServer(app_data_dir=root / "app-state", capability_root=capability_root())
                identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
                ref = LocalDataStore(root / "data").put_bytes(
                    tenant_id="tenant-a", data_asset_id="backtester-market", payload=payload,
                    media_type="text/csv", schema_id="market-history-v1", asset_ids=("000905.SH",),
                    normalized_fields=("date", "asset_id", "close"), created_by="analyst-a",
                )
                port = DataFetcherAdapter(
                    app.settings_for, CapabilityServiceCaller(app.registry), LocalDataStore(root / "data"),
                ).bind_data_store(identity)
                self.assertEqual(port.read_bytes(ref, tenant_id="tenant-a"), payload)

    def test_missing_app_secret_port_is_unavailable_without_local_fallback(self) -> None:
        identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
        snapshot = SettingsSnapshot(
            role="admin",
            model_service=ModelServiceSettings("unconfigured", ""),
            data_interface=DataInterfaceSettings("ifind-http", SecretRef("keychain", "ifind/analyst-a", "v1")),
            storage_export=StorageExportSettings(),
            preferences=PreferenceSettings(),
        )
        calls: list[dict[str, object]] = []

        def service(module: str, request: dict[str, object]) -> dict[str, object]:
            self.assertEqual(module, "datafetcher")
            calls.append(request)
            return {"ok": False, "status": "unavailable"}

        with self.assertRaises(UnavailableCapabilityError) as raised:
            DataFetcherAdapter(lambda principal: snapshot, service).dispatch({"action": "status"}, identity)
        self.assertEqual(raised.exception.capability, "datafetcher.app_secret_port")
        self.assertEqual(calls, [])
        with self.assertRaises(ValidationError):
            DataFetcherAdapter(lambda principal: snapshot, service).dispatch({"action": "status", "tenant_id": "tenant-b"}, identity)

    def test_data_asset_ref_registers_only_after_success_and_is_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            analyst_a = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
            analyst_b = SessionIdentity("analyst-b", "tenant-b", Role.ADMIN, "session-b")
            task = app.tasks.create(analyst_a, "DataAsset登记")
            dispatcher = ToolDispatcher(
                _GatewayResponse({"ok": True, "status": "completed", "data_asset_ref": _asset()}),
                JobRunner(), app.tasks, app.results, app.data_assets,
            )
            dispatcher.dispatch("datafetcher", {"action": "fetch", "task_id": task["task_id"]}, analyst_a)
            self.assertEqual(app.data_assets.get(analyst_a, "data:ifind-001")["tenant_id"], "tenant-a")
            with self.assertRaises(KeyError):
                app.data_assets.get(analyst_b, "data:ifind-001")

            failed = ToolDispatcher(
                _GatewayResponse({"ok": False, "status": "failed", "data_asset_ref": _asset("data:failed")}),
                JobRunner(), app.tasks, app.results, app.data_assets,
            )
            with self.assertRaises(ValidationError):
                failed.dispatch("datafetcher", {"action": "fetch", "task_id": task["task_id"]}, analyst_a)
            self.assertNotIn("tenant-a:data:failed", app._documents.read("data_assets"))

            incomplete = ToolDispatcher(
                _GatewayResponse({"ok": True, "status": "completed"}),
                JobRunner(), app.tasks, app.results, app.data_assets,
            )
            with self.assertRaises(ValidationError):
                incomplete.dispatch("datafetcher", {"action": "fetch", "task_id": task["task_id"]}, analyst_a)

    def test_reporter_formal_service_is_called_and_cross_tenant_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            analyst_a = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
            analyst_b = SessionIdentity("analyst-b", "tenant-b", Role.ADMIN, "session-b")
            task_a = app.tasks.create(analyst_a, "报告来源A")
            task_b = app.tasks.create(analyst_b, "报告来源B")
            app.results.commit_module_run(analyst_a, task_a["task_id"], "pricer", {
                "run_id": "run-a", "ok": True, "status": "completed", "analysis_case_id": "case-a",
                "resolved_contract": {"contract_fingerprint": "a" * 64, "product_version": "v1", "identity": {"product_id": "2.1", "name_zh": "雪球", "underlyings": ["000905.SH"], "currency": "CNY"}},
            })
            source = app.results.list_report_sources(tenant_id="tenant-a", task_id=task_a["task_id"])["sources"][0]
            adapter = ReporterAdapter(app.results)
            status = adapter.dispatch({"action": "status"}, analyst_a)
            self.assertEqual(status["status"], "available")
            with self.assertRaises(KeyError):
                adapter.dispatch({
                    "action": "run", "task_id": task_b["task_id"], "kind": "report",
                    "selection": {"source_id": source["source_id"]},
                }, analyst_b)

    def test_reporter_requires_a_host_context_bound_to_its_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            identity = SessionIdentity("analyst-a", "tenant-a", Role.ADMIN, "session-a")
            first = app.tasks.create(identity, "报告任务A")
            second = app.tasks.create(identity, "报告任务B")
            gateway = ToolGateway(app.registry, AuthorizationPolicy(), ReporterAdapter(app.results))
            with self.assertRaises(ValidationError):
                gateway.dispatch("reporter", {"action": "status"}, identity, request_id="reporter-status")
            catalog_context = app.registry.host_context(identity, "reporter")
            status = gateway.dispatch(
                "reporter", {"action": "status"}, identity,
                module_context=catalog_context, request_id="reporter-catalog",
            )
            self.assertEqual(status["status"], "available")
            with self.assertRaisesRegex(AuthorizationError, "module.run"):
                gateway.dispatch(
                    "reporter", {"action": "run", "kind": "card", "selection": {}}, identity,
                    module_context=app.registry.host_context(identity, "reporter"),
                    request_id="reporter-catalog-cannot-run",
                )
            context = app.registry.host_context(identity, "reporter", task_id=first["task_id"])
            with self.assertRaises(ValidationError):
                gateway.dispatch(
                    "reporter", {"action": "run", "kind": "card", "task_id": first["task_id"], "selection": {}}, identity,
                    module_context=context, request_id="reporter-first-task",
                )
            with self.assertRaises(ValidationError):
                gateway.dispatch(
                    "reporter", {"action": "run", "task_id": second["task_id"], "selection": {}}, identity,
                    module_context=context, request_id="reporter-wrong-task",
                )


if __name__ == "__main__":
    unittest.main()
