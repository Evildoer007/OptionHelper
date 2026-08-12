from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.contracts.contract_api import resolve_contract, validate_registry
from runtime.knowledger.registry_loader import get_default_registry_path, load_registry, load_term_catalog


class RegistryLoaderTest(unittest.TestCase):
    def test_only_loader_resolves_development_optionreg(self) -> None:
        self.assertEqual(get_default_registry_path(), ROOT / "references" / "optionreg.py")
        registry = load_registry()
        self.assertEqual(set(registry), {"term_catalog", "products"})
        self.assertEqual(len(registry["products"]), 65)
        self.assertEqual(validate_registry(registry), {})

    def test_contract_api_uses_same_registry(self) -> None:
        contract = resolve_contract("2.1")
        self.assertEqual(contract.product_id, "2.1")
        self.assertEqual(contract.name_zh, "看涨期权")
        self.assertEqual(len(contract.paths), 1)

    def test_public_copy_is_isolated_and_hot_catalog_is_deeply_read_only(self) -> None:
        first = load_registry()
        first["products"]["2.1"]["identity"]["name_zh"] = "污染"
        self.assertEqual(load_registry()["products"]["2.1"]["identity"]["name_zh"], "看涨期权")
        catalog = load_term_catalog()
        with self.assertRaises(TypeError):
            catalog["K"]["symbol"] = "polluted"  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
