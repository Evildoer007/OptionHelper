"""Backtester外置DataStore与opaque DataAssetRef边界回归。"""

# ruff: noqa: E402

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest
from modules.backtester.historical_data import HistoricalDataError, load_local_historical_data, load_port_historical_data
from runtime.contracts.contract_api import load_registry, resolve_contract

from modules.backtester.tests.fixtures import two_asset_history


PAYLOAD = b"date,asset_id,close,adj_close\n2024-01-02,000905.SH,100,101\n2024-01-03,000905.SH,101,102\n"


def _reference(storage_ref: str) -> dict[str, object]:
    return {
        "data_asset_id": "external-market-history",
        "storage_ref": storage_ref,
        "media_type": "text/csv",
        "schema_id": "market-history-v1",
        "asset_ids": ["000905.SH"],
        "normalized_fields": ["date", "asset_id", "close", "adj_close"],
        "coverage": {"by_asset": {"000905.SH": {"start_date": "2024-01-02", "end_date": "2024-01-03", "row_count": 2}}},
        "row_count": 2,
        "price_convention": {
            "contract_close_field": "close", "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close", "hv_adjustment": "forward", "close_equals_adj_close": False,
        },
        "content_hash": sha256(PAYLOAD).hexdigest(),
        "lineage": {"provider": "app-external-test"},
        "tenant_id": "tenant-a",
        "created_by": "eval",
        "access_scope": ["read"],
        "partition_spec": {},
    }


class _OpaqueExternalDataStore:
    def __init__(self) -> None:
        self.seen_storage_ref: str | None = None

    def read_bytes(self, ref: object, *, tenant_id: str) -> bytes:
        self.seen_storage_ref = str(getattr(ref, "storage_ref"))
        if tenant_id != "tenant-a":
            raise PermissionError("tenant mismatch")
        return PAYLOAD


class ExternalDataStoreBoundaryTest(unittest.TestCase):
    def test_external_controlled_data_root_never_becomes_repository_relative_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="backtester-external-data-") as temporary:
            data_root = Path(temporary)
            source = data_root / "market.csv"
            source.write_bytes(PAYLOAD)
            history = load_local_historical_data(source, project_root=PROJECT_ROOT, data_root=data_root)
            self.assertRegex(history.data_asset_ref["storage_ref"], r"^local-data:sha256:[0-9a-f]{64}$")
            self.assertNotIn(str(data_root), json.dumps(history.data_asset_ref, ensure_ascii=False))
            reloaded = load_local_historical_data(history.data_asset_ref, project_root=PROJECT_ROOT, data_root=data_root)
            self.assertEqual(reloaded.data_asset_ref["content_hash"], sha256(PAYLOAD).hexdigest())

    def test_injected_external_store_resolves_any_opaque_storage_ref(self) -> None:
        storage_ref = "app-data:tenant-a:external-market-history"
        store = _OpaqueExternalDataStore()
        with tempfile.TemporaryDirectory(prefix="backtester-data-root-") as temporary:
            history = load_port_historical_data(
                {"asset_id": "000905.SH"},
                project_root=PROJECT_ROOT,
                data_root=Path(temporary),
                call_port=lambda _: {"ok": True, "data_asset_ref": _reference(storage_ref)},
                data_store=store,
            )
        self.assertEqual(store.seen_storage_ref, storage_ref)
        self.assertEqual(history.data_asset_ref["storage_ref"], storage_ref)
        self.assertNotIn(temporary, json.dumps(history.data_asset_ref, ensure_ascii=False))

    def test_arbitrary_request_path_remains_rejected_without_datastore(self) -> None:
        reference = _reference("/private/tmp/uncontrolled-market.csv")
        with tempfile.TemporaryDirectory(prefix="backtester-data-root-") as temporary:
            with self.assertRaisesRegex(HistoricalDataError, "受控data目录"):
                load_port_historical_data(
                    {"asset_id": "000905.SH"},
                    project_root=PROJECT_ROOT,
                    data_root=Path(temporary),
                    call_port=lambda _: {"ok": True, "data_asset_ref": reference},
                )
        with tempfile.TemporaryDirectory(prefix="backtester-data-root-") as temporary:
            root = Path(temporary)
            data_root = root / "data"
            data_root.mkdir()
            outside = root / "uncontrolled-market.csv"
            outside.write_bytes(PAYLOAD)
            with self.assertRaisesRegex(HistoricalDataError, "受控data目录"):
                load_local_historical_data(outside, project_root=PROJECT_ROOT, data_root=data_root)

    def test_datastore_reference_rejects_extra_request_path_metadata(self) -> None:
        reference = _reference("app-data:tenant-a:external-market-history")
        reference["request_path"] = "/srv/private/market.csv"
        store = _OpaqueExternalDataStore()
        with tempfile.TemporaryDirectory(prefix="backtester-data-root-") as temporary:
            with self.assertRaisesRegex(HistoricalDataError, "未知字段：request_path"):
                load_port_historical_data(
                    {"asset_id": "000905.SH"},
                    project_root=PROJECT_ROOT,
                    data_root=Path(temporary),
                    call_port=lambda _: {"ok": True, "data_asset_ref": reference},
                    data_store=store,
                )
        self.assertIsNone(store.seen_storage_ref)

    def test_65_product_audited_metric_digest(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        rows: dict[str, object] = {}
        for product_id in load_registry()["products"]:
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            result = backtest(BacktestInput(
                contract,
                BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
                history,
            )).to_dict()
            rows[product_id] = {
                "ledger_hash": result["ledger_hash"],
                "common_metrics": result["common_metrics"],
                "specialized_metrics": result["specialized_metrics"],
            }
        digest = sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        # 专属指标已按Worst-Of、合同case分段及事件条件PnL审计；经济结果由独立账本摘要门禁锁定。
        self.assertEqual(digest, "08b30d20b726cc17555902b778c3dbeb621ff402e5c9a334bf28d980ca038086")


if __name__ == "__main__":
    unittest.main()
