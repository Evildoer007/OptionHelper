"""Executable PDF delivery contracts.

PDF is a public Reporter output, not an optional HTML rename.  These tests
exercise both delivery levels with the real runtime converter.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.designer.pdf_renderer import _extract_blocks, _mixed_markup, render_pdf


REPORT_TITLES = (
    "核心结论",
    "结构推荐",
    "合同参数",
    "收益结构",
    "估值定价",
    "历史回测",
    "风险提示",
)


def _page_count(pdf: Path) -> int:
    """Use the platform-independent Poppler probe when it is available."""

    completed = subprocess.run(
        ["pdfinfo", str(pdf)],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError("pdfinfo未返回页数")


def _page_size(pdf: Path) -> str:
    completed = subprocess.run(
        ["pdfinfo", str(pdf)],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("Page size:"):
            return line
    raise AssertionError("pdfinfo未返回页面尺寸")


class PdfDeliveryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json").read_text(encoding="utf-8")
        )

    def _render_pdf(self, *, output_type: str) -> bytes:
        payload = deepcopy(self.payload)
        payload["sections"] = ["research", "recommendation", "payoff", "pricing", "backtest", "risk"]
        artifact = render(DesignerInput(payload=payload, output_type=output_type, format="pdf"))
        self.assertEqual(artifact["mime_type"], "application/pdf")
        self.assertTrue(artifact["pdf"].startswith(b"%PDF-"))
        self.assertEqual(artifact["artifact_manifest"]["format"], "pdf")
        return artifact["pdf"]

    def test_card_is_a_real_nonempty_pdf_with_a_page(self) -> None:
        content = self._render_pdf(output_type="card")
        with tempfile.TemporaryDirectory(prefix="designer-card-pdf-") as directory:
            output = Path(directory) / "card.pdf"
            output.write_bytes(content)
            self.assertGreater(output.stat().st_size, 1024)
            self.assertEqual(_page_count(output), 1)
            self.assertIn("595.276", _page_size(output))
            self.assertIn("841.89", _page_size(output))

    def test_report_is_a_real_nonempty_pdf_with_a_page(self) -> None:
        content = self._render_pdf(output_type="report")
        with tempfile.TemporaryDirectory(prefix="designer-report-pdf-") as directory:
            output = Path(directory) / "report.pdf"
            output.write_bytes(content)
            self.assertGreater(output.stat().st_size, 1024)
            self.assertGreaterEqual(_page_count(output), 1)

    def test_report_pdf_projects_the_same_seven_continuous_sections_as_html(self) -> None:
        """The real PDF converter must receive the governed Report outline.

        ``render(..., format="pdf")`` returns its static HTML projection along
        with the bytes passed to ReportLab.  Inspecting that projection catches
        accidental outline drift before a visually valid but structurally wrong
        PDF is delivered.
        """

        payload = deepcopy(self.payload)
        payload["sections"] = ["research_logic", "risk", "custom"]
        payload["recommendation"] = {
            **dict(self.payload.get("recommendation", {})),
            "reason": "冻结的推荐理由应归入结构推荐。",
        }
        artifact = render(DesignerInput(payload=payload, output_type="report", format="pdf"))
        blocks = _extract_blocks(artifact["html"])
        headings = tuple(str(value) for kind, level, value in blocks if kind == "heading" and level == 2)

        self.assertTrue(artifact["pdf"].startswith(b"%PDF-"))
        self.assertEqual(headings, REPORT_TITLES)
        self.assertNotIn("研究逻辑", headings)
        self.assertIn("冻结的推荐理由应归入结构推荐。", artifact["html"])
        self.assertNotIn("report-toc", artifact["html"])

    def test_report_pdf_reserves_space_before_every_fixed_section_heading(self) -> None:
        html = render(DesignerInput(payload=self.payload, output_type="report"))["html"]
        from reportlab.platypus import CondPageBreak

        with patch("reportlab.platypus.CondPageBreak", wraps=CondPageBreak) as reserve_space:
            content = render_pdf(html)

        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertEqual(reserve_space.call_count, 7)
        self.assertTrue(all(call.args == (72,) for call in reserve_space.call_args_list))

    def test_negative_scientific_exponent_uses_reportlab_super_text(self) -> None:
        markup = _mixed_markup("4.56×10⁻³", latin_font="Arial", cjk_font="CJK")

        self.assertIn('<super><font name="Arial">-3</font></super>', markup)
        self.assertNotIn("⁻", markup)
        self.assertNotIn("³", markup)

    def test_negative_scientific_exponent_is_visibly_distinct_from_positive_pdf(self) -> None:
        converter = shutil.which("pdftoppm")
        if converter is None:
            self.skipTest("当前环境没有pdftoppm，无法执行PDF像素回归。")
        try:
            from PIL import Image, ImageChops
        except ImportError:
            self.skipTest("当前环境没有Pillow，无法执行PDF像素回归。")

        with tempfile.TemporaryDirectory(prefix="designer-pdf-exponent-") as directory:
            root = Path(directory)
            for name, value in (("negative", "4.56×10⁻³"), ("positive", "4.56×10³")):
                pdf = root / f"{name}.pdf"
                png_prefix = root / name
                pdf.write_bytes(render_pdf(f"<html><body><p>{value}</p></body></html>"))
                subprocess.run(
                    [converter, "-png", "-f", "1", "-singlefile", "-r", "216", str(pdf), str(png_prefix)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            negative = Image.open(root / "negative.png").convert("RGB")
            positive = Image.open(root / "positive.png").convert("RGB")
            self.assertIsNotNone(
                ImageChops.difference(negative, positive).getbbox(),
                "负指数PDF渲染不能与正指数完全相同。",
            )

    def test_card_three_line_tables_are_parsed_as_pdf_facts_not_escaped_html(self) -> None:
        payload = deepcopy(self.payload)
        payload["pricing"] = {
            "status": "ready",
            "metrics": [{"label": "现值", "value": 12.34}],
            "greeks": [{"label": "Delta", "value": 0.51}],
        }
        payload["backtest"] = {
            "status": "ready",
            "metrics": [
                {"label": "样本数", "value": 20},
                {"label": "胜率", "value": 0.6, "value_format": "percent"},
                {"label": "平均收益", "value": 0.04, "value_format": "percent"},
                {"label": "最大亏损", "value": -0.1},
            ],
        }
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]
        blocks = _extract_blocks(html)
        tables = [raw for kind, _level, raw in blocks if kind == "table"]

        self.assertEqual(len(tables), 2)
        self.assertIn(["现值", "12.34", "口径未提供"], tables[0])
        self.assertIn(["Delta", "0.51", "口径未提供"], tables[0])
        self.assertIn(["Gamma", "未提供", "口径未提供"], tables[0])
        self.assertNotIn("&lt;font", str(tables))


if __name__ == "__main__":
    unittest.main()
