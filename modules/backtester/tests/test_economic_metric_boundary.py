"""经济口径边界回归，独立于既有回测正确性测试。"""

# ruff: noqa: E402

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest
from runtime.contracts.contract_api import resolve_contract


class EconomicMetricBoundaryTests(unittest.TestCase):
    def test_max_loss_is_zero_when_every_contract_cashflow_pnl_is_positive(self) -> None:
        """最大亏损是损失金额，不得把最小正收益标为亏损。"""

        history = HistoricalData.from_frame(pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=5, freq="B"),
            "asset_id": "000300.SH",
            "close": [100.0, 110.0, 112.0, 114.0, 116.0],
        }))
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000300.SH"]},
            term_overrides={"T": 2 / 365, "K": 100.0, "Pi_0": 1.0},
        )

        result = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02", "2024-01-03")),
            history,
        )).to_dict()

        self.assertGreater(result["common_metrics"]["minimum_pnl"], 0.0)
        self.assertEqual(result["common_metrics"]["max_loss"], 0.0)
        self.assertEqual(result["economic_convention"]["pnl_basis"], "contract_cashflow_before_external_costs")
        self.assertFalse(result["economic_convention"]["external_costs_modelled"])
        self.assertIsNone(result["trade_ledger"][0]["client_net_pnl"])

    def test_backtest_payload_discloses_gross_pnl_and_win_rate_denominator(self) -> None:
        """Backtester只输出合同毛损益，并明确胜率使用有效结算样本作分母。"""

        history = HistoricalData.from_frame(pd.DataFrame({
            "date": pd.date_range("2024-02-01", periods=3, freq="B"),
            "asset_id": "000905.SH", "close": [100.0, 99.0, 90.0],
        }))
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]},
            term_overrides={"T": 2 / 365, "K": 100.0, "Pi_0": 2.0},
        )
        payload = backtest(BacktestInput(
            contract, BacktestConfig(entry_rule="explicit", entry_dates=("2024-02-01",)), history,
        )).to_dict()

        convention = payload["economic_convention"]
        self.assertEqual(convention["pnl_basis"], "contract_cashflow_before_external_costs")
        self.assertFalse(convention["external_costs_modelled"])
        self.assertEqual(convention["client_net_pnl_status"], "not_modelled")
        self.assertEqual(convention["win_rate_numerator"], "contract_cashflow_pnl_gt_zero")
        self.assertEqual(convention["win_rate_denominator"], "valid_trade_count")

    def test_max_loss_is_a_positive_magnitude_while_minimum_pnl_stays_signed(self) -> None:
        """最差合同现金流与最大亏损必须保留各自的符号语义。"""

        history = HistoricalData.from_frame(pd.DataFrame({
            "date": pd.date_range("2024-02-01", periods=3, freq="B"),
            "asset_id": "000905.SH", "close": [100.0, 99.0, 90.0],
        }))
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]},
            term_overrides={"T": 2 / 365, "K": 100.0, "Pi_0": 2.0},
        )
        result = backtest(BacktestInput(
            contract, BacktestConfig(entry_rule="explicit", entry_dates=("2024-02-01",)), history,
        )).to_dict()["common_metrics"]

        self.assertEqual(result["minimum_pnl"], -2.0)
        self.assertEqual(result["max_loss"], 2.0)


if __name__ == "__main__":
    unittest.main()
