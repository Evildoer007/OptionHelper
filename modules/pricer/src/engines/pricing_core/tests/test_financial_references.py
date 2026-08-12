from __future__ import annotations

from copy import deepcopy
import math
from statistics import NormalDist
import unittest

from .. import main

from .support import BASIS, MARKET, case_by_structure


def _price(family, structure, contract, method, market=None):
    return main.price_option(
        family,
        structure,
        {"contract": contract, "market": deepcopy(MARKET if market is None else market)},
        method,
        output="NONE",
    )


class VanillaReferenceTest(unittest.TestCase):
    def test_black_scholes_price_and_core_greeks(self):
        spot, strike, maturity = 100.0, 103.0, 0.75
        volatility, rate, dividend = 0.27, 0.03, 0.01
        run = _price(
            "VANILLA",
            "EUROPEAN_VANILLA",
            {
                "strike": strike,
                "maturity_years": maturity,
                "call_put": "CALL",
                "future": False,
                "basis": deepcopy(BASIS),
            },
            "BLACK_SCHOLES",
            {
                **MARKET,
                "spot": spot,
                "volatility": volatility,
                "risk_free_rate": rate,
                "dividend_yield": dividend,
            },
        )
        root_time = math.sqrt(maturity)
        d1 = (
            math.log(spot / strike)
            + (rate - dividend + 0.5 * volatility**2) * maturity
        ) / (volatility * root_time)
        d2 = d1 - volatility * root_time
        normal = NormalDist()
        density = normal.pdf(d1)
        discount_spot = math.exp(-dividend * maturity)
        discount_strike = math.exp(-rate * maturity)
        price = (
            spot * discount_spot * normal.cdf(d1)
            - strike * discount_strike * normal.cdf(d2)
        )
        expected = {
            "Delta": discount_spot * normal.cdf(d1),
            "Gamma": discount_spot * density / (spot * volatility * root_time),
            "Theta": (
                -spot * discount_spot * density * volatility / (2.0 * root_time)
                + dividend * spot * discount_spot * normal.cdf(d1)
                - rate * strike * discount_strike * normal.cdf(d2)
            ) / 365.0,
            "Vega": spot * discount_spot * density * root_time * 0.01,
            "Rho": strike * maturity * discount_strike * normal.cdf(d2) * 0.01,
        }
        self.assertAlmostEqual(run.result.pv_points_100, price, places=12)
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(run.result.greeks[name].value, value, places=12)

    def test_black_76_price_and_core_greeks(self):
        future, strike, maturity = 105.0, 100.0, 0.75
        volatility, rate = 0.24, 0.035
        run = _price(
            "VANILLA",
            "EUROPEAN_VANILLA",
            {
                "strike": strike,
                "maturity_years": maturity,
                "call_put": "CALL",
                "future": True,
                "basis": deepcopy(BASIS),
            },
            "BLACK_SCHOLES",
            {
                **MARKET,
                "spot": future,
                "volatility": volatility,
                "risk_free_rate": rate,
            },
        )
        root_time = math.sqrt(maturity)
        d1 = (
            math.log(future / strike) + 0.5 * volatility**2 * maturity
        ) / (volatility * root_time)
        d2 = d1 - volatility * root_time
        normal = NormalDist()
        density = normal.pdf(d1)
        discount = math.exp(-rate * maturity)
        price = discount * (
            future * normal.cdf(d1) - strike * normal.cdf(d2)
        )
        expected = {
            "Delta": discount * normal.cdf(d1),
            "Gamma": discount * density / (future * volatility * root_time),
            "Theta": (
                -future * discount * density * volatility / (2.0 * root_time)
                + rate * price
            ) / 365.0,
            "Vega": future * discount * density * root_time * 0.01,
            "Rho": -maturity * price * 0.01,
        }
        self.assertAlmostEqual(run.result.pv_points_100, price, places=12)
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(run.result.greeks[name].value, value, places=12)

    def test_explicit_carry_controls_price_and_rho_holds_carry_fixed(self):
        spot, strike, maturity = 100.0, 98.0, 0.5
        volatility, rate, carry = 0.22, 0.04, 0.015
        market = {
            **MARKET,
            "spot": spot,
            "volatility": volatility,
            "risk_free_rate": rate,
            "dividend_yield": 0.99,
            "carry": carry,
        }
        run = _price(
            "VANILLA",
            "EUROPEAN_VANILLA",
            {
                "strike": strike,
                "maturity_years": maturity,
                "call_put": "CALL",
                "basis": deepcopy(BASIS),
            },
            "BLACK_SCHOLES",
            market,
        )
        root_time = math.sqrt(maturity)
        d1 = (
            math.log(spot / strike) + (carry + 0.5 * volatility**2) * maturity
        ) / (volatility * root_time)
        d2 = d1 - volatility * root_time
        normal = NormalDist()
        price = (
            spot * math.exp((carry - rate) * maturity) * normal.cdf(d1)
            - strike * math.exp(-rate * maturity) * normal.cdf(d2)
        )
        self.assertAlmostEqual(run.result.pv_points_100, price, places=12)
        self.assertAlmostEqual(
            run.result.greeks["Rho"].value,
            -maturity * price * 0.01,
            places=12,
        )
        self.assertEqual(
            run.result.greeks["Rho"].bump_details["held_constant"], "carry"
        )


