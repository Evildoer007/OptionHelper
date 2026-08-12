from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from .. import main

from .support import CORE_GREEKS, EXTENDED_RISKS, representative_cases


class NineStructurePricingAndGreekTest(unittest.TestCase):
    def test_all_structures_are_deterministic_and_publish_complete_risk(self):
        for family, structure, parameters, method in representative_cases():
            with self.subTest(structure=structure):
                first = main.price_option(
                    family, structure, parameters, method, output="NONE"
                )
                second = main.price_option(
                    family, structure, parameters, method, output="NONE"
                )
                self.assertEqual(first.result, second.result)
                self.assertTrue(math.isfinite(first.result.pv_points_100))
                self.assertAlmostEqual(
                    first.result.pv_percent,
                    first.result.pv_points_100 / 100.0,
                    places=15,
                )
                self.assertAlmostEqual(
                    first.result.pv_amount,
                    first.result.pv_percent * 1_000_000.0,
                    places=8,
                )
                for name in CORE_GREEKS:
                    risk = first.result.greeks[name]
                    self.assertEqual(risk.status.value, "AVAILABLE")
                    self.assertTrue(math.isfinite(risk.value))
                    self.assertEqual(risk.value, risk.pv_points_100_value)
                    self.assertAlmostEqual(
                        risk.pv_percent_value, risk.value / 100.0, places=15
                    )
                    self.assertAlmostEqual(
                        risk.pv_amount_value,
                        risk.pv_percent_value * 1_000_000.0,
                        places=8,
                    )
                    self.assertTrue(risk.unit.startswith("pv_points_100"))
                    self.assertTrue(risk.bump_details)
                for name in EXTENDED_RISKS:
                    risk = first.result.extended_risks[name]
                    self.assertEqual(risk.status.value, "AVAILABLE")
                    self.assertTrue(math.isfinite(risk.value))
                    self.assertTrue(risk.bump_details)

    def test_json_contains_inputs_results_and_no_secrets_or_random_matrix(self):
        family, structure, parameters, method = representative_cases()[0]
        parameters["config"] = {
            "diagnostics": {
                "access_token": "do-not-write-this-value",
                "password": "do-not-write-this-password",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("PRICER_ENGINE_RESULT_ROOT")
            os.environ["PRICER_ENGINE_RESULT_ROOT"] = directory
            try:
                run = main.price_option(
                    family, structure, parameters, method, output="JSON"
                )
            finally:
                if previous is None:
                    os.environ.pop("PRICER_ENGINE_RESULT_ROOT", None)
                else:
                    os.environ["PRICER_ENGINE_RESULT_ROOT"] = previous
            output = Path(run.json_output_path)
            payload_text = output.read_text(encoding="utf-8")
            payload = json.loads(payload_text)
            self.assertEqual(payload["family"], family)
            self.assertEqual(payload["structure"], structure)
            self.assertEqual(payload["result"]["pv_points_100"], run.result.pv_points_100)
            lowered = payload_text.lower()
            self.assertNotIn("do-not-write-this-value", payload_text)
            self.assertNotIn("do-not-write-this-password", payload_text)
            self.assertNotIn("access_token", lowered)
            self.assertNotIn("password", lowered)
            self.assertNotIn("random_matrix", lowered)


if __name__ == "__main__":
    unittest.main()
