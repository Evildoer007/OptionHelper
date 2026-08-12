"""Independent tests for the published Designer contract.

This file supplements, rather than replaces, the historical migration test.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import build_design_system, render
from modules.designer.design_tokens import DESIGN_SYSTEM_VERSION, token_hash
from modules.designer.models import DesignerInput
from modules.designer.service import call_tool, capability


class DesignerContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads((ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8"))

    def test_design_system_is_single_versioned_bundle(self) -> None:
        system = build_design_system()
        self.assertEqual(system.design_system_version, DESIGN_SYSTEM_VERSION)
        self.assertEqual(system.token_hash, token_hash())
        self.assertEqual(system.tokens["modules"], ["DataFetcher", "Payoffer", "Pricer", "Backtester", "Reporter"])
        self.assertEqual(system.tokens["modes"], ["OptChat", "OptDesk"])
        self.assertEqual(system.echarts_theme["color"][0], "#C8102E")

    def test_report_and_card_are_two_views_of_same_payload(self) -> None:
        original = deepcopy(self.payload)
        report = render(DesignerInput(self.payload, output_type="report", html_report_layout="continuous"))
        card = render(DesignerInput(self.payload, output_type="card"))
        self.assertEqual(self.payload, original)
        self.assertIn(f'data-design-system-version="{DESIGN_SYSTEM_VERSION}"', report["html"])
        self.assertIn('data-output-type="card"', card["html"])
        self.assertNotIn("<aside class=\"report-toc\"", card["html"])
        self.assertEqual(report["semantic_fact_hash"], card["semantic_fact_hash"])
        self.assertNotEqual(report["presentation_input_hash"], card["presentation_input_hash"])
        self.assertEqual(len(report["artifact_hash"]), 64)
        self.assertEqual(report["artifact_manifest"]["html_sha256"], sha256(report["html"].encode()).hexdigest())

    def test_formula_is_mathml_and_chart_colors_are_not_payload_override(self) -> None:
        payload = deepcopy(self.payload)
        payload["payoff"] = {
            "status": "ready",
            "formula": "Pi_0 + max(S_T-K, 0)",
            "terms": [],
        }
        payload["research"] = {
            "charts": [{
                "id": "fixed-palette",
                "title": "走势",
                "x": ["1", "2"],
                "series": [{"name": "主序列", "data": [1, 2], "color": "#00ff00"}],
                "x_axis_name": "日期",
                "y_axis_name": "值",
                "source_note": "测试来源",
            }]
        }
        payload["sections"] = ["research", "payoff", "risk"]
        html = render(DesignerInput(payload))["html"]
        self.assertIn("<math display=\"block\">", html)
        self.assertIn("<msub><mi>Π</mi><mn>0</mn></msub>", html)
        self.assertIn("<msub><mi>S</mi><mi>T</mi></msub>", html)
        self.assertNotIn("Pi_0", html)
        self.assertNotIn("S_T", html)
        self.assertNotIn("#00ff00", html)
        self.assertIn("#C8102E", html)

    def test_service_exposes_real_render_capability(self) -> None:
        profile = capability()
        self.assertTrue(profile["ok"])
        self.assertEqual(profile["status"], "available")
        result = call_tool({"action": "status"})
        self.assertTrue(result["ok"])

    def test_format_is_normalized_before_dispatch(self) -> None:
        request = DesignerInput(self.payload, format="HTML")
        result = render(request)
        self.assertEqual(result["format"], "html")

    def test_card_and_pdf_reject_html_only_layouts(self) -> None:
        """连续页只属于HTML详细报告，不能污染Card或PDF语义。"""

        with self.assertRaises(ValueError):
            DesignerInput(self.payload, output_type="card", format="html", html_report_layout="with_toc")
        with self.assertRaises(ValueError):
            DesignerInput(self.payload, output_type="report", format="pdf", html_report_layout="continuous")
        self.assertEqual(
            render(DesignerInput(self.payload, output_type="card", format="html"))["layout"],
            "brief",
        )

    def test_card_is_a_compact_recommendation_brief_without_payoff_scenarios_or_charts(self) -> None:
        payload = deepcopy(self.payload)
        payload["payoff"] = {
            "status": "ready",
            "scenarios": [
                {"title": "上涨情景", "rule": "到期上涨时获得正向参与收益。"},
                {"title": "下跌情景", "rule": "到期下跌时承担约定损失。"},
            ],
            "svg_path": "artifacts/payoff.svg",
            "charts": [{"title": "不应进入Card"}],
        }
        html = render(DesignerInput(payload, output_type="card"))["html"]
        self.assertIn("推荐结构", html)
        self.assertIn("估值定价", html)
        self.assertIn("历史回测", html)
        self.assertNotIn("收益情景", html)
        self.assertNotIn("上涨情景", html)
        self.assertNotIn("下跌情景", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('class="chart', html)
        self.assertNotIn("echarts", html.lower())


if __name__ == "__main__":
    unittest.main()
