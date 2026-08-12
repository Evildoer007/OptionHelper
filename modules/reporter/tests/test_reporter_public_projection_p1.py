"""P1 regressions for Reporter reader-facing payloads.

These tests deliberately use only frozen upstream JSON-shaped facts.  They
guard the boundary where Reporter projects those facts to Designer; no
calculation or template rendering is involved here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.designer_handoff import build_collection_payload, build_design_brief, build_designer_payload
from modules.reporter.report_unit_builder import _backtest_content, _pricing_content


class ReporterPublicProjectionP1Tests(unittest.TestCase):
    def test_designer_payload_uses_only_public_chart_fields_and_no_unit_hash(self) -> None:
        pricing = _pricing_content({
            "status": "ready",
            "result": {"pricing": {
                "charts": [{
                    "id": "pv-profile", "title": "估值曲线", "type": "line",
                    "x": [90, 100], "series": [{"name": "估值", "data": [1.0, 2.0]}],
                    "source_note": "数据来源：本次估值结果。",
                    "accessibility_summary": "展示估值随标的价格变化。",
                    "run_id": "pricing-run-secret", "source_path": "/private/tmp/secret.json",
                }],
            }},
        })
        backtest = _backtest_content({
            "status": "ready",
            "result": {"backtest": {
                "common_metrics": {"sample_count": 2, "win_rate": 0.5},
                "charts": [{
                    "id": "history", "title": "历史表现", "type": "bar",
                    "x": ["a", "b"], "series": [{"name": "样本", "data": [1, 1]}],
                    "run_id": "backtest-run-secret", "source_path": "/private/tmp/history.json",
                }],
            }},
        })
        request = SimpleNamespace(
            output_type="report", selected_modules=("pricing", "backtest"), metadata={},
            report_run_id="report-p1", format="html", html_report_layout="continuous", audience="professional",
        )
        unit = {
            "unit_type": "ContractReportUnit", "semantic_fact_hash": "f" * 64,
            "subject": {"product_name": "看涨期权", "underlyings": ["000905.SH"]},
            "content": {
                "recommendation": {"product_name": "看涨期权", "underlyings": ["000905.SH"]},
                "payoff": {"status": "not_requested"}, "pricing": pricing, "backtest": backtest,
                "parameters": {}, "audit": {"limitations": []}, "risk": {"items": []},
            },
            "modules": {},
        }

        payload = build_designer_payload(unit, request)
        brief = build_design_brief(payload, request, report_unit_hashes=[unit["semantic_fact_hash"]])

        self.assertNotIn("report_unit_semantic_fact_hash", payload)
        self.assertEqual(brief["report_unit_semantic_fact_hashes"], ["f" * 64])
        for chart in payload["pricing"]["charts"] + payload["backtest"]["charts"]:
            self.assertNotIn("run_id", chart)
            self.assertNotIn("source_path", chart)

    def test_inconsistent_economic_convention_is_a_limitation_not_a_gross_return_claim(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {"backtest": {
                "common_metrics": {"sample_count": 6, "win_rate": 0.5, "average_pnl": 8.0},
                "economic_convention": {
                    "pnl_basis": "contract_cashflow_before_external_costs",
                    "external_costs_modelled": False,
                    "client_net_pnl_status": "not_modelled",
                    "win_rate_numerator": "contract_cashflow_pnl_gt_zero",
                    "win_rate_denominator": "wrong_denominator",
                },
            }},
        })
        overview = next(table for table in content["detail_tables"] if table["title"] == "公共回测统计")
        rows = {row["label"]: row for row in overview["rows"]}

        self.assertIn("分母为6个有效入场样本", rows["胜率"]["note"])
        self.assertIn("不等同客户净收益", rows["胜率"]["note"])
        self.assertNotIn("合约毛损益", rows["胜率"]["note"])
        self.assertIn("经济口径声明不一致", " ".join(content["limitations"]))

    def test_comparison_never_claims_that_independent_reports_exist(self) -> None:
        request = SimpleNamespace(output_type="report", delivery_mode="comparison", metadata={})
        payload = build_collection_payload([
            {"semantic_fact_hash": "a", "subject": {"product_name": "看涨期权", "underlyings": ["000905.SH"]}, "candidate": {"product_name": "看涨期权", "reason": "看涨。", "underlyings": ["000905.SH"]}, "content": {"contract_highlights": [{"label": "期限", "value": "1", "note": "年"}], "risk": {"items": []}}},
            {"semantic_fact_hash": "b", "subject": {"product_name": "看跌期权", "underlyings": ["000300.SH"]}, "candidate": {"product_name": "看跌期权", "reason": "看跌。", "underlyings": ["000300.SH"]}, "content": {"contract_highlights": [{"label": "期限", "value": "2", "note": "年"}], "risk": {"items": []}}},
        ], request)

        self.assertIn("另行生成对应单结构报告", payload["payoff"]["note"])
        self.assertNotIn("各候选独立报告", payload["payoff"]["note"])
        self.assertEqual(payload["recommendation"]["alternatives"][0]["tags"], ["标的：000905.SH", "期限：1年"])


if __name__ == "__main__":
    unittest.main()
