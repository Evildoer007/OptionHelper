"""Quality contract for the seven-section recommendation Report.

The fixture is intentionally public and frozen.  These tests verify only
projection and presentation; they never calculate a payoff, price or backtest.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.config import load_designer_config
from modules.designer.models import DesignerInput
from modules.designer.renderer import SECTION_ORDER, SECTION_TITLES, display_text


FIXED_TITLES = ("核心结论", "结构推荐", "合同参数", "收益结构", "估值定价", "历史回测", "风险提示")


def payload() -> dict:
    base = json.loads(
        (ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json").read_text(encoding="utf-8")
    )
    base["conclusion"] = {
        "structure_name": "熊市看涨价差",
        "underlyings": "000300.SH",
        "reasons": ["适合温和上涨且上行空间有限的判断。", "净权利金支出限定最大损失。"],
        "valuation_summary": [{"label": "现值", "value": -228.456, "note": "人民币"}],
        "greeks_summary": [{"label": "Delta", "value": 0.22456}],
        "backtest_summary": [
            {"label": "样本数", "value": 48},
            {"label": "胜率", "value": 0.34234, "value_format": "percent"},
        ],
        "risk_summary": ["结构收益上限受限。"],
        "next_steps": ["不应显示"],
        "summary": "收益结构、估值定价、历史回测已形成可用证据；应结合已确认条款、适用边界和风险揭示综合判断。",
    }
    base["recommendation"] = {
        "headline": "熊市看涨价差",
        "structure_name": "熊市看涨价差",
        "underlyings": "000300.SH",
        "reason": "匹配先上涨后回落、波动加大的市场判断。",
        "terms": [{"label": "执行价1", "value": 4000}],
        "suitable_for": ["预期标的温和上涨。"],
        "not_suitable_for": ["预期标的大幅上涨。"],
    }
    base["parameters"] = {
        "common_input": [
            {"cn": "执行价1", "symbol": "K_1", "value": 4050.126, "source": "user_selection"},
            {"cn": "合同约束", "symbol": "", "value": "K_1 < K_2", "source": "template_default"},
        ],
        "payoff_input": [{"cn": "观察设置", "symbol": "", "value": "到期观察", "source": "template_default"}],
        "pricing_input": [{"cn": "估值方法", "symbol": "", "value": "Black-Scholes", "source": "template_default"}],
        "backtest_input": [{"cn": "入场规则", "symbol": "", "value": "每月", "source": "user_selection"}],
    }
    base["payoff"] = {
        "status": "ready",
        "formula": "max(S_T-K_1,0)-max(S_T-K_2,0)-P_net",
        "scenarios": [{"title": "低于执行价1", "rule": "S_T <= K_1时损失P_net。"}],
    }
    base["pricing"] = {
        "status": "ready",
        "metrics": [{"label": "现值", "value": -228.456}],
        "greeks": [{"label": "Gamma", "value": 3.658e-5}],
        "charts": [{
            "id": "delta-risk",
            "title": "Delta风险曲线",
            "type": "line",
            "x": [4197.4098300000005, 4300.0],
            "series": [{"name": "Delta", "data": [0.14996392521746757, 0.22]}],
            "x_axis_name": "标的价格",
            "y_axis_name": "Delta",
            "source_note": "本次估值结果",
        }],
    }
    base["backtest"] = {"status": "ready", "metrics": [], "charts": []}
    base["research"] = {"headline": "不应显示", "thesis": "不应显示"}
    base["sections"] = ["research", "risk"]
    return base


class RecommendationReportNineFixesTests(unittest.TestCase):
    def test_report_has_fixed_seven_sections_and_keeps_frozen_reason_in_recommendation(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="report", html_report_layout="continuous"))["html"]
        rendered_titles = re.findall(r'<span class="section-title">([^<]+)</span>', html)

        self.assertEqual(SECTION_ORDER, ("conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"))
        self.assertEqual(tuple(SECTION_TITLES[key] for key in SECTION_ORDER), FIXED_TITLES)
        self.assertEqual(tuple(rendered_titles), FIXED_TITLES)
        recommendation = html.split('id="section-recommendation"', 1)[1].split("</section>", 1)[0]
        self.assertIn("匹配先上涨后回落、波动加大的市场判断。", recommendation)
        self.assertNotIn('<span class="section-title">研究逻辑</span>', html)
        self.assertNotIn("下一步", html)
        self.assertNotIn("已形成可用证据", html)
        self.assertNotIn('class="report-toc"', html)

    def test_conclusion_is_structured_and_recommendation_does_not_repeat_terms(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="report", html_report_layout="continuous"))["html"]
        conclusion = html.split('id="section-conclusion"', 1)[1].split('</section>', 1)[0]
        recommendation = html.split('id="section-recommendation"', 1)[1].split('</section>', 1)[0]

        for label in ("推荐结论", "推荐理由", "估值摘要", "回测摘要", "风险边界"):
            self.assertIn(label, conclusion)
        self.assertIn("熊市看涨价差", conclusion)
        self.assertIn("适合温和上涨", conclusion)
        self.assertIn("-228.46", conclusion)
        self.assertIn("34.23%", conclusion)
        self.assertNotIn('class="terms-grid"', recommendation)
        self.assertNotIn("4050.13", recommendation)

    def test_contract_terms_use_one_table_mathml_and_module_inputs_move_to_modules(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="report", html_report_layout="continuous"))["html"]
        parameters = html.split('id="section-parameters"', 1)[1].split('</section>', 1)[0]
        pricing = html.split('id="section-pricing"', 1)[1].split('</section>', 1)[0]
        backtest = html.split('id="section-backtest"', 1)[1].split('</section>', 1)[0]

        self.assertEqual(parameters.count("<table"), 1)
        self.assertIn("执行价1", parameters)
        self.assertIn("<msub><mi>K</mi><mn>1</mn></msub>", parameters)
        self.assertRegex(parameters, r'<td class="symbol">\s*-\s*</td>')
        self.assertIn("<math", parameters)
        self.assertNotIn("K_1", parameters)
        self.assertNotIn("估值方法", parameters)
        self.assertNotIn("入场规则", parameters)
        self.assertIn("估值参数", pricing)
        self.assertIn("估值方法", pricing)
        self.assertIn("回测参数", backtest)
        self.assertIn("入场规则", backtest)

    def test_numbers_and_formula_text_use_public_notation(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="report", html_report_layout="continuous"))["html"]

        self.assertEqual(display_text(34.234), "34.23")
        self.assertEqual(display_text(0.3, "percent"), "30%")
        self.assertEqual(display_text(3.658e-5), "3.66×10⁻⁵")
        self.assertIn("4,197.41", html)
        self.assertIn("0.15", html)
        visible_html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        self.assertNotIn("4197.4098300000005", visible_html)
        self.assertNotIn("K1", visible_html)
        self.assertNotIn("K2", visible_html)
        for raw_formula in ("S_T", "K_1", "K_2", "P_net"):
            self.assertNotIn(raw_formula, html)
        self.assertGreaterEqual(html.count("<math"), 3)

    def test_chart_script_is_valid_and_theme_is_red_three_line(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="report", html_report_layout="continuous"))["html"]
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory(prefix="designer-js-") as directory:
            script_path = Path(directory) / "report-inline.js"
            script_path.write_text(scripts[-1], encoding="utf-8")
            completed = subprocess.run(["node", "--check", str(script_path)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("chart-error", html)

        theme = load_designer_config().read_report_theme()
        table_rule = theme.split("table {", 1)[1].split("}", 1)[0]
        head_rule = theme.split("thead {", 1)[1].split("}", 1)[0]
        self.assertIn("border-top: 2px solid var(--red);", table_rule)
        self.assertIn("border-bottom: 2px solid var(--red);", table_rule)
        self.assertIn("border-bottom: 1px solid var(--red);", head_rule)
        self.assertNotIn("background:", head_rule)


if __name__ == "__main__":
    unittest.main()
