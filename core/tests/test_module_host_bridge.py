"""Static protocol checks for the one browser bridge shared by five pages."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js"
PAGES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")


class ModuleHostBridgeScopeTests(unittest.TestCase):
    def test_all_pages_use_one_host_signed_scope_injector(self) -> None:
        bridge = BRIDGE.read_text(encoding="utf-8")
        self.assertIn("hostScope = Object.freeze", bridge)
        self.assertIn("applyHostScope", bridge)
        self.assertIn("normalizeHostedRequest", bridge)
        self.assertIn('hostScope.contract_fingerprint', bridge)
        self.assertIn('delete body.product_id', bridge)
        self.assertIn('delete body.identity', bridge)
        self.assertIn('delete body.term_overrides', bridge)
        self.assertIn('delete body.history_reference', bridge)
        self.assertIn('body[field] = bound', bridge)
        self.assertNotIn('不能覆盖Host上下文', bridge)
        self.assertIn('ResultStore（run_id=', bridge)
        self.assertNotIn("event.data.task_id", bridge)
        for module in PAGES:
            page = ROOT / "modules" / module / "page" / f"{module}.html"
            self.assertIn("../module-host-bridge.js", page.read_text(encoding="utf-8"), page)


if __name__ == "__main__":
    unittest.main()
