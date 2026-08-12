"""Reader-facing Card and Report data presentation contract.

This suite intentionally uses frozen public payloads only.  It verifies that
Designer formats and presents facts without calculating or completing them.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.models import DesignerInput


def _payload() -> dict:
    return {
        "meta": {"title": "示例结构推荐报告", "as_of_date": "2026-08-11"},
        "recommendation": {
            "structure_name": "经典型雪球",
            "underlyings": "中证500指数",
            "reason": "在已确认的市场判断与风险承受范围内进行结构筛选。",
        },
        "pricing": {
            "status": "ready",
            "method": "蒙特卡洛模拟",
            "valuation_date": "2026-08-11",
            "metrics": [
                {"label": "现值", "value": 123456.789},
                {"label": "现值占比", "value": 0.34234, "value_format": "percent"},
                {"label": "票息", "value": 0.3, "value_format": "percent"},
                {"label": "标准误", "value": 12.3456},
                {"label": "额外指标", "value": 9.8765},
            ],
            "greeks": [
                {"label": "Vega", "value": 8.7654, "unit": "人民币/波动率变化1个百分点"},
                {"label": "Delta", "value": 0.1234, "unit": "人民币/标的价格点"},
                {"label": "Rho", "value": None, "status": "not_applicable"},
                {"label": "Gamma", "value": 0.00456},
                {"label": "Theta", "value": -1.2345},
            ],
            "charts": [
                {
                    "id": "delta-risk",
                    "title": "Delta风险曲线",
                    "type": "line",
                    "x": [90, 100, 110],
                    "series": [{"name": "Delta", "data": [0.1, 0.2, 0.3]}],
                    "x_axis_name": "标的价格",
                    "y_axis_name": "Delta",
                    "source_note": "本次估值结果",
                },
                {
                    "id": "delta-surface",
                    "title": "Delta曲面",
                    "type": "heatmap",
                    "x": [90, 100],
                    "y": [30, 60],
                    "data": [[0, 0, 0.12], [1, 0, 0.18], [0, 1, 0.09], [1, 1, 0.15]],
                    "x_axis_name": "标的价格",
                    "y_axis_name": "剩余期限",
                    "z_axis_name": "Delta",
                    "source_note": "本次估值结果",
                },
            ],
        },
        "backtest": {
            "status": "ready",
            "window": "2024-01-01至2025-01-01",
            "entry_rule": "每月首个交易日",
            "metrics": [
                {"label": "样本数", "value": 120},
                {"label": "胜率", "value": 0.6, "value_format": "percent"},
                {"label": "平均收益", "value": 0.03423, "value_format": "percent"},
                {"label": "最大亏损", "value": 1234.567},
            ],
            "card_metrics": [
                {"label": "敲出比例", "value": 0.42, "value_format": "percent"},
                {"label": "敲入比例", "value": 0.18, "value_format": "percent"},
                {"label": "平均敲出日", "value": 103.456},
                {"label": "未敲出平均收益", "value": -0.01234, "value_format": "percent"},
            ],
            "detail_tables": [
                {
                    "title": "年度表现",
                    "columns": [("year", "年份"), ("average_return", "平均收益")],
                    "rows": [{"year": "2025", "average_return": 0.12345, "average_return_format": "percent"}],
                }
            ],
            "charts": [{
                "id": "return-distribution",
                "title": "收益率分布",
                "type": "bar",
                "x": ["负收益", "正收益"],
                "series": [{"name": "样本数", "data": [48, 72]}],
                "x_axis_name": "收益区间",
                "y_axis_name": "样本数",
                "source_note": "本次历史回测结果",
            }],
        },
        "risk": {"items": ["标的价格不利变动可能导致损失。", "波动率变化会影响估值。", "第三条不进入Card。"]},
    }


class CardReportDataPresentationTests(unittest.TestCase):
    def test_report_formats_values_and_renders_full_pricing_and_backtest_detail(self) -> None:
        html = render(DesignerInput(payload=_payload(), output_type="report", html_report_layout="continuous"))["html"]

        self.assertIn("123,456.79", html)
        self.assertIn("34.23%", html)
        self.assertIn("30%", html)
        self.assertNotIn("123456.789", html)
        self.assertNotIn("34.234%", html)
        self.assertNotIn("30.00%", html)
        self.assertLess(html.index("Delta</td>"), html.index("Gamma</td>"))
        self.assertLess(html.index("Gamma</td>"), html.index("Vega</td>"))
        self.assertLess(html.index("Vega</td>"), html.index("Theta</td>"))
        self.assertLess(html.index("Theta</td>"), html.index("Rho</td>"))
        self.assertIn("不适用", html)
        self.assertIn("Delta风险曲线", html)
        self.assertIn("Delta曲面", html)
        self.assertIn('"type": "heatmap"', html)
        self.assertIn("年度表现", html)
        self.assertIn("收益率分布", html)
        self.assertIn(">2025<", html)
        self.assertNotIn(">2,025<", html)

    def test_card_has_fixed_width_natural_height_full_greeks_and_compact_tables(self) -> None:
        html = render(DesignerInput(payload=_payload(), output_type="card"))["html"]

        self.assertIn("width: min(210mm, calc(100% - 32px));", html)
        self.assertNotRegex(html, r"(?m)^\s*(?:min-)?height:\s*148\.5mm;")
        for greek in ("Delta", "Gamma", "Vega", "Theta", "Rho"):
            self.assertIn(greek, html)
        for metric in (
            "样本区间", "样本数", "胜率", "平均收益", "最大亏损",
            "敲出比例", "敲入比例", "平均敲出日", "未敲出平均收益",
        ):
            self.assertIn(metric, html)
        self.assertIn("标的价格不利变动可能导致损失；波动率变化会影响估值。", html)
        self.assertNotIn("第三条不进入Card。", html)
        self.assertNotIn('class="chart', html)
        self.assertNotIn("Delta风险曲线", html)
        self.assertNotIn("Delta曲面", html)
        self.assertNotIn('class="card-metric-grid"', html)
        self.assertEqual(html.count('class="card-data-table"'), 2)
        self.assertIn('<section class="card-analysis-grid"><div class="card-analysis"><h2>估值定价</h2><div class="table-wrap card-table-wrap"', html)

    def test_card_prose_formula_uses_mathml_without_raw_underscore(self) -> None:
        payload = _payload()
        payload["recommendation"]["reason"] = "当S_T低于K_1时关注P_net损失。"
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]

        self.assertIn("<math", html)
        self.assertIn("<msub><mi>S</mi><mi>T</mi></msub>", html)
        self.assertIn("<msub><mi>K</mi><mn>1</mn></msub>", html)
        self.assertIn("<msub><mi>P</mi><mi>net</mi></msub>", html)
        self.assertNotIn("S_T", html)
        self.assertNotIn("K_1", html)
        self.assertNotIn("P_net", html)

    def test_report_uses_report_date_and_card_keeps_a_compact_reason(self) -> None:
        report = render(DesignerInput(payload=_payload(), output_type="report", html_report_layout="continuous"))["html"]
        card = render(DesignerInput(payload=_payload(), output_type="card"))["html"]

        self.assertIn("报告日期", report)
        self.assertNotIn("截至日期", report)
        self.assertIn("<h1>示例结构推荐报告</h1>", card)
        self.assertIn(".designer-card .card-reasoning p", card)
        self.assertIn("font-size: var(--type-card-reason);", card)
        self.assertIn("--type-card-reason: 9px;", card)

    def test_card_is_a_compact_subset_of_the_same_report_facts(self) -> None:
        card = render(DesignerInput(payload=_payload(), output_type="card"))["html"]
        report = render(DesignerInput(payload=_payload(), output_type="report", html_report_layout="continuous"))["html"]

        for value in (
            "经典型雪球", "中证500指数", "123,456.79", "Delta", "Gamma", "Vega", "Theta", "Rho",
            "样本区间", "样本数", "胜率", "平均收益", "最大亏损", "敲出比例", "敲入比例",
        ):
            self.assertIn(value, card)
            self.assertIn(value, report)
        self.assertNotIn('class="chart', card)
        self.assertIn('class="chart', report)

    def test_card_pdf_uses_a4_width_and_does_not_impose_a_single_page_limit(self) -> None:
        artifact = render(DesignerInput(payload=_payload(), output_type="card", format="pdf"))
        pdf = artifact["pdf"]
        media_box = re.search(rb"/MediaBox \[ 0 0 ([0-9.]+) ([0-9.]+) \]", pdf)

        self.assertIsNotNone(media_box)
        assert media_box is not None
        width, height = (float(value) for value in media_box.groups())
        self.assertLess(width, height)
        self.assertAlmostEqual(width / height, 210 / 297, delta=0.02)
        self.assertGreaterEqual(pdf.count(b"/Type /Page\n"), 1)

    def test_long_card_content_grows_in_html_and_paginated_pdf_without_rejecting_the_structure(self) -> None:
        payload = deepcopy(_payload())
        payload["recommendation"]["reason"] = "已冻结研究依据。" * 800

        html = render(DesignerInput(payload=payload, output_type="card"))["html"]
        pdf = render(DesignerInput(payload=payload, output_type="card", format="pdf"))["pdf"]

        self.assertIn("已冻结研究依据。", html)
        self.assertGreaterEqual(pdf.count(b"/Type /Page\n"), 2)

    def test_partial_modules_show_their_truthful_state_without_a_fake_metric(self) -> None:
        payload = deepcopy(_payload())
        payload["pricing"] = {"status": "partial", "note": "仅完成市场数据校验。", "metrics": [{"label": "现值", "value": 1.2345}]}
        payload["backtest"] = {"status": "failed", "note": "历史数据不足。", "metrics": [{"label": "样本数", "value": 120}]}

        html = render(DesignerInput(payload=payload, output_type="report", html_report_layout="continuous"))["html"]
        self.assertIn("部分接入", html)
        self.assertIn("运行失败", html)
        self.assertNotIn("1.23", html)
        self.assertNotIn(">120<", html)


if __name__ == "__main__":
    unittest.main()
