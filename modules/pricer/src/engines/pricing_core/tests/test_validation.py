from __future__ import annotations

from copy import deepcopy
import math
import unittest

from .. import main

from .support import (
    autocall_parameters,
    case_by_structure,
    path_accumulator_parameters,
)


class PublicValidationTest(unittest.TestCase):
    def test_analytic_engines_reject_monte_carlo_only_config(self):
        family, structure, parameters, method = case_by_structure(
            "EUROPEAN_VANILLA"
        )
        parameters["config"] = {"paths": 10}
        with self.assertRaisesRegex(ValueError, "未知字段"):
            main.price_option(family, structure, parameters, method, output="NONE")

    def test_structure_specific_state_rejects_irrelevant_fields(self):
        family, structure, parameters, method = case_by_structure(
            "EUROPEAN_VANILLA"
        )
        parameters["valuation_state"] = {"knocked_in": True}
        with self.assertRaisesRegex(ValueError, "未知字段"):
            main.price_option(family, structure, parameters, method, output="NONE")
        parameters = autocall_parameters("SNOWBALL")
        parameters["valuation_state"]["accumulated_count"] = 1
        with self.assertRaisesRegex(ValueError, "accumulated_count"):
            main.price_option(
                "AUTOCALL", "SNOWBALL", parameters, "MONTE_CARLO_CPU", output="NONE"
            )

    def test_autocall_rejects_already_knocked_out_state(self):
        parameters = autocall_parameters("SNOWBALL")
        parameters["valuation_state"]["knocked_out"] = True
        with self.assertRaisesRegex(ValueError, "已敲出"):
            main.price_option(
                "AUTOCALL", "SNOWBALL", parameters, "MONTE_CARLO_CPU", output="NONE"
            )

    def test_autocall_handles_coupon_schedule_ending_before_final_day(self):
        parameters = autocall_parameters("PHOENIX")
        parameters["contract"]["coupon_schedule"] = parameters["contract"][
            "coupon_schedule"
        ][:2]
        run = main.price_option(
            "AUTOCALL", "PHOENIX", parameters, "MONTE_CARLO_CPU", output="NONE"
        )
        self.assertTrue(math.isfinite(run.result.pv_points_100))

    def test_schedule_order_empty_and_mixed_barriers_are_rejected(self):
        parameters = path_accumulator_parameters()
        parameters["contract"]["observation_schedule"] = []
        with self.assertRaisesRegex(ValueError, "不得为空"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )
        parameters = path_accumulator_parameters()
        parameters["contract"]["observation_schedule"][1]["trading_day"] = 1
        with self.assertRaisesRegex(ValueError, "严格递增"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )
        parameters = path_accumulator_parameters()
        parameters["contract"]["observation_schedule"][0].pop("barrier")
        with self.assertRaisesRegex(ValueError, "全部为空或全部明确"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )

    def test_forward_curve_weight_and_quantity_basis_are_explicitly_bounded(self):
        for structure, parameters in (
            ("SNOWBALL", autocall_parameters("SNOWBALL")),
            ("PATH_ACCUMULATOR", path_accumulator_parameters()),
        ):
            parameters["contract"]["forward_curve_weight"] = 1.1
            family = "AUTOCALL" if structure == "SNOWBALL" else "ACCUMULATOR"
            with self.subTest(structure=structure), self.assertRaisesRegex(
                ValueError, "0和1"
            ):
                main.price_option(
                    family, structure, parameters, "MONTE_CARLO_CPU", output="NONE"
                )
        for structure in ("STATIC_ACCUMULATOR", "PATH_ACCUMULATOR"):
            family, _, parameters, method = case_by_structure(structure)
            parameters["contract"]["quantity_basis"] = "PER_OBSERVATION"
            with self.subTest(structure=structure), self.assertRaisesRegex(
                ValueError, "WHOLE_CONTRACT"
            ):
                main.price_option(
                    family, structure, parameters, method, output="NONE"
                )

    def test_static_accumulator_observation_domain_is_validated(self):
        family, structure, parameters, method = case_by_structure(
            "STATIC_ACCUMULATOR"
        )
        parameters["contract"]["first_observation"] = 2.5
        with self.assertRaisesRegex(ValueError, "整数"):
            main.price_option(family, structure, parameters, method, output="NONE")
        _, _, parameters, _ = case_by_structure("STATIC_ACCUMULATOR")
        parameters["contract"]["day_adjustment"] = 2.0
        with self.assertRaisesRegex(ValueError, "小于first_observation"):
            main.price_option(family, structure, parameters, method, output="NONE")

    def test_wrong_method_seed_path_limit_and_unknown_fields_are_rejected(self):
        family, structure, parameters, _ = case_by_structure("BINARY")
        with self.assertRaisesRegex(ValueError, "不支持方法"):
            main.price_option(
                family, structure, parameters, "BLACK_SCHOLES", output="NONE"
            )
        parameters = path_accumulator_parameters()
        parameters["config"]["seed"] = 7
        with self.assertRaisesRegex(ValueError, "seed=20240101"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )
        parameters = path_accumulator_parameters()
        parameters["config"]["paths"] = 2001
        with self.assertRaisesRegex(ValueError, "不得超过2000"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )
        parameters = path_accumulator_parameters()
        parameters["contract"]["mystery"] = 1
        with self.assertRaisesRegex(ValueError, "mystery"):
            main.price_option(
                "ACCUMULATOR",
                "PATH_ACCUMULATOR",
                parameters,
                "MONTE_CARLO_CPU",
                output="NONE",
            )


if __name__ == "__main__":
    unittest.main()
