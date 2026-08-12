"""DataFetcher真实本机路径测试，不调用外部数据源。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "datafetcher" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.datafetcher.config import DataFetcherConfig
from modules.datafetcher.models import DataRequest
from modules.datafetcher.market_conventions import china_market_convention
from modules.datafetcher.providers.local import LocalCsvProvider
from modules.datafetcher.quality_validator import DataQualityError, validate_daily_history
from modules.datafetcher.request_validator import request_fingerprint
from modules.datafetcher.request_validator import validate_request
from modules.datafetcher.server import Handler
from modules.datafetcher import service as datafetcher_service
from modules.datafetcher.service import call_tool, fetch_data, list_data_assets
from runtime.adapters.local_store import LocalDataStore
from runtime.protocol.models import CallerContext as ProtocolCallerContext, DataAssetRef as ProtocolDataAssetRef, SecretRef as ProtocolSecretRef


def _hold_cache_request_lock(root: str, identity: str, ready, release) -> None:
    from modules.datafetcher.cache_resolver import LocalCache
    with LocalCache(Path(root)).fetch_lock(identity):
        ready.set()
        release.wait(5)


def _observe_cache_request_lock(root: str, identity: str, acquired) -> None:
    from modules.datafetcher.cache_resolver import LocalCache
    with LocalCache(Path(root)).fetch_lock(identity):
        acquired.set()


def _verified_sse_sessions(
    sessions: tuple[str, ...], *, coverage_start: str | None = None, coverage_end: str | None = None,
) -> dict[str, object]:
    return {
        "sessions": sessions,
        "calendar_id": "CN-SSE-fixture",
        "calendar_version": "fixture-v1",
        "coverage_start_date": coverage_start or sessions[0],
        "coverage_end_date": coverage_end or sessions[-1],
    }


class DataFetcherRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "input"
        self.input_root.mkdir()
        self.csv = self.input_root / "fixture.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "asset_id": ["510300.SH"] * 3,
            "close": [5000.0, 5010.0, 5020.0],
            "adj_close": [5000.0, 5010.0, 5020.0],
        }).to_csv(self.csv, index=False)
        self.other_csv = self.input_root / "other.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "asset_id": ["510300.SH"] * 3,
            "close": [6000.0, 6010.0, 6020.0],
            "adj_close": [6000.0, 6010.0, 6020.0],
        }).to_csv(self.other_csv, index=False)
        self.config = DataFetcherConfig(
            data_root=self.root / "data",
            result_root=self.root / "result",
            cache_root=self.root / "cache",
            local_csv_root=self.input_root,
            provider_priority=("local",),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, start: str = "2024-01-02", end: str = "2024-01-03", local_csv: str = "fixture.csv") -> DataRequest:
        return DataRequest(
            asset_ids=("510300.SH",),
            start_date=start,
            end_date=end,
            fields=("close", "adj_close"),
            source_priority=("local",),
            cache_policy="extend_only",
            local_csv=local_csv,
        )

    def test_local_csv_creates_protocol_shaped_asset_and_sanitized_run(self) -> None:
        result = fetch_data(self.request(), config=self.config, task_id="runtime")
        self.assertTrue(result.ok)
        asset = result.run.data_asset_ref
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertIsInstance(asset, ProtocolDataAssetRef)
        self.assertEqual(set(asdict(asset)), set(ProtocolDataAssetRef.__dataclass_fields__))
        self.assertEqual(asset.normalized_fields, ("date", "asset_id", "close", "adj_close"))
        self.assertEqual(asset.coverage["by_asset"]["510300.SH"]["row_count"], 2)
        self.assertEqual(asset.lineage["provider"], "local")
        self.assertEqual(asset.tenant_id, "local")
        self.assertEqual(asset.created_by, "local-user")
        self.assertIn("valuation_timestamp", asset.price_convention)
        self.assertIn("calendar_version", asset.price_convention)
        self.assertTrue(asset.storage_ref.startswith("data:local:data-"))
        self.assertTrue(LocalDataStore(self.config.data_root.resolve()).resolve(asset, tenant_id="local").is_file())
        self.assertNotIn(str(self.csv), asset.storage_ref)
        snapshot = (self.root / "result" / "output_datafetch" / "tenants" / "local" / "runtime" / result.run.data_fetch_run_id / "input_snapshot.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.csv), snapshot)

    def test_asset_ref_uses_shared_market_history_schema_and_coverage_shape(self) -> None:
        from modules.pricer.models import HistoricalData, validate_market_data_asset

        result = fetch_data(self.request(), config=self.config)
        self.assertTrue(result.ok)
        asset = result.run.data_asset_ref
        assert asset is not None
        self.assertEqual(asset.schema_id, "market-history-v1")
        self.assertTrue({"date", "asset_id", "close", "adj_close"}.issubset(asset.normalized_fields))
        self.assertEqual(
            {"start_date", "end_date", "sessions", "calendar_id", "calendar_version", "by_asset"}.difference(asset.coverage),
            set(),
        )
        self.assertEqual(asset.coverage["sessions"], ["2024-01-02", "2024-01-03"])
        self.assertEqual(asset.coverage["calendar_version"], "unverified")
        self.assertFalse(str(asset.coverage["calendar_id"]).startswith("CN-"))
        historical = HistoricalData(
            source_ref=asset.storage_ref,
            rows=result.data_preview,
            content_hash=asset.content_hash,
            schema_id=asset.schema_id,
            asset_ids=asset.asset_ids,
            normalized_fields=asset.normalized_fields,
            coverage=asset.coverage,
        )
        recognized = validate_market_data_asset(asset, historical, asset.asset_ids)
        self.assertFalse(recognized["verified_cn_sessions"])

    def test_app_entry_requires_protocol_injection_and_preserves_caller(self) -> None:
        handler = getattr(datafetcher_service, "call_tool_from_app", None)
        self.assertTrue(callable(handler))
        caller = ProtocolCallerContext("tenant-app", "user-app", "user", ("data:read",), "session-app", "app", "request-app")
        secret_ref = ProtocolSecretRef(provider="keychain", key="optionhelper/ifind")
        payload = {
            "asset_id": "510300.SH", "start_date": "2024-01-02", "end_date": "2024-01-03",
            "fields": ["close", "adj_close"], "source_priority": ["local"], "cache_policy": "extend_only", "local_csv": "fixture.csv",
        }
        with patch.object(DataFetcherConfig, "from_runtime", return_value=self.config):
            result = handler(payload, caller_context=caller, secret_ref=secret_ref, secret_port=lambda _reference: "{}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_asset_ref"]["tenant_id"], "tenant-app")
        self.assertEqual(result["data_asset_ref"]["created_by"], "user-app")
        self.assertNotIn("secret_ref", json.dumps(result))
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "result").rglob("*.json")
        )
        self.assertNotIn(secret_ref.key, persisted)

    def test_app_entry_rejects_missing_or_forged_injection(self) -> None:
        handler = getattr(datafetcher_service, "call_tool_from_app", None)
        self.assertTrue(callable(handler))
        caller = ProtocolCallerContext("tenant-app", "user-app", "user", ("data:read",), "session-app", "app", "request-app")
        secret_ref = ProtocolSecretRef(provider="keychain", key="optionhelper/ifind")
        with self.assertRaises(ValueError):
            handler({}, caller_context=None, secret_ref=secret_ref)
        with self.assertRaises(ValueError):
            handler({}, caller_context=caller, secret_ref={"provider": "keychain", "key": "optionhelper/ifind"})
        with self.assertRaises(ValueError):
            handler({"access_token": "forbidden"}, caller_context=caller, secret_ref=secret_ref)

    def test_failed_app_tenant_run_keeps_tenant_partition_and_complete_manifest(self) -> None:
        caller = ProtocolCallerContext("tenant-app", "user-app", "user", ("data:read",), "session-app", "app", "request-app")
        request = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03",
            fields=("close",), source_priority=("ifind_http",), offline=True,
        )
        result = fetch_data(request, caller, config=self.config, task_id="failed-app")
        self.assertFalse(result.ok)
        self.assertEqual(result.run.tenant_id, "tenant-app")
        manifests = list((self.root / "result" / "output_datafetch" / "tenants").rglob("manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["tenant_id"], "tenant-app")
        self.assertTrue(manifest["complete"])
        self.assertIn("error.json", manifest["file_hashes"])
        run_dir = manifests[0].parent
        for filename, expected_hash in manifest["file_hashes"].items():
            self.assertEqual(__import__("hashlib").sha256((run_dir / filename).read_bytes()).hexdigest(), expected_hash)

    def test_same_request_reuses_cache_without_provider_call(self) -> None:
        first = fetch_data(self.request(), config=self.config)
        second = fetch_data(self.request(), config=self.config)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.run.cache_decision, "cache_hit")
        self.assertEqual(second.run.provider_calls, ())
        self.assertEqual(first.run.data_asset_ref.content_hash, second.run.data_asset_ref.content_hash)

    def test_result_exposes_real_preview_and_quality_summary(self) -> None:
        result = fetch_data(self.request(), config=self.config)
        self.assertTrue(result.ok)
        payload = result.to_dict()
        self.assertEqual(payload["data_preview"][0]["date"], "2024-01-02")
        self.assertEqual(payload["data_preview"][0]["close"], 5000.0)
        self.assertEqual(payload["quality_report"]["unordered_rows"], 0)
        self.assertEqual(payload["quality_report"]["observed_rows"]["status"], "valid")
        self.assertEqual(payload["quality_report"]["calendar_completeness"], "unverified")

    def test_asset_list_uses_controlled_cache_index_only(self) -> None:
        result = fetch_data(self.request(), config=self.config)
        self.assertTrue(result.ok)
        from modules.datafetcher.cache_resolver import LocalCache
        assets = LocalCache(self.config.cache_root).list_assets(tenant_id="local")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["data_asset_ref"]["data_asset_id"], result.run.data_asset_ref.data_asset_id)
        self.assertNotIn("data_path", assets[0])

    def test_extend_only_merges_missing_tail_without_duplicate_rows(self) -> None:
        original_fetch = LocalCsvProvider.fetch
        requested_intervals: list[tuple[str, str]] = []

        def recorded_fetch(provider, request, config):
            requested_intervals.append((request.start_date, request.end_date))
            return original_fetch(provider, request, config)

        with patch.object(LocalCsvProvider, "fetch", new=recorded_fetch):
            first = fetch_data(self.request(end="2024-01-03"), config=self.config)
            extended = fetch_data(self.request(end="2024-01-04"), config=self.config)
        self.assertTrue(first.ok)
        self.assertTrue(extended.ok)
        self.assertEqual(extended.run.cache_decision, "cache_extended")
        self.assertEqual(requested_intervals, [("2024-01-02", "2024-01-03"), ("2024-01-04", "2024-01-04")])
        asset = extended.run.data_asset_ref
        assert asset is not None
        self.assertEqual(asset.row_count, 3)
        self.assertEqual(asset.coverage["by_asset"]["510300.SH"]["end_date"], "2024-01-04")

    def test_explicit_calendar_cache_hit_does_not_fetch_holiday_edges(self) -> None:
        config = DataFetcherConfig(
            data_root=self.root / "holiday-data", result_root=self.root / "holiday-result",
            cache_root=self.root / "holiday-cache", local_csv_root=self.input_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": _verified_sse_sessions(("2024-01-02", "2024-01-03", "2024-01-04"), coverage_start="2024-01-01", coverage_end="2024-01-07")},
        )
        first = fetch_data(self.request(start="2024-01-02", end="2024-01-04"), config=config)
        calls: list[tuple[str, str]] = []
        original_fetch = LocalCsvProvider.fetch

        def recorded_fetch(provider, request, provider_config):
            calls.append((request.start_date, request.end_date))
            return original_fetch(provider, request, provider_config)

        with patch.object(LocalCsvProvider, "fetch", new=recorded_fetch):
            reused = fetch_data(self.request(start="2024-01-01", end="2024-01-07"), config=config)
        self.assertTrue(first.ok)
        self.assertTrue(reused.ok)
        self.assertEqual(reused.run.cache_decision, "cache_hit")
        self.assertEqual(calls, [])

    def test_explicit_calendar_cache_fills_only_internal_missing_session(self) -> None:
        gap_csv = self.input_root / "internal-gap.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-04"], "asset_id": ["510300.SH", "510300.SH"],
            "close": [5.0, 5.2], "adj_close": [5.0, 5.2],
        }).to_csv(gap_csv, index=False)
        base = DataFetcherConfig(
            data_root=self.root / "internal-data", result_root=self.root / "internal-result",
            cache_root=self.root / "internal-cache", local_csv_root=self.input_root, provider_priority=("local",),
        )
        request = self.request(start="2024-01-02", end="2024-01-04", local_csv="internal-gap.csv")
        seeded = fetch_data(request, config=base)
        explicit = DataFetcherConfig(
            data_root=base.data_root, result_root=base.result_root, cache_root=base.cache_root,
            local_csv_root=base.local_csv_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": _verified_sse_sessions(("2024-01-02", "2024-01-03", "2024-01-04"))},
        )
        calls: list[tuple[str, str]] = []

        def missing_session(_provider, interval, _config):
            calls.append((interval.start_date, interval.end_date))
            return pd.DataFrame({
                "date": ["2024-01-03"], "asset_id": ["510300.SH"],
                "close": [5.1], "adj_close": [5.1],
            })

        with patch.object(LocalCsvProvider, "fetch", new=missing_session):
            completed = fetch_data(request, config=explicit)
        self.assertTrue(seeded.ok)
        self.assertTrue(completed.ok)
        self.assertEqual(calls, [("2024-01-03", "2024-01-03")])
        self.assertEqual(completed.run.cache_decision, "cache_extended")
        self.assertEqual(completed.run.data_asset_ref.row_count, 3)

    def test_observed_gap_is_calendar_unverified_not_failed(self) -> None:
        gap_csv = self.input_root / "weekday-gap.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-04"],
            "asset_id": ["510300.SH", "510300.SH"],
            "close": [5000.0, 5020.0],
            "adj_close": [5000.0, 5020.0],
        }).to_csv(gap_csv, index=False)
        request = DataRequest(
                asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-04", fields=("close",),
                source_priority=("local",), cache_policy="extend_only", local_csv="weekday-gap.csv",
            )
        result = fetch_data(request, config=self.config)
        reused = fetch_data(request, config=self.config)
        self.assertTrue(result.ok)
        self.assertEqual(result.quality_report["observed_rows"]["status"], "valid")
        self.assertEqual(result.quality_report["calendar_completeness"], "unverified")
        self.assertEqual(reused.run.cache_decision, "cache_hit")

    def test_explicit_calendar_rejects_missing_sessions_and_accepts_complete(self) -> None:
        frame = pd.DataFrame({
            "date": ["2024-01-02", "2024-01-04"],
            "asset_id": ["510300.SH", "510300.SH"],
            "close": [5000.0, 5020.0],
        })
        with self.assertRaisesRegex(DataQualityError, "2024-01-03"):
            validate_daily_history(
                frame, ("close",), expected_trading_dates={"510300.SH": ("2024-01-02", "2024-01-03", "2024-01-04")},
            )
        complete = validate_daily_history(
            frame, ("close",), expected_trading_dates={"510300.SH": ("2024-01-02", "2024-01-04")},
        )
        self.assertEqual(complete["calendar_completeness"], "complete")

    def test_auto_adjustment_is_per_asset_for_stock_etf_index_and_mixed_request(self) -> None:
        index = china_market_convention("000905.SH", "auto")
        etf = china_market_convention("510300.SH", "auto")
        stock = china_market_convention("600000.SH", "auto")
        self.assertEqual(index["exchange"], "SSE")
        self.assertEqual(index["asset_class"], "index")
        self.assertEqual(index["historical_return_field"], "close")
        self.assertEqual(index["effective_adjustment"], "none")
        self.assertEqual(etf["exchange"], "SSE")
        self.assertEqual(etf["asset_class"], "etf")
        self.assertEqual(etf["historical_return_field"], "adj_close")
        self.assertEqual(etf["currency"], "CNY")
        self.assertEqual(etf["effective_adjustment"], "forward")
        self.assertEqual(stock["effective_adjustment"], "forward")
        self.assertEqual(china_market_convention("399001.SZ")["exchange"], "SZSE")
        caller = ProtocolCallerContext("local", "local-user", "local", ("data:read",), "local", "local")
        mixed = validate_request(
            DataRequest(asset_ids=("000905.SH", "510300.SH", "600000.SH"), start_date="2024-01-02", end_date="2024-01-03", fields=("adj_close",), cache_policy="extend_only"),
            self.config,
            caller,
        )
        self.assertEqual(mixed.adjustment, "auto")
        self.assertEqual(mixed.fields, ("close", "adj_close"))

    def test_configured_sse_sessions_reject_missing_observation_without_asset_ref(self) -> None:
        config = DataFetcherConfig(
            data_root=self.root / "calendar-data", result_root=self.root / "calendar-result", cache_root=self.root / "calendar-cache",
            local_csv_root=self.input_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": _verified_sse_sessions(("2024-01-02", "2024-01-03", "2024-01-04"))},
        )
        gap_csv = self.input_root / "calendar-gap.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-04"], "asset_id": ["510300.SH", "510300.SH"],
            "close": [5000.0, 5020.0], "adj_close": [5000.0, 5020.0],
        }).to_csv(gap_csv, index=False)
        result = fetch_data(
            DataRequest(asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-04", fields=("close",), source_priority=("local",), local_csv="calendar-gap.csv"),
            config=config,
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.run.data_asset_ref)
        self.assertEqual(result.run.error["code"], "quality_error")
        self.assertIn("2024-01-03", result.run.error["message"])

    def test_invalid_configured_sessions_fail_before_asset_registration(self) -> None:
        invalid_cases = {
            "invalid-date": ("2024-01-02", "not-a-date"),
            "duplicate": ("2024-01-02", "2024-01-02"),
            "unordered": ("2024-01-03", "2024-01-02"),
        }
        for label, sessions in invalid_cases.items():
            with self.subTest(label=label):
                config = DataFetcherConfig(
                    data_root=self.root / f"{label}-data", result_root=self.root / f"{label}-result",
                    cache_root=self.root / f"{label}-cache", local_csv_root=self.input_root, provider_priority=("local",),
                    trading_calendar_sessions={"SSE": sessions},
                )
                result = fetch_data(self.request(), config=config)
                self.assertFalse(result.ok)
                self.assertIsNone(result.run.data_asset_ref)
                self.assertEqual(result.run.error["code"], "quality_error")
                self.assertIn("sessions必须", result.run.error["message"])

    def test_missing_requested_exchange_sessions_remains_unverified(self) -> None:
        sz_csv = self.input_root / "sz-etf.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"], "asset_id": ["159915.SZ", "159915.SZ"],
            "close": [2.0, 2.1], "adj_close": [2.0, 2.1],
        }).to_csv(sz_csv, index=False)
        config = DataFetcherConfig(
            data_root=self.root / "missing-calendar-data", result_root=self.root / "missing-calendar-result",
            cache_root=self.root / "missing-calendar-cache", local_csv_root=self.input_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": ("2024-01-02", "2024-01-03")},
        )
        result = fetch_data(DataRequest(
            asset_ids=("159915.SZ",), start_date="2024-01-02", end_date="2024-01-03",
            fields=("close", "adj_close"), source_priority=("local",), local_csv="sz-etf.csv",
        ), config=config)
        self.assertTrue(result.ok)
        asset = result.run.data_asset_ref
        assert asset is not None
        self.assertEqual(result.quality_report["calendar_completeness"], "unverified")
        self.assertEqual(asset.coverage["calendar_version"], "unverified")
        self.assertFalse(str(asset.coverage["calendar_id"]).startswith("CN-"))

    def test_explicit_sessions_are_clipped_to_request_interval(self) -> None:
        config = DataFetcherConfig(
            data_root=self.root / "clipped-calendar-data", result_root=self.root / "clipped-calendar-result",
            cache_root=self.root / "clipped-calendar-cache", local_csv_root=self.input_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": _verified_sse_sessions(("2023-12-29", "2024-01-02", "2024-01-03", "2024-01-04"))},
        )
        result = fetch_data(self.request(), config=config)
        self.assertTrue(result.ok)
        self.assertEqual(result.quality_report["calendar_completeness"], "complete")
        self.assertEqual(result.quality_report["calendar_missing_dates"], {})

    def test_explicit_calendar_rejects_observed_non_session_date(self) -> None:
        unexpected_csv = self.input_root / "unexpected-session.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-06"], "asset_id": ["510300.SH", "510300.SH"],
            "close": [5.0, 5.1], "adj_close": [5.0, 5.1],
        }).to_csv(unexpected_csv, index=False)
        config = DataFetcherConfig(
            data_root=self.root / "unexpected-data", result_root=self.root / "unexpected-result",
            cache_root=self.root / "unexpected-cache", local_csv_root=self.input_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": _verified_sse_sessions(("2024-01-02",), coverage_end="2024-01-06")},
        )
        result = fetch_data(DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-06",
            fields=("close", "adj_close"), source_priority=("local",), local_csv="unexpected-session.csv",
        ), config=config)
        self.assertFalse(result.ok)
        self.assertIsNone(result.run.data_asset_ref)
        self.assertEqual(result.run.error["code"], "quality_error")
        self.assertIn("2024-01-06", result.run.error["message"])

    def test_incomplete_multi_asset_provider_result_is_rejected(self) -> None:
        class IncompleteProvider:
            name = "ifind_http"
            network = False

            @staticmethod
            def estimate_quota(_request):
                return 0

            @staticmethod
            def fetch(_request, _config):
                return pd.DataFrame({
                    "date": ["2024-01-02", "2024-01-03"],
                    "asset_id": ["510300.SH", "510300.SH"],
                    "close": [5000.0, 5010.0],
                })

        request = DataRequest(
            asset_ids=("510300.SH", "600000.SH"), start_date="2024-01-02", end_date="2024-01-03",
            fields=("close",), source_priority=("ifind_http",),
        )
        with patch("modules.datafetcher.service._providers", return_value={"ifind_http": IncompleteProvider()}):
            result = fetch_data(request, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "provider_unavailable")
        self.assertEqual(result.run.provider_calls[0]["outcome"], "normalization_error")

    def test_failed_provider_attempt_keeps_estimated_quota_in_run_audit(self) -> None:
        class MalformedProvider:
            name = "ifind_http"
            network = False

            @staticmethod
            def estimate_quota(_request):
                return 1

            @staticmethod
            def fetch(_request, _config):
                return pd.DataFrame({"date": ["2024-01-02"], "asset_id": ["510300.SH"]})

        request = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03",
            fields=("close",), source_priority=("ifind_http",),
        )
        with patch("modules.datafetcher.service._providers", return_value={"ifind_http": MalformedProvider()}):
            result = fetch_data(request, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.quota_usage["used"], 1)
        self.assertEqual(result.run.provider_calls[0]["quota_units"], 1)

    def test_wind_is_not_default_fallback_and_requires_explicit_enablement(self) -> None:
        self.assertEqual(DataRequest.__dataclass_fields__["source_priority"].default, ("ifind_http",))
        self.assertEqual(DataFetcherConfig.__dataclass_fields__["provider_priority"].default, ("ifind_http",))
        self.assertEqual(DataRequest.__dataclass_fields__["cache_policy"].default, "force_refresh")
        caller = ProtocolCallerContext("local", "local-user", "local", ("data:read",), "local", "local")
        with self.assertRaisesRegex(ValueError, "Wind"):
            validate_request(
                DataRequest(asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",), source_priority=("wind",), cache_policy="extend_only"),
                self.config,
                caller,
            )

    def test_unsupported_asset_categories_are_rejected_before_provider_access(self) -> None:
        result = fetch_data(
            DataRequest(asset_ids=("IF2409.CFE",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",), source_priority=("local",)),
            config=self.config,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "validation_error")

    def test_asset_reference_declares_hv_input_requirements_without_calculating_hv(self) -> None:
        result = fetch_data(self.request(), config=self.config)
        self.assertTrue(result.ok)
        asset = result.run.data_asset_ref
        assert asset is not None
        requirements = asset.price_convention["hv_input_requirements_by_asset"]["510300.SH"]
        self.assertEqual(requirements["windows_trading_days"], [10, 20, 60, 122])
        self.assertEqual(requirements["return_price_field"], "adj_close")
        self.assertEqual(requirements["return_type"], "log_return")
        self.assertEqual(requirements["minimum_close_observations"], {"10": 11, "20": 21, "60": 61, "122": 123})

    def test_tenant_cache_isolation_returns_own_protocol_ref(self) -> None:
        caller_a = ProtocolCallerContext("tenant-a", "user-a", "user", ("data:read",), "session-a", "tool")
        caller_b = ProtocolCallerContext("tenant-b", "user-b", "user", ("data:read",), "session-b", "tool")
        first = fetch_data(self.request(), caller_a, config=self.config)
        second = fetch_data(self.request(), caller_b, config=self.config)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first.run.cache_decision, "cache_miss_fetched")
        self.assertEqual(second.run.cache_decision, "cache_miss_fetched")
        self.assertEqual(first.run.data_asset_ref.tenant_id, "tenant-a")
        self.assertEqual(second.run.data_asset_ref.tenant_id, "tenant-b")
        self.assertEqual(second.run.data_asset_ref.created_by, "user-b")

    def test_offline_mode_never_calls_network_provider(self) -> None:
        request = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",),
            source_priority=("ifind_http",), offline=True,
        )
        result = fetch_data(request, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "offline_miss")
        self.assertEqual(result.run.provider_calls[0]["outcome"], "skipped_offline")

    def test_quota_guard_rejects_before_ifind_request(self) -> None:
        request = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",),
            source_priority=("ifind_http",), quota_limit=0,
        )
        result = fetch_data(request, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.status, "quota_exceeded")
        self.assertEqual(result.run.provider_calls[0]["outcome"], "quota_exceeded")
        self.assertEqual(result.run.quota_usage["limit"], 0)

    def test_ifind_quota_guard_continues_to_zero_quota_local(self) -> None:
        class QuotaBlockedIFind:
            name = "ifind_http"
            network = True

            @staticmethod
            def estimate_quota(_request):
                return 1

            @staticmethod
            def fetch(_request, _config):
                raise AssertionError("quota保护应在调用iFinD前生效")

        request = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03",
            fields=("close", "adj_close"), source_priority=("ifind_http", "local"),
            local_csv="fixture.csv", quota_limit=0,
        )
        with patch("modules.datafetcher.service._providers", return_value={
            "ifind_http": QuotaBlockedIFind(), "local": LocalCsvProvider(),
        }):
            result = fetch_data(request, config=self.config)
        self.assertTrue(result.ok)
        self.assertEqual([item["outcome"] for item in result.run.provider_calls], ["quota_exceeded", "succeeded"])
        self.assertEqual(result.run.quota_usage["used"], 0)

    def test_none_adjustment_makes_adj_close_raw_and_metadata_unadjusted(self) -> None:
        raw_csv = self.input_root / "raw.csv"
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"], "asset_id": ["510300.SH", "510300.SH"],
            "close": [5.0, 5.1], "adj_close": [50.0, 51.0],
        }).to_csv(raw_csv, index=False)
        result = fetch_data(DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",),
            adjustment="none", source_priority=("local",), local_csv="raw.csv",
        ), config=self.config)
        self.assertTrue(result.ok)
        asset = result.run.data_asset_ref
        assert asset is not None
        self.assertEqual([row["adj_close"] for row in result.data_preview], [5.0, 5.1])
        self.assertEqual(asset.price_convention["asset_market_conventions"]["510300.SH"]["effective_adjustment"], "none")
        self.assertNotIn("forward", json.dumps(asset.price_convention))

    def test_request_fingerprint_is_stable_and_path_free(self) -> None:
        request = self.request()
        self.assertEqual(request_fingerprint(request), request_fingerprint(request))
        self.assertNotIn(str(self.csv), request_fingerprint(request))

    def test_semantically_equal_field_order_reuses_request_and_cache_identity(self) -> None:
        caller = ProtocolCallerContext("local", "local-user", "local", ("data:read",), "local", "local")
        left = validate_request(self.request(), self.config, caller)
        right = validate_request(DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03",
            fields=("adj_close", "close"), source_priority=("local",), cache_policy="extend_only", local_csv="fixture.csv",
        ), self.config, caller)
        self.assertEqual(left.fields, right.fields)
        self.assertEqual(request_fingerprint(left), request_fingerprint(right))
        first = fetch_data(left, caller, config=self.config)
        second = fetch_data(right, caller, config=self.config)
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(second.run.cache_decision, "cache_hit")

    def test_different_local_csv_content_has_independent_cache_identity(self) -> None:
        first = fetch_data(self.request(local_csv="fixture.csv"), config=self.config)
        second = fetch_data(self.request(local_csv="other.csv"), config=self.config)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.run.cache_decision, "cache_miss_fetched")
        self.assertNotEqual(first.run.request_hash, second.run.request_hash)
        first_asset = first.run.data_asset_ref
        second_asset = second.run.data_asset_ref
        assert first_asset is not None and second_asset is not None
        self.assertNotEqual(first_asset.content_hash, second_asset.content_hash)
        self.assertNotEqual(first_asset.lineage["local_source_fingerprint"], second_asset.lineage["local_source_fingerprint"])
        self.assertNotIn(str(self.other_csv), str(asdict(second_asset)))

    def test_concurrent_identical_request_reads_local_csv_once(self) -> None:
        original_fetch = LocalCsvProvider.fetch
        calls = [0]
        counter_lock = threading.Lock()
        start = threading.Barrier(2)

        def counted_fetch(provider, request, config):
            with counter_lock:
                calls[0] += 1
            time.sleep(0.05)
            return original_fetch(provider, request, config)

        def run_request():
            start.wait()
            return fetch_data(self.request(), config=self.config)

        with patch.object(LocalCsvProvider, "fetch", new=counted_fetch):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: run_request(), range(2)))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(calls[0], 1)
        self.assertEqual(sorted(result.run.cache_decision for result in results), ["cache_hit", "cache_miss_fetched"])

    def test_identical_request_lock_is_shared_across_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready, release, acquired = context.Event(), context.Event(), context.Event()
        first = context.Process(target=_hold_cache_request_lock, args=(str(self.config.cache_root), "same-request", ready, release))
        second = context.Process(target=_observe_cache_request_lock, args=(str(self.config.cache_root), "same-request", acquired))
        first.start()
        self.assertTrue(ready.wait(5))
        second.start()
        self.assertFalse(acquired.wait(0.3))
        release.set()
        self.assertTrue(acquired.wait(5))
        first.join(5)
        second.join(5)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_corrupt_cached_csv_is_refetched_before_returning_asset_ref(self) -> None:
        first = fetch_data(self.request(), config=self.config)
        self.assertTrue(first.ok)
        asset = first.run.data_asset_ref
        assert asset is not None
        cached_csv = self.config.cache_root / "assets" / f"{asset.content_hash}.csv"
        corrupted = pd.read_csv(cached_csv)
        corrupted.loc[0, "close"] = 999.0
        corrupted.to_csv(cached_csv, index=False)

        repaired = fetch_data(self.request(), config=self.config)
        self.assertTrue(repaired.ok)
        self.assertEqual(repaired.run.cache_decision, "cache_miss_fetched")
        self.assertEqual(repaired.run.data_asset_ref.content_hash, asset.content_hash)
        self.assertEqual(float(pd.read_csv(cached_csv).loc[0, "close"]), 5000.0)

    def test_corrupt_cache_index_fails_closed_without_overwrite(self) -> None:
        first = fetch_data(self.request(), config=self.config)
        self.assertTrue(first.ok)
        index_path = self.config.cache_root / "index.json"
        index_path.write_text("{broken", encoding="utf-8")
        result = fetch_data(self.request(), config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "cache_corrupt")
        self.assertEqual(index_path.read_text(encoding="utf-8"), "{broken")

    def test_tool_and_page_share_service_boundary(self) -> None:
        payload = {
            "asset_id": "510300.SH",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "fields": ["close"],
            "source_priority": ["local"],
            "cache_policy": "extend_only",
            "local_csv": "fixture.csv",
        }
        environment = {
            "OPTIONHELPER_DATA_ROOT": str(self.input_root),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "page-result"),
        }
        with patch.dict(os.environ, environment, clear=False):
            tool_result = call_tool({"action": "fetch", **payload})
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("POST", "/api/fetch", body=json.dumps(payload), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                page_result = json.loads(response.read().decode("utf-8"))
                connection.close()
            finally:
                worker.join(timeout=5)
                server.server_close()
        self.assertTrue(tool_result["ok"])
        self.assertEqual(response.status, 200)
        self.assertTrue(page_result["ok"])
        self.assertEqual(page_result["cache_decision"], "cache_hit")
        self.assertEqual(tool_result["data_asset_ref"]["content_hash"], page_result["data_asset_ref"]["content_hash"])
        page_source = (PROJECT_ROOT / "modules" / "datafetcher" / "page" / "datafetcher.js").read_text(encoding="utf-8")
        self.assertIn("/api/fetch", page_source)
        page_html = (PROJECT_ROOT / "modules" / "datafetcher" / "page" / "datafetcher.html").read_text(encoding="utf-8")
        self.assertNotIn('type="date"', page_html)
        self.assertEqual(page_html.count('placeholder="yyyy/mm/dd"'), 2)

    def test_local_host_lists_and_downloads_only_indexed_assets(self) -> None:
        payload = {
            "asset_id": "510300.SH", "start_date": "2024-01-02", "end_date": "2024-01-03",
            "fields": ["close"], "source_priority": ["local"], "cache_policy": "extend_only", "local_csv": "fixture.csv",
        }
        environment = {
            "OPTIONHELPER_DATA_ROOT": str(self.input_root),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "page-result"),
        }
        with patch.dict(os.environ, environment, clear=False):
            result = call_tool({"action": "fetch", **payload})
            asset_id = result["data_asset_ref"]["data_asset_id"]
            for path, expected in (("/api/assets", 200), (f"/api/assets/{asset_id}/download", 200), ("/api/assets/not-a-real-asset/download", 404), ("/icons/optionhelper-logo.svg", 200), ("/icons/optionhelper-app-icon-tile-light.svg", 200)):
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                try:
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                finally:
                    worker.join(timeout=5)
                    server.server_close()
                self.assertEqual(response.status, expected)
                if path == "/api/assets":
                    listed = json.loads(body.decode("utf-8"))
                    self.assertEqual(listed["assets"][0]["data_asset_ref"]["data_asset_id"], asset_id)
                if path.endswith("/download") and expected == 200:
                    self.assertIn(b"2024-01-02", body)
                if path.startswith("/icons/"):
                    self.assertIn(b"<svg", body)

if __name__ == "__main__":
    unittest.main()
