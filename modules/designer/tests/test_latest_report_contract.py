"""Latest immutable public Report outline and navigation contract."""

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


class LatestReportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json").read_text(encoding="utf-8")
        )

    def test_report_has_exactly_seven_fixed_sections_without_toc(self) -> None:
        html = render(DesignerInput(payload=self.payload, output_type="report"))["html"]
        titles = tuple(re.findall(r'<span class="section-title">([^<]+)</span>', html))

        self.assertEqual(titles, REPORT_TITLES)
        self.assertNotIn('class="report-toc"', html)
        self.assertNotIn("报告目录", html)

    def test_recommendation_uses_frozen_reason_as_a_frozen_fact(self) -> None:
        payload = dict(self.payload)
        payload["recommendation"] = {
            **dict(self.payload.get("recommendation", {})),
            "reason": "上涨观点与期限约束共同支持该结构。",
        }
        html = render(DesignerInput(payload=payload, output_type="report"))["html"]

        recommendation_start = html.index('id="section-recommendation"')
        recommendation_end = html.index("</section>", recommendation_start)
        self.assertIn("上涨观点与期限约束共同支持该结构。", html[recommendation_start:recommendation_end])
        self.assertNotIn('id="section-research_logic"', html)


if __name__ == "__main__":
    unittest.main()
