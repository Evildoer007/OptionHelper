"""Regression tests for independent market-data and trading-calendar dates."""

from __future__ import annotations

import unittest

from runtime.contracts.contract_api import resolve_contract

from modules.pricer.config import PricingConfig
from modules.pricer.models import PricingInput
from modules.pricer.valuation_solver import price

from .test_future_calendar_decoupling import _synthetic_market_inputs


class MarketCalendarAsOfSemanticsTests(unittest.TestCase):
    def test_requested_effective_and_market_as_of_remain_distinct_when_today_has_no_quote(self) -> None:
        """An intraday/no-data request uses the latest quote without changing its session."""
        history, history_ref, calendar, calendar_ref = _synthetic_market_inputs()
        contract = resolve_contract(
            "2.1",
            identity={
                "underlyings": ["000300.SH"],
                "contract_start_date": "2026-08-07",
                "contract_end_date": "2026-08-14",
                "reference_prices": {"000300.SH": 4024.0},
            },
            term_overrides={"T": 7 / 365},
        )

        result = price(PricingInput(
            contract=contract,
            pricing_config=PricingConfig.from_mapping({
                "valuation_date": "2026-08-12",
                "model_method": "black_scholes",
                "risk_free_rate": 0.02,
                "dividend_yield": 0.0,
            }),
            historical_data=history,
            market_data_refs=(history_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
        ))

        market = result.market_snapshot
        self.assertEqual(market["requested_valuation_date"], "2026-08-12")
        self.assertEqual(market["effective_valuation_session"], "2026-08-07")
        self.assertEqual(market["market_as_of_date"], "2026-08-07")
        self.assertEqual(market["valuation_date"], "2026-08-07")

    def test_market_date_fallback_does_not_invent_a_path_state(self) -> None:
        history, history_ref, calendar, calendar_ref = _synthetic_market_inputs()
        config = PricingConfig.from_mapping({
            "valuation_date": "2026-08-12", "model_method": "monte_carlo",
            "path_count": 1000, "risk_free_rate": 0.02, "dividend_yield": 0.0,
        })
        at_market_as_of = resolve_contract("5.1", identity={
            "underlyings": ["000300.SH"], "contract_start_date": "2026-08-07",
            "contract_end_date": "2026-08-14", "reference_prices": {"000300.SH": 4024.0},
        }, term_overrides={"T": 7 / 365})
        priced = price(PricingInput(
            at_market_as_of, config, history, (history_ref,), calendar, calendar_ref,
        ))
        self.assertEqual(priced.status, "priced")
        self.assertEqual(priced.market_snapshot["requested_valuation_date"], "2026-08-12")
        self.assertEqual(priced.market_snapshot["effective_valuation_session"], "2026-08-07")
        self.assertEqual(priced.market_snapshot["market_as_of_date"], "2026-08-07")

        midlife = resolve_contract("5.1", identity={
            "underlyings": ["000300.SH"], "contract_start_date": "2026-08-06",
            "contract_end_date": "2026-08-14", "reference_prices": {"000300.SH": 4023.0},
        }, term_overrides={"T": 8 / 365})
        unsupported = price(PricingInput(
            midlife, config, history, (history_ref,), calendar, calendar_ref,
        ))
        self.assertEqual(unsupported.status, "unsupported")
        self.assertFalse(unsupported.quote_eligible)
        self.assertIsNone(unsupported.pv_amount)
        self.assertIn("observed_contract_state", unsupported.limitations[0])


if __name__ == "__main__":
    unittest.main()
