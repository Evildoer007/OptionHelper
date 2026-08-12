"""Cross-module calculation matrix with controlled iFind-like daily data.

This is intentionally a new integration test rather than a modification of an
existing regression: it verifies the project Host path used by the three
calculators, including the case in which a requested valuation day has no
daily bar yet.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core import tool_entry


class ModuleCalculationMatrixTests(unittest.TestCase):
    def test_payoffer_pricer_and_backtester_cover_products_dates_and_parameters(self) -> None:
        """Each calculator completes across distinct product families and inputs."""

        payoffer_cases = (
            ("2.1", "000300.SH", {"T": 0.5, "K": 105.0, "Pi_0": 4.5, "n_C": 2.0}),
            ("6.1", "000905.SH", {"T": 0.25, "K": 98.0, "A": 15.0, "Pi_0": 3.0}),
            ("10.7", "159928.SZ", {
                "T": 1 / 12, "Kd": 94.0, "Ku": 106.0,
                "H_KO_1": 84.0, "H_KO_2": 116.0, "alpha_u": 0.6, "alpha_d": 0.7,
            }),
        )
        with tempfile.TemporaryDirectory(prefix="optionhelper-matrix-payoffer-") as temporary:
            root = Path(temporary)
            for product_id, asset, terms in payoffer_cases:
                with self.subTest(module="payoffer", product_id=product_id, asset=asset):
                    result = tool_entry.run_module_request({
                        "module": "payoffer", "product_id": product_id,
                        "identity": {"underlyings": [asset]}, "term_overrides": terms,
                    }, project_root=root)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["result"]["module"], "payoffer")
                    self.assertEqual(result["result"]["contract_fingerprint"], result["contract_fingerprint"])

        # The dates respectively exercise: a normal available session, today's
        # missing final bar, and a weekend request.  The simulated provider
        # deliberately does not emit a row for 2026-08-12.
        pricer_cases = (
            ("2.1", "000300.SH", "2026-08-11", "2026-08-11", 16, {"T": 0.50, "K": 103.0, "Pi_0": 4.0}),
            ("10.7", "159928.SZ", "2026-08-12", "2026-08-11", 64, {
                "T": 1 / 12, "Kd": 94.0, "Ku": 106.0, "H_KO_1": 84.0, "H_KO_2": 116.0,
            }),
            ("9.1", "000905.SH", "2026-08-09", "2026-08-07", 128, {
                "T": 0.08, "H_KI": 75.0, "H_KO": 104.0, "c": 0.12, "g": 0.12,
            }),
        )
        with self._ifind_fixture(), tempfile.TemporaryDirectory(prefix="optionhelper-matrix-pricer-") as temporary:
            root = Path(temporary)
            for product_id, asset, requested, effective, path_count, terms in pricer_cases:
                with self.subTest(module="pricer", product_id=product_id, requested=requested):
                    result = tool_entry.run_module_request({
                        "module": "pricer", "product_id": product_id,
                        "identity": {"underlyings": [asset]}, "term_overrides": terms,
                        "horizon": "1个月",
                        "pricing_config": {
                            "valuation_date": requested, "path_count": path_count,
                            "hv_window": 20, "risk_free_rate": 0.018, "dividend_yield": 0.0,
                        },
                        "data_window": {"start_date": "2025-01-02", "end_date": requested},
                    }, project_root=root, ifind_probe=_assert_fixture_token)
                    self.assertTrue(result["ok"])
                    pricing = result["result"]["pricing"]
                    snapshot = result["result"]["market_snapshot"]
                    self.assertEqual(pricing["status"], "priced")
                    self.assertEqual(snapshot["requested_valuation_date"], requested)
                    self.assertEqual(snapshot["market_as_of_date"], effective)
                    self.assertEqual(snapshot["effective_valuation_session"], effective)
                    if product_id != "2.1":
                        self.assertEqual(pricing["path_count"], path_count)
                    if requested == "2026-08-12":
                        self.assertNotEqual(snapshot["data_ref"]["coverage"]["end_date"], effective)

        backtester_cases = (
            ("2.1", "000300.SH", {"T": 0.08, "K": 102.0, "Pi_0": 3.0}, {"entry_rule": "daily"}),
            ("6.1", "000905.SH", {"T": 0.06, "K": 99.0, "A": 12.0, "Pi_0": 2.5}, {"entry_rule": "monthly"}),
            ("9.1", "159928.SZ", {"T": 0.08, "H_KI": 76.0, "H_KO": 104.0, "c": 0.12, "g": 0.12}, {
                "entry_rule": "explicit", "entry_dates": ["2025-03-03", "2025-04-01"],
            }),
        )
        with self._ifind_fixture(), tempfile.TemporaryDirectory(prefix="optionhelper-matrix-backtester-") as temporary:
            root = Path(temporary)
            for product_id, asset, terms, entry_config in backtester_cases:
                with self.subTest(module="backtester", product_id=product_id, asset=asset):
                    result = tool_entry.run_module_request({
                        "module": "backtester", "product_id": product_id,
                        "identity": {"underlyings": [asset]}, "term_overrides": terms,
                        "data_window": {"start_date": "2025-01-02", "end_date": "2026-08-12"},
                        "backtest_config": {
                            "start_date": "2025-03-03", "end_date": "2026-08-11",
                            "complete_tenor": True, **entry_config,
                        },
                    }, project_root=root, ifind_probe=_assert_fixture_token)
                    self.assertTrue(result["ok"])
                    payload = result["result"]
                    self.assertEqual(payload["module"], "backtester")
                    self.assertGreater(len(payload["backtest"]["trade_ledger"]), 0)

    @contextmanager
    def _ifind_fixture(self):
        provider = _MatrixIFindProvider()
        with (
            patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "matrix-refresh"}),
            patch("modules.datafetcher.service._providers", return_value={"ifind_http": provider}),
            patch("modules.datafetcher.calendar_service.IFindHttpProvider", return_value=provider),
        ):
            yield


def _assert_fixture_token(token: str) -> None:
    if token != "matrix-refresh":
        raise AssertionError("fixture iFind token was not supplied")


class _MatrixIFindProvider:
    name = "ifind_http"
    network = True

    @staticmethod
    def estimate_quota(request) -> int:
        return len(request.asset_ids)

    def fetch(self, request, config):
        del config
        dates = pd.date_range(request.start_date, request.end_date, freq="B")
        if request.end_date == "2026-08-12":
            dates = dates[dates < pd.Timestamp(request.end_date)]
        rows = []
        for asset_index, asset_id in enumerate(request.asset_ids):
            base = 100.0 + asset_index * 7.0
            for row_index, timestamp in enumerate(dates):
                close = base * (1.0 + 0.0002 * row_index)
                rows.append({
                    "date": timestamp.strftime("%Y-%m-%d"), "asset_id": asset_id,
                    "open": close - 0.05, "high": close + 0.15, "low": close - 0.15,
                    "close": close, "adj_open": close - 0.05, "adj_high": close + 0.15,
                    "adj_low": close - 0.15, "adj_close": close, "volume": 1_000_000.0,
                })
        return pd.DataFrame(rows)

    def fetch_calendar(self, request, config):
        del config
        sessions = tuple(timestamp.strftime("%Y-%m-%d") for timestamp in pd.date_range(
            request.start_date, request.end_date, freq="B",
        ))
        exchanges = {"SSE" if asset.endswith(".SH") else "SZSE" for asset in request.asset_ids}
        return {exchange: sessions for exchange in exchanges}


if __name__ == "__main__":
    unittest.main()
