"""DataFetcher权限、输入与iFind错误边界回归。

所有场景使用临时DataStore和内存HTTP响应，不连接iFind或读取任何真实凭据。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

from runtime.protocol.models import CallerContext, DataAssetRef, SecretRef

from modules.datafetcher.config import DataFetcherConfig
from modules.datafetcher.calendar_service import fetch_calendar_asset
from modules.datafetcher.models import CalendarRequest, DataRequest
from modules.datafetcher.providers.base import ProviderUnauthorized, ProviderUnavailable
from modules.datafetcher.providers.local import LocalCsvProvider
from modules.datafetcher.providers.ifind_http import IFindDownloadError, IFindHttpProvider, get_access_token
from modules.datafetcher.server import Handler, _download_media
from modules.datafetcher.service import DataFetcherError
from modules.datafetcher.service import fetch_calendar_data, fetch_data, list_data_assets, read_data_asset


class _Response:
    ok = True
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {"errorcode": -1, "errmsg": "fixture-refresh-token-must-not-escape"}


class DataFetcherSecurityRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.csv_root = self.root / "input"
        self.csv_root.mkdir()
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "asset_id": ["510300.SH", "510300.SH"],
            "close": [5.0, 5.1],
            "adj_close": [5.0, 5.1],
        }).to_csv(self.csv_root / "fixture.csv", index=False)
        self.config = DataFetcherConfig(
            data_root=self.data_root,
            result_root=self.root / "result",
            cache_root=self.data_root / "datafetcher-cache",
            local_csv_root=self.csv_root,
            provider_priority=("local",),
        ).resolved()
        self.caller_a = CallerContext("tenant-a", "principal-a", "user", ("data:read",), "session-a", "test")
        self.caller_b = CallerContext("tenant-a", "principal-b", "user", ("data:read",), "session-b", "test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _request() -> DataRequest:
        return DataRequest(
            asset_ids=("510300.SH",),
            start_date="2024-01-02",
            end_date="2024-01-03",
            fields=("close",),
            source_priority=("local",),
            cache_policy="extend_only",
            local_csv="fixture.csv",
        )

    def test_same_tenant_different_principal_cannot_reuse_list_or_read_another_asset(self) -> None:
        first = fetch_data(self._request(), self.caller_a, config=self.config)
        self.assertTrue(first.ok)
        first_ref = first.run.data_asset_ref
        assert first_ref is not None

        with patch.dict(os.environ, {
            "OPTIONHELPER_DATA_ROOT": str(self.data_root),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "result"),
        }, clear=False):
            self.assertEqual(list_data_assets(caller=self.caller_b), [])
            with self.assertRaises(PermissionError):
                read_data_asset(first_ref.data_asset_id, caller=self.caller_b)

        original_fetch = LocalCsvProvider.fetch
        local_reads = [0]

        def counted_fetch(provider, request, config):
            local_reads[0] += 1
            return original_fetch(provider, request, config)

        with patch("modules.datafetcher.providers.local.LocalCsvProvider.fetch", new=counted_fetch):
            second = fetch_data(self._request(), self.caller_b, config=self.config)
        self.assertTrue(second.ok)
        second_ref = second.run.data_asset_ref
        assert second_ref is not None
        self.assertEqual(second.run.cache_decision, "cache_rebound")
        self.assertEqual(local_reads, [0])
        self.assertEqual(second_ref.created_by, "principal-b")
        self.assertNotEqual(first_ref.data_asset_id, second_ref.data_asset_id)

    def test_data_read_capability_is_required_for_fetch_and_index_access(self) -> None:
        no_read = CallerContext("tenant-a", "principal-a", "user", (), "session-a", "test")
        result = fetch_data(self._request(), no_read, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "validation_error")
        self.assertIn("data:read", result.run.error["message"])
        with self.assertRaises(PermissionError):
            list_data_assets(caller=no_read)

    def test_calendar_requires_data_read_before_provider_or_cache_access(self) -> None:
        no_read = CallerContext("tenant-a", "principal-a", "user", (), "session-a", "test")
        request = CalendarRequest(("510300.SH",), "2024-01-02", "2024-01-03")
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar") as provider:
            result = fetch_calendar_data(request, no_read, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "calendar_validation_error")
        self.assertIn("data:read", result.run.error["message"])
        provider.assert_not_called()

    def test_calendar_rejects_unsupported_asset_and_negative_quota_before_provider(self) -> None:
        invalid = (
            CalendarRequest(("AAPL.US",), "2024-01-02", "2024-01-03"),
            CalendarRequest(("510300.SH",), "2024-01-02", "2024-01-03", quota_limit=-1),
        )
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar") as provider:
            for request in invalid:
                with self.subTest(request=request):
                    result = fetch_calendar_data(request, self.caller_a, config=self.config)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.run.error["code"], "calendar_validation_error")
        provider.assert_not_called()

    def test_calendar_concurrent_request_fetches_provider_once(self) -> None:
        request = CalendarRequest(("510300.SH",), "2024-01-02", "2024-01-03")
        entered, release = threading.Event(), threading.Event()
        calls = [0]

        def delayed_calendar(_provider, _request, _config):
            calls[0] += 1
            entered.set()
            self.assertTrue(release.wait(3))
            return {"SSE": ("2024-01-02", "2024-01-03")}

        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", new=delayed_calendar):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(fetch_calendar_data, request, self.caller_a, config=self.config)
                self.assertTrue(entered.wait(2))
                second = executor.submit(fetch_calendar_data, request, self.caller_a, config=self.config)
                time.sleep(0.05)
                release.set()
                results = (first.result(4), second.result(4))
        self.assertTrue(all(item.ok for item in results))
        self.assertEqual(calls, [1])
        self.assertEqual(sorted(item.run.cache_decision for item in results), ["provider_fetched", "verified_cache_reused"])

    def test_bare_calendar_sessions_never_mark_market_history_complete(self) -> None:
        config = DataFetcherConfig(
            data_root=self.root / "bare-data", result_root=self.root / "bare-result",
            cache_root=self.root / "bare-cache", local_csv_root=self.csv_root, provider_priority=("local",),
            trading_calendar_sessions={"SSE": ("2024-01-02", "2024-01-03")},
        ).resolved()
        result = fetch_data(self._request(), self.caller_a, config=config)
        self.assertTrue(result.ok)
        self.assertEqual(result.quality_report["calendar_completeness"], "unverified")
        assert result.run.data_asset_ref is not None
        self.assertEqual(result.run.data_asset_ref.coverage["calendar_version"], "unverified")

    def test_data_asset_identity_binds_request_lineage_and_observed_coverage(self) -> None:
        force_caller = CallerContext("tenant-a", "principal-a", "user", ("data:read", "data:force_refresh"), "session-a", "test")
        broad = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-01", end_date="2024-01-03", fields=("close",),
            source_priority=("local",), cache_policy="force_refresh", local_csv="fixture.csv",
        )
        narrow = DataRequest(
            asset_ids=("510300.SH",), start_date="2024-01-02", end_date="2024-01-03", fields=("close",),
            source_priority=("local",), cache_policy="force_refresh", local_csv="fixture.csv",
        )
        first, second = (
            fetch_data(broad, force_caller, config=self.config),
            fetch_data(narrow, force_caller, config=self.config),
        )
        self.assertTrue(first.ok and second.ok)
        first_ref, second_ref = first.run.data_asset_ref, second.run.data_asset_ref
        assert first_ref is not None and second_ref is not None
        self.assertNotEqual(first_ref.data_asset_id, second_ref.data_asset_id)
        self.assertEqual(first_ref.lineage["request_hash"], first.run.request_hash)
        self.assertEqual(second_ref.lineage["request_hash"], second.run.request_hash)
        self.assertEqual(first_ref.coverage["start_date"], "2024-01-02")
        self.assertEqual(first_ref.coverage["requested_start_date"], "2024-01-01")

    def test_calendar_validation_failure_reuses_verified_cache(self) -> None:
        request = CalendarRequest(("510300.SH",), "2024-01-02", "2024-01-03")
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": ("2024-01-02", "2024-01-03")}):
            _, original, _, _, _ = fetch_calendar_asset(request, self.caller_a, self.config)
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": ("2024-01-03", "2024-01-02")}):
            _, cached, _, decision, calls = fetch_calendar_asset(request, self.caller_a, self.config)
        self.assertEqual(cached, original)
        self.assertEqual(decision, "verified_cache_fallback")
        self.assertEqual(calls[0]["outcome"], "calendar_validation_error")

    def test_calendar_malformed_remote_container_reuses_verified_cache(self) -> None:
        request = CalendarRequest(("510300.SH",), "2024-01-02", "2024-01-03")
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": ("2024-01-02", "2024-01-03")}):
            _, original, _, _, _ = fetch_calendar_asset(request, self.caller_a, self.config)
        for malformed in (None, [], {"SSE": None}):
            with self.subTest(malformed=malformed):
                with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value=malformed):
                    _, cached, _, decision, calls = fetch_calendar_asset(request, self.caller_a, self.config)
                self.assertEqual(cached, original)
                self.assertEqual(decision, "verified_cache_fallback")
                self.assertEqual(calls[0]["outcome"], "calendar_validation_error")

    def test_ifind_connection_distinguishes_unavailable_from_unauthorized_and_secret_port_failure(self) -> None:
        configured = DataFetcherConfig(
            data_root=self.data_root, result_root=self.root / "result", cache_root=self.data_root / "datafetcher-cache",
            ifind_secret_ref=SecretRef("keychain", "fixture/ifind"), ifind_secret_port=lambda _ref: "fixture-value",
        ).resolved()
        with patch("modules.datafetcher.providers.ifind_http.get_access_token", side_effect=IFindDownloadError("fixture timeout")):
            with self.assertRaises(ProviderUnavailable):
                IFindHttpProvider().test_connection(configured)
        with patch("modules.datafetcher.providers.ifind_http.get_access_token", side_effect=IFindDownloadError("fixture denied", unauthorized=True)):
            with self.assertRaises(ProviderUnauthorized):
                IFindHttpProvider().test_connection(configured)
        port_down = DataFetcherConfig(
            data_root=self.data_root, result_root=self.root / "result", cache_root=self.data_root / "datafetcher-cache",
            ifind_secret_ref=SecretRef("keychain", "fixture/ifind"),
            ifind_secret_port=lambda _ref: (_ for _ in ()).throw(RuntimeError("fixture secret port detail")),
        ).resolved()
        with self.assertRaises(ProviderUnavailable) as raised:
            IFindHttpProvider().test_connection(port_down)
        self.assertNotIn("fixture secret port detail", str(raised.exception))

    def test_index_reference_is_not_listed_or_downloaded_when_datastore_manifest_disagrees(self) -> None:
        result = fetch_data(self._request(), self.caller_a, config=self.config)
        self.assertTrue(result.ok)
        index_path = self.config.cache_root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        record = next(iter(index["entries"].values()))
        record["data_asset_refs"]["principal-a"]["lineage"]["request_hash"] = "forged-reference"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with patch.dict(os.environ, {
            "OPTIONHELPER_DATA_ROOT": str(self.data_root),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "result"),
        }, clear=False):
            self.assertEqual(list_data_assets(caller=self.caller_a), [])
            assert result.run.data_asset_ref is not None
            with self.assertRaises(FileNotFoundError):
                read_data_asset(result.run.data_asset_ref.data_asset_id, caller=self.caller_a)

    def test_unknown_top_level_request_key_is_rejected_not_ignored(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知字段"):
            DataRequest.from_mapping({
                "asset_id": "510300.SH",
                "start_date": "2024-01-02",
                "end_date": "2024-01-03",
                "fields": ["close"],
                "unexpected_provider_option": "ignored-before-fix",
            })

        result = fetch_data({
            "asset_id": "510300.SH",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "fields": ["close"],
            "unexpected_provider_option": "ignored-before-fix",
        }, self.caller_a, config=self.config)
        self.assertFalse(result.ok)
        self.assertEqual(result.run.error["code"], "validation_error")

    def test_ifind_remote_error_does_not_echo_fixture_secret(self) -> None:
        with patch("modules.datafetcher.providers.ifind_http.requests.post", return_value=_Response()):
            with self.assertRaises(IFindDownloadError) as raised:
                get_access_token("fixture-refresh-token-must-not-escape")
        self.assertNotIn("fixture-refresh-token-must-not-escape", str(raised.exception))

    def test_host_secret_ref_is_a_reference_only_not_a_value(self) -> None:
        reference = SecretRef("keychain", "optionhelper/ifind")
        self.assertEqual(reference.redacted(), {"provider": "keychain", "key": "optionhelper/ifind", "version": None})

    def test_download_media_type_and_extension_are_bound_to_the_asset_ref(self) -> None:
        base = {
            "data_asset_id": "asset-fixture",
            "storage_ref": "data:tenant-a:asset-fixture:hash:metadata",
            "asset_ids": ("510300.SH",),
            "normalized_fields": ("date",),
            "coverage": {},
            "row_count": 1,
            "price_convention": {},
            "content_hash": "hash",
            "lineage": {},
            "tenant_id": "tenant-a",
            "created_by": "principal-a",
        }
        history = DataAssetRef(media_type="text/csv", schema_id="market-history-v1", **base)
        calendar = DataAssetRef(media_type="application/json", schema_id="trading-calendar", **base)
        unknown = DataAssetRef(media_type="text/plain", schema_id="untrusted", **base)
        self.assertEqual(_download_media(history), ("text/csv; charset=utf-8", ".csv"))
        self.assertEqual(_download_media(calendar), ("application/json; charset=utf-8", ".json"))
        with self.assertRaises(DataFetcherError):
            _download_media(unknown)

    def test_local_host_downloads_calendar_as_json_not_csv(self) -> None:
        calendar = DataAssetRef(
            data_asset_id="calendar-fixture",
            storage_ref="data:tenant-a:calendar-fixture:hash:metadata",
            media_type="application/json",
            schema_id="trading-calendar",
            asset_ids=("510300.SH",),
            normalized_fields=("session",),
            coverage={},
            row_count=1,
            price_convention={},
            content_hash="hash",
            lineage={},
            tenant_id="tenant-a",
            created_by="principal-a",
        )
        with patch("modules.datafetcher.server.read_data_asset", return_value=(calendar, b'{"sessions":["2024-01-02"]}')):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/api/assets/calendar-fixture/download")
                response = connection.getresponse()
                body = response.read()
                content_type = response.getheader("Content-Type")
                disposition = response.getheader("Content-Disposition")
                connection.close()
            finally:
                worker.join(timeout=5)
                server.server_close()
        self.assertEqual(response.status, 200)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertEqual(disposition, 'attachment; filename="calendar-fixture.json"')
        self.assertEqual(body, b'{"sessions":["2024-01-02"]}')


if __name__ == "__main__":
    unittest.main()
