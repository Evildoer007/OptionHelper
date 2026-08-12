"""Lock the single continuous Report outline and independent Card surface."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import build_design_system, render
from modules.designer.config import load_designer_config
from modules.designer.models import DesignerInput
from modules.designer.renderer import SECTION_ORDER, SECTION_TITLES


FIXED_TITLES = (
    "核心结论",
    "结构推荐",
    "合同参数",
    "收益结构",
    "估值定价",
    "历史回测",
    "风险提示",
)


class FixedReportSectionsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = ROOT / "modules/designer/tests/fixtures/report-payload.example.json"
        cls.payload = json.loads(fixture.read_text(encoding="utf-8"))

    def test_report_ignores_every_input_attempt_to_change_the_outline(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["risk", "payoff", "custom", "research"]
        payload["meta"]["section_titles"] = {
            key: f"自定义标题{position}"
            for position, key in enumerate(reversed(SECTION_ORDER), start=1)
        }

        html = render(DesignerInput(payload=payload, output_type="report"))["html"]
        rendered_ids = re.findall(r'<section id="section-([^"]+)" class="report-section"', html)
        rendered_titles = re.findall(r'<span class="section-title">([^<]+)</span>', html)

        self.assertEqual(tuple(rendered_ids), SECTION_ORDER)
        self.assertEqual(tuple(rendered_titles), FIXED_TITLES)
        self.assertEqual(SECTION_TITLES, dict(zip(SECTION_ORDER, FIXED_TITLES, strict=True)))
        for title in FIXED_TITLES:
            self.assertEqual(rendered_titles.count(title), 1)
        self.assertNotIn("自定义标题", html)
        self.assertNotIn('class="report-toc"', html)

    def test_report_keeps_all_seven_sections_when_every_module_is_empty(self) -> None:
        payload = {"meta": {"layout": "brief", "as_of_date": "2026年8月11日"}, "sections": []}
        html = render(DesignerInput(payload=payload, output_type="report"))["html"]
        self.assertEqual(
            tuple(re.findall(r'<span class="section-title">([^<]+)</span>', html)),
            FIXED_TITLES,
        )

    def test_card_uses_its_own_single_page_structure(self) -> None:
        html = render(DesignerInput(payload=self.payload, output_type="card"))["html"]
        self.assertIn('data-output-type="card"', html)
        self.assertNotIn('class="report-section"', html)
        self.assertNotIn('id="section-', html)
        self.assertNotIn('class="report-toc"', html)
        self.assertNotIn("<img", html)

    def test_runtime_assets_contain_no_legacy_html_navigation(self) -> None:
        config = load_designer_config()
        report_template = config.read_template("report.html")
        structural_theme = config.report_theme_path.read_text(encoding="utf-8")
        tokens = build_design_system().tokens

        self.assertNotIn("report-toc", report_template)
        self.assertNotIn("report-toc", structural_theme)
        self.assertFalse(any(name.startswith("directory_") for name in tokens["colors"]))
        self.assertEqual(set(config.template_root.iterdir()), {config.template_path("card.html"), config.template_path("report.html")})


if __name__ == "__main__":
    unittest.main()
