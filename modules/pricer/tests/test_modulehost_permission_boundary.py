"""Pricer catalog与run权限不可互相替代。"""

from __future__ import annotations

import unittest

from core import tool_entry
from .pricer_test_fixtures import modulehost_v2_scope


class ModuleHostPermissionBoundaryTest(unittest.TestCase):
    def test_module_run_cannot_call_catalog(self) -> None:
        caller, context = modulehost_v2_scope(request_policy=("module.run",))

        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.catalog"):
            tool_entry.call_tool(
                "pricer",
                {"action": "catalog"},
                caller_context=caller,
                host_context=context,
            )


if __name__ == "__main__":
    unittest.main()
