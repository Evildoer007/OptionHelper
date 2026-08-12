"""计算模块发布边界的聚焦回归。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
for source in (
    PROJECT_ROOT,
    PROJECT_ROOT / "core" / "src",
    PROJECT_ROOT / "modules" / "datafetcher" / "src",
    PROJECT_ROOT / "modules" / "pricer" / "src",
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from core import tool_entry
from modules.datafetcher.models import DataFetchRun
from modules.pricer.observed_state import ObservedContractState, ObservedStateError
from modules.pricer.service import PricerWebInputError, _formal_pricing_input
from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import DataFetchRunRef
from runtime.protocol.models import CallerContext
from runtime.protocol.module_host import ModuleHostContext


class ComputeReleaseProtocolTest(unittest.TestCase):
    def test_core_tool_entry_forwards_host_owned_dependencies_without_mutating_input(self) -> None:
        request = {"contract": {"frozen": True}}
        context = ModuleHostContext(
            session_ref="local:test",
            capability_token=f"v2.999999999999.{'1' * 64}",
            analysis_case_id="case-a",
            task_id="task-a",
            candidate_id="candidate-a",
            catalog_version="v1.0",
            contract_fingerprint="a" * 64,
            module="pricer",
            page_hash="b" * 64,
            capability_version="12.1",
            protocol_version="v1.1",
            context_id="mhc_compute_release_0001",
            host_kind="app",
            request_policy=("module.run",),
        )
        caller = CallerContext("tenant-a", "principal-a", "admin", ("module.run",), "session-a", "option-helper-app", "request-a")
        store = object()
        data_store = SimpleNamespace(read_bytes=lambda _ref, *, tenant_id: b"")
        calls: list[tuple[dict, dict]] = []

        def handler(value: dict, **kwargs: object) -> dict:
            calls.append((value, kwargs))
            return {"ok": True}

        with (
            patch.object(tool_entry, "bootstrap_runtime"),
            patch.object(tool_entry.importlib, "import_module", return_value=SimpleNamespace(call_tool=handler)),
        ):
            result = tool_entry.call_tool(
                "pricer", request, caller_context=caller, host_context=context,
                result_store=store, data_store=data_store,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(request, {"contract": {"frozen": True}})
        self.assertEqual(calls[0][0], request)
        self.assertEqual(
            calls[0][1],
            {
                "host_context": context,
                "result_store": store,
                "data_store": data_store,
                "tenant_id": "tenant-a",
            },
        )

    def test_backtester_requires_and_forwards_only_host_datastore(self) -> None:
        context = ModuleHostContext(
            session_ref="local:test", capability_token=f"v2.999999999999.{'1' * 64}",
            analysis_case_id="case-a", task_id="task-a", candidate_id="candidate-a",
            catalog_version="v1.0", contract_fingerprint="a" * 64, module="backtester",
            page_hash="b" * 64, capability_version="12.1", protocol_version="v1.1",
            context_id="mhc_backtester_datastore_0001", host_kind="app", request_policy=("module.run",),
        )
        caller = CallerContext("tenant-a", "principal-a", "admin", ("module.run",), "session-a", "option-helper-app", "request-a")
        data_store = SimpleNamespace(read_bytes=lambda _ref, *, tenant_id: b"")
        calls: list[dict[str, object]] = []

        def handler(_value: dict, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"ok": True}

        with (
            patch.object(tool_entry, "bootstrap_runtime"),
            patch.object(tool_entry.importlib, "import_module", return_value=SimpleNamespace(call_tool=handler)),
        ):
            with self.assertRaisesRegex(tool_entry.ToolDispatchError, "DataStorePort"):
                tool_entry.call_tool("backtester", {"action": "run"}, caller_context=caller, host_context=context)
            result = tool_entry.call_tool(
                "backtester", {"action": "run"}, caller_context=caller, host_context=context, data_store=data_store,
            )

        self.assertTrue(result["ok"])
        self.assertIs(calls[0]["data_store"], data_store)
        self.assertEqual(calls[0]["tenant_id"], "tenant-a")

    def test_datastore_cannot_be_supplied_to_a_non_data_compute_module(self) -> None:
        context = ModuleHostContext(
            session_ref="local:test", capability_token=f"v2.999999999999.{'1' * 64}",
            analysis_case_id="case-a", task_id="task-a", candidate_id="candidate-a",
            catalog_version="v1.0", contract_fingerprint="a" * 64, module="payoffer",
            page_hash="b" * 64, capability_version="12.1", protocol_version="v1.1",
            context_id="mhc_payoffer_datastore_0001", host_kind="app", request_policy=("module.run",),
        )
        caller = CallerContext("tenant-a", "principal-a", "admin", ("module.run",), "session-a", "option-helper-app", "request-a")
        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "Pricer或Backtester"):
            tool_entry.call_tool(
                "payoffer", {"action": "run"}, caller_context=caller, host_context=context,
                data_store=SimpleNamespace(read_bytes=lambda _ref, *, tenant_id: b""),
            )

    def test_core_rejects_payload_aliases_for_host_dependencies_and_paths(self) -> None:
        context = ModuleHostContext(
            session_ref="local:test", capability_token=f"v2.999999999999.{('1' * 64)}",
            analysis_case_id="case-a", task_id="task-a", candidate_id="candidate-a",
            catalog_version="v1.0", contract_fingerprint="a" * 64, module="backtester",
            page_hash="b" * 64, capability_version="12.1", protocol_version="v1.1",
            context_id="mhc_payload_field_reject_0001", host_kind="app", request_policy=("module.catalog",),
        )
        caller = CallerContext("tenant-a", "principal-a", "admin", ("module.catalog",), "session-a", "option-helper-app", "request-a")
        for field in (
            "data_store", "DataStore", "data-store", "filePath", "PATH",
            "history_reference", "historyReference", "history-reference",
        ):
            with self.assertRaisesRegex(tool_entry.ToolDispatchError, "App Host|DataAssetRef"):
                tool_entry.call_tool(
                    "backtester", {"action": "catalog", field: "forged"},
                    caller_context=caller, host_context=context,
                )

    def test_core_tool_entry_rejects_an_unscoped_run(self) -> None:
        with (
            patch.object(tool_entry, "bootstrap_runtime"),
            patch.object(tool_entry.importlib, "import_module", return_value=SimpleNamespace(call_tool=lambda value, **kwargs: {"ok": True})),
        ):
            with self.assertRaisesRegex(tool_entry.ToolDispatchError, "CallerContext|ModuleHostContext|授权"):
                tool_entry.call_tool("pricer", {"action": "run"})

    def test_core_tool_entry_rejects_unscoped_introspection(self) -> None:
        with (
            patch.object(tool_entry, "bootstrap_runtime"),
            patch.object(tool_entry.importlib, "import_module", return_value=SimpleNamespace(call_tool=lambda value, **kwargs: {"ok": True})),
        ):
            with self.assertRaisesRegex(tool_entry.ToolDispatchError, "CallerContext"):
                tool_entry.call_tool("pricer", {"action": "catalog"})

    def test_catalog_and_run_permissions_are_not_interchangeable(self) -> None:
        base = ModuleHostContext(
            session_ref="local:test", capability_token=f"v2.999999999999.{'1' * 64}",
            analysis_case_id="case-a", task_id="task-a", candidate_id="candidate-a",
            catalog_version="v1.0", contract_fingerprint="a" * 64, module="pricer",
            page_hash="b" * 64, capability_version="12.1", protocol_version="v1.1",
            context_id="mhc_permission_split_0001", host_kind="app", request_policy=("module.run",),
        )
        run_caller = CallerContext("tenant-a", "principal-a", "admin", ("module.run",), "session-a", "option-helper-app")
        catalog_caller = replace(run_caller, capabilities=("module.catalog",))
        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.catalog"):
            tool_entry.call_tool("pricer", {"action": "catalog"}, caller_context=run_caller, host_context=base)
        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.run"):
            tool_entry.call_tool(
                "pricer", {"action": "run"}, caller_context=catalog_caller,
                host_context=replace(base, request_policy=("module.catalog",)),
            )

    def test_observed_state_hash_is_derived_and_tampering_is_rejected(self) -> None:
        value = {
            "valuation_date": "2026-01-02",
            "lifecycle_status": "active",
            "occurred_events": [{"event_type": "knock_in", "event_date": "2026-01-02"}],
            "source_refs": ["data-asset:observed-state"],
        }
        state = ObservedContractState.from_value(value, valuation_date="2026-01-02")
        self.assertRegex(str(state.state_hash), r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(ObservedStateError, "state_hash"):
            ObservedContractState.from_value({**value, "state_hash": "0" * 64}, valuation_date="2026-01-02")

    def test_data_fetch_run_ref_has_the_complete_core_shape(self) -> None:
        run = DataFetchRun(
            data_fetch_run_id="datafetch-a",
            task_id="task-a",
            status="complete",
            request_hash="r" * 64,
            cache_decision="hit",
            tenant_id="tenant-a",
            expected_manifest_hash="a" * 64,
            expected_data_asset_hash="b" * 64,
        )
        reference = DataFetchRunRef(**run.to_dict()["data_fetch_run_ref"])
        self.assertEqual(reference.tenant_id, "tenant-a")

    def test_pricer_rejects_a_forged_resolved_contract_snapshot(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        forged = replace(contract, registry_snapshot_hash="0" * 64)
        with self.assertRaisesRegex(PricerWebInputError, "快照"):
            _formal_pricing_input({"contract": forged.to_protocol_dict(), "pricing_config": {}, "market_data_refs": []})


if __name__ == "__main__":
    unittest.main()
