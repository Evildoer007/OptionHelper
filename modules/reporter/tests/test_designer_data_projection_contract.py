"""Reporter to Designer public data projection contract.

Projection is restricted to already frozen module facts.  This suite checks
that it adds reader labels and structure, never a valuation or backtest value.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.report_unit_builder import _backtest_content, _pricing_content


class DesignerDataProjectionTests(unittest.TestCase):
    def test_pricing_projection_orders_all_greeks_and_maps_curves_and_surfaces(self) -> None:
        module = {
            "status": "ready",
            "result": {
                "pricing": {
                    "method": "monte_carlo",
                    "valuation_date": "2026-08-11",
                    "pv_amount": 123.456,
                    "greeks": {
                        "vega": {"pv_amount_value": 4.56, "pv_amount_unit": "CNY"},
                        "delta": {"pv_amount_value": 0.12, "pv_amount_unit": "CNY"},
                        "gamma": {"pv_amount_value": 0.01, "pv_amount_unit": "CNY"},
                        "theta": {"pv_amount_value": -0.4, "pv_amount_unit": "CNY"},
                    },
                    "risk_curves": [{
                        "key": "delta_spot", "name": "Delta风险曲线",
                        "x_axis": {"name": "标的价格"}, "y_axis": {"name": "Delta"},
                        "points": [{"x": 90, "y": 0.1}, {"x": 100, "y": 0.2}],
                    }],
                    "risk_surfaces": [{
                        "key": "delta_surface", "name": "Delta曲面",
                        "x_axis": {"name": "标的价格"}, "y_axis": {"name": "剩余期限"}, "z_axis": {"name": "Delta"},
                        "data": [{"x": 90, "y": 30, "z": 0.1}, {"x": 100, "y": 30, "z": 0.2}],
                    }],
                }
            },
        }
        public = _pricing_content(module)

        self.assertEqual([row["label"] for row in public["greeks"]], ["Delta", "Gamma", "Vega", "Theta", "Rho"])
        self.assertEqual(public["greeks"][-1]["value"], "不适用")
        self.assertEqual(public["charts"][0]["type"], "line")
        self.assertEqual(public["charts"][1]["type"], "heatmap")
        self.assertEqual(public["charts"][1]["x"], [90, 100])

    def test_backtest_projection_keeps_core_detail_and_exactly_four_card_metrics(self) -> None:
        module = {
            "status": "ready",
            "result": {
                "backtest": {
                    "common_metrics": {
                        "sample_count": 12, "win_rate": 0.5, "average_return": 0.02,
                        "median_return": 0.01, "minimum_pnl": -20.0,
                        "return_distribution": [{"label": "正收益", "count": 6}],
                    },
                    "sample_definition": {"entry_rule": "monthly"},
                    "event_summary": {"tau_out": {"count": 4, "rate": 1 / 3}},
                    "monitor_summary": {"n_coupon": {"average": 6}},
                    "outcome_summary": [{"label": "敲出", "count": 4, "rate": 1 / 3}],
                    "annual_summary": [{"year": 2025, "average_return": 0.02}],
                    "underlying_performance": [{"underlying": "000905.SH", "average_return": 0.06}],
                    "metric_profile": {"profile_id": "dual_knock_autocall"},
                    "specialized_metrics": {
                        "profile_id": "dual_knock_autocall",
                        "events": {"tau_in": {"rate": 0.2}, "tau_out": {"rate": 0.4}},
                        "conditional_summary": {"knock_in_then_no_knock_out_rate": 0.1},
                        "three_outcome_summary": [{"label": "敲出", "count": 4, "rate": 0.4}],
                    },
                }
            },
        }
        public = _backtest_content(module)

        self.assertEqual(
            [row["label"] for row in public["metrics"][:4]],
            ["样本数", "胜率", "平均合同现金流收益率", "最差损益"],
        )
        self.assertEqual(len(public["card_metrics"]), 4)
        self.assertGreaterEqual(len(public["detail_tables"]), 4)
        self.assertIn("标的表现", [table["title"] for table in public["detail_tables"]])
        self.assertEqual([chart["title"] for chart in public["charts"][:2]], ["收益率分布", "路径结果分布"])


if __name__ == "__main__":
    unittest.main()
