"""Reader-facing geometry, ordering and public notation contracts."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "modules" / "designer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer import render
from modules.designer.components import render_formula
from modules.designer.config import load_designer_config
from modules.designer.design_renderer import CARD_CONTENT_ORDER
from modules.designer.design_tokens import TOKENS
from modules.designer.models import DesignerInput
from modules.designer.renderer import PUBLIC_BRAND, SECTION_ORDER, text


class ReportPresentationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads((ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json").read_text(encoding="utf-8"))

    def test_card_has_one_fixed_reader_order_without_payoff_chart(self) -> None:
        payload = deepcopy(self.payload)
        payload["payoff"] = {
            "status": "ready",
            "formula": "Pi_0 + max(S_T-K,0)",
            "scenarios": [{"title": "上涨", "rule": "收益随标的上涨。"}],
            "svg_path": "artifacts/payoff.svg",
        }
        payload["pricing"] = {
            "status": "ready",
            "metrics": [{"label": "现值", "value": 123456.789, "note": "CNY"}],
            "greeks": [{"label": "Delta", "value": 0.1234567, "unit": "CNY_per_spot"}],
        }
        payload["backtest"] = {
            "status": "ready",
            "window": "2024-01-01至2025-01-01",
            "metrics": [
                {"label": "样本数", "value": 120},
                {"label": "胜率", "value": "60%"},
                {"label": "最大亏损", "value": -12345.6789, "note": "CNY"},
            ],
        }
        payload["risk"] = {"items": ["标的价格下跌可能造成损失。", "波动率变化会影响估值。", "不应进入Card。"]}
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]

        positions = [html.index(label) for label in ("推荐结构", "估值定价", "历史回测", "风险提示")]
        self.assertEqual(CARD_CONTENT_ORDER, ("recommendation", "pricing", "backtest", "risk"))
        self.assertEqual(positions, sorted(positions))
        self.assertIn("<h1>场外衍生品结构研究报告</h1>", html)
        self.assertIn(f'<p class="designer-card__brand">{PUBLIC_BRAND}</p>', html)
        self.assertNotIn("收益结构", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('class="chart', html)
        self.assertIn("123,456.79", html)
        self.assertIn("-12,345.68", html)
        self.assertNotIn("123456.789", html)
        theme = load_designer_config().read_report_theme()
        self.assertIn("border-bottom: var(--border-accent) solid var(--red-deep);", theme)
        self.assertIn("border-top: var(--border-accent) solid var(--red-deep);", theme)
        self.assertIn(".report-section#section-risk h2::after { background: var(--red-deep); }", theme)
        self.assertIn("background: var(--gold-pale);", theme)
        self.assertIn("var(--color-gold-border)", theme)
        conclusion_block = theme.split(".conclusion-band {", 1)[1].split("}", 1)[0]
        self.assertIn("border-left: 1px solid var(--gold);", conclusion_block)
        self.assertNotIn("red-pale", conclusion_block)
        self.assertNotIn("red-surface", conclusion_block)

    def test_detailed_report_has_fixed_section_order_and_a4_reader_geometry(self) -> None:
        payload = deepcopy(self.payload)
        payload["meta"]["layout"] = "report"
        payload["sections"] = list(SECTION_ORDER)
        html = render(DesignerInput(payload=payload, output_type="report", html_report_layout="continuous"))["html"]
        self.assertIn("<h1>场外衍生品结构研究报告</h1>", html)
        self.assertIn(f'<p class="report-head__brand">{PUBLIC_BRAND}</p>', html)
        positions = [html.index(f'id="section-{name}"') for name in SECTION_ORDER]
        self.assertEqual(positions, sorted(positions))
        titles = [
            "核心结论",
            "结构推荐",
            "合同参数",
            "收益结构",
            "估值定价",
            "历史回测",
            "风险提示",
        ]
        self.assertEqual([html.index(f'<span class="section-title">{title}</span>') for title in titles], sorted(
            html.index(f'<span class="section-title">{title}</span>') for title in titles
        ))
        self.assertNotIn('<aside class="report-toc"', html)
        theme = load_designer_config().read_report_theme()
        self.assertIn("width: min(210mm, calc(100% - 48px));", theme)
        self.assertIn("@page card", theme)
        self.assertIn("size: A4;", theme)
        self.assertIn(".payoff-figure img", theme)
        self.assertIn("object-fit: contain;", theme)

    def test_report_uses_direct_section_titles_without_ordinal_markers(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = ["recommendation", "payoff"]
        report = render(DesignerInput(payload=payload, output_type="report", html_report_layout="continuous"))["html"]
        theme = load_designer_config().read_report_theme()

        self.assertIn('<span class="section-title">结构推荐</span>', report)
        self.assertNotIn('id="section-research_logic"', report)
        self.assertEqual(
            [report.index(f'id="section-{key}"') for key in SECTION_ORDER],
            sorted(report.index(f'id="section-{key}"') for key in SECTION_ORDER),
        )
        self.assertNotIn('<aside class="report-toc"', report)
        self.assertNotIn('class="section-index"', report)
        self.assertNotIn('class="toc__index"', report)
        self.assertIn(".report-section h2::before", theme)

    def test_report_ignores_legacy_section_title_overrides(self) -> None:
        payload = deepcopy(self.payload)
        payload["sections"] = list(SECTION_ORDER)
        payload["meta"]["section_titles"] = {
            "conclusion": "结论",
            "recommendation": "对象和条款",
            "parameters": "附录",
            "payoff": "收益结构",
            "pricing": "估值与敏感性",
            "backtest": "回测",
            "risk": "风险",
        }
        payload["conclusion"] = {
            "structure_name": "跨式",
            "underlyings": "000300.SH",
            "reasons": ["已确认市场判断对应较高波动率预期。"],
            "valuation_summary": [{"label": "现值", "value": 125.6, "note": "人民币"}],
            "greeks_summary": [], "backtest_summary": [], "risk_summary": [],
        }
        payload["recommendation"] = {
            "headline": "推荐结构与关键条款",
            "structure_name": "跨式",
            "reason": "已确认市场判断对应较高波动率预期。",
        }
        payload["pricing"] = {
            "status": "ready",
            "metrics": [{"label": "现值", "value": "125.6", "note": "CNY"}],
        }

        report = render(DesignerInput(payload=payload, output_type="report", html_report_layout="continuous"))["html"]

        for title in (
            "核心结论",
            "结构推荐",
            "合同参数",
            "收益结构",
            "估值定价",
            "历史回测",
            "风险提示",
        ):
            self.assertIn(f'<span class="section-title">{title}</span>', report)
        for legacy_title in (
            "结论",
            "执行摘要",
            "市场观点与适用范围",
            "推荐结构与关键条款",
            "合同参数与估值假设",
            "收益机制与情景",
            "估值与敏感性",
            "风险与限制",
            "对象和条款",
            "附录",
            "回测",
            "风险",
        ):
            self.assertNotIn(f'<span class="section-title">{legacy_title}</span>', report)
        self.assertNotIn("<h3>结论</h3>", report)
        self.assertNotIn("<h3>推荐结构与关键条款</h3>", report)
        self.assertIn("推荐跨式，挂钩000300.SH。", report)
        self.assertIn("125.6", report)

    def test_continuous_report_uses_a_compact_reading_hierarchy(self) -> None:
        scale = TOKENS.type_scale
        values = {name: float(str(scale[name]).removesuffix("px")) for name in (
            "report_title_brief", "section_continuous", "subsection_brief", "report_body", "table", "meta",
        )}

        self.assertGreater(values["report_title_brief"], values["section_continuous"])
        self.assertGreater(values["section_continuous"], values["subsection_brief"])
        self.assertGreater(values["subsection_brief"], values["report_body"])
        self.assertGreater(values["report_body"], values["table"])
        self.assertGreater(values["table"], values["meta"])
        self.assertEqual(str(scale["report_title_brief"]), "23px")
        self.assertEqual(str(scale["report_body"]), "12.5px")

    def test_formula_and_numbers_never_use_source_like_underscores_or_exponent_notation(self) -> None:
        formula = render_formula(formula="Pi_0 + max(S_T-K,0)")
        self.assertIn("<msub><mi>Π</mi><mn>0</mn></msub>", formula)
        self.assertIn("<msub><mi>S</mi><mi>T</mi></msub>", formula)
        self.assertNotIn("Pi_0", formula)
        self.assertNotIn("S_T", formula)
        self.assertEqual(text(1.2e-7), "1.2×10⁻⁷")
        self.assertEqual(text(123456.789), "123,456.79")
        self.assertEqual(text("CNY_per_1pct_vol"), "人民币/波动率变化1个百分点")


if __name__ == "__main__":
    unittest.main()
