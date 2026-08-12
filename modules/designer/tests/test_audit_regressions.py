"""Regression contracts from the independent Designer security and UX audit."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import call_tool, load_designer_config, render
from modules.designer.components import render_formula
from modules.designer.models import DesignerInput


class DesignerAuditRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8")
        )

    def _chart_payload(self) -> dict[str, object]:
        payload = deepcopy(self.payload)
        payload["pricing"] = {
            "status": "ready",
            "metrics": [],
            "greeks": [],
            "charts": [
                {
                    "title": "标的走势",
                    "type": "line",
                    "x": ["T0", "T1"],
                    "series": [
                        {"name": "标的", "data": [100, 102]},
                        {"name": "比较", "data": [100, 99]},
                    ],
                    "x_axis_name": "日期",
                    "y_axis_name": "点位",
                    "source_note": "回归样例",
                }
            ],
        }
        return payload

    def test_public_tool_rejects_caller_controlled_echarts_paths(self) -> None:
        for value in ("/etc/hosts", "https://example.invalid/echarts.js"):
            response = call_tool(
                {
                    "action": "render",
                    "payload": self.payload,
                    "asset_mode": "portable",
                    "echarts_path": value,
                }
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "invalid_request")

    def test_public_tool_returns_structured_payload_errors(self) -> None:
        payload = deepcopy(self.payload)
        payload["pricing"] = {"status": "unknown"}
        response = call_tool({"action": "render", "payload": payload})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "invalid_payload")

    def test_absolute_svg_path_is_not_embedded(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["payoff", "risk"]
        payload["payoff"] = {"status": "ready", "svg_path": "/private/tmp/not-a-public-asset.svg"}
        html = render(DesignerInput(payload=payload))["html"]
        self.assertNotIn("data:image/svg+xml;base64", html)
        self.assertIn("未提供本次参数化收益图", html)

    def test_no_chart_report_has_no_echarts_dependency(self) -> None:
        artifact = render(DesignerInput(payload=self.payload, asset_mode="portable"))
        self.assertNotIn("<script src=", artifact["html"])
        self.assertEqual(artifact["portable_assets"], [])
        self.assertEqual(artifact["artifact_manifest"]["assets"], [])

    def test_chart_report_has_accessible_data_and_non_colour_line_styles(self) -> None:
        artifact = render(DesignerInput(payload=self._chart_payload(), asset_mode="portable"))
        html = artifact["html"]
        self.assertIn('class="chart-data"', html)
        self.assertIn("T0", html)
        self.assertIn("lineTypes", html)
        self.assertIn("symbols", html)
        self.assertEqual(len(artifact["portable_assets"]), 1)
        self.assertEqual(len(artifact["artifact_manifest"]["assets"]), 1)

        shared = render(DesignerInput(payload=self._chart_payload(), asset_mode="shared"))
        shared_asset = shared["artifact_manifest"]["assets"][0]
        self.assertEqual(shared_asset["asset_id"], "echarts")
        self.assertEqual(len(shared_asset["sha256"]), 64)
        self.assertNotIn("content_base64", shared_asset)

    def test_pdf_keeps_fixed_report_chart_facts_without_browser_scripts(self) -> None:
        payload = self._chart_payload()
        payload["sections"] = ["risk"]
        with patch("modules.designer.design_renderer._pdf_bytes", return_value=b"%PDF-1.7\n") as pdf_renderer:
            artifact = render(DesignerInput(payload=payload, format="pdf"))
        self.assertEqual(artifact["mime_type"], "application/pdf")
        pdf_projection = pdf_renderer.call_args.args[0]
        self.assertIn("标的走势", pdf_projection)
        self.assertNotIn("<script", pdf_projection)

    def test_card_preserves_ready_module_metrics(self) -> None:
        payload = deepcopy(self.payload)
        payload["pricing"] = {
            "status": "ready",
            "metrics": [{"label": "估值", "value": "12.34", "note": "测试口径"}],
        }
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]
        self.assertIn("12.34", html)
        self.assertIn("测试口径", html)
        self.assertIn("<main", html)

    def test_mobile_landmarks_and_unclipped_table_contract(self) -> None:
        payload = deepcopy(self.payload)
        row = {
            "cn": "执行价",
            "en": "Strike",
            "symbol": "K",
            "value": "100%",
            "source": "user_selection",
        }
        payload["parameters"] = {
            "payoff_input": [row],
            "pricing_input": [row],
            "backtest_input": [row],
        }
        html = render(DesignerInput(payload=payload))["html"]
        theme = load_designer_config().read_report_theme()
        self.assertIn('class="skip-link" href="#report-main"', html)
        self.assertIn('<main id="report-main"', html)
        self.assertIn('class="table-wrap table-wrap--parameters"', html)
        self.assertNotIn("可横向滚动", html)
        self.assertIn("body.report-profile-brief .report-document", theme[theme.index("@media (max-width:") :])
        self.assertIn(".skip-link:focus", theme)
        self.assertNotIn("overflow-x: auto", theme)
        self.assertNotIn(".report-toc", theme)

    def test_mathml_core_root_and_matrix_are_preserved(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["payoff", "risk"]
        payload["payoff"] = {
            "status": "ready",
            "formula_mathml": (
                "<math><mrow><msqrt><mi>K</mi></msqrt><mo>+</mo>"
                "<mtable><mtr><mtd><mn>1</mn></mtd></mtr></mtable></mrow></math>"
            ),
        }
        html = render(DesignerInput(payload=payload))["html"]
        self.assertIn("<msqrt><mi>K</mi></msqrt>", html)
        self.assertIn("<mtable><mtr><mtd><mn>1</mn></mtd></mtr></mtable>", html)

        core = render_formula(
            formula_mathml=(
                "<math><mrow><msubsup><mi>S</mi><mn>0</mn><mi>T</mi></msubsup>"
                "<mspace width=\"thinmathspace\"/><mo>+</mo><mi>K</mi></mrow></math>"
            )
        )
        self.assertIn("<msubsup><mi>S</mi><mn>0</mn><mi>T</mi></msubsup>", core)
        self.assertIn("<mspace/>", core)

        rejected = render_formula(formula_mathml="<math><script>alert(1)</script></math>")
        self.assertIn("&lt;math&gt;", rejected)
        self.assertNotIn("<script>", rejected)


if __name__ == "__main__":
    unittest.main()
