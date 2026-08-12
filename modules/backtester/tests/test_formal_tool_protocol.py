"""Backtester正式BacktestInput与Host Store边界回归。"""

# ruff: noqa: E402

from __future__ import annotations

import csv
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester.service import BacktesterWebInputError, call_tool
from modules.backtester.tests.fixtures import modulehost_v2_scope, single_asset_market_payload
from runtime.adapters.local_store import LocalDataStore, LocalResultStore
from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import ModuleRunRef
from runtime.protocol.module_host import verify_module_host_context


class FormalBacktesterProtocolTest(unittest.TestCase):
    def test_frozen_contract_and_data_asset_are_committed_once(self) -> None:
        payload = single_asset_market_payload()
        rows = list(csv.DictReader(payload.decode("utf-8").splitlines()))
        sessions = tuple(row["date"] for row in rows)
        contract = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"], "contract_start_date": sessions[0]},
            term_overrides={"T": 2 / 365},
        )
        with tempfile.TemporaryDirectory(prefix="backtester-formal-") as temporary:
            data_store = LocalDataStore(Path(temporary) / "data")
            data_ref = data_store.put_bytes(
                tenant_id="tenant-a",
                data_asset_id="formal-market",
                payload=payload,
                media_type="text/csv",
                schema_id="market-history-v1",
                asset_ids=("000905.SH",),
                normalized_fields=("date", "asset_id", "open", "high", "low", "close", "adj_close"),
                coverage={"start": sessions[0], "end": sessions[-1], "sessions": sessions},
                row_count=len(rows),
                price_convention={
                    "contract_close_field": "close",
                    "contract_adjustment": "unadjusted",
                    "hv_close_field": "adj_close",
                    "hv_adjustment": "forward",
                    "close_equals_adj_close": True,
                    "calendar": "trading_days",
                },
                lineage={"fixture": "formal-backtester"},
                created_by="host-test",
            )
            result_store = LocalResultStore(Path(temporary) / "result")
            caller, context, token_secret = modulehost_v2_scope(contract)
            verify_module_host_context(
                context,
                token_secret=token_secret,
                session_id=caller.session_id,
                principal_id=caller.principal_id,
                audience=caller.audience,
                now=1_800_000_000,
            )
            request = {
                "action": "run",
                "contract": contract.to_protocol_dict(),
                "backtest_config": {"entry_rule": "explicit", "entry_dates": [sessions[0]]},
                "historical_data": data_ref,
            }
            output = call_tool(
                request, host_context=context, data_store=data_store,
                result_store=result_store, tenant_id="tenant-a",
            )

            reference = ModuleRunRef(**output["module_run_ref"])
            run_dir = result_store.resolve_module_run(reference, tenant_id="tenant-a")
            committed = (run_dir / "commit_marker.json").is_file()
            snapshot = json.loads((run_dir / "input_snapshot.json").read_text(encoding="utf-8"))
            persisted_output = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            persisted = json.dumps({"snapshot": snapshot, "result": persisted_output}, ensure_ascii=False)

            self.assertEqual(set(snapshot), {"contract", "backtest_config", "historical_data"})
            self.assertEqual(snapshot["historical_data"]["storage_ref"], data_ref.storage_ref)
            self.assertNotIn("data_store", persisted.casefold())
            self.assertNotIn(str(Path(temporary)), persisted)

        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["resolved_contract"]["contract_fingerprint"], contract.contract_fingerprint)
        self.assertEqual(reference.module, "backtester")
        self.assertTrue(committed)

    def test_service_rejects_normalized_payload_datastore_aliases(self) -> None:
        for field in ("data_store", "DataStore", "data-store", "DATA_STORE"):
            with self.assertRaisesRegex(BacktesterWebInputError, "Host私有依赖"):
                call_tool({"action": "catalog", field: "forged"})

    def test_formal_read_rejects_bytes_that_do_not_match_signed_reference(self) -> None:
        payload = single_asset_market_payload()
        with tempfile.TemporaryDirectory(prefix="backtester-formal-tamper-") as temporary:
            data_store = LocalDataStore(Path(temporary) / "data")
            data_ref = data_store.put_bytes(
                tenant_id="tenant-a", data_asset_id="formal-market", payload=payload,
                media_type="text/csv", schema_id="market-history-v1", asset_ids=("000905.SH",),
                normalized_fields=("date", "asset_id", "close", "adj_close"), created_by="host-test",
            )
            contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
            _caller_context, context, _token_secret = modulehost_v2_scope(contract)

            class TamperedStore:
                def read_bytes(self, _ref: object, *, tenant_id: str) -> bytes:
                    self.tenant_id = tenant_id
                    return payload + b"\n"

            with self.assertRaisesRegex(BacktesterWebInputError, "content_hash"):
                call_tool(
                    {"action": "run", "contract": contract.to_protocol_dict(), "backtest_config": {}, "historical_data": data_ref},
                    host_context=context, data_store=TamperedStore(), tenant_id="tenant-a",
                )

    def test_formal_run_rejects_non_market_history_assets_before_store_read(self) -> None:
        payload = single_asset_market_payload()
        with tempfile.TemporaryDirectory(prefix="backtester-formal-schema-") as temporary:
            data_store = LocalDataStore(Path(temporary) / "data")
            data_ref = data_store.put_bytes(
                tenant_id="tenant-a", data_asset_id="formal-market", payload=payload,
                media_type="text/csv", schema_id="market-history-v1", asset_ids=("000905.SH",),
                normalized_fields=("date", "asset_id", "close", "adj_close"), created_by="host-test",
            )
            contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
            _caller_context, context, _token_secret = modulehost_v2_scope(contract)

            for forged in (
                replace(data_ref, schema_id="trading-calendar"),
                replace(data_ref, media_type="application/json"),
            ):
                with self.subTest(schema_id=forged.schema_id, media_type=forged.media_type):
                    with self.assertRaisesRegex(BacktesterWebInputError, "market-history-v1 text/csv"):
                        call_tool(
                            {
                                "action": "run", "contract": contract.to_protocol_dict(),
                                "backtest_config": {}, "historical_data": forged,
                            },
                            host_context=context, data_store=data_store, tenant_id="tenant-a",
                        )


if __name__ == "__main__":
    unittest.main()
