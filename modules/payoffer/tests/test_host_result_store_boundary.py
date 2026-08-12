"""Payoffer正式Host边界的回归契约。

这些测试只约束模块适配层：Host负责验证合同引用并注入ResultStore，Payoffer
不得重新解释ProductVersion，也不得从页面payload接受Host签名字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import PayoffInput, resolve_contract
from runtime.adapters.local_store import LocalResultStore
from runtime.protocol.module_host import HostObjectRef, ModuleHostContext
from runtime.protocol.models import ModuleRunRef
from modules.payoffer.impl import engine as payoff_engine
from modules.payoffer.service import PayoffEngineError, call_tool, run_payoff
from .host_v2_fixture import authorized_context


@dataclass(frozen=True)
class _RunRef:
    module: str
    tenant_id: str
    task_id: str
    run_id: str
    expected_semantic_result_hash: str
    expected_artifact_manifest_hash: str


class _RecordingStore:
    def __init__(self) -> None:
        self.commits: list[dict[str, object]] = []

    def commit_module_run(self, **kwargs: object) -> _RunRef:
        self.commits.append(dict(kwargs))
        return _RunRef(
            module=str(kwargs["module"]),
            tenant_id=str(kwargs["tenant_id"]),
            task_id=str(kwargs["task_id"]),
            run_id=str(kwargs["run_id"]),
            expected_semantic_result_hash="a" * 64,
            expected_artifact_manifest_hash="b" * 64,
        )

    def resolve_module_run(self, ref: _RunRef, *, tenant_id: str) -> Path:
        if tenant_id != ref.tenant_id:
            raise PermissionError("cross tenant")
        return Path("/host-owned-result-store") / ref.module / ref.task_id / ref.run_id


class PayofferHostResultStoreBoundaryTest(unittest.TestCase):
    def _contract(self):
        return resolve_contract(
            "2.1",
            identity={
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": 6500.0},
            },
            term_overrides={"K": 105.0},
        )

    def _context(
        self,
        *,
        contract_ref: bool = True,
        contract_ref_hash: str | None = None,
        contract_ref_schema: str = "optionhelper.resolved-contract/v1",
    ) -> ModuleHostContext:
        contract = self._contract()
        _, context = authorized_context(
            request_policy=("module.run",),
            analysis_case_id="case_host",
            task_id="task_host",
            candidate_id="candidate_host",
            catalog_version="catalog_host",
            contract_fingerprint=contract.contract_fingerprint,
            contract_ref=HostObjectRef(
                reference_id="resolved-contract:test",
                schema_id=contract_ref_schema,
                content_hash=contract_ref_hash or contract.contract_fingerprint,
            ) if contract_ref else None,
        )
        return context

    def test_host_fields_override_page_payload_and_are_persisted_unchanged(self) -> None:
        contract = self._contract()
        store = _RecordingStore()
        result = call_tool(
            {
                "action": "run",
                "payoff_input": PayoffInput(contract=contract),
                "task_id": "task_from_page",
                "analysis_case_id": "case_from_page",
                "candidate_id": "candidate_from_page",
                "catalog_version": "catalog_from_page",
                "contract_fingerprint": "f" * 64,
                "run_id": "run_host",
            },
            host_context=self._context(),
            result_store=store,
            tenant_id="tenant_host",
        )

        self.assertEqual(len(store.commits), 1)
        manifest = store.commits[0]["files"]["manifest.json"]
        self.assertEqual(manifest["analysis_case_id"], "case_host")
        self.assertEqual(manifest["candidate_id"], "candidate_host")
        self.assertEqual(manifest["catalog_version"], "catalog_host")
        self.assertEqual(manifest["contract_fingerprint"], contract.contract_fingerprint)
        self.assertEqual(result["analysis_case_id"], "case_host")
        self.assertEqual(result["candidate_id"], "candidate_host")
        self.assertEqual(result["catalog_version"], "catalog_host")
        self.assertEqual(result["contract_fingerprint"], contract.contract_fingerprint)

    def test_host_store_is_the_only_store_and_run_ref_keeps_module(self) -> None:
        store = _RecordingStore()
        with patch.object(payoff_engine, "LocalResultStore", side_effect=AssertionError("second store")):
            result = run_payoff(
                PayoffInput(contract=self._contract()),
                run_id="run_host",
                host_context=self._context(),
                result_store=store,
                tenant_id="tenant_host",
            )

        self.assertEqual(len(store.commits), 1)
        self.assertEqual(store.commits[0]["module"], "payoffer")
        self.assertEqual(result["module_run_ref"]["module"], "payoffer")
        self.assertNotIn("destination", result)

    def test_missing_run_id_gets_a_unique_hosted_run_id(self) -> None:
        store = _RecordingStore()
        request = {"action": "run", "payoff_input": PayoffInput(contract=self._contract())}

        first = call_tool(
            request,
            host_context=self._context(),
            result_store=store,
            tenant_id="tenant_host",
        )
        second = call_tool(
            request,
            host_context=self._context(),
            result_store=store,
            tenant_id="tenant_host",
        )

        self.assertTrue(first["run_id"])
        self.assertTrue(second["run_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual([item["run_id"] for item in store.commits], [first["run_id"], second["run_id"]])

    def test_formal_run_commits_once_to_real_core_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary))
            result = call_tool(
                {"action": "run", "payoff_input": PayoffInput(contract=self._contract()), "run_id": "run_core_store"},
                host_context=self._context(),
                result_store=store,
                tenant_id="tenant_host",
            )
            reference = ModuleRunRef(**result["module_run_ref"])
            destination = store.resolve_module_run(reference, tenant_id="tenant_host")
            self.assertEqual(reference.module, "payoffer")
            self.assertTrue((destination / "result.json").is_file())
            self.assertTrue((destination / "artifacts" / "payoff.svg").is_file())

    def test_formal_run_rejects_contract_without_host_verified_reference(self) -> None:
        with self.assertRaisesRegex(PayoffEngineError, "验证|contract_ref|ProductVersion"):
            run_payoff(
                PayoffInput(contract=self._contract()),
                run_id="run_unverified",
                host_context=self._context(contract_ref=False),
                result_store=_RecordingStore(),
                tenant_id="tenant_host",
            )

        with self.assertRaisesRegex(PayoffEngineError, "验证|contract_ref|fingerprint"):
            run_payoff(
                PayoffInput(contract=self._contract()),
                run_id="run_wrong_reference",
                host_context=self._context(contract_ref_hash="e" * 64),
                result_store=_RecordingStore(),
                tenant_id="tenant_host",
            )

        with self.assertRaisesRegex(PayoffEngineError, "ResolvedContract|引用"):
            run_payoff(
                PayoffInput(contract=self._contract()),
                run_id="run_wrong_schema",
                host_context=self._context(contract_ref_schema="arbitrary-schema"),
                result_store=_RecordingStore(),
                tenant_id="tenant_host",
            )

    def test_formal_call_without_host_store_does_not_fall_back_to_local_store(self) -> None:
        with patch.object(payoff_engine, "LocalResultStore", side_effect=AssertionError("implicit local store")):
            with self.assertRaisesRegex((PayoffEngineError, TypeError), "Host|ResultStore|host_context|result_store"):
                call_tool({"action": "run", "payoff_input": PayoffInput(contract=self._contract()), "run_id": "run_missing_store"})

        with self.assertRaisesRegex(PayoffEngineError, "tenant_id"):
            call_tool(
                {"action": "run", "payoff_input": PayoffInput(contract=self._contract()), "run_id": "run_missing_tenant"},
                host_context=self._context(),
                result_store=_RecordingStore(),
            )


if __name__ == "__main__":
    unittest.main()
