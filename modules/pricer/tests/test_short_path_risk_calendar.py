"""Short path-risk grid must never fill missing real sessions with weekdays."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import unittest

import pandas as pd

from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import DataAssetRef

from modules.pricer import PricingInput, price
from modules.pricer.market_resolver import market_snapshot_from_history
from modules.pricer.models import TradingCalendarData
from modules.pricer.observed_state import ObservedContractState
from modules.pricer.product_pricing_adapter import ProductPricingAdapter

from .pricer_test_fixtures import demo_config, market_asset


class ShortPathRiskCalendarTests(unittest.TestCase):
    def test_weekend_grid_point_is_not_applicable_and_does_not_change_main_pv(self) -> None:
        history, history_ref = market_asset(("A",))
        calendar, calendar_ref = _sparse_real_calendar()
        contract = resolve_contract(
            "5.1",
            identity={"underlyings": ["A"], "reference_prices": {"A": 100.0}},
            term_overrides={"T": 7 / 365},
        )
        config = replace(demo_config(("A",)), time_to_maturity=7 / 365)

        snapshot = market_snapshot_from_history(
            pd.DataFrame(history.rows), contract.underlyings,
            valuation_date=config.valuation_date, hv_window=config.hv_window,
            risk_free_rate=config.risk_free_rate, dividend_yield=config.dividend_yield,
            trading_calendar={
                "calendar_id": "CN-SSE", "calendar_version": "sparse-short-risk-v1",
                "sessions": calendar.sessions, "verified_cn_sessions": True, "source": "host-injected",
            },
        )
        base_config = replace(config, historical_volatility=snapshot["historical_volatility"])
        base = ProductPricingAdapter(
            contract, base_config, snapshot,
            ObservedContractState.from_value(None, valuation_date=config.valuation_date),
        ).reprice()

        result = price(PricingInput(
            contract, config, history, (history_ref,), calendar, calendar_ref,
        ))

        self.assertEqual(result.status, "priced")
        self.assertEqual(result.pv_amount, base.pv_amount)
        theta = next(curve for curve in result.risk_curves if curve["key"] == "theta_time")
        self.assertEqual(theta["points"][0]["status"], "not_applicable")
        self.assertIsNone(theta["points"][0]["y"])
        self.assertIn("不足两个真实交易session", theta["points"][0]["reason"])
        delta_surface = next(surface for surface in result.risk_surfaces if surface["key"] == "delta_surface")
        self.assertTrue(all(item["status"] == "not_applicable" for item in delta_surface["data"][:5]))
        self.assertTrue(all(item["value"][2] is None for item in delta_surface["data"][:5]))


def _sparse_real_calendar() -> tuple[TradingCalendarData, DataAssetRef]:
    sessions = ("2023-07-28", "2023-08-02", "2023-08-04")
    digest = sha256("\n".join(sessions).encode("utf-8")).hexdigest()
    calendar = TradingCalendarData(
        source_ref="memory://sparse-short-risk-calendar", asset_ids=("A",), sessions=sessions,
        sessions_by_exchange={"SSE": sessions}, asset_exchange={"A": "SSE"},
        requested_start_date=sessions[0], requested_end_date=sessions[-1], content_hash=digest,
    )
    reference = DataAssetRef(
        data_asset_id="sparse-short-risk-calendar", storage_ref=calendar.source_ref,
        media_type="application/json", schema_id="trading-calendar", asset_ids=("A",),
        normalized_fields=("session",),
        coverage={
            "start_date": sessions[0], "end_date": sessions[-1], "sessions": list(sessions),
            "calendar_id": "CN-SSE", "calendar_version": "sparse-short-risk-v1",
        },
        row_count=len(sessions), price_convention={"contains_market_prices": False},
        content_hash=digest, lineage={"fixture": "sparse-real-short-risk"},
    )
    return calendar, reference


if __name__ == "__main__":
    unittest.main()
