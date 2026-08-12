"""Backtester页面合同现金流毛口径的独立回归。"""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EconomicPresentationBoundaryTest(unittest.TestCase):
    def test_page_labels_contract_cashflow_gross_results_and_win_rate_denominator(self) -> None:
        """页面不能把合同现金流毛口径显示为客户净收益。"""

        page = (PROJECT_ROOT / "modules" / "backtester" / "page" / "backtester.html").read_text(encoding="utf-8")

        self.assertIn("合同现金流毛收益率", page)
        self.assertIn("胜率分母：有效结算样本", page)
        self.assertIn("客户净损益未建模", page)


if __name__ == "__main__":
    unittest.main()
