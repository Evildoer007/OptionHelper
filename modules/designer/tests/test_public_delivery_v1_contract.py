"""Public v1.0 delivery contracts shared with Reporter.

These tests keep the customer-facing surface separate from the internal
artifact receipt.  They exercise only the published Designer Tool behaviour.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import call_tool, render
from modules.designer.models import DesignerInput


class DesignerPublicDeliveryV1Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8")
        )

    def test_continuous_is_the_only_public_html_report_layout(self) -> None:
        for layout in ("directory", "report", "brief", "with_toc"):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                DesignerInput(payload=self.payload, html_report_layout=layout)
        artifact = render(DesignerInput(payload=self.payload, html_report_layout="continuous"))
        self.assertEqual(artifact["layout"], "brief")

    def test_card_is_a_compact_recommendation_brief_without_chart_or_toc(self) -> None:
        payload = deepcopy(self.payload)
        payload["payoff"] = {
            "status": "not_run",
            "note": "尚未完成收益结构分析。",
            "scenarios": [],
        }
        payload["recommendation"] = {
            "headline": "优先确认结构边界",
            "reason": "关键条款尚待确认，不宜直接形成正式结论。",
            "terms": [{"label": "执行价", "value": "待确认"}],
        }
        payload["risk"] = {"items": ["标的价格下行时可能发生损失。"]}

        artifact = render(DesignerInput(payload=payload, output_type="card"))
        html = artifact["html"]
        for expected in ("推荐结构", "估值定价", "历史回测", "风险提示"):
            self.assertIn(expected, html)
        for excluded in ("收益情景", "下一步", "执行价", "尚未完成收益结构分析"):
            self.assertNotIn(excluded, html)
        self.assertNotIn("<aside class=\"report-toc\"", html)
        self.assertNotIn('class="chart', html)
        self.assertNotIn("echarts", html.lower())

    def test_report_uses_only_report_derived_payoff_figure(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["payoff", "risk"]
        payload["payoff"] = {"status": "ready", "svg_path": "ignored.svg"}
        with tempfile.TemporaryDirectory(prefix="designer-report-figure-") as directory:
            root = Path(directory)
            (root / "report.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" data-report-figure-profile="payoffer-report-figure/v1">'
                '<text x="1" y="12">收益曲线</text></svg>',
                encoding="utf-8",
            )
            artifact = render(DesignerInput(payload=payload, input_dir=root))
            self.assertNotIn("data:image/svg+xml;base64", artifact["html"])

            payload["payoff"]["report_svg_path"] = "report.svg"
            artifact = render(DesignerInput(payload=payload, input_dir=root))
            self.assertIn("data:image/svg+xml;base64", artifact["html"])

    def test_visible_delivery_rejects_internal_execution_language(self) -> None:
        payload = deepcopy(self.payload)
        payload["recommendation"] = {
            "headline": "结构结论",
            "reason": "请查看RunRef和source_id。",
        }
        response = call_tool({"action": "render", "payload": payload})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "invalid_payload")

    def test_pdf_cli_never_injects_an_html_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="designer-pdf-cli-") as directory:
            root = Path(directory)
            source = root / "payload.json"
            output = root / "report.pdf"
            source.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "modules/designer/scripts/render_report.py"),
                    "--input", str(source), "--output", str(output), "--format", "pdf",
                ],
                capture_output=True,
                text=True,
            )
            combined = completed.stdout + completed.stderr
            self.assertNotIn("html_report_layout", combined)
            self.assertEqual(completed.returncode, 0, combined)
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
