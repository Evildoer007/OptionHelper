"""Payoffer正式输入与本机ResultStore提交回归。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import ResolvedContract, resolve_contract
from runtime.contracts.contract_types import semantic_hash
from modules.payoffer.models import PayoffInput
from modules.payoffer.service import PayoffEngineError, render_payoff, run_runtime
from modules.payoffer.impl import engine as payoff_engine


def _asset_snapshot() -> dict[str, str]:
    figures = PROJECT_ROOT / "modules" / "payoffer" / "figures"
    return {
        str(path.relative_to(figures)): sha256(path.read_bytes()).hexdigest()
        for path in sorted(figures.rglob("*"))
        if path.is_file()
    }


def _asset_pair_snapshot() -> dict[str, str]:
    figures = PROJECT_ROOT / "modules" / "payoffer" / "figures"
    return {
        path.stem: sha256(path.read_bytes() + b"\0" + (figures / "svg" / f"{path.stem}.svg").read_bytes()).hexdigest()
        for path in sorted((figures / "json").glob("*.json"))
    }


def _golden_pairs() -> dict[str, str]:
    baseline = PROJECT_ROOT / "tests" / "baselines" / "payoffer_default_assets.sha256"
    return {
        name: digest
        for line in baseline.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for digest, name in [line.split(maxsplit=1)]
    }


class PayofferRuntimeContractStoreTest(unittest.TestCase):
    def _contract(self, *, strike: float = 105.0):
        return resolve_contract(
            "2.1",
            identity={
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": 6500.0},
            },
            term_overrides={"K": strike},
        )

    def test_render_payoff_consumes_resolved_contract_and_keeps_all_default_assets_read_only(self) -> None:
        before = _asset_snapshot()
        contract = self._contract()

        artifact = render_payoff(PayoffInput(contract=contract))

        self.assertIn("<svg", artifact.svg)
        self.assertEqual(artifact.payload["product_id"], "2.1")
        self.assertEqual(
            artifact.payload["resolved_contract"]["contract_fingerprint"],
            contract.contract_fingerprint,
        )
        self.assertEqual(artifact.payload["resolved_contract"]["terms"]["K"], 105.0)
        self.assertEqual(
            artifact.payload["default_visual_asset"]["name_zh"],
            "看涨期权",
        )
        self.assertEqual(len(before), 130)
        self.assertEqual(before, _asset_snapshot())

    def test_all_65_default_asset_pairs_match_golden_master(self) -> None:
        self.assertEqual(len(_golden_pairs()), 65)
        self.assertEqual(_asset_pair_snapshot(), _golden_pairs())

    def test_render_payoff_rejects_non_contract_input_and_unmapped_product_version(self) -> None:
        contract = self._contract()
        with self.assertRaisesRegex(PayoffEngineError, "PayoffInput"):
            render_payoff({"contract": contract})
        explicit_version = ResolvedContract(
            identity={**contract.identity, "product_version": "signed-product-v1"},
            terms=contract.terms,
            term_sources=contract.term_sources,
            paths=contract.paths,
            product_version="signed-product-v1",
            resolved_schedules=contract.resolved_schedules,
        )
        with self.assertRaisesRegex(PayoffEngineError, "ProductVersion"):
            render_payoff(PayoffInput(contract=explicit_version))

    def test_run_payoff_commits_complete_local_result_store_record(self) -> None:
        original_result_root = payoff_engine.RESULT_ROOT
        with tempfile.TemporaryDirectory() as temporary:
            payoff_engine.RESULT_ROOT = Path(temporary)
            try:
                result = run_runtime(
                    "看涨期权",
                    {"K": 110.0},
                    None,
                    "payoffer_contract_test",
                    "run_001",
                )
            finally:
                payoff_engine.RESULT_ROOT = original_result_root

            destination = Path(result["destination"])
            self.assertIn("payoffer-development", destination.parts)
            self.assertIn("local-development", destination.parts)
            required = {
                "manifest.json",
                "input_snapshot.json",
                "resolved_contract.json",
                "data_refs.json",
                "limitations.json",
                "result.json",
                "artifacts/artifact_manifest.json",
                "commit_marker.json",
                "artifacts/payoff.json",
                "artifacts/payoff.svg",
            }
            self.assertTrue(all((destination / relative).is_file() for relative in required))
            staging_root = Path(temporary) / "payoffer-development" / "tenants" / "local-development" / ".staging"
            self.assertFalse(staging_root.exists() and any(staging_root.iterdir()))

            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            result_payload = json.loads((destination / "result.json").read_text(encoding="utf-8"))
            artifact_manifest = json.loads((destination / "artifacts" / "artifact_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["module"], "payoffer")
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["analysis_case_id"], "payoffer_contract_test")
            self.assertEqual(manifest["catalog_version"], "local-development")
            self.assertEqual(result_payload["contract_fingerprint"], manifest["contract_fingerprint"])
            self.assertEqual(result_payload["payoff_semantic_hash"], manifest["payoff_semantic_hash"])
            self.assertEqual(manifest["semantic_result_hash"], semantic_hash(result_payload))
            for relative, expected_hash in artifact_manifest["file_hashes"].items():
                payload = (destination / relative).read_bytes()
                self.assertEqual(expected_hash, sha256(payload).hexdigest())
            self.assertEqual(
                result["module_run_ref"]["expected_semantic_result_hash"],
                artifact_manifest["semantic_result_hash"],
            )
            self.assertEqual(result["module_run_ref"]["module"], "payoffer")

    def test_existing_run_is_rejected_without_overwrite(self) -> None:
        original_result_root = payoff_engine.RESULT_ROOT
        with tempfile.TemporaryDirectory() as temporary:
            payoff_engine.RESULT_ROOT = Path(temporary)
            try:
                run_runtime("看涨期权", {"K": 105.0}, None, "payoffer_contract_test", "run_001")
                with self.assertRaisesRegex(Exception, "已存在"):
                    run_runtime("看涨期权", {"K": 105.0}, None, "payoffer_contract_test", "run_001")
            finally:
                payoff_engine.RESULT_ROOT = original_result_root


if __name__ == "__main__":
    unittest.main()
