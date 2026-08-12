"""Backtester页面日期控件与ISO协议回归。"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAGE = PROJECT_ROOT / "modules" / "backtester" / "page" / "backtester.html"
DATE_SCRIPT = PROJECT_ROOT / "modules" / "backtester" / "page" / "backtester.js"


class BacktesterPageDateTests(unittest.TestCase):
    def test_page_uses_text_date_controls_and_one_shared_date_utility(self) -> None:
        html = PAGE.read_text(encoding="utf-8")
        self.assertRegex(html, r'id="startDate"[^>]*type="text"[^>]*placeholder="yyyy/mm/dd"')
        self.assertRegex(html, r'id="endDate"[^>]*type="text"[^>]*placeholder="yyyy/mm/dd"')
        self.assertNotIn('id="startDate" type="date"', html)
        self.assertIn('<script src="./backtester.js"></script>', html)
        self.assertIn('window.BacktesterDate.normalize', html)

    def test_date_utility_formats_and_validates_before_iso_submission(self) -> None:
        program = """
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const context={window:{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const date=context.window.BacktesterDate;
assert.strictEqual(date.normalize('2024/02/29'),'2024-02-29');
assert.strictEqual(date.normalize('2024-02-29'),'2024-02-29');
assert.strictEqual(date.display('2024-02-29'),'2024/02/29');
assert.strictEqual(date.normalize(''),null);
assert.throws(()=>date.normalize('2024/02/30'),/有效日历日期/);
assert.throws(()=>date.normalize('2024.02.29'),/yyyy/);
"""
        completed = subprocess.run(["node", "-e", program, str(DATE_SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_page_requires_task_bound_data_asset_and_optional_adj_close_hv(self) -> None:
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("DataAssetRef", html)
        self.assertIn("页面不会读取本地CSV路径", html)
        self.assertNotIn('id="historyReference"', html)
        self.assertIn('id="entryHvWindow"', html)
        self.assertIn('id="entryHvBins"', html)
        self.assertIn("合同结算", html)
        self.assertIn("entry_hv_window", html)


if __name__ == "__main__":
    unittest.main()
