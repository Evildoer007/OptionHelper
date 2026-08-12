"""ProductPricingAdapter回归：正式网页链只经Pricer统一数值入口。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import resolve_contract
from modules.pricer import PricingConfig, PricingInput, price
from modules.pricer.product_pricing_adapter import ProductPricingAdapter, product_mapping
from .pricer_test_fixtures import demo_input


class ProductPricingAdapterTest(unittest.TestCase):
    def _contract(self, product_id: str):
        return resolve_contract(product_id, identity={
            "underlyings": ["000905.SH"],
            "contract_reference_spots": {"000905.SH": 7000.0},
        })

    def test_vanilla_pv_and_risk_revaluations_use_main_entry(self) -> None:
        config = PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02)
        with patch("modules.pricer.product_pricing_adapter.price_option", wraps=__import__("modules.pricer.product_pricing_adapter", fromlist=["price_option"]).price_option) as entry:
            result = price(PricingInput(self._contract("2.1"), config))
        self.assertEqual(result.input_snapshot["numerical_core"], "Pricer.unified.price")
        self.assertGreater(entry.call_count, 1)  # PV、Greek/曲线/曲面均经同一入口重估。
        self.assertTrue(all(call.args[:2] == ("VANILLA", "EUROPEAN_VANILLA") for call in entry.call_args_list))
        self.assertTrue(all(call.kwargs["output"] == "NONE" for call in entry.call_args_list))

    def test_optionreg_path_mapping_is_advertised_for_executable_registry_entries(self) -> None:
        self.assertEqual(set(ProductPricingAdapter.__dict__.get("_EXACT_PRODUCT_IDS", ())), {"2.1", "2.2", "3.1", "3.2", "3.3", "3.4", "4.1", "4.2", "4.3", "4.4"})
        barrier = price(demo_input(self._contract("5.1")))
        self.assertEqual(barrier.status, "priced")
        self.assertEqual(barrier.method, "monte_carlo")

    def test_all_optionreg_families_share_the_formal_path_base(self) -> None:
        for product_id in ("5.1", "6.1", "7.1", "8.1", "9.1", "10.1"):
            mapping = product_mapping(product_id)
            self.assertEqual(mapping.family, "OPTIONREG")
            self.assertEqual(mapping.structure, "OPTIONREG_PATH")
            self.assertEqual(mapping.status, "supported")

    def test_all_european_portfolios_use_airbag_base_and_publish_risk(self) -> None:
        config = PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02)
        for product_id in ("3.1", "3.2", "3.3", "3.4", "4.1", "4.2", "4.3", "4.4"):
            result = price(PricingInput(self._contract(product_id), config))
            self.assertEqual(result.status, "priced", product_id)
            self.assertEqual(result.diagnostics["engine"], "standard.airbag", product_id)
            self.assertEqual(len(result.risk_curves), 4, product_id)

    def test_european_portfolio_mc_uses_common_path_interpreter(self) -> None:
        result = price(demo_input(self._contract("3.1")))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.version, "standard-optionreg-discrete-mc-1")


if __name__ == "__main__":
    unittest.main()
