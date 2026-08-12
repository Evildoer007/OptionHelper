from __future__ import annotations

import json
from pathlib import Path
import unittest

from .. import main

from .support import representative_cases


GOLDEN = json.loads(
    (Path(__file__).with_name("golden_standard_results.json")).read_text(encoding="utf-8")
)


class StandardGoldenResultTest(unittest.TestCase):
    def test_all_nine_structures_match_validated_mac_cpu_golden_master(self):
        for family, structure, parameters, method in representative_cases():
            run = main.price_option(
                family, structure, parameters, method, output="NONE"
            )
            expected = GOLDEN[structure]
            with self.subTest(structure=structure):
                self.assertAlmostEqual(
                    run.result.pv_points_100, expected["pv_points_100"], places=12
                )
                for name, value in expected["greeks"].items():
                    self.assertAlmostEqual(
                        run.result.greeks[name].value, value, places=12
                    )
                for name, value in expected["extended_risks"].items():
                    self.assertAlmostEqual(
                        run.result.extended_risks[name].value, value, places=12
                    )
                self.assertEqual(
                    run.result.diagnostics.get("path_pv_sha256_float32"),
                    expected["path_hash"],
                )


if __name__ == "__main__":
    unittest.main()
