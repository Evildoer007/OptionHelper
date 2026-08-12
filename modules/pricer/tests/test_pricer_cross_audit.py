"""Pricer交叉审计发现的协议与金融语义回归。"""

from __future__ import annotations

from dataclasses import replace
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
from modules.pricer.models import validate_market_data_asset
from modules.pricer.engines.pricing_core.engine.derivatives.models import ValuationState
from modules.pricer.engines.pricing_core.engine.derivatives.standard.optionreg_path import _contract_for_state
from modules.pricer.engines.pricing_core.optionhelper_core import PricingInputError
from modules.pricer import service

from .pricer_test_fixtures import calendar_asset, demo_config, market_asset


def _path_input(contract, config, history, data_ref, state):
    calendar, calendar_ref = calendar_asset(
        tuple(contract.underlyings),
        start_date=str(config.valuation_date),
    )
    return PricingInput(
        contract=contract,
        pricing_config=config,
        historical_data=history,
        market_data_refs=(data_ref,),
        trading_calendar_data=calendar,
        trading_calendar_ref=calendar_ref,
        observed_contract_state=state,
    )


class PricerCrossAuditTest(unittest.TestCase):
    def test_closed_form_time_zero_premium_keeps_currency_amount_unit(self) -> None:
        identity = {"underlyings": ["A"], "reference_prices": {"A": 7_443.4332}}
        free = resolve_contract("2.1", identity=identity, term_overrides={"Pi_0": 0.0})
        paid = resolve_contract("2.1", identity=identity, term_overrides={"Pi_0": 1.0})
        config = PricingConfig(spot=7_443.4332, historical_volatility=.20, risk_free_rate=.02)
        free_result = price(PricingInput(free, config))
        paid_result = price(PricingInput(paid, config))
        self.assertAlmostEqual(paid_result.pv_amount - free_result.pv_amount, -1.0, places=12)

    def test_page_mc10_demo_does_not_secretly_read_default_history(self) -> None:
        body = {
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "contract_start_date": "2026-07-28",
                "contract_reference_spots": {"000905.SH": 7_443.4332},
            },
            "term_overrides": {},
            "pricing_config": {
                "valuation_date": "2026-07-28",
                "spot": 7_443.4332,
                "volatility_override": 0.20,
                "risk_free_rate": 0.02,
                "dividend_yield": 0.0,
                "time_to_maturity": 1.0,
                "model_method": "monte_carlo",
                "path_count": 10,
                "demo_mode": True,
                "demo_calendar": {
                    "calendar_id": "demo-european-vanilla",
                    "calendar_version": "v1",
                    "sessions": ["2026-07-28"],
                    "discrete_path": False,
                },
            },
        }
        runtime = service.PricerRuntime()
        with (
            patch.object(runtime, "_history_asset", wraps=runtime._history_asset) as history_loader,
            patch.object(service, "_write_run"),
        ):
            output = runtime.run(body)

        self.assertEqual(output["pricing"]["precision_status"], "demo_only")
        self.assertFalse(output["pricing"]["quote_eligible"])
        history_loader.assert_not_called()
        self.assertEqual(output["market_snapshot"]["source"], "explicit_demo_market_snapshot")
        self.assertEqual(output["market_snapshot"]["data_lineage"]["mode"], "explicit_demo_only")

    def test_risk_vega_axes_use_same_one_percent_unit_as_public_greek(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["A"], "reference_prices": {"A": 100.0}})
        result = price(PricingInput(contract, PricingConfig(spot=100.0, historical_volatility=.20, risk_free_rate=.02)))
        curve = next(item for item in result.risk_curves if item["key"] == "vega_volatility")
        surface = next(item for item in result.risk_surfaces if item["key"] == "vega_surface")
        self.assertEqual(result.greeks["vega"].pv_amount_unit, "CNY_per_1pct_volatility")
        self.assertEqual(curve["y_axis"]["unit"], result.greeks["vega"].pv_amount_unit)
        self.assertEqual(surface["z_axis"]["unit"], result.greeks["vega"].pv_amount_unit)

    def test_scenario_rejects_wrong_asset_coordinate(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["A"], "reference_prices": {"A": 100.0}})
        config = PricingConfig(
            spot=100.0,
            historical_volatility=.20,
            risk_free_rate=.02,
            scenarios=[{"name": "wrong_asset", "spot_shift": {"B": -.10}}],
        )
        with self.assertRaisesRegex(PricingInputError, "spot_shift.*标的|A"):
            price(PricingInput(contract, config))

    def test_unimplemented_historical_event_is_rejected_not_ignored(self) -> None:
        contract = resolve_contract(
            "9.14",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
        )
        history, data_ref = market_asset(("A",))
        state = {
            "valuation_date": "2023-07-28",
            "lifecycle_status": "active",
            "occurred_events": [{
                "event_type": "reset",
                "event_date": "2023-06-30",
                "observation_stage": 100,
            }],
            "realized_cashflows": [],
            "source_refs": ["module-run:reset-audit"],
        }
        result = price(_path_input(contract, demo_config(("A",)), history, data_ref, state))
        self.assertEqual(result.status, "unsupported")
        self.assertFalse(result.quote_eligible)
        self.assertRegex(" ".join(result.messages), "reset|未实现|不支持")

    def test_terminal_lifecycle_cannot_restart_future_path_simulation(self) -> None:
        contract = resolve_contract(
            "9.1",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
        )
        history, data_ref = market_asset(("A",))
        state = {
            "valuation_date": "2023-07-28",
            "lifecycle_status": "terminated",
            "occurred_events": [{
                "event_type": "manual_termination",
                "event_date": "2023-06-30",
                "observation_stage": 100,
            }],
            "realized_cashflows": [{
                "payment_date": "2023-06-30",
                "amount": 101.0,
                "status": "paid",
            }],
            "source_refs": ["module-run:termination-audit"],
        }
        result = price(_path_input(contract, demo_config(("A",)), history, data_ref, state))
        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(result.pv_amount)

    def test_midlife_initial_state_cannot_bypass_observed_state_gate(self) -> None:
        contract = resolve_contract(
            "9.1",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
        )
        history, data_ref = market_asset(("A",))
        result = price(_path_input(
            contract, demo_config(("A",)), history, data_ref,
            {"valuation_date": "2023-07-28", "lifecycle_status": "initial"},
        ))
        self.assertEqual(result.status, "unsupported")
        self.assertRegex(" ".join(result.messages), "initial|存续")

    def test_remaining_observation_stage_rebases_ordinal_terms(self) -> None:
        _, data_ref = market_asset(("A",))
        identity = {
            "underlyings": ["A"],
            "reference_prices": {"A": 100.0},
            "contract_start_date": "2023-01-30",
        }
        accumulator = resolve_contract("8.1", identity=identity, trading_dates=data_ref.coverage["sessions"])
        early_profit = resolve_contract("9.12", identity=identity, trading_dates=data_ref.coverage["sessions"])
        descending = resolve_contract("9.9", identity=identity, trading_dates=data_ref.coverage["sessions"])
        hedge = resolve_contract("9.15", identity=identity, trading_dates=data_ref.coverage["sessions"])
        state = ValuationState(calendar_day=180, observation_stage=120)
        sessions = tuple(data_ref.coverage["sessions"])
        accumulator_remaining = _contract_for_state(accumulator, state, sessions)
        early_profit_remaining = _contract_for_state(early_profit, state, sessions)
        descending_remaining = _contract_for_state(descending, state, sessions)
        hedge_remaining = _contract_for_state(hedge, state, sessions)
        self.assertEqual(accumulator_remaining.terms["Llock"], 0)
        self.assertEqual(early_profit_remaining.terms["coupon_switch_observation"], 6)
        self.assertEqual(descending_remaining.terms["ko_barrier_schedule"][0], (1, 100.0))
        self.assertEqual(hedge_remaining.terms["hedge_schedule"], ((2, 80.0), (10, 75.0)))

    def test_historical_coupon_count_enters_remaining_phoenix_cashflow(self) -> None:
        historical, data_ref = market_asset(("A",))
        contract = resolve_contract(
            "9.19",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
            trading_dates=data_ref.coverage["sessions"],
        )

        def state(count: int) -> dict[str, object]:
            return {
                "valuation_date": "2023-07-28",
                "lifecycle_status": "active",
                "occurred_events": [{
                    "event_type": "observation_checkpoint",
                    "event_date": "2023-07-28",
                    "observation_stage": 120,
                    "accumulated_count": count,
                }],
                "realized_cashflows": [],
                "source_refs": [f"module-run:coupon-count-{count}"],
            }

        without_coupon = price(_path_input(contract, demo_config(("A",)), historical, data_ref, state(0)))
        with_coupons = price(_path_input(contract, demo_config(("A",)), historical, data_ref, state(5)))
        self.assertEqual(without_coupon.status, "priced")
        self.assertEqual(with_coupons.status, "priced")
        self.assertNotEqual(without_coupon.pv_amount, with_coupons.pv_amount)

    def test_midlife_range_accrual_is_rejected_until_full_state_is_supported(self) -> None:
        historical, data_ref = market_asset(("A",))
        contract = resolve_contract(
            "10.8",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
            trading_dates=data_ref.coverage["sessions"],
        )
        state = {
            "valuation_date": "2023-02-10",
            "lifecycle_status": "active",
            "occurred_events": [{
                "event_type": "observation_checkpoint",
                "event_date": "2023-02-10",
                "observation_stage": 9,
                "accumulated_count": 5,
            }],
            "realized_cashflows": [],
            "source_refs": ["module-run:range-accrual-state"],
        }
        result = price(_path_input(
            contract,
            replace(demo_config(("A",)), valuation_date="2023-02-10"),
            historical,
            data_ref,
            state,
        ))
        self.assertEqual(result.status, "unsupported")
        self.assertRegex(" ".join(result.messages), "区间计息|n_in|历史")

    def test_midlife_accumulator_requires_explicit_historical_quantity(self) -> None:
        historical, data_ref = market_asset(("A",))
        contract = resolve_contract(
            "8.1",
            identity={
                "underlyings": ["A"],
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
            trading_dates=data_ref.coverage["sessions"],
        )
        state = {
            "valuation_date": "2023-07-28",
            "lifecycle_status": "active",
            "occurred_events": [{
                "event_type": "observation_checkpoint",
                "event_date": "2023-07-28",
                "observation_stage": 120,
            }],
            "realized_cashflows": [],
            "source_refs": ["module-run:accumulator-missing-quantity"],
        }
        result = price(_path_input(contract, demo_config(("A",)), historical, data_ref, state))
        self.assertEqual(result.status, "unsupported")
        self.assertRegex(" ".join(result.messages), "accumulated_quantity|累计数量")

    def test_vanna_currency_amount_uses_real_spot_coordinate(self) -> None:
        values = []
        for reference in (100.0, 7_000.0):
            contract = resolve_contract("2.1", identity={"underlyings": ["A"], "reference_prices": {"A": reference}})
            result = price(PricingInput(
                contract,
                PricingConfig(spot=reference, historical_volatility=.20, risk_free_rate=.02),
            ))
            values.append(result.extended_greeks["vanna"])
        self.assertEqual(values[0].pv_amount_unit, "CNY_per_spot_per_1pct_volatility")
        self.assertAlmostEqual(values[0].pv_amount_value, values[1].pv_amount_value, places=15)

    def test_data_asset_by_asset_coverage_cannot_name_another_asset(self) -> None:
        historical, data_ref = market_asset(("A",))
        bad_coverage = {
            **dict(data_ref.coverage),
            "by_asset": {"WRONG": {"start": "2021-01-04", "end": "2026-07-31"}},
        }
        with self.assertRaisesRegex(ValueError, "by_asset.*标的|A"):
            validate_market_data_asset(
                replace(data_ref, coverage=bad_coverage),
                replace(historical, coverage=bad_coverage),
                ("A",),
            )

    def test_verified_history_supplies_hv_unless_override_is_explicit(self) -> None:
        historical, data_ref = market_asset(("A",))
        contract = resolve_contract("2.1", identity={"underlyings": ["A"], "reference_prices": {"A": 100.0}})
        result = price(PricingInput(
            contract,
            PricingConfig(
                valuation_date="2023-07-28",
                spot=100.0,
                historical_volatility=.50,
                model_method="black_scholes",
            ),
            historical,
            (data_ref,),
        ))
        actual_hv = result.market_snapshot["historical_volatility"]["A"]
        self.assertAlmostEqual(result.market_snapshot["volatility"]["A"], actual_hv, places=15)
        self.assertNotAlmostEqual(actual_hv, .50, places=6)
        self.assertEqual(result.market_snapshot["volatility_source"], "historical_volatility")

    def test_page_distinguishes_demo_from_low_precision_quote_failure(self) -> None:
        source = service.PAGE.read_text(encoding="utf-8")
        self.assertIn("精度门禁未通过，不可报价", source)


if __name__ == "__main__":
    unittest.main()
