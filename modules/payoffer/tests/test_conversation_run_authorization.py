"""Payoffer正式运行的Desk与OptChat精确授权边界。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from core import tool_entry
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import PayoffInput, resolve_contract
from runtime.protocol.module_host import HostObjectRef
from .host_v2_fixture import authorized_context


class PayofferConversationRunAuthorizationTest(unittest.TestCase):
    def _authorization(self, capability: str):
        contract = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"], "reference_prices": {"000905.SH": 6500.0}},
            term_overrides={"K": 105.0},
        )
        caller, host = authorized_context(
            request_policy=(capability,),
            task_id="task_proxy",
            analysis_case_id="case_proxy",
            candidate_id="candidate_proxy",
            catalog_version="catalog_proxy",
            contract_fingerprint=contract.contract_fingerprint,
            contract_ref=HostObjectRef(
                reference_id="resolved-contract:proxy",
                schema_id="optionhelper.resolved-contract/v1",
                content_hash=contract.contract_fingerprint,
            ),
        )
        return contract, caller, host

    def test_desk_and_optchat_each_use_their_exact_run_capability(self) -> None:
        for capability in ("module.run", "conversation.tool.run"):
            with self.subTest(capability=capability), tempfile.TemporaryDirectory() as temporary:
                contract, caller, host = self._authorization(capability)
                result = tool_entry.call_tool(
                    "payoffer",
                    {"action": "run", "payoff_input": PayoffInput(contract=contract), "run_id": capability.replace(".", "-")},
                    caller_context=caller,
                    host_context=host,
                    result_store=LocalResultStore(Path(temporary)),
                )
                self.assertTrue(result["ok"])

    def test_caller_and_host_run_capabilities_must_match(self) -> None:
        contract, caller, host = self._authorization("module.run")
        mismatched_caller = replace(caller, capabilities=("conversation.tool.run",))
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(tool_entry.ToolDispatchError, "匹配|授权"):
            tool_entry.call_tool(
                "payoffer",
                {"action": "run", "payoff_input": PayoffInput(contract=contract)},
                caller_context=mismatched_caller,
                host_context=host,
                result_store=LocalResultStore(Path(temporary)),
            )

    def test_optchat_run_capability_cannot_read_catalog(self) -> None:
        _, caller, host = self._authorization("conversation.tool.run")
        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.catalog"):
            tool_entry.call_tool(
                "payoffer",
                {"action": "catalog"},
                caller_context=caller,
                host_context=host,
            )


if __name__ == "__main__":
    unittest.main()
