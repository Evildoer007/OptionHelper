from __future__ import annotations

import inspect
import unittest

from .. import main

from .support import FAMILY_STRUCTURES


class CatalogAndAPIContractTest(unittest.TestCase):
    def test_exact_seven_families_and_ten_structures(self):
        self.assertEqual(main.list_families(), tuple(FAMILY_STRUCTURES))
        structures = []
        for family, expected in FAMILY_STRUCTURES.items():
            self.assertEqual(main.list_structures(family), expected)
            structures.extend(expected)
        self.assertEqual(len(structures), 10)
        self.assertEqual(len(set(structures)), 10)

    def test_every_structure_has_one_explicit_standard_route(self):
        route_ids = set()
        for family, structures in FAMILY_STRUCTURES.items():
            for structure in structures:
                description = main.describe_structure(family, structure)
                self.assertEqual(description["family"], family)
                self.assertEqual(description["structure"], structure)
                self.assertEqual(len(description["methods"]), 1)
                self.assertEqual(tuple(description["greeks"]["core"]), (
                    "Delta", "Gamma", "Theta", "Vega", "Rho"
                ))
                route_ids.add(description["route_id"])
        self.assertEqual(len(route_ids), 8)

    def test_public_api_is_standard_only(self):
        price_signature = inspect.signature(main.price_option)
        solve_signature = inspect.signature(main.solve_option)
        self.assertEqual(
            tuple(price_signature.parameters),
            ("family", "structure", "parameters", "method", "output"),
        )
        self.assertEqual(
            tuple(solve_signature.parameters),
            ("family", "structure", "parameters", "method", "target", "output"),
        )
        self.assertNotIn("compare_option", main.__all__)
        self.assertFalse(hasattr(main, "compare_option"))

    def test_fuzzy_names_and_family_mismatches_are_rejected(self):
        for family, structure in (
            ("vanilla", "EUROPEAN_VANILLA"),
            ("VANILLA", "BINARY"),
            ("AUTOCALL", "PATH_ACCUMULATOR"),
        ):
            with self.subTest(family=family, structure=structure):
                with self.assertRaises(ValueError):
                    main.describe_structure(family, structure)


if __name__ == "__main__":
    unittest.main()
