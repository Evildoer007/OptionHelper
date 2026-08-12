"""Latest public Report outline: seven continuous chapters, no research-logic chapter."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "modules" / "designer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer import render
from modules.designer.models import DesignerInput


REPORT_TITLES = (
    "核心结论",
    "结构推荐",
    "合同参数",
    "收益结构",
    "估值定价",
    "历史回测",
    "风险提示",
)


class SevenSectionReportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8")
        )

    def test_report_has_exactly_seven_fixed_sections_without_toc_or_research_logic(self) -> None:
        payload = dict(self.payload)
        payload["sections"] = ["research_logic", "risk", "custom"]
        payload["recommendation"] = {
            **dict(self.payload.get("recommendation", {})),
            "reason": "冻结的推荐理由应归入结构推荐。",
        }

        html = render(DesignerInput(payload=payload, output_type="report"))["html"]
        titles = tuple(re.findall(r'<span class="section-title">([^<]+)</span>', html))

        self.assertEqual(titles, REPORT_TITLES)
        self.assertNotIn('id="section-research_logic"', html)
        self.assertNotIn('<span class="section-title">研究逻辑</span>', html)
        self.assertNotIn('class="report-toc"', html)
        recommendation = html.split('id="section-recommendation"', 1)[1].split("</section>", 1)[0]
        self.assertIn("冻结的推荐理由应归入结构推荐。", recommendation)

    def test_card_remains_without_payoff_figure(self) -> None:
        html = render(DesignerInput(payload=self.payload, output_type="card"))["html"]
        self.assertNotIn('class="chart', html)
        self.assertNotIn("收益结构", html)


if __name__ == "__main__":
    unittest.main()
