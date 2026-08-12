"""Payoffer结构化Tool入口的开发态回归。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class PayofferToolEntryDispatchTest(unittest.TestCase):
    def _catalog_call(self, capability: str) -> dict[str, object]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PROJECT_ROOT / "modules" / "payoffer" / "tests"),
                str(PROJECT_ROOT / "core"),
                str(PROJECT_ROOT / "core" / "src"),
            )
        )
        code = (
            "import json; "
            "from tool_entry import ToolDispatchError, call_tool; "
            "from modules.payoffer.tests.host_v2_fixture import authorized_context; "
            f"caller, host = authorized_context(request_policy=({capability!r},)); "
            "\ntry:\n"
            "    result = call_tool('payoffer', {'action': 'catalog'}, caller_context=caller, host_context=host)\n"
            "except ToolDispatchError as error:\n"
            "    result = {'rejected': True, 'message': str(error)}\n"
            "print(json.dumps(result, ensure_ascii=False))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_development_tool_entry_returns_payoffer_catalog(self) -> None:
        payload = self._catalog_call("module.catalog")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["module"], "payoffer")
        self.assertEqual(len(payload["products"]), 65)

    def test_module_run_cannot_authorize_catalog(self) -> None:
        payload = self._catalog_call("module.run")
        self.assertTrue(payload["rejected"])
        self.assertIn("module.catalog", payload["message"])


if __name__ == "__main__":
    unittest.main()
