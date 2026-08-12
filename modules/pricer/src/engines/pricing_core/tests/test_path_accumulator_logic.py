from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ..engine.derivatives.basis import ResultBasis
from ..engine.derivatives.enums import CallPut, PricingMethod
from ..engine.derivatives.instruments import PathAccumulatorOption
from ..engine.derivatives.models import (
    MarketState,
    SchedulePoint,
    ValuationConfig,
    ValuationState,
)
from ..engine.derivatives.random_source import NpyRandomSource
from ..engine.derivatives.standard.path_accumulator import (
    simulate_path_accumulator_paths,
)


class PathAccumulatorCashflowLogicTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.random_path = Path(self.directory.name) / "zeros.npy"
        np.save(self.random_path, np.zeros((1, 16), dtype=np.float64))
        self.source = NpyRandomSource(self.random_path, seed=20240101)

    def tearDown(self):
        self.directory.cleanup()

    def _instrument(
        self,
        *,
        ko_terminates: bool,
        ko_begin: int,
        forward_curve_weight: float = 1.0,
        schedule=None,
    ):
        if schedule is None:
            schedule = tuple(
                SchedulePoint(trading_day=day, calendar_day=day, barrier=105.0)
                for day in (1, 2, 3)
            )
        return PathAccumulatorOption(
            basis=ResultBasis(notional=1_000_000.0, currency="CNY"),
            call_put=CallPut.CALL,
            strike=100.0,
            knock_out=105.0,
            multiplier=2.0,
            ko_begin_trading_day=ko_begin,
            lock_trading_days=0,
            ko_terminates=ko_terminates,
            observation_schedule=schedule,
            forward_curve_weight=forward_curve_weight,
        )

    def _config(self):
        return ValuationConfig(
            method=PricingMethod.MONTE_CARLO_CPU,
            paths=1,
            seed=20240101,
            threads=1,
            random_source=self.source,
        )

    @staticmethod
    def _market(**overrides):
        values = {
            "as_of": date(2026, 8, 6),
            "spot": 110.0,
            "volatility": 0.0,
            "risk_free_rate": 0.0,
            "dividend_yield": 0.0,
        }
        values.update(overrides)
        return MarketState(**values)

    @staticmethod
    def _state():
        return ValuationState(trading_day=1, calendar_day=1)

    def test_ko_begin_delays_terminating_knockout_but_not_accumulation(self):
        paths = simulate_path_accumulator_paths(
            self._instrument(ko_terminates=True, ko_begin=3),
            self._market(),
            self._config(),
            self._state(),
        )
        self.assertEqual(float(paths[0]), 20.0)

    def test_ko_begin_delays_nonterminating_barrier_filter(self):
        paths = simulate_path_accumulator_paths(
            self._instrument(ko_terminates=False, ko_begin=3),
            self._market(),
            self._config(),
            self._state(),
        )
        self.assertEqual(float(paths[0]), 20.0)

    def test_forward_curve_weight_scales_explicit_forward_deviation_from_spot(self):
        schedule = (SchedulePoint(trading_day=2, calendar_day=2, barrier=200.0),)
        market = self._market(
            spot=100.0,
            forward_curve=((121.0, 1),),
        )
        full = simulate_path_accumulator_paths(
            self._instrument(
                ko_terminates=True,
                ko_begin=1,
                forward_curve_weight=1.0,
                schedule=schedule,
            ),
            market,
            self._config(),
            self._state(),
        )
        scaled = simulate_path_accumulator_paths(
            self._instrument(
                ko_terminates=True,
                ko_begin=1,
                forward_curve_weight=0.5,
                schedule=schedule,
            ),
            market,
            self._config(),
            self._state(),
        )
        self.assertAlmostEqual(float(full[0]), 21.0, delta=1e-5)
        self.assertAlmostEqual(float(scaled[0]), 10.5, delta=1e-5)

    def test_final_observation_uses_zero_random_steps(self):
        schedule = (SchedulePoint(trading_day=1, calendar_day=1, barrier=200.0),)
        paths = simulate_path_accumulator_paths(
            self._instrument(
                ko_terminates=True,
                ko_begin=1,
                schedule=schedule,
            ),
            self._market(),
            self._config(),
            self._state(),
        )
        self.assertEqual(float(paths[0]), 10.0)


if __name__ == "__main__":
    unittest.main()
