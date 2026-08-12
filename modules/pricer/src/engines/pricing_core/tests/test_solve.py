from __future__ import annotations

from copy import deepcopy
import unittest

from .. import main

from .support import autocall_parameters, path_accumulator_parameters


class UnifiedSolveTest(unittest.TestCase):
    def test_all_autocall_coupon_solutions_reprice_to_target(self):
        for structure in ("SNOWBALL", "PHOENIX", "TRIGGER"):
            parameters = autocall_parameters(structure)
            run = main.solve_option(
                "AUTOCALL",
                structure,
                parameters,
                "MONTE_CARLO_CPU",
                {"variable": "coupon", "target_pv_points_100": 0.0},
                output="NONE",
            )
            repriced = deepcopy(parameters)
            repriced["contract"].update(run.result.contract_patch)
            price = main.price_option(
                "AUTOCALL",
                structure,
                repriced,
                "MONTE_CARLO_CPU",
                output="NONE",
            )
            with self.subTest(structure=structure):
                self.assertTrue(run.result.converged)
                self.assertEqual(run.result.variable, "coupon")
                self.assertLessEqual(
                    abs(price.result.pv_points_100),
                    run.result.target_pv_absolute_tolerance,
                )

    def test_path_accumulator_strike_solution_reprices_to_target(self):
        parameters = path_accumulator_parameters()
        run = main.solve_option(
            "ACCUMULATOR",
            "PATH_ACCUMULATOR",
            parameters,
            "MONTE_CARLO_CPU",
            {"variable": "strike", "target_pv_points_100": 0.0},
            output="NONE",
        )
        repriced = deepcopy(parameters)
        repriced["contract"].update(run.result.contract_patch)
        price = main.price_option(
            "ACCUMULATOR",
            "PATH_ACCUMULATOR",
            repriced,
            "MONTE_CARLO_CPU",
            output="NONE",
        )
        self.assertTrue(run.result.converged)
        self.assertLessEqual(
            abs(price.result.pv_points_100),
            run.result.target_pv_absolute_tolerance,
        )

    def test_unsupported_target_and_locked_cashflows_are_rejected(self):
        parameters = autocall_parameters("SNOWBALL")
        with self.assertRaises(ValueError):
            main.solve_option(
                "AUTOCALL",
                "SNOWBALL",
                parameters,
                "MONTE_CARLO_CPU",
                {"variable": "strike", "target_pv_points_100": 0.0},
                output="NONE",
            )
        parameters["contract"]["call_schedule"][0]["amount"] = 1.0
        with self.assertRaisesRegex(ValueError, "不得锁定amount"):
            main.solve_option(
                "AUTOCALL",
                "SNOWBALL",
                parameters,
                "MONTE_CARLO_CPU",
                {"variable": "coupon", "target_pv_points_100": 0.0},
                output="NONE",
            )


if __name__ == "__main__":
    unittest.main()
