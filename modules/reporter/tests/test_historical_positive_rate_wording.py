from __future__ import annotations

import unittest

from modules.reporter.src.report_unit_builder import _backtest_content


class HistoricalPositiveRateWordingTests(unittest.TestCase):
    def test_public_win_rate_states_its_historical_sample_denominator(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {
                "backtest": {
                    "common_metrics": {
                        "sample_count": 43,
                        "win_rate": 1.0,
                        "average_return": 0.069,
                        "minimum_pnl": 126_027.40,
                    },
                    "backtest_config": {"entry_rule": "monthly"},
                },
            },
        })

        metrics = {row["label"]: row for row in content["metrics"]}
        self.assertEqual(metrics["胜率"]["value"], 1.0)
        self.assertIn("43个有效入场样本", metrics["胜率"]["note"])
        self.assertIn("不代表未来获利概率", metrics["胜率"]["note"])
        self.assertEqual(metrics["最差损益"]["value"], 126_027.40)


if __name__ == "__main__":
    unittest.main()
