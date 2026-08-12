"""OptionReg全产品离散路径MC门禁。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import load_registry, resolve_contract
from modules.pricer import PricingConfig, PricingInput, price
from modules.pricer.observed_state import ObservedContractState
from modules.pricer.product_pricing_adapter import ProductPricingAdapter, product_mapping
from .pricer_test_fixtures import adapter_market_snapshot, demo_config, demo_input


def _contract_and_config(product_id: str):
    product = load_registry()["products"][product_id]
    assets = ("A", "B") if "S0Vec" in product["terms"] else ("A",)
    contract = resolve_contract(product_id, identity={
        "underlyings": assets,
        "reference_prices": {asset: 100.0 for asset in assets},
    })
    multi = len(assets) > 1
    config = demo_config(assets)
    return contract, config


class AllOptionRegProductsTest(unittest.TestCase):
    def test_all_entry_status_true_products_run_through_one_adapter_and_path_base(self) -> None:
        registry = load_registry()
        executable = [product_id for product_id, product in registry["products"].items() if product["identity"]["entry_status"]]
        self.assertEqual(len(executable), 65)
        for product_id in executable:
            contract, config = _contract_and_config(product_id)
            adapter = ProductPricingAdapter(
                contract, config, adapter_market_snapshot(contract.underlyings),
                ObservedContractState.from_value(None, valuation_date=config.valuation_date),
            )
            result = adapter.reprice()
            self.assertEqual(result.status, "priced", product_id)
            self.assertEqual(result.precision_status, "demo_only", product_id)
            self.assertFalse(result.quote_eligible, product_id)
            self.assertEqual(result.path_count, 10, product_id)
            self.assertIsNotNone(result.pv_amount, product_id)
            self.assertTrue(result.diagnostics.get("path_pv_sha256_float64"), product_id)
            mapping = product_mapping(product_id)
            self.assertEqual(mapping.status, "supported", product_id)

    def test_representative_public_chain_includes_risk_revalue_and_real_spot_axis(self) -> None:
        contract, config = _contract_and_config("5.1")
        result = price(demo_input(contract))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.input_snapshot["numerical_core"], "Pricer.unified.price")
        self.assertEqual(len(result.risk_curves), 4)
        points = result.risk_curves[0]["points"]
        self.assertEqual([round(point["x"], 8) for point in points], [90.0, 95.0, 100.0, 105.0, 110.0])

    def test_multi_asset_risk_is_explicit_parallel_factor(self) -> None:
        contract, config = _contract_and_config("9.18")
        result = price(demo_input(contract))
        self.assertEqual(result.diagnostics["spot_risk_factor"], "parallel_proportional_all_assets")
        self.assertIn("平行比例", result.risk_curves[0]["x_axis"]["name"])

    def test_readme_style_independent_snowball_mc10_still_runs(self) -> None:
        directory = PROJECT_ROOT / "modules" / "pricer" / "src" / "engines" / "pricing_core"
        code = """
from main import price_option
from tests.support import autocall_parameters
parameters = autocall_parameters('SNOWBALL')
parameters['config']['paths'] = 10
run = price_option('AUTOCALL', 'SNOWBALL', parameters, 'MONTE_CARLO_CPU', output='NONE')
print(run.result.pv_amount)
"""
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=directory, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip())


if __name__ == "__main__":
    unittest.main()
