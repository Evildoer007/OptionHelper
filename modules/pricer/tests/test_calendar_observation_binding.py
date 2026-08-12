"""Calendar-backed n_obs tests for OptionReg path products."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import unittest

import numpy as np

from runtime.contracts.contract_api import PricePath, evaluate_contract, resolve_contract

from modules.pricer import PricingConfig, PricingInput, price
from modules.pricer.product_pricing_adapter import _contract_with_maturity

from .pricer_test_fixtures import calendar_asset, demo_config, market_asset


_SESSIONS = (
    "2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02", "2026-03-30", "2026-03-31",
)
_AS_OF = date(2026, 1, 30)
_TENOR = 60 / 365
_CALENDAR = {"sessions": _SESSIONS}


def _contract(product_id: str, **overrides):
    return resolve_contract(
        product_id,
        identity={"underlyings": ["A"], "reference_prices": {"A": 100.0}},
        term_overrides={"T": _TENOR, **overrides},
    )


def _path() -> PricePath:
    dates = tuple(date.fromisoformat(value) for value in _SESSIONS)
    times = np.asarray([(value - _AS_OF).days / 365 for value in dates], dtype=float)
    return PricePath.from_values([100.0] * len(dates), times=times, dates=dates, asset_ids=("A",))


class CalendarObservationBindingTests(unittest.TestCase):
    def test_closed_form_route_does_not_require_future_calendar(self) -> None:
        result = price(PricingInput(
            _contract("4.2"),
            PricingConfig(spot=100.0, historical_volatility=.2, risk_free_rate=.02),
        ))

        self.assertEqual(result.status, "priced")

    def test_barrier_optionreg_mc_requires_future_calendar(self) -> None:
        with self.assertRaisesRegex(ValueError, "trading-calendar"):
            price(PricingInput(
                _contract("5.1"),
                PricingConfig(spot=100.0, historical_volatility=.2, risk_free_rate=.02, model_method="monte_carlo"),
            ))

    def test_terminal_only_optionreg_mc_does_not_require_calendar(self) -> None:
        result = price(PricingInput(
            _contract("6.1"),
            PricingConfig(
                spot=100.0, historical_volatility=.2, risk_free_rate=.02,
                model_method="monte_carlo", path_count=11,
            ),
        ))

        self.assertEqual(result.status, "priced")
        self.assertNotIn("calendar", result.diagnostics)
        self.assertEqual(result.diagnostics["path_grid"]["mode"], "valuation_and_contractual_maturity_endpoints")

    def test_unsupported_explicit_method_is_not_preempted_by_calendar_gate(self) -> None:
        result = price(PricingInput(
            _contract("5.1"),
            PricingConfig(spot=100.0, historical_volatility=.2, risk_free_rate=.02, model_method="black_scholes"),
        ))

        self.assertEqual(result.status, "unsupported")

    def test_terminal_european_portfolio_monte_carlo_uses_contractual_endpoints_without_calendar(self) -> None:
        contract = _contract("3.1")
        config = demo_config(("A",))
        result = price(PricingInput(contract, config))
        self.assertEqual(result.status, "priced")
        self.assertEqual(
            result.diagnostics["path_grid"]["mode"],
            "valuation_and_contractual_maturity_endpoints",
        )

    def test_accumulator_monthly_last_binds_n_obs_to_selected_calendar_dates(self) -> None:
        contract = _contract("8.1", O_KO="monthly_last")
        remaining = _contract_with_maturity(
            contract, _TENOR, remaining_years=_TENOR,
            trading_calendar=_CALENDAR, as_of=_AS_OF,
        )

        self.assertEqual(remaining.terms["n_obs"], 2)
        self.assertEqual(evaluate_contract(remaining, _path()).monitor_values["Q_acc"], 200.0)

    def test_range_accrual_daily_binds_n_obs_to_all_calendar_observations(self) -> None:
        contract = _contract("10.8")
        remaining = _contract_with_maturity(
            contract, _TENOR, remaining_years=_TENOR,
            trading_calendar=_CALENDAR, as_of=_AS_OF,
        )

        settlement = evaluate_contract(remaining, _path())
        self.assertEqual(remaining.terms["n_obs"], len(_SESSIONS))
        self.assertEqual(settlement.monitor_values["n_obs_actual"], len(_SESSIONS))

    def test_accumulator_explicit_observation_indices_are_not_resampled(self) -> None:
        contract = _contract("8.1")
        explicit = replace(contract, terms={**contract.terms, "O_KO": (0, 2, 5), "n_obs": 3}, contract_fingerprint="")
        remaining = _contract_with_maturity(
            explicit, _TENOR, remaining_years=_TENOR,
            trading_calendar=_CALENDAR, as_of=_AS_OF,
        )

        self.assertEqual(remaining.terms["n_obs"], 3)
        self.assertEqual(evaluate_contract(remaining, _path()).monitor_values["Q_acc"], 300.0)


if __name__ == "__main__":
    unittest.main()
