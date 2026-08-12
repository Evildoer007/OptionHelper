from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.contracts.contract_api import (
    FormulaError,
    PricePath,
    ResolvedContract,
    evaluate_contract,
    evaluate_formula,
    load_registry,
    resolve_contract,
    resolve_schedule,
    verify_product_snapshot_binding,
)
from runtime.contracts import contract_engine


class ContractRuntimeTest(unittest.TestCase):
    def test_resolved_contract_is_deeply_immutable_and_fingerprinted(self) -> None:
        first = resolve_contract("2.1", identity={"underlyings": ["000300.SH"]})
        second = resolve_contract("2.1", identity={"underlyings": ["000300.SH"]})
        changed = resolve_contract("2.1", identity={"underlyings": ["000300.SH"]}, term_overrides={"K": 101.0})
        self.assertEqual(first.contract_fingerprint, second.contract_fingerprint)
        self.assertNotEqual(first.contract_fingerprint, changed.contract_fingerprint)
        self.assertTrue(first.product_version.startswith("unversioned:"))
        self.assertEqual(set(first.term_sources), set(first.terms))
        self.assertEqual(set(first.to_dict()), {"identity", "terms", "term_sources", "paths"})
        self.assertIn("contract_fingerprint", first.to_protocol_dict())
        with self.assertRaises(TypeError):
            first.identity["currency"] = "USD"  # type: ignore[index]
        with self.assertRaises(TypeError):
            first.identity._data["currency"] = "USD"  # type: ignore[attr-defined,index]
        with self.assertRaises(TypeError):
            first.terms["monitor"]["x"] = "1"  # type: ignore[index]

        alternate_id = resolve_contract("2.1", identity={"contract_id": "another-run", "underlyings": ["000300.SH"]})
        self.assertEqual(first.contract_fingerprint, alternate_id.contract_fingerprint)

    def test_price_convention_and_resolved_calendar_are_real_contract_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "price_convention"):
            resolve_contract("2.1", identity={"underlyings": ["000300.SH"], "price_convention": "unknown"})
        with self.assertRaisesRegex(ValueError, "reference_prices"):
            resolve_contract("2.1", identity={"underlyings": ["000300.SH"], "price_convention": "absolute_market"})
        absolute = resolve_contract(
            "2.1",
            identity={
                "underlyings": ["000300.SH"],
                "price_convention": "absolute_market",
                "reference_prices": {"000300.SH": 3000.0},
            },
            term_overrides={"S0": 3000.0, "K": 3000.0},
        )
        self.assertEqual(absolute.identity["price_convention"], "absolute_market")
        normalized = resolve_contract(
            "2.1",
            identity={"underlyings": ["000300.SH"], "reference_prices": {"000300.SH": 3000.0}},
        )
        path = PricePath.from_values([3000.0, 3300.0], times=[0.0, 1.0], asset_ids=["000300.SH"])
        self.assertEqual(
            tuple(flow.to_dict() for flow in evaluate_contract(absolute, path).cashflows),
            tuple(flow.to_dict() for flow in evaluate_contract(normalized, path).cashflows),
        )

        scheduled = resolve_contract(
            "9.1",
            identity={"underlyings": ["000300.SH"]},
            trading_dates=["2026-01-02", "2026-01-30", "2026-02-02", "2026-02-27"],
        )
        self.assertEqual(scheduled.resolved_schedules["O_KO"]["status"], "resolved")
        self.assertEqual(
            tuple(scheduled.resolved_schedules["O_KO"]["dates"]),
            ("2026-01-30", "2026-02-27"),
        )

    def test_product_version_is_registry_derived_not_caller_supplied(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知字段"):
            resolve_contract("2.1", identity={"underlyings": ["000300.SH"], "product_version": "spoofed"})

    def test_registry_binding_rejects_an_unattested_published_product_version(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000300.SH"]})
        forged = replace(
            contract,
            identity={**contract.identity, "product_version": "v999-forged"},
            product_version="v999-forged",
            contract_fingerprint="",
        )
        with self.assertRaisesRegex(ValueError, "product_version|ProductVersion"):
            verify_product_snapshot_binding(forged, load_registry())

        published = replace(
            contract,
            identity={**contract.identity, "product_version": "v1.0"},
            product_version="v1.0",
            contract_fingerprint="",
        )
        self.assertEqual(
            verify_product_snapshot_binding(
                published,
                load_registry(),
                attested_product_version="v1.0",
            ),
            published,
        )

    def test_registry_binding_rejects_self_consistent_tampered_terms(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000300.SH"]})
        forged = replace(
            contract,
            terms={**contract.terms, "margin_call": not contract.terms["margin_call"]},
            contract_fingerprint="",
        )
        with self.assertRaisesRegex(ValueError, "terms|条款|快照"):
            verify_product_snapshot_binding(forged, load_registry())

    def test_resolved_contract_paths_are_bound_to_registry_product_snapshot(self) -> None:
        registry = load_registry()
        contract = resolve_contract("2.1", registry=registry)
        self.assertEqual(verify_product_snapshot_binding(contract, registry), contract)
        protocol = contract.to_protocol_dict()
        self.assertRegex(protocol["registry_snapshot_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(protocol["product_snapshot_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(protocol["product_paths_hash"], r"^[0-9a-f]{64}$")

        wrong_registry = load_registry()
        wrong_registry["products"]["2.1"]["paths"] = wrong_registry["products"]["2.2"]["paths"]
        with self.assertRaisesRegex(ValueError, "Registry快照|产品快照|paths"):
            verify_product_snapshot_binding(contract, wrong_registry)

        with self.assertRaisesRegex(ValueError, "paths"):
            ResolvedContract(
                contract.identity,
                contract.terms,
                contract.term_sources,
                tuple(wrong_registry["products"]["2.1"]["paths"]),
                product_version=contract.product_version,
                resolved_schedules=contract.resolved_schedules,
                registry_snapshot_hash=contract.registry_snapshot_hash,
                product_snapshot_hash=contract.product_snapshot_hash,
                product_paths_hash=contract.product_paths_hash,
            )
        legacy = ResolvedContract(
            {**contract.identity, "product_version": "signed-product-v1"}, contract.terms, contract.term_sources, contract.paths,
            product_version="signed-product-v1", resolved_schedules=contract.resolved_schedules,
        )
        with self.assertRaisesRegex(ValueError, "快照绑定"):
            legacy.to_protocol_dict()

    def test_formula_interpreter_rejects_arbitrary_python_and_resource_abuse(self) -> None:
        for expression in ("x.__class__", "__import__('os')", "[x for x in [1]]", "2 ** 100", "'x' * 1000000"):
            with self.subTest(expression=expression), self.assertRaises(FormulaError):
                evaluate_formula(expression, {"x": 1})

    def test_formula_hot_path_reuses_one_verified_term_catalog(self) -> None:
        contract = resolve_contract("2.1")
        path = PricePath.from_values(
            [100.0, 110.0], times=[0.0, 1.0], asset_ids=contract.underlyings
        )
        contract_engine._shared_term_catalog.cache_clear()
        with patch.object(contract_engine, "_load_term_catalog", wraps=contract_engine._load_term_catalog) as loader:
            for _ in range(200):
                evaluate_contract(contract, path)
        self.assertEqual(loader.call_count, 1)

    def test_absent_first_value_fact_does_not_break_non_event_path(self) -> None:
        contract = resolve_contract("9.11")
        path = PricePath.from_values(
            [100.0, 100.0], times=[0.0, 2.0], dates=["2026-01-02", "2027-12-31"],
            asset_ids=contract.underlyings,
        )
        result = evaluate_contract(contract, path)
        self.assertEqual(result.monitor_values["S_out"], float("inf"))

    def test_schedule_resolver_uses_trading_day_ordinals(self) -> None:
        dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-02-02", "2026-02-03"]
        self.assertEqual([str(value.date()) for value in resolve_schedule("monthly_2nd", dates)], ["2026-01-05", "2026-02-03"])


if __name__ == "__main__":
    unittest.main()
