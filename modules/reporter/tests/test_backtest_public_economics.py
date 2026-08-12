"""Reader-facing economic basis and branch-coverage wording."""

from __future__ import annotations

import unittest

from modules.reporter.src.report_unit_builder import _backtest_content


class BacktestPublicEconomicsTests(unittest.TestCase):
    def test_pnl_is_disclosed_as_contract_gross_not_customer_net(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {
                "backtest": {
                    "common_metrics": {
                        "sample_count": 10,
                        "win_rate": 0.6,
                        "average_pnl": 125.0,
                        "minimum_pnl": 20.0,
                        "maximum_pnl": 260.0,
                    },
                    "backtest_config": {"return_denominator": "notional"},
                    "branch_coverage": {
                        "declared_pair_count": 4,
                        "observed_pair_count": 2,
                        "uncovered_pair_count": 2,
                        "status": "partial",
                    },
                }
            },
        })

        overview = next(table for table in content["detail_tables"] if table["title"] == "公共回测统计")
        rows = {row["label"]: row for row in overview["rows"]}
        self.assertIn("平均损益", rows)
        self.assertIn("最小损益", rows)
        self.assertIn("最大损益", rows)
        self.assertIn("合同条款现金流口径", rows["平均损益"]["note"])
        self.assertIn("不等同客户净收益", rows["胜率"]["note"])

        coverage = next(table for table in content["detail_tables"] if table["title"] == "分支覆盖")
        coverage_rows = {row["label"]: row for row in coverage["rows"]}
        self.assertEqual(coverage_rows["已观察分支"]["value"], 2)
        self.assertIn("4个合同声明分支", coverage_rows["已观察分支"]["note"])
        self.assertIn("不代表未观察分支已实际发生", coverage_rows["未观察分支"]["note"])

    def test_positive_minimum_pnl_is_never_called_maximum_loss(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {"backtest": {"common_metrics": {"sample_count": 3, "minimum_pnl": 12.0}}},
        })
        labels = {
            row["label"]
            for table in content["detail_tables"]
            for row in table.get("rows", [])
        }
        self.assertIn("最小损益", labels)
        self.assertNotIn("最大亏损", labels)


if __name__ == "__main__":
    unittest.main()
