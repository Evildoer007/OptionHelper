"""12.1运行门禁与页面风险数据的独立回归。"""

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
from modules.pricer.model_router import resolve_route
from modules.pricer.observed_state import ObservedContractState, ObservedStateError
from modules.pricer.service import static_assets
from .pricer_test_fixtures import demo_input


class PricerRuntimeGateTest(unittest.TestCase):
    def _contract(self):
        return resolve_contract("2.1", identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": 7000.0}})

    def test_state_router_and_real_risk_grid(self) -> None:
        self.assertEqual(resolve_route("2.1", ("black_scholes", "monte_carlo"), "auto").method, "black_scholes")
        with self.assertRaises(ObservedStateError):
            ObservedContractState.from_value({"valuation_date": "2026-01-01", "unknown": True}, valuation_date="2026-01-01")
        result = price(PricingInput(self._contract(), PricingConfig(spot=7000.0, historical_volatility=.2, risk_free_rate=.02)))
        self.assertEqual({row["key"] for row in result.risk_curves}, {"delta_spot", "gamma_spot", "theta_time", "vega_volatility"})
        self.assertEqual({row["key"] for row in result.risk_surfaces}, {"delta_surface", "gamma_surface", "theta_surface", "vega_surface"})
        self.assertTrue(all(row["points"] for row in result.risk_curves))
        self.assertTrue(all(row["data"] for row in result.risk_surfaces))
        self.assertTrue(result.risk_scenarios)
        self.assertEqual(result.market_snapshot["volatility_source"], "historical_volatility")

    def test_public_greeks_keep_one_structured_type_for_shared_path_mc(self) -> None:
        accumulator = resolve_contract("8.1", identity={"underlyings": ["000905.SH"], "contract_reference_spots": {"000905.SH": 7000.0}})
        result = price(demo_input(accumulator))
        self.assertEqual(result.greeks["delta"].status, "available")
        self.assertIsNotNone(result.greeks["delta"].pv_amount_value)

    def test_mc_risk_grid_reuses_frozen_path_source(self) -> None:
        config = PricingConfig(spot=7000.0, historical_volatility=.2, model_method="monte_carlo", path_count=10, demo_mode=True)
        result = price(PricingInput(self._contract(), config))
        repeat = price(PricingInput(self._contract(), config))
        hashes = [point["path_pv_sha256_float64"] for surface in result.risk_surfaces for point in surface["data"]]
        repeat_hashes = [point["path_pv_sha256_float64"] for surface in repeat.risk_surfaces for point in surface["data"]]
        self.assertTrue(all(hashes))
        self.assertEqual(hashes, repeat_hashes)
        self.assertEqual(result.market_snapshot["random_source"]["sha256"], repeat.market_snapshot["random_source"]["sha256"])

    def test_raw_market_spot_scenarios_are_not_rescaled_by_reference_price(self) -> None:
        reference_spot = 7_443.4332
        contract = resolve_contract(
            "2.1",
            identity={
                "underlyings": ["000905.SH"],
                "contract_start_date": "2026-07-28",
                "contract_reference_spots": {"000905.SH": reference_spot},
            },
        )
        config = PricingConfig(
            valuation_date="2026-07-28", spot=reference_spot, volatility_override=.20,
            risk_free_rate=.02, dividend_yield=.0, time_to_maturity=1.0,
            model_method="monte_carlo", path_count=10, demo_mode=True,
            demo_calendar={
                "calendar_id": "demo-european-vanilla", "calendar_version": "v1",
                "sessions": ["2026-07-28"], "discrete_path": False,
            },
        )
        result = price(PricingInput(contract, config))
        scenario_spots = {
            row["spot_shift"]: row["spot"]
            for row in result.risk_scenarios
            if row["remaining_days"] == 365.0
        }
        self.assertEqual(scenario_spots, {
            -.10: reference_spot * .90,
            0.0: reference_spot,
            .10: reference_spot * 1.10,
        })
        self.assertAlmostEqual(result.pv_amount, 260.21026392060776, places=10)
        self.assertAlmostEqual(result.greeks["delta"].pv_amount_value, .32968970127478836, places=10)
        self.assertAlmostEqual(result.greeks["vega"].pv_amount_value, 9.428256766531986, places=10)

    def test_logo_uses_shared_asset_route_without_copying(self) -> None:
        page = (PROJECT_ROOT / "modules" / "pricer" / "page" / "pricer.html").read_text(encoding="utf-8")
        route = "/icons/optionhelper-logo.svg"
        self.assertIn('src="../../icons/optionhelper-logo.svg"', page)
        self.assertIn('href="../../icons/optionhelper-app-icon-tile-light.svg"', page)
        self.assertIn("identity.underlyings||contract.underlyings", page)
        asset, content_type = static_assets()[route]
        self.assertTrue(asset.is_file())
        self.assertEqual(content_type, "image/svg+xml")


if __name__ == "__main__":
    unittest.main()
