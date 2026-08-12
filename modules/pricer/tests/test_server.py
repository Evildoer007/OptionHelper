"""Pricer独立网页服务的回归测试。"""

from __future__ import annotations

import json
import csv
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.pricer import service as pricer_server
from core import tool_entry

from .pricer_test_fixtures import modulehost_v2_scope, stored_market_asset


class PricerServerTest(unittest.TestCase):
    def test_catalog_derives_fields_from_terms_without_old_product_specs(self) -> None:
        caller, context = modulehost_v2_scope(request_policy=("module.catalog",))
        catalog = tool_entry.call_tool(
            "pricer",
            {"action": "catalog"},
            caller_context=caller,
            host_context=context,
        )
        self.assertEqual(len(catalog["products"]), 65)
        call = next(item for item in catalog["products"] if item["product_id"] == "2.1")
        self.assertTrue(call["entry_status"])
        self.assertIn("K", {field["key"] for field in call["payoff_fields"]})
        self.assertEqual(call["pricing_methods"], ["black_scholes", "monte_carlo"])
        self.assertEqual(call["pricer_status"], "supported")
        barrier = next(item for item in catalog["products"] if item["product_id"] == "5.1")
        self.assertEqual(barrier["pricer_status"], "supported")
        self.assertEqual(barrier["pricer_methods"], ["monte_carlo"])

    def test_pricer_refuses_to_substitute_valuation_spot_for_contract_reference(self) -> None:
        runtime = pricer_server.PricerRuntime()
        with self.assertRaisesRegex(pricer_server.PricerWebInputError, "绑定真实DataAssetRef"):
            runtime.run({
                "product_id": "2.1",
                "identity": {"underlyings": ["000905.SH"]},
                "pricing_config": {"model_method": "black_scholes"},
            })

    def test_pricer_resolves_contract_and_prices_without_backtester_service(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pricer-server-data-") as temporary:
            store, reference = stored_market_asset(Path(temporary))
            pricing_runtime = pricer_server.PricerRuntime(data_port=store)
            pricing_request = {
                "product_id": "2.1",
                "identity": {"underlyings": ["000905.SH"]},
                "term_overrides": {"T": 2 / 244, "Pi_0": 1.0},
                "pricing_config": {
                    "valuation_date": "2023-07-28",
                    "model_method": "black_scholes",
                    "risk_free_rate": 0.01,
                },
                "market_data_refs": [asdict(reference)],
            }
            with patch.object(pricer_server, "_write_run"):
                priced = pricing_runtime.run(pricing_request)
        self.assertEqual(priced["pricing"]["method"], "black_scholes")
        self.assertEqual(
            set(priced["resolved_contract"]),
            {
                "identity", "terms", "term_sources", "paths", "product_version",
                "resolved_schedules", "contract_fingerprint", "registry_snapshot_hash",
                "product_snapshot_hash", "product_paths_hash",
            },
        )

    def test_pricer_derives_remaining_time_from_contract_start_date_when_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pricer-server-data-") as temporary:
            store, reference = stored_market_asset(Path(temporary))
            runtime = pricer_server.PricerRuntime(data_port=store)
            request = {
                "product_id": "2.1",
                "identity": {
                    "underlyings": ["000905.SH"],
                    "contract_start_date": "2026-07-18",
                },
                "pricing_config": {"model_method": "black_scholes", "risk_free_rate": 0.01},
                "market_data_refs": [asdict(reference)],
            }
            with patch.object(pricer_server, "_write_run"):
                priced = runtime.run(request)
        valuation_date = date.fromisoformat(priced["market_snapshot"]["valuation_date"])
        elapsed_days = (valuation_date - date(2026, 7, 18)).days
        self.assertAlmostEqual(
            priced["pricing"]["input_snapshot"]["pricing_config"]["time_to_maturity"],
            1.0 - elapsed_days / 365.0,
        )

    def test_pricer_rejects_expired_contract_when_remaining_time_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pricer-server-data-") as temporary:
            store, reference = stored_market_asset(Path(temporary))
            runtime = pricer_server.PricerRuntime(data_port=store)
            request = {
                "product_id": "2.1",
                "identity": {
                    "underlyings": ["000905.SH"],
                    "contract_start_date": "2025-07-24",
                },
                "pricing_config": {"model_method": "black_scholes", "risk_free_rate": 0.01},
                "market_data_refs": [asdict(reference)],
            }
            with self.assertRaisesRegex(pricer_server.PricerWebInputError, "已到期"):
                runtime.run(request)

    def test_output_is_partitioned_by_module_task_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {"product_id": "2.1"}
            fingerprint = "a" * 64
            output = {
                "ok": True,
                "module": "pricer",
                "contract_fingerprint": fingerprint,
                "resolved_contract": {"contract_fingerprint": fingerprint},
                "pricing": {"contract_fingerprint": fingerprint},
            }
            with patch.object(pricer_server, "PROJECT_ROOT", root), patch.object(pricer_server, "RESULT_ROOT", root / "result"):
                pricer_server._write_run("task_001", "run_001", request, output)
            destination = root / "result" / "output_pricing" / "task_001" / "run_001"
            self.assertEqual(json.loads((destination / "input.json").read_text(encoding="utf-8")), request)
            self.assertEqual(json.loads((destination / "result.json").read_text(encoding="utf-8")), output)
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"], ["input.json", "resolved_contract.json", "result.json", "manifest.json"])
            self.assertEqual(manifest["contract_fingerprint"], fingerprint)
            with (root / "result" / "output_pricing" / "pricing_results.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["产品编号"], "2.1")


if __name__ == "__main__":
    unittest.main()
