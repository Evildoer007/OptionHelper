"""Backtester正式证据链与path/case覆盖缺口回归。"""

# ruff: noqa: E402

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest
from modules.backtester.historical_data import HistoricalDataError, validate_data_asset_ref
from modules.backtester.service import BacktesterRuntime, call_tool
from modules.backtester import service as backtester_service
from modules.backtester.tests.fixtures import temporary_market_layout, two_asset_history
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import load_registry, resolve_contract
from runtime.protocol.models import ModuleRunRef


class FormalEvidenceChainTest(unittest.TestCase):
    def test_runtime_commits_atomic_module_run_and_returns_top_level_refs(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="backtester-result-store-") as temporary,
            temporary_market_layout() as (project_root, data_root, relative),
            patch.object(backtester_service, "PROJECT_ROOT", project_root),
            patch.object(backtester_service, "RUNTIME_PATHS", SimpleNamespace(data_root=data_root)),
        ):
            store = LocalResultStore(temporary)
            runtime = BacktesterRuntime(result_store=store, tenant_id="tenant-a")
            response = runtime.run({
                "product_id": "2.1",
                "identity": {"underlyings": ["000905.SH"]},
                "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2021-01-04"]},
                "history_reference": relative,
                "task_id": "evidence-task",
                "run_id": "evidence-run",
            })

            self.assertTrue(response["ok"])
            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["data_refs"], [response["backtest"]["data_asset_ref"]])
            self.assertNotIn(str(PROJECT_ROOT), json.dumps(response["data_refs"], ensure_ascii=False))
            expected_contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]}).to_protocol_dict()
            self.assertEqual(response["resolved_contract"], expected_contract)
            self.assertTrue(response["resolved_contract"]["registry_snapshot_hash"])
            self.assertTrue(response["resolved_contract"]["product_snapshot_hash"])
            self.assertTrue(response["resolved_contract"]["product_paths_hash"])

            run_ref = ModuleRunRef(**response["module_run_ref"])
            self.assertEqual(run_ref.module, "backtester")
            run_dir = store.resolve_module_run(run_ref, tenant_id="tenant-a")
            required = {
                "manifest.json", "input_snapshot.json", "resolved_contract.json", "data_refs.json",
                "limitations.json", "result.json", "artifacts/artifact_manifest.json", "commit_marker.json",
                "artifacts/backtest_result.json", "artifacts/trade_ledger.csv",
                "artifacts/branch_coverage.json",
            }
            self.assertTrue(all((run_dir / name).is_file() for name in required))
            self.assertEqual(json.loads((run_dir / "manifest.json").read_text())["status"], "succeeded")
            self.assertEqual(json.loads((run_dir / "data_refs.json").read_text()), response["data_refs"])
            artifact_manifest = json.loads((run_dir / "artifacts/artifact_manifest.json").read_text())
            self.assertIn("artifacts/backtest_result.json", artifact_manifest["file_hashes"])
            self.assertIn("artifacts/trade_ledger.csv", artifact_manifest["file_hashes"])
            self.assertIn("artifacts/branch_coverage.json", artifact_manifest["file_hashes"])

    def test_missing_historical_ports_return_structured_failure(self) -> None:
        response = call_tool({
            "action": "run",
            "product_id": "2.1",
            "data_request": {"asset_id": "000905.SH"},
            "task_id": "missing-port-task",
            "run_id": "missing-port-run",
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["error_code"], "historical_data_port_unavailable")
        self.assertEqual(response["error"]["stage"], "historical_data")
        self.assertEqual(response["required_ports"], ["historical_data_port", "data_store"])

    def test_naked_physical_path_cannot_be_a_formal_data_ref(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        reference = dict(history.data_asset_ref)
        reference["storage_ref"] = "/srv/private/market.csv"
        reference["content_hash"] = sha256(b"controlled-payload").hexdigest()
        with self.assertRaisesRegex(HistoricalDataError, "opaque"):
            validate_data_asset_ref(reference)

    def test_formal_data_ref_rejects_empty_required_protocol_fields(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        for field in ("data_asset_id", "media_type", "schema_id", "tenant_id", "created_by"):
            reference = dict(history.data_asset_ref)
            reference[field] = ""
            with self.subTest(field=field), self.assertRaisesRegex(HistoricalDataError, "不能为空"):
                validate_data_asset_ref(reference)


class BranchCoverageEvidenceTest(unittest.TestCase):
    def test_single_path_smoke_reports_uncovered_declared_pairs(self) -> None:
        contract = resolve_contract("9.11", identity={"underlyings": ["000905.SH"]})
        history = HistoricalData.from_frame(two_asset_history())
        result = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
            history,
        )).to_dict()

        coverage = result["branch_coverage"]
        declared = sum(len(path["cases"]) for path in contract.paths)
        self.assertEqual(coverage["declared_pair_count"], declared)
        self.assertEqual(coverage["observed_pair_count"], 1)
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(len(coverage["uncovered_pairs"]), declared - 1)
        self.assertTrue(all(item["reason"] == "not_observed_in_backtest_sample" for item in coverage["uncovered_pairs"]))
        self.assertEqual(
            {(item["path_id"], item["case_id"]) for item in coverage["declared_pairs"]},
            {(path_id, case_id) for path_id, path in enumerate(contract.paths) for case_id, _ in enumerate(path["cases"])},
        )

    def test_constructible_vanilla_cases_are_covered_by_real_price_paths(self) -> None:
        contract = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"]},
            term_overrides={"T": 2 / 365, "Pi_0": 1.0},
        )
        observed: set[tuple[int, int]] = set()
        for prices in ([100.0, 100.0, 80.0], [100.0, 100.0, 120.0]):
            frame = pd.DataFrame({
                "date": pd.date_range("2024-01-02", periods=3, freq="B"),
                "asset_id": "000905.SH",
                "close": prices,
            })
            payload = backtest(BacktestInput(
                contract,
                BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
                HistoricalData.from_frame(frame),
            )).to_dict()
            observed.update((item["path_id"], item["case_id"]) for item in payload["branch_coverage"]["observed_pairs"])
        self.assertEqual(observed, {(0, 0), (0, 1)})

    def test_all_65_smokes_publish_catalog_wide_machine_readable_gaps(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        coverages: list[dict[str, object]] = []
        for product_id in load_registry()["products"]:
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            payload = backtest(BacktestInput(
                contract,
                BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
                history,
            )).to_dict()
            coverages.append(payload["branch_coverage"])

        self.assertEqual(len(coverages), 65)
        self.assertEqual(sum(int(item["declared_pair_count"]) for item in coverages), 210)
        self.assertEqual(sum(int(item["observed_pair_count"]) for item in coverages), 65)
        self.assertEqual(sum(int(item["uncovered_pair_count"]) for item in coverages), 145)
        self.assertEqual(sum(item["status"] == "complete" for item in coverages), 1)
        self.assertTrue(all(gap["product_id"] for item in coverages for gap in item["uncovered_pairs"]))
        json.dumps(coverages, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
