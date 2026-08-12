"""Pricer受控历史数据边界回归。"""

from __future__ import annotations

from pathlib import Path
import hashlib
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.pricer import service
from .pricer_test_fixtures import market_csv_payload


class _HostDataPort:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve(self, ref, *, tenant_id: str) -> Path:
        assert tenant_id == "local"
        return self.path


class ServiceDataBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pricer-boundary-")
        self.history_path = Path(self.temporary.name) / "market.csv"
        self.history_path.write_bytes(market_csv_payload())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_csv_is_limited_to_pricer_datastore(self) -> None:
        with self.assertRaisesRegex(ValueError, "绑定真实DataAssetRef"):
            service._history_path(None)
        with self.assertRaisesRegex(service.PricerWebInputError, "受控DataStore"):
            service._history_path("/private/tmp/not-pricer-history.csv")

    def test_host_data_asset_ref_is_resolved_by_injected_port(self) -> None:
        runtime = service.PricerRuntime(data_port=_HostDataPort(self.history_path))
        history = service.load_market_history(self.history_path)
        sessions = tuple(history["date"].dt.strftime("%Y-%m-%d").unique())
        reference = {
            "data_asset_id": "market-000905",
            "storage_ref": "data:local:market-000905:abc:meta",
            "media_type": "text/csv",
            "schema_id": "market-history-v1",
            "asset_ids": ("000905.SH",),
            "normalized_fields": ("date", "asset_id", "close", "adj_close"),
            "coverage": {"start_date": sessions[0], "end_date": sessions[-1], "sessions": sessions, "calendar_id": "CN-SSE", "calendar_version": "fixture-v1"},
            "row_count": len(history), "price_convention": {},
            "content_hash": hashlib.sha256(self.history_path.read_bytes()).hexdigest(), "lineage": {},
        }
        path, ref = runtime._history_asset(reference)
        self.assertEqual(path, self.history_path)
        self.assertEqual(ref.storage_ref, reference["storage_ref"])
        self.assertEqual(ref.content_hash, reference["content_hash"])

    def test_host_asset_content_hash_is_checked_against_resolved_bytes(self) -> None:
        runtime = service.PricerRuntime(data_port=_HostDataPort(self.history_path))
        with self.assertRaisesRegex(service.PricerWebInputError, "content_hash"):
            runtime._history_asset({
                "data_asset_id": "market-000905", "storage_ref": "data:test", "media_type": "text/csv",
                "schema_id": "market-history-v1", "asset_ids": ("000905.SH",),
                "normalized_fields": ("date", "asset_id", "close", "adj_close"), "coverage": {},
                "row_count": 0, "price_convention": {}, "content_hash": "0" * 64, "lineage": {},
            })

    def test_pricer_never_imports_datafetcher(self) -> None:
        source = Path(service.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(?:from|import)\s+.*DataFetcher")


if __name__ == "__main__":
    unittest.main()
