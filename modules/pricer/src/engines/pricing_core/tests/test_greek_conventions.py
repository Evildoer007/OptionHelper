from __future__ import annotations

from datetime import date
import unittest

from ..engine.derivatives.basis import ResultBasis
from ..engine.derivatives.enums import PricingMethod
from ..engine.derivatives.models import MarketState, ValuationConfig
from ..engine.derivatives.standard.risk import (
    StandardGreekConvention,
    ThetaRollValue,
    calculate_standard_greeks,
)


class GreekConventionDefinitionTest(unittest.TestCase):
    def test_polynomial_recovers_delta_gamma_theta_vega_rho_volga_vanna(self):
        market = MarketState(
            as_of=date(2026, 8, 6),
            spot=100.0,
            volatility=0.20,
            risk_free_rate=0.03,
        )
        basis = ResultBasis(notional=1_000_000.0, currency="CNY")
        config = ValuationConfig(
            method=PricingMethod.BLACK_SCHOLES,
            greek_bumps=(
                ("spot_relative_bump", 0.01),
                ("volatility_absolute_bump", 0.01),
                ("risk_free_rate_absolute_bump", 0.01),
                ("theta_calendar_day_shift", 1),
                ("theta_trading_day_shift", 1),
            ),
        )

        def price(changed: MarketState) -> float:
            return (
                changed.spot**2
                + 3.0 * changed.spot * changed.volatility
                + 5.0 * changed.volatility**2
                + 7.0 * changed.risk_free_rate
            )

        base = price(market)
        core, extended, diagnostics = calculate_standard_greeks(
            base_price_points_100=base,
            market=market,
            config=config,
            basis=basis,
            price_market=price,
            theta_roll=lambda convention: ThetaRollValue(
                price_points_100=(
                    base - 2.0 * convention.theta_calendar_day_shift
                ),
                calendar_day_shift=convention.theta_calendar_day_shift,
                trading_day_shift=convention.theta_trading_day_shift,
                description="independent polynomial theta",
            ),
        )
        expected = {
            "delta": 2.0 * market.spot + 3.0 * market.volatility,
            "gamma": 2.0,
            "theta": -2.0,
            "vega": (3.0 * market.spot + 10.0 * market.volatility) * 0.01,
            "rho": 7.0 * 0.01,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(core[name].value, value, places=10)
                self.assertAlmostEqual(core[name].pv_percent_value, value / 100.0)
                self.assertAlmostEqual(
                    core[name].pv_amount_value, value / 100.0 * 1_000_000.0
                )
        self.assertAlmostEqual(extended["volga"].value, 10.0 * 0.01**2, places=10)
        self.assertAlmostEqual(extended["vanna"].value, 3.0 * 0.01, places=10)
        self.assertEqual(diagnostics["canonical_value_basis"], "PV_POINTS_100")
        self.assertTrue(diagnostics["common_random_numbers_required"])

    def test_invalid_or_ambiguous_bumps_are_rejected(self):
        with self.assertRaises(ValueError):
            StandardGreekConvention(spot_relative_bump=1.0)
        with self.assertRaises(ValueError):
            StandardGreekConvention(volatility_absolute_bump=0.0)
        with self.assertRaises(ValueError):
            StandardGreekConvention(theta_calendar_day_shift=0)
        config = ValuationConfig(
            method=PricingMethod.BLACK_SCHOLES,
            greek_bumps=(("vol_bump", 0.01),),
        )
        with self.assertRaisesRegex(ValueError, "未知字段"):
            StandardGreekConvention.from_valuation_config(config)


if __name__ == "__main__":
    unittest.main()
