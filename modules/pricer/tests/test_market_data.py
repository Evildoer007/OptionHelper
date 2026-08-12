"""Pricer市场数据口径回归测试。"""

from __future__ import annotations

import math
import unittest
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.pricer.market_resolver import market_snapshot_from_history


class PricerMarketDataTest(unittest.TestCase):
    def test_uses_close_for_spot_and_adj_close_for_hv(self) -> None:
        history = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=4, freq="B"),
            "asset_id": ["000905.SH"] * 4,
            "close": [100.0, 100.0, 100.0, 101.0],
            "adj_close": [100.0, 110.0, 121.0, 133.1],
        })
        snapshot = market_snapshot_from_history(
            history,
            ["000905.SH"],
            valuation_date=None,
            hv_window=2,
            risk_free_rate=0.02,
            dividend_yield=0.0,
        )
        self.assertEqual(snapshot["spot"], {"000905.SH": 101.0})
        self.assertEqual(snapshot["history_start_date"], "2024-01-02")
        self.assertEqual(snapshot["history_end_date"], "2024-01-05")
        self.assertAlmostEqual(snapshot["historical_volatility"]["000905.SH"], 0.0)
        self.assertEqual(snapshot["spot_price_field"], "close")
        self.assertEqual(snapshot["hv_price_field"], "adj_close")
        self.assertEqual(snapshot["return_method"], "log_return")
        self.assertEqual(snapshot["annualization_trading_days"], 244)
        self.assertTrue(math.isfinite(snapshot["historical_volatility"]["000905.SH"]))


if __name__ == "__main__":
    unittest.main()
