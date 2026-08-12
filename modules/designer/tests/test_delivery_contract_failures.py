"""Fail-first contracts for Designer delivery quality.

These tests describe public behaviour only.  They intentionally supplement
the existing suite while the implementation is brought into compliance.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import DesignerDependencyError, build_design_system, call_tool, load_designer_config, render
from modules.designer.design_tokens import TOKENS, token_hash
from modules.designer.models import DesignerInput


_DECLARATION = re.compile(r"(?P<name>--[a-z0-9-]+)\s*:\s*(?P<value>[^;]+);", re.IGNORECASE)


def _declarations(css: str) -> list[tuple[str, str]]:
    return [(match.group("name"), match.group("value").strip()) for match in _DECLARATION.finditer(css)]


def _normalise_css_value(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[position : position + 2], 16) / 255 for position in (1, 3, 5)]
        channels = [item / 12.92 if item <= 0.04045 else ((item + 0.055) / 1.055) ** 2.4 for item in channels]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class _LocalReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        attribute = "src" if tag in {"script", "img"} else "href" if tag == "link" else None
        if not attribute or not values.get(attribute):
            return
        reference = str(values[attribute])
        if not reference.startswith(("data:", "http://", "https://", "//", "#")):
            self.references.append(reference.split("?", 1)[0].split("#", 1)[0])


class DesignerDeliveryFailureContractTest(unittest.TestCase):
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
                    "id": "accessible-trend",
                    "title": "标的走势",
                    "type": "line",
                    "x": ["2026-08-01", "2026-08-02"],
                    "series": [{"name": "收盘价", "data": [100, 102]}],
                    "x_axis_name": "日期",
                    "y_axis_name": "点位",
                    "source_note": "测试数据",
                    "accessibility_summary": "收盘价由100升至102。",
                }
            ],
        }
        return payload

    def test_loaded_theme_contains_each_generated_token_exactly_once(self) -> None:
        """The runtime CSS must be generated from, or strictly verified against, TOKENS."""

        system = build_design_system()
        theme = load_designer_config().read_report_theme()
        expected = dict(_declarations(system.css_variables))
        actual_pairs = _declarations(theme)
        actual: dict[str, list[str]] = {}
        for name, value in actual_pairs:
            actual.setdefault(name, []).append(value)

        self.assertIn(f"designer-token-hash:{token_hash()}", theme.replace(" ", ""))
        for name, expected_value in expected.items():
            self.assertIn(name, actual, f"运行时主题缺少Token变量{name}")
            self.assertEqual(len(actual[name]), 1, f"{name}存在重复手写定义")
            self.assertEqual(
                _normalise_css_value(actual[name][0]),
                _normalise_css_value(expected_value),
                f"{name}与唯一Token源不一致",
            )
        structural = load_designer_config().report_theme_path.read_text(encoding="utf-8")
        self.assertNotRegex(structural, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertNotRegex(structural, r"font-size\s*:\s*(?:\d|\.)")
        self.assertNotIn("--fs-", structural)
        self.assertNotIn("__BREAKPOINT_NARROW__", theme)
        self.assertEqual(
            theme.count(f"@media (max-width: {TOKENS.breakpoints['narrow']}px)"),
            2,
        )
        self.assertEqual(
            system.echarts_theme["textStyle"]["fontSize"],
            int(TOKENS.type_scale["meta"].removesuffix("px")),
        )

    def test_chart_pdf_uses_static_accessible_data_without_browser_scripts(self) -> None:
        """A PDF converter receives the chart's factual text/table projection, never JS-only markup."""

        request = DesignerInput(payload=self._chart_payload(), format="pdf")
        with patch("modules.designer.design_renderer._pdf_bytes", return_value=b"%PDF-1.7\n") as converter:
            try:
                artifact = render(request)
            except DesignerDependencyError as error:
                self.assertRegex(str(error), r"图表.*PDF|PDF.*图表|静态")
                self.assertFalse(converter.called)
                return

        pdf_html = artifact["html"]
        self.assertTrue(converter.called, "静态PDF路径没有调用PDF转换器")
        self.assertNotIn("chartSpecs.forEach", pdf_html)
        self.assertIn("收盘价由100升至102", pdf_html)
        self.assertIn("标的走势数据", pdf_html)

    def test_portable_tool_returns_self_contained_verified_asset_bundle(self) -> None:
        response = call_tool(
            {
                "action": "render",
                "payload": self._chart_payload(),
                "asset_mode": "portable",
                "html_report_layout": "continuous",
            }
        )
        self.assertTrue(response["ok"])
        artifact = response["artifact"]
        assets = artifact.get("portable_assets")
        self.assertIsInstance(assets, list)
        self.assertGreater(len(assets), 0)

        with tempfile.TemporaryDirectory(prefix="designer-portable-") as directory:
            destination = Path(directory)
            (destination / "report.html").write_text(artifact["html"], encoding="utf-8")
            asset_paths: set[str] = set()
            for item in assets:
                self.assertIsInstance(item, dict)
                self.assertTrue({"path", "media_type", "sha256", "content_base64"}.issubset(item))
                relative = PurePosixPath(str(item["path"]))
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                raw = base64.b64decode(str(item["content_base64"]), validate=True)
                self.assertEqual(sha256(raw).hexdigest(), item["sha256"])
                output = destination.joinpath(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(raw)
                asset_paths.add(relative.as_posix())

            parser = _LocalReferenceParser()
            parser.feed(artifact["html"])
            self.assertGreater(len(parser.references), 0)
            for reference in parser.references:
                relative = PurePosixPath(reference)
                self.assertNotIn("..", relative.parts)
                self.assertIn(relative.as_posix(), asset_paths)
                self.assertTrue(destination.joinpath(*relative.parts).is_file())

    def test_portable_cli_materializes_the_public_asset_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="designer-cli-portable-") as directory:
            destination = Path(directory)
            output = destination / "report.html"
            chart_input = destination / "chart-payload.json"
            chart_input.write_text(json.dumps(self._chart_payload(), ensure_ascii=False), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "modules/designer/scripts/render_report.py"),
                    "--input",
                    str(chart_input),
                    "--output",
                    str(output),
                    "--asset-mode",
                    "portable",
                    "--html-report-layout",
                    "continuous",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            parser = _LocalReferenceParser()
            parser.feed(output.read_text(encoding="utf-8"))
            self.assertGreater(len(parser.references), 0)
            for reference in parser.references:
                self.assertTrue(destination.joinpath(*PurePosixPath(reference).parts).is_file())

    def test_statuses_have_semantic_tokens_attributes_and_css(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["payoff", "pricing", "backtest", "risk"]
        payload["payoff"] = {"status": "ready", "note": "收益结构已接入。"}
        payload["pricing"] = {"status": "partial", "note": "仅部分估值输入可用。"}
        payload["backtest"] = {"status": "failed", "note": "历史数据校验失败。"}

        html = render(DesignerInput(payload=payload))["html"]
        theme = load_designer_config().read_report_theme()
        expected_colors = {
            "ready": TOKENS.colors["blue_gray"],
            "partial": TOKENS.colors["risk_gold"],
            "failed": TOKENS.colors["brand_red"],
        }
        for status, color in expected_colors.items():
            self.assertIn(status, TOKENS.states)
            self.assertEqual(TOKENS.states[status]["color"], color)
            self.assertIn(f'data-status="{status}"', html)
            self.assertRegex(theme, rf'\[data-status=["\']{status}["\']\][^{{]*\{{')

    def test_card_has_fixed_public_brief_hierarchy_without_module_status(self) -> None:
        payload = deepcopy(self.payload)
        payload["recommendation"] = {
            "headline": "采用保护型结构",
            "reason": "在明确风险边界下保留上行参与。",
            "terms": [],
        }
        payload["payoff"] = {"status": "ready", "note": "已接入。"}
        payload["pricing"] = {"status": "partial", "note": "部分接入。"}
        payload["backtest"] = {"status": "failed", "note": "运行失败。"}
        payload["risk"] = {"items": ["本金可能发生损失。"], "disclaimer": "仅供内部研究。"}

        html = render(DesignerInput(payload=payload, output_type="card"))["html"]
        conclusion = html.rfind('class="card-conclusion"')
        analysis = html.rfind('class="card-analysis-grid"')
        risk = html.rfind('class="card-risk"')
        self.assertGreaterEqual(conclusion, 0)
        self.assertGreater(analysis, conclusion)
        self.assertGreater(risk, analysis)
        public_markup = html[html.index("<body") :]
        for value in ("ready", "partial", "failed"):
            self.assertNotIn(f'data-status="{value}"', public_markup)

        theme = load_designer_config().read_report_theme()
        for selector in (".designer-card .card-conclusion", ".designer-card .card-analysis-grid", ".designer-card .card-risk"):
            self.assertIn(selector, theme)

    def test_mathml_is_not_nested_and_preserves_chinese_and_symbols(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["payoff", "risk"]
        payload["payoff"] = {
            "status": "ready",
            "formula_mathml": (
                "<math><mrow><mi>K</mi><mo>=</mo><mtext>执行价</mtext></mrow></math>"
            ),
            "terms": [],
        }
        html = render(DesignerInput(payload=payload))["html"]
        self.assertEqual(html.count("<math"), 1)
        self.assertIn("<mi>K</mi><mo>=</mo><mtext>执行价</mtext>", html)
        self.assertNotIn("<math display=\"block\"><math", html)

        payload["payoff"]["formula_mathml"] = "<math><math><mi>K</mi></math></math>"
        nested_html = render(DesignerInput(payload=payload))["html"]
        self.assertEqual(nested_html.count("<math"), 1)
        self.assertIn("&lt;math&gt;", nested_html)

    def test_chart_exposes_readable_summary_and_image_semantics(self) -> None:
        html = render(DesignerInput(payload=self._chart_payload()))["html"]
        chart = re.search(r'<div class="chart"[^>]*id="accessible-trend"[^>]*>', html)
        self.assertIsNotNone(chart)
        tag = chart.group(0)
        self.assertIn('role="img"', tag)
        described_by = re.search(r'aria-describedby="([^"]+)"', tag)
        self.assertIsNotNone(described_by)
        summary_id = described_by.group(1)
        self.assertRegex(html, rf'id="{re.escape(summary_id)}"[^>]*class="[^"]*chart-summary[^"]*"')
        self.assertIn("收盘价由100升至102。", html)

    def test_narrow_parameter_table_and_a4_print_contract(self) -> None:
        theme = load_designer_config().read_report_theme()
        parameter_rule = re.search(r"\.parameter-table\s*\{(?P<body>[^}]*)\}", theme, re.DOTALL)
        self.assertIsNotNone(parameter_rule)
        self.assertRegex(parameter_rule.group("body"), r"min-width\s*:\s*0")
        self.assertNotIn("overflow-x: auto", theme)

        self.assertRegex(theme, r"@page\s*\{[^}]*size\s*:\s*A4\s*;", re.DOTALL)
        print_block = theme[theme.index("@media print") :]
        self.assertNotIn(".report-toc", theme)
        self.assertRegex(print_block, r"thead\s*\{[^}]*display\s*:\s*table-header-group")
        self.assertIn("break-inside: avoid", print_block)
        self.assertRegex(print_block, r"\.parameter-table\s*\{[^}]*min-width\s*:\s*0")

    def test_small_text_semantic_colors_meet_wcag_aa(self) -> None:
        self.assertGreaterEqual(
            _contrast_ratio(TOKENS.colors["risk_gold"], TOKENS.colors["risk_gold_soft"]),
            4.5,
        )
        self.assertGreaterEqual(
            _contrast_ratio(TOKENS.colors["muted_soft"], TOKENS.colors["paper"]),
            4.5,
        )


if __name__ == "__main__":
    unittest.main()
