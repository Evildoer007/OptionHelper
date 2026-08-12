"""Checked-in, independently executable acceptance evidence for all 65 products."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import unittest

from runtime.contracts.contract_api import load_registry

from .product_acceptance_matrix_builder import (
    REQUIRED_CONFIG_FIELDS,
    build_acceptance_rows,
    csv_rows,
)


_FIXTURE_DIR = Path(__file__).with_name("fixtures")
_JSON_MATRIX = _FIXTURE_DIR / "product_acceptance_matrix.json"
_CSV_MATRIX = _FIXTURE_DIR / "product_acceptance_matrix.csv"


class ProductAcceptanceMatrixTests(unittest.TestCase):
    def test_checked_in_json_and_csv_are_aligned_65_product_execution_snapshots(self) -> None:
        saved_json = json.loads(_JSON_MATRIX.read_text(encoding="utf-8"))
        with _CSV_MATRIX.open(newline="", encoding="utf-8") as stream:
            saved_csv = list(csv.DictReader(stream))

        self.assertEqual(len(saved_json), 65)
        self.assertEqual(saved_csv, csv_rows(saved_json))

    def test_generator_executes_terminal_and_monitored_mc10_routes_without_a_calendar_downgrade(self) -> None:
        rows = build_acceptance_rows(("2.1", "3.1", "5.1", "9.18"))
        self.assertEqual([row["product_id"] for row in rows], ["2.1", "3.1", "5.1", "9.18"])
        for row in rows:
            self.assertEqual(row["method"], "monte_carlo")
            self.assertEqual(row["probe_scope"], "demo/logic-only")
            self.assertEqual(row["path_count"], 10)
            self.assertEqual(row["precision_status"], "demo_only")
            self.assertFalse(row["quote_eligible"])

    def test_every_snapshot_row_has_the_required_contract_market_output_and_gate_evidence(self) -> None:
        rows = json.loads(_JSON_MATRIX.read_text(encoding="utf-8"))
        registry = load_registry()
        executable = {
            product_id for product_id, product in registry["products"].items()
            if product["identity"]["entry_status"] is True
        }
        self.assertEqual(executable, {row["product_id"] for row in rows})

        for row in rows:
            with self.subTest(product_id=row["product_id"]):
                self.assertTrue(row["resolved_contract"]["bound"])
                self.assertTrue(row["resolved_contract"]["product_id_matches"])
                self.assertEqual(len(row["resolved_contract"]["contract_fingerprint"]), 64)
                self.assertEqual(
                    row["input_fields_summary"]["pricing_config_fields"],
                    list(REQUIRED_CONFIG_FIELDS),
                )
                self.assertEqual(row["input_fields_summary"]["market_data_ref"]["schema_id"], "market-history-v1")
                self.assertEqual(row["status"], "priced")
                self.assertIsInstance(row["pv_amount"], float)
                self.assertEqual(row["greeks_shape"]["names"], ["delta", "gamma", "rho", "theta", "vega"])
                self.assertEqual(row["greeks_shape"]["count"], 5)
                self.assertTrue(row["greeks_shape"]["finite_values"])
                self.assertEqual(row["risk_output_shape"]["curve_count"], 4)
                self.assertEqual(len(row["risk_output_shape"]["curve_point_counts"]), 4)
                self.assertTrue(all(count > 0 for count in row["risk_output_shape"]["curve_point_counts"]))
                self.assertEqual({key: row["market_date_fallback"][key] for key in (
                    "requested_valuation_date", "effective_valuation_session", "market_as_of_date",
                )}, {
                    "requested_valuation_date": "2023-07-28",
                    "effective_valuation_session": "2023-07-27",
                    "market_as_of_date": "2023-07-27",
                })
                self.assertEqual(row["market_date_fallback"]["status"], "priced")
                self.assertEqual(row["market_date_fallback"]["method"], "monte_carlo")
                self.assertEqual(row["market_date_fallback"]["path_count"], 10)
                self.assertEqual(row["market_date_fallback"]["precision_status"], "demo_only")
                self.assertFalse(row["market_date_fallback"]["quote_eligible"])
                self.assertEqual(row["fallback_probe_scope"], "date-routing-only")
                required = row["calendar_requirement"] == "trading-calendar"
                self.assertEqual(row["input_fields_summary"]["trading_calendar_ref_bound"], required)
                expected_error = "missing_trading_calendar" if required else "not_required"
                self.assertEqual(row["limitations_or_error"]["missing_calendar"]["category"], expected_error)
                # The acceptance matrix deliberately uses the MC10 logic
                # route for every formal MC-capable product.  Closed-form
                # coverage is tested separately and must not turn this
                # matrix into a quote-eligible result.
                self.assertEqual(row["method"], "monte_carlo")
                self.assertEqual(row["probe_scope"], "demo/logic-only")
                self.assertEqual(row["path_count"], 10)
                self.assertEqual(row["precision_status"], "demo_only")
                self.assertFalse(row["quote_eligible"])


if __name__ == "__main__":
    unittest.main()
