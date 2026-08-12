"""Regression contracts for public Reporter -> Designer projection.

The fixtures deliberately match the structured shapes emitted by the formal
Payoffer, Pricer and Backtester modules.  They are not simplified display
fixtures: the purpose is to prevent structured runtime values or internal
delivery vocabulary from leaking into Card, Report or PDF output.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT,
    ROOT / "core" / "src",
    ROOT / "modules" / "reporter" / "src",
    ROOT / "modules" / "designer" / "src",
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.designer.pdf_renderer import _extract_blocks
from modules.reporter.designer_handoff import _public_limitations, build_designer_payload
from modules.reporter.models import ReportRequest
from modules.reporter.report_unit_builder import _backtest_content, _parameter_rows, _payoff_content, _pricing_content
from modules.reporter.selection_facts import build_host_selection_source_refs


class RealPublicProjectionRegressions(unittest.TestCase):
    def test_formal_payoffer_shape_keeps_condition_payoff_and_case_semantics(self) -> None:
        content = _payoff_content({
            "status": "ready",
            "result": {
                "path_panels": [{
                    "title": "路径1",
                    "condition_tex": "True",
                    "payoff_tex": "max(S_T-K,0)-Pi_0",
                    "piece_summaries": [
                        {"domain": "0 <= S_T <= K", "payoff_tex": "cash(0, -n_C * P)"},
                        {"domain": "S_T > K", "payoff_tex": "cash(0, -n_C * P) + cash(T, n_C * (S_T-K))"},
                    ],
                }],
            },
        })

        self.assertEqual(len(content.get("scenarios", [])), 2)
        visible = " ".join(
            str(value)
            for scenario in content["scenarios"]
            for value in scenario.values()
        )
        # Reporter preserves the frozen formula tokens. Designer owns the
        # reader-facing MathML projection and must remove raw underscores.
        self.assertIn("0 <= S_T <= K", visible)
        self.assertNotIn("cash(", visible)
        self.assertTrue(all(str(scenario.get("rule", "")).strip() for scenario in content["scenarios"]))

        html = render(DesignerInput(payload={
            "meta": {"title": "公式投影测试", "as_of_date": "2026-08-11"},
            "payoff": content,
            "risk": {"items": ["权利金可能全部损失。"]},
        }))["html"]
        self.assertIn("<msub>", html)
        self.assertNotIn("S_T", html)

        fallback = _payoff_content({
            "status": "ready",
            "result": {
                "path_panels": [{
                    "title": "单一路径",
                    "condition_tex": "S_T > K",
                    "payoff_tex": "max(S_T-K,0)-Pi_0",
                    "piece_summaries": [],
                }],
            },
        })
        self.assertEqual(len(fallback.get("scenarios", [])), 1)
        fallback_visible = " ".join(str(value) for value in fallback["scenarios"][0].values())
        self.assertIn("S_T > K", fallback_visible)
        self.assertIn("max(S_T-K,0)-Pi_0", fallback_visible)

    def test_structured_greek_is_projected_without_python_mapping_repr(self) -> None:
        content = _pricing_content({
            "status": "ready",
            "result": {
                "pricing": {
                    "method": "monte_carlo",
                    "pv": 12.34,
                    "currency": "CNY",
                    "greeks": {
                        "Delta": {
                            "value": 0.51,
                            "unit": "CNY_per_1pct_vol",
                            "status": "computed",
                            "method": "central_difference",
                        }
                    },
                }
            },
        })

        self.assertEqual([row["label"] for row in content.get("greeks", [])], ["Delta", "Gamma", "Vega", "Theta", "Rho"])
        greek = content["greeks"][0]
        self.assertEqual(greek.get("value"), 0.51)
        self.assertEqual(greek.get("unit"), "人民币/波动率变化1个百分点")
        self.assertNotIn("_per_", str(greek.get("unit")))
        self.assertEqual(greek.get("status"), "computed")
        visible = " ".join(str(value) for value in greek.values())
        for forbidden in ("{'", "':", "central_difference"):
            self.assertNotIn(forbidden, visible)

    def test_backtest_entry_rule_and_win_rate_are_public_values(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {
                "backtest": {
                    "sample_count": 20,
                    "win_rate": 0.6,
                    "market_data": {"date_start": "2024-01-01", "date_end": "2025-01-01"},
                    "backtest_config": {"entry_rule": "monthly"},
                }
            },
        })
        visible = str(content)
        self.assertNotIn("monthly", visible)
        win_rate = next(item for item in content["metrics"] if item["label"] == "胜率")
        self.assertEqual(win_rate["value"], 0.6)
        self.assertEqual(win_rate["value_format"], "percent")
        self.assertIn("不代表未来获利概率", win_rate["note"])

    def test_machine_limitation_codes_are_mapped_to_public_chinese(self) -> None:
        public = _public_limitations([
            "exchange_calendar_not_exposed_by_data_source_weekday_validation_only",
            "no_nav_curve_is_generated",
        ])

        self.assertEqual(len(public), 2)
        visible = " ".join(public)
        self.assertIn("交易日历", visible)
        self.assertIn("净值曲线", visible)
        self.assertNotIn("_", visible)
        self.assertNotIn("not_exposed", visible)

    def test_contract_parameters_are_public_values_and_not_rendered_three_times(self) -> None:
        rows = _parameter_rows({
            "terms": {
                "pricing_methods": ["black_scholes", "monte_carlo"],
                "monitor": {},
                "margin_call": False,
            },
            "term_sources": {
                "pricing_methods": "default",
                "monitor": "default",
                "margin_call": "default",
            },
        })
        values = {str(row["cn"]): str(row["value"]) for row in rows}
        self.assertNotIn("[", values["适用估值方法"])
        self.assertNotIn("{", values["观察设置"])
        self.assertNotEqual(values["追加保证金"], "False")

        payload = {
            "meta": {"title": "参数去重测试", "as_of_date": "2026-08-11", "layout": "brief"},
            "sections": ["parameters", "risk"],
            "parameters": {
                "payoff_input": rows,
                "pricing_input": rows,
                "backtest_input": rows,
            },
            "risk": {"items": ["期权可能发生权利金损失。"], "disclaimer": "仅供研究。"},
        }
        html = render(DesignerInput(payload=payload))["html"]
        self.assertEqual(html.count("适用估值方法"), 1)
        self.assertEqual(html.count("追加保证金"), 1)
        self.assertNotIn(">pricing_methods<", html)
        self.assertNotIn(">margin_call<", html)

    def test_host_selection_preserves_public_suitability_risk_and_terms(self) -> None:
        source_refs = build_host_selection_source_refs({
            "source_id": "source-a",
            "task_id": "task-a",
            "analysis_case_id": "case-a",
            "catalog_version": "catalog-a",
            "candidates": [{
                "candidate_id": "candidate-a",
                "product_id": "2.1",
                "product_version": "product-a",
                "product_name": "看涨期权",
                "reason": "适用于已确认的温和看涨判断。",
                "suitable_for": ["可承受权利金损失。"],
                "not_suitable_for": ["要求保本。"],
                "main_risks": ["权利金可能全部损失。"],
                "key_terms": [{"label": "期限", "value": "1年"}],
            }],
        }, "tenant-a")
        candidate = source_refs["evidence_refs"]["recommendation_set"]["payload"]["candidates"][0]

        self.assertEqual(candidate["reason"], "适用于已确认的温和看涨判断。")
        self.assertTrue(candidate["suitable_for"])
        self.assertTrue(candidate["not_suitable_for"])
        self.assertTrue(candidate["main_risks"])
        self.assertTrue(candidate["key_terms"])
        self.assertNotIn("App Host", str(candidate))

    def test_report_has_as_of_suitability_risk_and_nonrepeated_conclusion(self) -> None:
        reason = "适用于已确认的温和看涨判断。"
        recommendation = {
            "candidate_id": "candidate-a",
            "product_id": "2.1",
            "product_name": "看涨期权",
            "underlyings": ["000905.SH"],
            "reason": reason,
            "suitable_for": ["可承受权利金全部损失。"],
            "not_suitable_for": ["要求本金保障。"],
            "main_risks": ["权利金可能全部损失。"],
            "key_terms": [{"label": "期限", "value": "1年"}],
        }
        unit = {
            "unit_type": "ContractReportUnit",
            "subject": {"product_name": "看涨期权", "underlyings": ["000905.SH"]},
            "modules": {},
            "content": {
                "recommendation": recommendation,
                "payoff": {"status": "not_run"},
                "pricing": {
                    "status": "ready",
                    "method": "Black-Scholes",
                    "valuation_date": "2026-08-11",
                    "metrics": [{"label": "估值", "value": "12.34", "note": "CNY"}],
                },
                "backtest": {"status": "not_run"},
                "parameters": {},
                "audit": {"limitations": []},
                "risk": {"items": recommendation["main_risks"], "disclaimer": "仅供研究。"},
            },
        }
        request = ReportRequest(
            tenant_id="tenant-a",
            task_id="task-a",
            report_run_id="report-a",
            analysis_case_id="case-a",
            subject_type="contract",
            subject_ref={
                "delivery_mode": "single",
                "candidate_ids": ["candidate-a"],
                "selected_modules": ["recommender", "pricing"],
            },
            source_refs={},
            output_type="report",
            format="html",
            html_report_layout="continuous",
            audience="professional",
            metadata={},
        )

        payload = build_designer_payload(unit, request)
        html = render(DesignerInput(payload=payload))["html"]
        visible = " ".join(str(block[2]) for block in _extract_blocks(html) if isinstance(block[2], str))
        self.assertEqual(payload["meta"].get("as_of_date"), "2026-08-11")
        self.assertEqual(payload["recommendation"].get("underlyings"), "000905.SH")
        self.assertNotIn("截至日期</dt><dd>待补充", html)
        self.assertIn("可承受权利金全部损失", visible)
        self.assertIn("要求本金保障", visible)
        self.assertIn("权利金可能全部损失", visible)
        self.assertGreaterEqual(visible.count(reason), 1)
        self.assertNotIn("已形成可用证据", visible)
        self.assertNotIn("应结合已确认条款", visible)
        for forbidden in ("App Host", "['", "exchange_calendar_", "no_nav_", "CNY_per_", "monthly", "cash(", "研究链路", "已接入结果"):
            self.assertNotIn(forbidden, visible)

    def test_pdf_projection_keeps_mathml_and_bound_payoff_figure(self) -> None:
        captured: dict[str, str] = {}

        def fake_pdf(html: str, _base: Path) -> bytes:
            captured["html"] = html
            return b"%PDF-1.7\nprojection-test"

        with tempfile.TemporaryDirectory(prefix="reporter-public-projection-") as directory:
            root = Path(directory)
            (root / "payoff.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'data-report-figure-profile="payoffer-report-figure/v1">'
                '<text x="1" y="12">到期损益</text></svg>',
                encoding="utf-8",
            )
            payload = {
                "meta": {"title": "PDF语义测试", "as_of_date": "2026-08-11", "layout": "brief"},
                "sections": ["payoff", "risk"],
                "payoff": {
                    "status": "ready",
                    "report_svg_path": "payoff.svg",
                    "formula_mathml": "<math><mrow><mi>S</mi><mo>-</mo><mi>K</mi></mrow></math>",
                    "scenarios": [{"title": "上涨", "rule": "S_T > K时获得正损益。"}],
                },
                "risk": {"items": ["权利金可能全部损失。"], "disclaimer": "仅供研究。"},
            }
            with patch("modules.designer.design_renderer._pdf_bytes", side_effect=fake_pdf):
                artifact = render(DesignerInput(payload=payload, format="pdf", input_dir=root))

        self.assertTrue(artifact["pdf"].startswith(b"%PDF-"))
        self.assertIn("<math", captured["html"])
        self.assertIn("data:image/svg+xml;base64", captured["html"])
        self.assertNotIn("<code", captured["html"])

    def test_real_pdf_text_extraction_keeps_formula_scenario_and_risk(self) -> None:
        payload = {
            "meta": {"title": "PDF文本回归", "as_of_date": "2026-08-11", "layout": "brief"},
            "sections": ["payoff", "risk"],
            "payoff": {
                "status": "ready",
                "formula_mathml": "<math><mrow><mi>S</mi><mo>-</mo><mi>K</mi></mrow></math>",
                "scenarios": [{"title": "上涨情景", "rule": "标的高于执行价时获得正损益。"}],
            },
            "risk": {"items": ["权利金可能全部损失。"], "disclaimer": "仅供研究。"},
        }
        artifact = render(DesignerInput(payload=payload, format="pdf"))
        extracted = " ".join(str(block[2]) for block in _extract_blocks(artifact["html"]))

        self.assertTrue(artifact["pdf"].startswith(b"%PDF-"))
        self.assertIn("上涨情景", extracted)
        self.assertIn("标的高于执行价时获得正损益", extracted)
        self.assertIn("权利金可能全部损失", extracted)
        self.assertIn("S", extracted)
        self.assertIn("K", extracted)

    def test_card_has_only_compact_recommendation_pricing_backtest_and_risk_content(self) -> None:
        payload = {
            "meta": {"title": "看涨期权简报", "as_of_date": "2026-08-11", "layout": "brief"},
            "recommendation": {
                "headline": "看涨期权",
                "underlyings": "000300.SH",
                "reason": "适合看涨且预期波动率上升的情形，最大损失限于期权费。",
                "terms": [{"label": "不应显示", "value": "不应显示"}],
            },
            "payoff": {"status": "ready", "scenarios": [{"title": "不应显示", "rule": "不应显示"}]},
            "pricing": {
                "status": "ready", "valuation_date": "2026-08-11",
                "metrics": [{"label": "现值", "value": "12.34", "note": "人民币"}, {"label": "标准误", "value": "0.03", "note": "人民币"}],
                "greeks": [
                    {"label": "Delta", "value": "0.51", "unit": "人民币/标的点"},
                    {"label": "Vega", "value": "0.48", "unit": "人民币/波动率变化1个百分点"},
                    {"label": "Gamma", "value": "0.01", "unit": ""},
                ],
            },
            "backtest": {
                "status": "ready", "window": "2024-01-01至2025-01-01",
                "metrics": [
                    {"label": "样本数", "value": "20", "note": ""},
                    {"label": "胜率", "value": "60%", "note": ""},
                    {"label": "最大亏损", "value": "-15%", "note": ""},
                    {"label": "平均收益", "value": "5%", "note": ""},
                ],
            },
            "risk": {"items": ["权利金可能全部损失。", "时间价值会持续衰减。", "第三项不应显示。"]},
            "parameters": {},
            "next_steps": ["不应显示"],
        }
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]

        for required in ("推荐结构", "估值定价", "历史回测", "风险提示", "看涨期权", "12.34", "60%"):
            self.assertIn(required, html)
        for excluded in ("收益情景", "不应显示", "下一步", "参数", "<img", "<svg", "echarts"):
            self.assertNotIn(excluded, html)
        self.assertIn("@page card", html)
        # Card holds its A4 reader width, while real frozen facts may extend
        # the document naturally rather than reserving an empty half page.
        self.assertIn("width: min(210mm, calc(100% - 32px));", html)
        self.assertNotRegex(html, r"(?m)^\s*(?:min-)?height:\s*148\.5mm;")
        for greek in ("Delta", "Gamma", "Vega", "Theta", "Rho"):
            self.assertIn(greek, html)
        self.assertEqual(html.count('class="card-data-table"'), 2)
        self.assertNotIn('class="card-metric-grid"', html)


if __name__ == "__main__":
    unittest.main()
