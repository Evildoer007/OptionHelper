"""正式报价精度和存续路径状态门禁回归。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import resolve_contract
from modules.pricer import PricingConfig, PricingInput, price
from modules.pricer.engines.pricing_core.engine.derivatives.results import PricingResult
from modules.pricer.precision_gate import assess_quote_precision

from .pricer_test_fixtures import calendar_asset, market_asset


def _contract(product_id: str, *, start_date: str | None = None):
    identity = {
        "underlyings": ("A",),
        "reference_prices": {"A": 100.0},
    }
    if start_date is not None:
        identity["contract_start_date"] = start_date
    return resolve_contract(product_id, identity=identity)


def _mc_input(product_id: str, *, path_count: int, start_date: str | None = None, state=None) -> PricingInput:
    historical, data_ref = market_asset(("A",))
    contract = _contract(product_id, start_date=start_date)
    calendar, calendar_ref = calendar_asset(("A",))
    return PricingInput(
        contract=contract,
        pricing_config=PricingConfig(
            valuation_date="2023-07-28",
            spot=100.0,
            historical_volatility=0.20,
            volatility_override=0.20,
            dividend_yield=0.0,
            model_method="monte_carlo",
            path_count=path_count,
            risk_free_rate=0.02,
        ),
        historical_data=historical,
        market_data_refs=(data_ref,),
        trading_calendar_data=calendar if contract.terms.get("monitor") else None,
        trading_calendar_ref=calendar_ref if contract.terms.get("monitor") else None,
        observed_contract_state=state,
    )


class PrecisionAndStateGateTest(unittest.TestCase):
    def test_precision_gate_accepts_only_sufficient_paths_and_relative_error(self) -> None:
        priced = PricingResult(
            pv_amount=100.0,
            pv_percent=0.10,
            pv_points_100=10.0,
            currency="CNY",
            greeks={},
            method="monte_carlo",
            version="test",
            standard_error=4.0,
        )

        decision = assess_quote_precision(
            priced,
            method="monte_carlo",
            path_count=1_000,
            demo_mode=False,
        )
        self.assertEqual(decision.status, "quote_eligible")
        self.assertTrue(decision.eligible)

    def test_zero_pv_relative_error_is_not_serialized_as_infinity(self) -> None:
        priced = PricingResult(
            pv_amount=0.0,
            pv_percent=0.0,
            pv_points_100=0.0,
            currency="CNY",
            greeks={},
            method="monte_carlo",
            version="test",
            standard_error=0.1,
        )

        decision = assess_quote_precision(
            priced,
            method="monte_carlo",
            path_count=2_000,
            demo_mode=False,
        )
        self.assertEqual(decision.status, "low_precision")
        self.assertIsNone(decision.diagnostics["relative_standard_error"])

    def test_mc11_is_not_quote_eligible_even_when_relative_se_is_acceptable(self) -> None:
        result = price(_mc_input("5.1", path_count=11))

        self.assertEqual(result.status, "priced")
        self.assertAlmostEqual(result.pv_amount, -4.829647843071477)
        self.assertAlmostEqual(result.standard_error, 0.17035215692852268)
        self.assertEqual(result.precision_status, "low_precision")
        self.assertFalse(result.quote_eligible)
        gate = result.diagnostics["quote_precision_gate"]
        self.assertEqual(gate["minimum_path_count"], 1_000)
        self.assertEqual(gate["maximum_relative_standard_error"], 0.05)
        self.assertLess(gate["relative_standard_error"], 0.05)
        self.assertEqual(gate["reasons"], ["path_count_below_minimum"])

    def test_midlife_path_contract_requires_observed_state(self) -> None:
        result = price(_mc_input("5.1", path_count=100, start_date="2023-07-27"))

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.precision_status, "not_priced")
        self.assertFalse(result.quote_eligible)
        self.assertIn("存续期路径结构必须提供observed_contract_state", result.messages[0])

    def test_midlife_path_contract_accepts_explicit_active_state(self) -> None:
        result = price(_mc_input(
            "5.1",
            path_count=11,
            start_date="2023-07-27",
            state={"valuation_date": "2023-07-28", "lifecycle_status": "active"},
        ))

        self.assertEqual(result.status, "priced")

    def test_midlife_european_contract_does_not_require_path_state(self) -> None:
        historical, data_ref = market_asset(("A",))
        result = price(PricingInput(
            _contract("2.1", start_date="2023-07-27"),
            PricingConfig(
                valuation_date="2023-07-28",
                spot=100.0,
                historical_volatility=0.20,
                risk_free_rate=0.02,
                model_method="black_scholes",
            ),
            historical,
            (data_ref,),
        ))

        self.assertEqual(result.status, "priced")
        self.assertTrue(result.quote_eligible)

    def test_midlife_terminal_only_mc_contract_does_not_require_path_state(self) -> None:
        result = price(_mc_input("6.1", path_count=11, start_date="2023-07-27"))

        self.assertEqual(result.status, "priced")
        self.assertEqual(result.precision_status, "low_precision")


if __name__ == "__main__":
    unittest.main()
