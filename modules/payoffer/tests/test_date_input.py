"""Payoffer日期输入显示与ISO协议转换回归。"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAGE_DIR = PROJECT_ROOT / "modules" / "payoffer" / "page"


class PayofferDateInputTests(unittest.TestCase):
    def test_date_parser_accepts_display_and_legacy_iso_values(self) -> None:
        script = """
const date = require(process.argv[1]);
const result = { slash: date.parseDateInput('2026/07/31'), iso: date.parseDateInput('2026-07-31'), display: date.formatDateInput('2026-07-31') };
for (const value of ['2026/02/29', '2026/7/31', '2026.07.31']) {
  try { date.parseDateInput(value); result[value] = 'accepted'; }
  catch (error) { result[value] = error.message; }
}
console.log(JSON.stringify(result));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(PAGE_DIR / "date_input.js")],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["slash"], "2026-07-31")
        self.assertEqual(result["iso"], "2026-07-31")
        self.assertEqual(result["display"], "2026/07/31")
        self.assertIn("有效日历日期", result["2026/02/29"])
        self.assertIn("yyyy/mm/dd", result["2026/7/31"])
        self.assertIn("yyyy/mm/dd", result["2026.07.31"])

    def test_page_uses_the_single_text_date_boundary(self) -> None:
        html = (PAGE_DIR / "payoffer.html").read_text(encoding="utf-8")
        self.assertIn('src="./date_input.js"', html)
        self.assertIn('id="contractStartDate" class="date-input" type="text"', html)
        self.assertIn('placeholder="yyyy/mm/dd"', html)
        self.assertNotIn('id="contractStartDate" type="date"', html)
        self.assertIn("parseDateInput($('contractStartDate').value)", html)
        self.assertIn("formatDateInput(before.contract_start_date)", html)


if __name__ == "__main__":
    unittest.main()
