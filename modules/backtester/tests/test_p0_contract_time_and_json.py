"""Backtester合同期限与严格JSON的P0回归测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest  # noqa: E402
from modules.backtester.entry_generator import BacktestInputError  # noqa: E402
from modules.backtester.path_replay import replay_path  # noqa: E402
from runtime.contracts.contract_api import FormulaError, resolve_contract  # noqa: E402


def _history(prices: list[float], *, start: str = "2021-01-04") -> HistoricalData:
    dates = pd.date_range(start, periods=len(prices), freq="B")
    return HistoricalData.from_frame(pd.DataFrame({
        "date": dates,
        "asset_id": "000905.SH",
        "close": prices,
        "adj_close": prices,
    }), data_asset_ref={
        "price_convention": {
            "contract_close_field": "close",
            "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close",
            "hv_adjustment": "forward",
            "close_equals_adj_close": True,
        },
    })


def _run(product_id: str, prices: list[float], *, start: str = "2021-01-04"):
    contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH"]})
    return backtest(BacktestInput(
        contract,
        BacktestConfig(entry_rule="explicit", entry_dates=(start,)),
        _history(prices, start=start),
    ))


class ContractTimeAndStrictJsonRegressionTest(unittest.TestCase):
    def test_count_tenor_retry_does_not_mask_unrelated_interpreter_errors(self) -> None:
        contract = resolve_contract("8.1", identity={"underlyings": ["000905.SH"]})
        dates = pd.date_range("2021-01-04", periods=240, freq="B")
        values = np.full((240, 1), 100.0)

        with patch(
            "modules.backtester.path_replay.evaluate_payoff",
            side_effect=FormulaError("synthetic_formula_error"),
        ) as mocked_evaluate:
            with self.assertRaisesRegex(BacktestInputError, "synthetic_formula_error"):
                replay_path(contract, dates=dates, values=values, price_fields={})

        mocked_evaluate.assert_called_once()

    def test_accumulator_240_observations_are_a_complete_non_ko_contract(self) -> None:
        result = _run("8.1", [100.0] * 240)
        trade = result.trades[0]
        expected_exit = pd.date_range("2021-01-04", periods=240, freq="B")[-1].strftime("%Y-%m-%d")

        self.assertEqual(trade.exit_date, expected_exit)
        self.assertEqual(trade.historical_contract.end_date, expected_exit)
        self.assertEqual(trade.cashflows, ({"date": expected_exit, "time": 1.0, "amount": 240_000.0},))
        self.assertAlmostEqual(trade.actual_time_years, trade.actual_calendar_days / 365.0)

    def test_non_ko_monitor_sentinels_never_enter_ledger_or_strict_json(self) -> None:
        cases = (
            ("9.11", "S_out", [100.0] * 550),
            ("9.12", "n_out", [90.0] + [80.0] * 549),
        )
        for product_id, monitor_name, prices in cases:
            with self.subTest(product_id=product_id, monitor_name=monitor_name):
                payload = _run(product_id, prices).to_dict()
                self.assertIsNone(payload["trade_ledger"][0]["events"][monitor_name])
                json.dumps(payload, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
