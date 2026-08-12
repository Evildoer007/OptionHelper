"""65个登记结构的受控历史回放矩阵回归。"""

# ruff: noqa: E402

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, HistoricalData
from modules.backtester.catalog_replay_matrix import replay_catalog_matrix, write_catalog_replay_matrix
from modules.backtester.tests.fixtures import two_asset_history


class CatalogReplayMatrixTest(unittest.TestCase):
    def test_65_products_emit_same_source_json_and_csv_matrix_in_temporary_directory(self) -> None:
        """受控夹具逐产品回放；partial必须保留为partial而非伪成功。"""

        rows = replay_catalog_matrix(
            HistoricalData.from_frame(two_asset_history()),
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
        )

        self.assertEqual(len(rows), 65)
        required = {
            "product_id", "product_name", "status", "data_asset_ref", "entry_rule", "complete_tenor",
            "sample_count", "skipped_count", "skipped_entries", "observed_events", "observed_path_cases",
            "ledger", "ledger_hash", "branch_coverage", "contract_cashflow_pnl", "return_denominator",
            "win_rate", "client_net_pnl", "reason",
        }
        self.assertTrue(all(required <= set(row) for row in rows))
        self.assertTrue(all(row["status"] in {"complete", "partial", "blocked"} for row in rows))
        self.assertTrue(all(row["client_net_pnl"] == {"status": "not_modelled", "value": None} for row in rows))
        self.assertTrue(all(row["contract_cashflow_pnl"]["basis"] == "contract_cashflow_before_external_costs" for row in rows))
        self.assertTrue(all(row["win_rate"]["numerator"] == "contract_cashflow_pnl_gt_zero_count" for row in rows))
        self.assertTrue(all(row["win_rate"]["denominator"] == "valid_trade_count" for row in rows))
        self.assertTrue(all(row["win_rate"]["denominator_count"] == row["sample_count"] for row in rows if row["status"] != "blocked"))
        self.assertTrue(all(row["skipped_count"] == 0 for row in rows))
        self.assertTrue(all(row["status"] == row["branch_coverage"]["status"] for row in rows))
        self.assertTrue(any(row["status"] == "complete" for row in rows))
        self.assertTrue(any(row["status"] == "partial" for row in rows))

        with tempfile.TemporaryDirectory(prefix="backtester-catalog-matrix-") as temporary:
            json_path, csv_path = write_catalog_replay_matrix(rows, Path(temporary))
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            with csv_path.open(encoding="utf-8", newline="") as source:
                csv_rows = list(csv.DictReader(source))

        self.assertEqual(len(json_rows), 65)
        self.assertEqual(len(csv_rows), 65)
        self.assertEqual([row["product_id"] for row in json_rows], [row["product_id"] for row in csv_rows])
        for json_row, csv_row in zip(json_rows, csv_rows):
            self.assertEqual(json_row["status"], csv_row["status"])
            self.assertEqual(json_row["product_name"], csv_row["product_name"])
            self.assertEqual(json_row["data_asset_ref"], json.loads(csv_row["data_asset_ref"]))
            self.assertEqual(json_row["branch_coverage"], json.loads(csv_row["branch_coverage"]))
            self.assertEqual(json_row["ledger"], json.loads(csv_row["ledger"]))
            self.assertEqual(json_row["win_rate"], json.loads(csv_row["win_rate"]))

    def test_skipped_explicit_entry_makes_a_completed_coverage_product_partial(self) -> None:
        rows = replay_catalog_matrix(
            HistoricalData.from_frame(two_asset_history()),
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04", "2030-01-02")),
        )
        row = next(item for item in rows if item["product_id"] == "10.4")
        self.assertEqual(row["branch_coverage"]["status"], "complete")
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["skipped_reason_counts"], {"entry_date_not_in_aligned_trading_calendar": 1})
        self.assertEqual(row["reason"], "skipped_entries")


if __name__ == "__main__":
    unittest.main()
