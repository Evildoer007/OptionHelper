from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core import tool_entry
from core.tool_entry import ProjectRequestError, _latest_available_data_date


class IntradayValuationDateTests(unittest.TestCase):
    def test_requested_today_uses_latest_date_actually_present_in_data(self) -> None:
        ref = SimpleNamespace(
            asset_ids=("159928.SZ",),
            coverage={
                "end_date": "2026-08-12",
                "sessions": ["2026-08-10", "2026-08-11"],
                "by_asset": {
                    "159928.SZ": {
                        "start_date": "2022-08-15",
                        "end_date": "2026-08-11",
                        "row_count": 967,
                    },
                },
            },
        )

        resolved = _latest_available_data_date(ref, requested_date=date(2026, 8, 12))

        self.assertEqual(resolved, "2026-08-11")

    def test_multi_underlying_uses_latest_date_covered_by_every_asset(self) -> None:
        ref = SimpleNamespace(
            asset_ids=("000300.SH", "399006.SZ"),
            coverage={
                "end_date": "2026-08-12",
                "by_asset": {
                    "000300.SH": {"end_date": "2026-08-12"},
                    "399006.SZ": {"end_date": "2026-08-11"},
                },
            },
        )

        resolved = _latest_available_data_date(ref, requested_date=date(2026, 8, 12))

        self.assertEqual(resolved, "2026-08-11")

    def test_missing_observed_end_date_fails_instead_of_using_requested_end(self) -> None:
        ref = SimpleNamespace(
            asset_ids=("159928.SZ",),
            coverage={"end_date": "2026-08-12", "by_asset": {}},
        )

        with self.assertRaisesRegex(ProjectRequestError, "实际数据截止日"):
            _latest_available_data_date(ref, requested_date=date(2026, 8, 12))

    def test_mc10_path_pricer_uses_previous_available_data_date(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-intraday-") as temporary:
            with (
                patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "fixture-refresh"}),
                patch(
                    "modules.datafetcher.service._providers",
                    return_value={"ifind_http": _IntradayIFindProvider()},
                ),
                patch(
                    "modules.datafetcher.calendar_service.IFindHttpProvider",
                    return_value=_IntradayIFindProvider(),
                ),
            ):
                result = tool_entry.run_module_request(
                    {
                        "module": "pricer",
                        "product_id": "5.1",
                        "identity": {"underlyings": ["159928.SZ"]},
                        "horizon": "3个月",
                        "term_overrides": {"T": 0.25},
                        "pricing_config": {
                            "valuation_date": "2026-08-12",
                            "model_method": "monte_carlo",
                            "path_count": 10,
                            "demo_mode": True,
                        },
                        "data_window": {"start_date": "2025-08-12", "end_date": "2026-08-12"},
                    },
                    project_root=Path(temporary),
                    ifind_probe=lambda token: self.assertEqual(token, "fixture-refresh"),
                )

        self.assertTrue(result["ok"])
        pricing = result["result"]["pricing"]
        snapshot = result["result"]["market_snapshot"]
        self.assertEqual(pricing["path_count"], 10)
        self.assertEqual(pricing["precision_status"], "demo_only")
        self.assertFalse(pricing["quote_eligible"])
        self.assertEqual(snapshot["requested_valuation_date"], "2026-08-12")
        self.assertEqual(snapshot["effective_valuation_session"], "2026-08-11")
        self.assertEqual(snapshot["market_as_of_date"], "2026-08-11")


class _IntradayIFindProvider:
    name = "ifind_http"
    network = True

    @staticmethod
    def estimate_quota(request) -> int:
        return len(request.asset_ids)

    def fetch(self, request, config):
        del config
        requested_end = pd.Timestamp(request.end_date)
        dates = pd.date_range(request.start_date, request.end_date, freq="B")
        dates = dates[dates < requested_end]
        rows = []
        for asset_id in request.asset_ids:
            for index, timestamp in enumerate(dates):
                close = 100.0 + index * 0.01
                rows.append({
                    "date": timestamp.strftime("%Y-%m-%d"), "asset_id": asset_id,
                    "open": close - 0.1, "high": close + 0.2, "low": close - 0.2, "close": close,
                    "adj_open": close - 0.1, "adj_high": close + 0.2,
                    "adj_low": close - 0.2, "adj_close": close, "volume": 1_000_000.0,
                })
        return pd.DataFrame(rows)

    def fetch_calendar(self, request, config):
        del config
        sessions = tuple(
            timestamp.strftime("%Y-%m-%d")
            for timestamp in pd.date_range(request.start_date, request.end_date, freq="B")
        )
        exchanges = {"SSE" if asset.endswith(".SH") else "SZSE" for asset in request.asset_ids}
        return {exchange: sessions for exchange in exchanges}

if __name__ == "__main__":
    unittest.main()