class StructuralIdentityTest(unittest.TestCase):
    def test_explicit_carry_reproduces_same_base_pv_for_all_spot_models(self):
        from .support import representative_cases

        for family, structure, parameters, method in representative_cases():
            base = main.price_option(
                family, structure, parameters, method, output="NONE"
            )
            explicit = deepcopy(parameters)
            market = explicit["market"]
            market["carry"] = market["risk_free_rate"] - market["dividend_yield"]
            market["dividend_yield"] = 0.99
            changed = main.price_option(
                family, structure, explicit, method, output="NONE"
            )
            with self.subTest(structure=structure):
                self.assertAlmostEqual(
                    changed.result.pv_points_100,
                    base.result.pv_points_100,
                    places=12,
                )

    def test_cash_and_asset_binary_call_put_parities(self):
        maturity = 0.75
        payout = 10.0
        cash = []
        asset = []
        for call_put in ("CALL", "PUT"):
            cash.append(
                _price(
                    "DIGITAL",
                    "BINARY",
                    {
                        "strike": 100.0,
                        "maturity_years": maturity,
                        "call_put": call_put,
                        "payout_type": "Cash-or-Nothing",
                        "payout": payout,
                        "basis": deepcopy(BASIS),
                    },
                    "BINARY_ANALYTIC",
                )
            )
            asset.append(
                _price(
                    "DIGITAL",
                    "BINARY",
                    {
                        "strike": 100.0,
                        "maturity_years": maturity,
                        "call_put": call_put,
                        "payout_type": "Asset-or-Nothing",
                        "basis": deepcopy(BASIS),
                    },
                    "BINARY_ANALYTIC",
                )
            )
        self.assertAlmostEqual(
            sum(run.result.pv_points_100 for run in cash),
            payout * math.exp(-MARKET["risk_free_rate"] * maturity),
            places=12,
        )
        self.assertAlmostEqual(
            sum(run.result.pv_points_100 for run in asset),
            MARKET["spot"] * math.exp(-MARKET["dividend_yield"] * maturity),
            places=12,
        )

    def test_barrier_in_plus_out_equals_vanilla(self):
        maturity = 0.75
        for call_put, barrier in (("CALL", 120.0), ("PUT", 80.0)):
            common = {
                "strike": 100.0,
                "barrier": barrier,
                "maturity_years": maturity,
                "call_put": call_put,
                "monitoring": "Continuous",
                "rebate": 0.0,
                "rebate_at_hit": True,
                "basis": deepcopy(BASIS),
            }
            knock_in = _price(
                "BARRIER", "BARRIER", {**common, "knock": "In"},
                "REINER_RUBINSTEIN",
            )
            knock_out = _price(
                "BARRIER", "BARRIER", {**common, "knock": "Out"},
                "REINER_RUBINSTEIN",
            )
            vanilla = _price(
                "VANILLA",
                "EUROPEAN_VANILLA",
                {
                    "strike": 100.0,
                    "maturity_years": maturity,
                    "call_put": call_put,
                    "basis": deepcopy(BASIS),
                },
                "BLACK_SCHOLES",
            )
            with self.subTest(call_put=call_put):
                self.assertAlmostEqual(
                    knock_in.result.pv_points_100 + knock_out.result.pv_points_100,
                    vanilla.result.pv_points_100,
                    places=11,
                )
                for name, tolerance in {
                    "Delta": 2e-4,
                    "Gamma": 2e-5,
                    "Theta": 2e-4,
                    "Vega": 2e-4,
                    "Rho": 2e-4,
                }.items():
                    combined = (
                        knock_in.result.greeks[name].value
                        + knock_out.result.greeks[name].value
                    )
                    self.assertLessEqual(
                        abs(combined - vanilla.result.greeks[name].value),
                        tolerance,
                    )

    def test_airbag_equals_explicit_weighted_legs_for_pv_and_greeks(self):
        _, _, parameters, _ = case_by_structure("AIRBAG")
        airbag = main.price_option(
            "AIRBAG", "AIRBAG", parameters, "STATIC_REPLICATION", output="NONE"
        )
        components = []
        for leg in parameters["contract"]["legs"]:
            family = "VANILLA" if leg["structure"] == "EUROPEAN_VANILLA" else "BARRIER"
            component = main.price_option(
                family,
                leg["structure"],
                {"contract": leg["contract"], "market": parameters["market"]},
                leg["method"],
                output="NONE",
            )
            components.append((leg["weight"], component))
        self.assertAlmostEqual(
            airbag.result.pv_points_100,
            sum(weight * run.result.pv_points_100 for weight, run in components),
            places=12,
        )
        for name in ("Delta", "Gamma", "Theta", "Vega", "Rho"):
            self.assertAlmostEqual(
                airbag.result.greeks[name].value,
                sum(weight * run.result.greeks[name].value for weight, run in components),
                places=11,
            )


if __name__ == "__main__":
    unittest.main()
