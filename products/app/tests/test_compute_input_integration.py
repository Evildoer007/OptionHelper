"""App-friendly calculator requests compile to one formal contract chain."""

from __future__ import annotations

import importlib.util
import inspect
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT / "core" / "src", APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.input_adapter import prepare_compute_request
from runtime.protocol.module_host import ModuleHostContext
from backend.authorization.roles import Role
from backend.errors import ValidationError
from backend.identity.session_identity import SessionIdentity
from backend.stores import _LocalDocumentStore
from backend.stores.data_store import DataStore
from backend.stores.result_store import ResultStore
from backend.task_runtime.task_service import TaskService
from backend.tool_gateway import ToolGateway, _business_payload, _reject_path_payload, _reject_untrusted_host_fields


ASSET = "000905.SH"


def _data_ref() -> dict[str, object]:
    return {
        "data_asset_id": "market-a", "storage_ref": f"data:local:market-a:{'a' * 64}:{'b' * 64}",
        "media_type": "text/csv", "schema_id": "market-history-v1", "asset_ids": [ASSET],
        "normalized_fields": ["date", "asset_id", "close", "adj_close"],
        "coverage": {"start": "2023-01-03", "end": "2023-01-04", "sessions": ["2023-01-03", "2023-01-04"], "calendar_id": "CN-SSE", "calendar_version": "test-v1"},
        "row_count": 2, "price_convention": {"adjustment": "close_and_adj_close"},
        "content_hash": "a" * 64, "lineage": {"provider": "test"}, "tenant_id": "local",
        "created_by": "principal-a", "access_scope": ["read"], "partition_spec": {},
    }


def _friendly(module: str) -> dict[str, object]:
    request: dict[str, object] = {
        "product_id": "2.1",
        "identity": {"underlyings": [ASSET], "reference_prices": {ASSET: 100.0}, "contract_start_date": "2023-01-03"},
        "term_overrides": {"K": 105.0},
    }
    if module == "pricer":
        request["pricing_config"] = {"valuation_date": "2023-01-04", "risk_free_rate": 0.02, "dividend_yield": 0.0, "model_method": "black_scholes"}
    elif module == "backtester":
        request["backtest_config"] = {"entry_rule": "explicit", "entry_dates": ["2023-01-03"]}
    return request


