"""OptChat与Desk调用Pricer时的执行权限边界。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import tool_entry
from modules.pricer import service as pricer_service

from . import test_formal_protocol_state_store as formal_fixture
from .pricer_test_fixtures import modulehost_v2_scope


class OptChatPermissionBoundaryTest(unittest.TestCase):
    def test_conversation_tool_run_can_execute_formal_pricer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = formal_fixture.FormalToolAndResultStoreTest()
            pricing_input, data_store, result_store = fixture._formal_fixture(temporary)
            caller, context = modulehost_v2_scope(
                pricing_input.contract,
                request_policy=("conversation.tool.run",),
            )
            request = {
                "contract": pricing_input.contract,
                "pricing_config": pricing_input.pricing_config,
                "market_data_refs": pricing_input.market_data_refs,
            }
            with (
                patch.object(pricer_service, "LocalDataStore", return_value=data_store),
                patch.object(pricer_service, "RESULT_ROOT", Path(temporary) / "result"),
            ):
                output = tool_entry.call_tool(
                    "pricer",
                    request,
                caller_context=caller,
                host_context=context,
                result_store=result_store,
                data_store=data_store,
            )

        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["module"], "pricer")

    def test_conversation_tool_run_cannot_call_catalog(self) -> None:
        caller, context = modulehost_v2_scope(request_policy=("conversation.tool.run",))

        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.catalog"):
            tool_entry.call_tool(
                "pricer",
                {"action": "catalog"},
                caller_context=caller,
                host_context=context,
            )

    def test_caller_and_host_execution_permissions_must_match(self) -> None:
        caller, context = modulehost_v2_scope(request_policy=("conversation.tool.run",))
        desk_only_caller = replace(caller, capabilities=("module.run",))

        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "匹配"):
            tool_entry.call_tool(
                "pricer",
                {"action": "run"},
                caller_context=desk_only_caller,
                host_context=context,
            )


if __name__ == "__main__":
    unittest.main()
