"""正式Pricer链路回归：OptionReg适配、基座路由和固定MC随机源。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import resolve_contract
import modules.pricer as public_pricer
from modules.pricer import HistoricalData, PricingConfig, PricingConfigError, PricingInput, PricingInputError, price
from .pricer_test_fixtures import demo_input


class OptionHelperPricingTest(unittest.TestCase):
    def _contract(self, product_id: str, reference: float = 7000.0):
        return resolve_contract(product_id, identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": reference}})

    def test_auto_uses_exact_bsm_and_keeps_market_spot_greek_units(self) -> None:
        result = price(PricingInput(self._contract("2.1"), PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02)))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.method, "black_scholes")
        self.assertGreater(result.pv_amount, 0.0)
        self.assertEqual(result.greeks["delta"].pv_amount_unit, "CNY_per_spot")
        self.assertEqual(result.greeks["delta"].status, "available")

    def test_explicit_mc_uses_frozen_random_source_and_is_repeatable(self) -> None:
        config = PricingConfig(
            spot=7000.0,
            historical_volatility=.2,
            risk_free_rate=.02,
            model_method="monte_carlo",
            path_count=100,
            scenarios=[{"name": "down", "spot_shift": {"000905.SH": -.1}}],
        )
        first = price(PricingInput(self._contract("2.1"), config))
        second = price(PricingInput(self._contract("2.1"), config))
        analytic = price(PricingInput(
            self._contract("2.1"),
            PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02),
        ))
        self.assertEqual(first.method, "monte_carlo")
        self.assertEqual(first.pv_amount, second.pv_amount)
        self.assertIsNotNone(first.standard_error)
        self.assertGreater(first.standard_error, 0.0)
        self.assertLessEqual(abs(first.pv_amount - analytic.pv_amount), 3 * first.standard_error)
        self.assertEqual(first.input_snapshot["numerical_core"], "Pricer.unified.price")
        self.assertEqual(first.input_snapshot["path_pv_sha256_float64"], second.input_snapshot["path_pv_sha256_float64"])
        self.assertEqual(first.scenario_pv[0]["path_pv_sha256_float64"], second.scenario_pv[0]["path_pv_sha256_float64"])
        self.assertEqual(first.market_snapshot["random_source"]["sha256"], "5762ac55cb922bd9dd2cb9ebf7f97857153519bf0405470299b8c4061910b8ed")

    def test_structured_product_uses_shared_optionreg_path(self) -> None:
        result = price(demo_input(self._contract("8.1")))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.method, "monte_carlo")
        self.assertIsNotNone(result.pv_amount)

    def test_hv_market_source_scenarios_and_probability_are_preserved(self) -> None:
        config = PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02, scenarios=[{"name": "down", "spot_shift": {"000905.SH": -.1}}])
        result = price(PricingInput(self._contract("2.2"), config))
        self.assertEqual(result.market_snapshot["source"], "pricing_config")
        self.assertEqual(len(result.scenario_pv), 1)
        self.assertIn("in_the_money", result.probabilities)

    def test_historical_data_drives_spot_hv_and_provenance(self) -> None:
        rows = tuple({
            "date": f"2026-01-{day:02d}",
            "asset_id": "000905.SH",
            "close": 100.0 + day,
            "adj_close": 100.0 * (1.001 ** day) * (1.0 + (0.002 if day % 2 else -0.001)),
        } for day in range(1, 25))
        coverage = {
            "start_date": "2026-01-01", "end_date": "2026-01-24",
            "sessions": [f"2026-01-{day:02d}" for day in range(1, 25)],
            "calendar_id": "local-development", "calendar_version": "test-v1",
        }
        historical = HistoricalData("data/market/test.csv", rows, coverage=coverage, storage_mode="local-development")
        result = price(PricingInput(
            self._contract("2.1", reference=100.0),
            PricingConfig(risk_free_rate=.02, hv_window=20),
            historical_data=historical,
        ))
        self.assertEqual(result.market_snapshot["spot"], {"000905.SH": 124.0})
        self.assertGreater(result.market_snapshot["historical_volatility"]["000905.SH"], 0.0)
        self.assertEqual(result.market_snapshot["source_ref"], historical.source_ref)
        self.assertEqual(result.market_snapshot["data_lineage"]["storage_mode"], "local-development")

    def test_state_and_fixed_random_config_are_strict(self) -> None:
        with self.assertRaises(PricingConfigError):
            PricingConfig(random_seed=7)
        with self.assertRaises(PricingInputError):
            price(PricingInput(
                self._contract("2.1"),
                PricingConfig(spot=7000.0, historical_volatility=.2),
                observed_contract_state={"valuation_date": None, "lifecycle_status": "active", "occurred_events": ["exercise"]},
            ))

    def test_public_module_has_one_pricing_entry(self) -> None:
        self.assertTrue(callable(public_pricer.price))
        self.assertFalse(hasattr(public_pricer, "price_contract"))

    def test_baseline_manifest_locks_existing_files(self) -> None:
        root = PROJECT_ROOT / "modules" / "pricer" / "src" / "engines" / "pricing_core"
        manifest = json.loads((root / "baseline_manifest.json").read_text(encoding="utf-8"))
        for item in ("random_source", "golden_results"):
            payload = (root / manifest[item]["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), manifest[item]["sha256"])


if __name__ == "__main__":
    unittest.main()
