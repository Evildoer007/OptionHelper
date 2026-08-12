"""Pricer对ResolvedContract与PricingConfig的回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import resolve_contract
from modules.pricer import PricingConfig, PricingInput, price
from .pricer_test_fixtures import demo_input


def call_contract(*, product_id: str = "2.1", **overrides):
    return resolve_contract(
        product_id,
        identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": 100.0}},
        term_overrides=overrides,
    )


class PricingCoreTest(unittest.TestCase):
    def test_bsm_european_call_uses_resolved_contract_and_returns_greeks(self) -> None:
        contract = call_contract(T=1.0, K=100.0, Pi_0=0.0)
        result = price(PricingInput(contract, PricingConfig(spot=100.0, historical_volatility=0.20, risk_free_rate=0.05, model_method="black_scholes")))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.method, "black_scholes")
        self.assertAlmostEqual(result.pv, 10.450583572, places=6)
        self.assertAlmostEqual(result.greeks["delta"].pv_amount_value, 0.6368, places=3)
        self.assertIsNone(result.standard_error)
        self.assertIn("contract", result.input_snapshot)

    def test_relative_contract_normalizes_valuation_spot_by_contract_reference(self) -> None:
        contract = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": 7000.0}},
            term_overrides={"K": 95.0, "Pi_0": 0.0},
        )
        result = price(PricingInput(contract, PricingConfig(spot=8000.0, historical_volatility=0.2, model_method="black_scholes")))
        self.assertEqual(result.market_snapshot["normalized_spot"], {"000905.SH": 8000.0 / 7000.0 * 100.0})
        self.assertGreater(result.pv, 0.0)

    def test_closed_form_returns_real_cash_value_after_100_base_normalization(self) -> None:
        normalized = call_contract(T=1.0, K=95.0, Pi_0=0.0)
        normalized_result = price(PricingInput(normalized, PricingConfig(spot=8000.0 / 7000.0 * 100.0, historical_volatility=0.2, model_method="black_scholes")))
        raw = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": 7000.0}},
            term_overrides={"T": 1.0, "K": 95.0, "Pi_0": 0.0},
        )
        raw_result = price(PricingInput(raw, PricingConfig(spot=8000.0, historical_volatility=0.2, model_method="black_scholes")))
        self.assertAlmostEqual(raw_result.pv, normalized_result.pv * 70.0)
        self.assertAlmostEqual(raw_result.greeks["delta"].pv_amount_value, normalized_result.greeks["delta"].pv_amount_value)
        self.assertAlmostEqual(raw_result.greeks["gamma"].pv_amount_value, normalized_result.greeks["gamma"].pv_amount_value / 70.0)
        self.assertAlmostEqual(raw_result.greeks["vega"].pv_amount_value, normalized_result.greeks["vega"].pv_amount_value * 70.0)

    def test_closed_form_portfolio_uses_explicit_base_legs(self) -> None:
        contract = call_contract(product_id="4.2", T=0.5, Kp=90.0, Kc=110.0)
        result = price(PricingInput(contract, PricingConfig(spot=100.0, historical_volatility=0.2, risk_free_rate=0.01, model_method="auto")))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.diagnostics["engine"], "standard.airbag")

    def test_barrier_uses_shared_optionreg_monte_carlo(self) -> None:
        contract = call_contract(product_id="5.1", T=0.25)
        result = price(demo_input(contract))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.method, "monte_carlo")

    def test_closed_form_is_rejected_when_product_has_no_accurate_bsm_binding(self) -> None:
        contract = call_contract(product_id="5.1")
        result = price(PricingInput(contract, PricingConfig(spot=100.0, historical_volatility=0.2, model_method="black_scholes")))
        self.assertEqual(result.status, "unsupported")

    def test_accumulator_uses_shared_optionreg_monte_carlo(self) -> None:
        contract = call_contract(product_id="8.1", n_obs=3, Llock=1)
        result = price(demo_input(contract))
        self.assertEqual(result.status, "priced")


if __name__ == "__main__":
    unittest.main()
