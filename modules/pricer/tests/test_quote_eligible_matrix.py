"""OptionReg全产品定价链路与正式报价精度门禁回归。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import load_registry, resolve_contract
from modules.pricer import PricingConfig, PricingInput, price

from .pricer_test_fixtures import calendar_asset, market_asset


def _quote_config(assets: tuple[str, ...]) -> PricingConfig:
    multi_asset = len(assets) > 1
    return PricingConfig(
        valuation_date="2023-07-28",
        spot={asset: 100.0 + index * 5.0 for index, asset in enumerate(assets)} if multi_asset else 100.0,
        historical_volatility={asset: .20 for asset in assets} if multi_asset else .20,
        dividend_yield={asset: 0.0 for asset in assets} if multi_asset else .0,
        correlation=[
            [1.0 if left == right else .25 for right in range(len(assets))]
            for left in range(len(assets))
        ] if multi_asset else None,
        model_method="monte_carlo",
        path_count=11,
        demo_mode=False,
        risk_free_rate=.02,
    )


class QuoteEligibleMatrixTest(unittest.TestCase):
    def test_all_entry_products_are_priced_but_mc11_is_not_quote_eligible(self) -> None:
        registry = load_registry()
        executable = [
            product_id for product_id, product in registry["products"].items()
            if product["identity"]["entry_status"]
        ]
        self.assertEqual(len(executable), 65)

        for product_id in executable:
            terms = registry["products"][product_id]["terms"]
            assets = ("A", "B") if "S0Vec" in terms else ("A",)
            contract = resolve_contract(
                product_id,
                identity={
                    "underlyings": assets,
                    "reference_prices": {asset: 100.0 for asset in assets},
                },
            )
            history, data_ref = market_asset(assets)
            calendar, calendar_ref = calendar_asset(assets)
            result = price(PricingInput(
                contract=contract,
                pricing_config=_quote_config(assets),
                historical_data=history,
                market_data_refs=(data_ref,),
                trading_calendar_data=calendar,
                trading_calendar_ref=calendar_ref,
            ))
            self.assertEqual(result.status, "priced", product_id)
            self.assertIsNotNone(result.pv_amount, product_id)
            self.assertEqual(result.precision_status, "low_precision", product_id)
            self.assertFalse(result.quote_eligible, product_id)
            self.assertEqual(result.path_count, 11, product_id)
            self.assertIn("quote_precision_gate", result.diagnostics, product_id)


if __name__ == "__main__":
    unittest.main()
