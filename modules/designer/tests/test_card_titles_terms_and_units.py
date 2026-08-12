from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.designer.pdf_renderer import _extract_blocks


def payload() -> dict:
    return {
        "meta": {"title": "159928.SZ经典型雪球推荐卡片", "as_of_date": "2026-08-11"},
        "recommendation": {"headline": "经典型雪球", "underlyings": "159928.SZ", "reason": "使用同一份冻结合同进行分析。"},
        "contract_highlights": [
            {"label": "期限", "value": "2", "note": "年"},
            {"label": "名义本金", "value": "10,000,000", "note": "人民币"},
            {"label": "敲出障碍", "value": "103", "note": "S₀=100标准化水平"},
            {"label": "敲入障碍", "value": "70", "note": "S₀=100标准化水平"},
            {"label": "年化票息", "value": "20%", "note": "年化，ACT/365"},
            {"label": "观察频率", "value": "敲出：每月最后一个交易日；敲入：每个交易日", "note": "仅交易日"},
        ],
        "pricing": {"status": "not_run", "note": "本次未运行该项分析。"},
        "backtest": {
            "status": "ready",
            "window": "2022-08-12至2026-08-11",
            "metrics": [
                {"label": "样本数", "value": 13, "note": "个有效入场样本"},
                {"label": "胜率", "value": 6 / 13, "value_format": "percent", "note": "占有效入场样本"},
                {"label": "平均损益", "value": -1_291_991.75, "note": "人民币/份合同"},
                {"label": "最大亏损", "value": -2_941_712.2, "note": "人民币/份合同"},
            ],
            "card_metrics": [
                {"label": "敲出比例", "value": 6 / 13, "value_format": "percent", "note": "占有效入场样本"},
                {"label": "敲入比例", "value": 7 / 13, "value_format": "percent", "note": "占有效入场样本"},
                {"label": "敲入未敲出比例", "value": 1.0, "value_format": "percent", "note": "占已敲入样本"},
                {"label": "敲入后敲出比例", "value": 0.0, "value_format": "percent", "note": "占已敲入样本"},
            ],
        },
        "parameters": {},
        "payoff": {"status": "not_run"},
        "risk": {"items": ["敲入后可能承担标的下跌风险。"]},
    }


class CardTitlesTermsAndUnitsDesignerTests(unittest.TestCase):
    def test_card_uses_meta_title_and_places_one_shared_contract_summary_before_analysis(self) -> None:
        html = render(DesignerInput(payload=payload(), output_type="card"))["html"]

        self.assertIn("<title>159928.SZ经典型雪球推荐卡片</title>", html)
        self.assertIn("<h1>159928.SZ经典型雪球推荐卡片</h1>", html)
        self.assertEqual(html.count("关键条款"), 1)
        self.assertLess(html.index("关键条款"), html.index('class="card-analysis-grid"'))
        self.assertIn("以下估值定价与历史回测均基于同一份已确认合同。", html)
        self.assertIn("本次尚未形成可引用的估值结果。", html)
        for value in ("期限", "2年", "年化票息", "20%", "敲出：每月最后一个交易日；敲入：每个交易日"):
            self.assertIn(value, html)

    def test_card_and_report_escape_the_same_dynamic_title_and_pdf_keeps_contract_terms(self) -> None:
        value = payload()
        value["meta"]["title"] = "159928.SZ<经典型雪球&推荐>"
        card = render(DesignerInput(payload=value, output_type="card"))["html"]
        report = render(DesignerInput(payload=value, output_type="report", html_report_layout="continuous"))["html"]
        pdf = render(DesignerInput(payload=value, output_type="card", format="pdf"))

        for html in (card, report, pdf["html"]):
            self.assertIn("159928.SZ&lt;经典型雪球&amp;推荐&gt;", html)
            self.assertNotIn("159928.SZ<经典型雪球&推荐>", html)
        self.assertTrue(pdf["pdf"].startswith(b"%PDF-"))
        self.assertIn("关键条款", pdf["html"])
        contract_rows = [raw for kind, _level, raw in _extract_blocks(pdf["html"]) if kind == "metric_grid"]
        self.assertIn(["期限", "2年", ""], contract_rows[0])
        self.assertTrue(any(row[0] == "年化票息" and "20%" in row[1] for row in contract_rows[0]))

    def test_missing_metric_note_is_explicit_instead_of_dash(self) -> None:
        value = payload()
        value["backtest"]["metrics"][0].pop("note")
        html = render(DesignerInput(payload=value, output_type="card"))["html"]

        self.assertIn("<td>口径未提供</td>", html)
        self.assertNotIn("<td>-</td>", html)

    def test_assets_do_not_ship_static_examples_that_can_drift_from_runtime(self) -> None:
        self.assertFalse((ROOT / "modules" / "designer" / "assets" / "examples").exists())


if __name__ == "__main__":
    unittest.main()