class ComputeInputIntegrationTest(unittest.TestCase):
    def test_tool_entry_signature_and_three_formal_inputs_share_contract(self) -> None:
        spec = importlib.util.spec_from_file_location("source_tool_entry", ROOT / "core" / "tool_entry.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        signature = inspect.signature(module.call_tool)
        self.assertEqual(list(signature.parameters), ["module", "request", "caller_context", "host_context", "result_store", "data_store"])
        self.assertTrue(callable(module.prepare_compute_request))

        prepared = {
            name: prepare_compute_request(name, _friendly(name), data_refs=() if name == "payoffer" else (_data_ref(),))
            for name in ("payoffer", "pricer", "backtester")
        }
        fingerprints = {value["contract_fingerprint"] for value in prepared.values()}
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(set(prepared["payoffer"]["request"]), {"action", "payoff_input"})
        self.assertEqual(set(prepared["pricer"]["request"]), {"action", "contract", "pricing_config", "market_data_refs"})
        self.assertEqual(set(prepared["backtester"]["request"]), {"action", "contract", "backtest_config", "historical_data"})
        for value in prepared.values():
            contract = value["resolved_contract"]
            self.assertEqual(contract["contract_fingerprint"], value["contract_fingerprint"])
            self.assertEqual(contract["registry_snapshot_hash"], value["registry_snapshot_hash"])
            self.assertEqual(contract["product_snapshot_hash"], value["product_snapshot_hash"])

    def test_host_binding_is_generated_after_contract_resolution(self) -> None:
        class Registry:
            manifest = {"catalog_version": "v1.0"}

            def bind_contract_context(self, _identity: object, context: ModuleHostContext, **values: object) -> ModuleHostContext:
                return replace(context, **values)

        gateway = object.__new__(ToolGateway)
        gateway._registry = Registry()
        context = ModuleHostContext(
            session_ref="local:test", capability_token=f"v2.999999999999.{('1' * 64)}",
            analysis_case_id=None, task_id="task-a", candidate_id=None, catalog_version=None,
            contract_fingerprint=None, module="payoffer", page_hash="2" * 64,
            capability_version="v1.0", protocol_version="v1.0", context_id="mhc_compute_input_0001",
        )
        prepared = prepare_compute_request("payoffer", _friendly("payoffer"))
        binding = {
            **prepared,
            "catalog_version": "v1.0",
            "contract_ref": {
                "reference_id": f"contract-{prepared['contract_fingerprint'][:24]}",
                "schema_id": "optionhelper.resolved-contract/v1",
                "content_hash": prepared["contract_fingerprint"],
            },
        }
        bound = gateway._bind_contract_context(SessionIdentity("principal-a", "local", Role.ADMIN, "session-a"), context, binding)
        self.assertEqual(bound.task_id, "task-a")
        self.assertEqual(bound.catalog_version, "v1.0")
        self.assertEqual(bound.contract_fingerprint, prepared["contract_fingerprint"])
        self.assertEqual(bound.contract_ref.content_hash, prepared["contract_fingerprint"])
        self.assertEqual(bound.contract_ref.schema_id, "optionhelper.resolved-contract/v1")

    def test_same_task_reuses_frozen_contract_and_rejects_conflicting_terms(self) -> None:
        first = prepare_compute_request("payoffer", _friendly("payoffer"))
        hosted_pricing = _friendly("pricer")
        for field in ("product_id", "identity", "term_overrides"):
            hosted_pricing.pop(field)
        pricing = prepare_compute_request(
            "pricer",
            hosted_pricing,
            data_refs=(_data_ref(),),
            resolved_contract=first["resolved_contract"],
        )
        self.assertEqual(pricing["contract_fingerprint"], first["contract_fingerprint"])
        self.assertEqual(pricing["product_snapshot_hash"], first["product_snapshot_hash"])
        conflicting = _friendly("backtester")
        conflicting["term_overrides"] = {"K": 106.0}
        with self.assertRaisesRegex(Exception, "冻结ResolvedContract冲突"):
            prepare_compute_request(
                "backtester",
                conflicting,
                data_refs=(_data_ref(),),
                resolved_contract=first["resolved_contract"],
            )

    def test_pricer_allows_zero_data_refs_only_for_complete_mc10_demo_snapshot(self) -> None:
        demo = _friendly("pricer")
        demo["pricing_config"] = {
            "valuation_date": "2023-01-03", "spot": 100.0, "volatility_override": 0.20,
            "time_to_maturity": 1.0, "risk_free_rate": 0.02, "dividend_yield": 0.0,
            "model_method": "monte_carlo", "path_count": 10, "demo_mode": True,
            "demo_calendar": {
                "calendar_id": "demo-european-vanilla", "calendar_version": "v1",
                "sessions": ["2023-01-03"], "discrete_path": False,
            },
        }
        prepared = prepare_compute_request("pricer", demo)
        self.assertEqual(prepared["request"]["market_data_refs"], [])

        invalid = dict(demo)
        invalid["pricing_config"] = {**demo["pricing_config"], "path_count": 11}
        with self.assertRaisesRegex(Exception, "唯一DataAssetRef"):
            prepare_compute_request("pricer", invalid)

    def test_browser_cannot_supply_host_identity(self) -> None:
        for field in (
            "tenant_id", "principal_id", "session_id", "host_context", "capability_token", "data_store", "data_store_port",
            "DataStore", "dataStore", "data-store", "DATA_STORE",
        ):
            with self.assertRaises(ValidationError):
                _reject_untrusted_host_fields({"nested": {field: "forged"}})
        for field in ("file_path", "filePath", "FILE-PATH", "path"):
            with self.assertRaises(ValidationError):
                _reject_path_payload({"items": [{field: "/tmp/forged.csv"}]})
        self.assertEqual(
            _business_payload({"task_id": "host-task", "contract_fingerprint": "a" * 64, "product_id": "2.1"}),
            {"product_id": "2.1"},
        )

    def test_app_gateway_injects_a_bound_datastore_only_for_backtester(self) -> None:
        context = ModuleHostContext(
            session_ref="session:a", capability_token=f"v2.999999999999.{'1' * 64}",
            analysis_case_id="case-a", task_id="task-a", candidate_id="candidate-a",
            catalog_version="v1.0", contract_fingerprint=None, module="backtester",
            page_hash="b" * 64, capability_version="12.1", protocol_version="v1.1",
            context_id="mhc_app_backtester_store_0001", host_kind="app", request_policy=("module.run",),
        )
        identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")
        data_ref = {**_data_ref(), "tenant_id": "tenant-a"}
        data_port = object()
        result_port = object()
        captured: dict[str, object] = {}

        class Registry:
            capability_root = Path("/verified-capability")
            manifest = {"catalog_version": "v1.0"}

            def validate_host_request(self, *_args: object) -> ModuleHostContext:
                return context

            def bind_contract_context(self, _identity: object, value: ModuleHostContext, **claims: object) -> ModuleHostContext:
                return replace(value, **claims)

        class Policy:
            def require(self, *_args: object) -> None:
                return None

        class Contracts:
            def get(self, *_args: object) -> None:
                return None

            def bind(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                return {
                    "contract_fingerprint": "a" * 64, "catalog_version": "v1.0",
                    "contract_ref": {"reference_id": "contract-a", "schema_id": "optionhelper.resolved-contract/v1", "content_hash": "a" * 64},
                }

        class Assets:
            def resolve_for_compute(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                return data_ref

        class Results:
            def bind_module_store(self, *_args: object) -> object:
                return result_port

        class DataFetcher:
            def bind_data_store(self, principal: SessionIdentity) -> object:
                self.principal = principal
                return data_port

        class Entry:
            @staticmethod
            def prepare_compute_request(*_args: object, **_kwargs: object) -> dict[str, object]:
                return {
                    "request": {"action": "run", "contract": {"contract_fingerprint": "a" * 64}, "backtest_config": {}, "historical_data": data_ref},
                    "resolved_contract": {"contract_fingerprint": "a" * 64}, "contract_fingerprint": "a" * 64,
                }

            @staticmethod
            def call_tool(module: str, request: dict[str, object], **kwargs: object) -> dict[str, object]:
                captured.update({"module": module, "request": request, **kwargs})
                return {"ok": False, "status": "failed"}

        datafetcher = DataFetcher()
        gateway = ToolGateway(
            Registry(), Policy(), datafetcher=datafetcher, results=Results(), data_assets=Assets(), contracts=Contracts(),
        )
        with (
            patch("backend.tool_gateway.capability_import_scope", return_value=nullcontext()),
            patch("backend.tool_gateway.load_verified_capability_service"),
            patch.object(gateway, "_load_tool_entry", return_value=Entry()),
        ):
            gateway.dispatch(
                "backtester", {"action": "run", "product_id": "2.1", "identity": {"underlyings": [ASSET]}, "backtest_config": {}},
                identity, module_context=context, request_id="request-a",
            )

        self.assertIs(captured["data_store"], data_port)
        self.assertIs(captured["result_store"], result_port)
        self.assertEqual(captured["caller_context"].tenant_id, "tenant-a")
        self.assertNotIn("data_store", captured["request"])
        self.assertEqual(datafetcher.principal, identity)

    def test_core_commit_is_reconciled_when_app_index_fails_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")
            task = TaskService(state).create(identity, "index recovery")
            store = ResultStore(state)
            original_update = state.update
            failed = False

            def flaky(name, mutator):
                nonlocal failed
                if name == "results" and not failed:
                    failed = True
                    raise OSError("injected App index failure")
                return original_update(name, mutator)

            with patch.object(state, "update", side_effect=flaky):
                reference = store.commit_module_run(identity, task["task_id"], "payoffer", {
                    "ok": True, "status": "succeeded", "run_id": "run-a", "analysis_case_id": "case-a",
                    "candidate_id": "candidate-a", "catalog_version": "v1.0", "contract_fingerprint": "a" * 64,
                    "resolved_contract": {"identity": {"product_id": "2.1", "name_zh": "看涨期权"}, "product_version": "v1.0", "contract_fingerprint": "a" * 64},
                })
            self.assertEqual(reference["run_id"], "run-a")
            self.assertEqual(state.read("result_recovery"), {})
            self.assertEqual(len(list((Path(temporary) / "module-runs").rglob("commit_marker.json"))), 1)
            self.assertEqual(store.resolve_module_run(identity, reference)["run_id"], "run-a")


if __name__ == "__main__":
    unittest.main()
