from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.protocol.models import CallerContext, SecretRef

from modules.datafetcher.config import DataFetcherConfig
from modules.datafetcher.calendar_service import CalendarValidationError, fetch_calendar_asset
from modules.datafetcher.models import CalendarRequest
from modules.datafetcher.providers.base import ProviderUnavailable
from modules.datafetcher.providers.ifind_http import calendar_dates, calendar_response
from modules.datafetcher.service import fetch_calendar_data, list_data_assets, read_data_asset


def _sessions(start: str, end: str) -> tuple[str, ...]:
    values = {
        "2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12",
        "2026-08-13", "2026-08-14", "2026-08-17", "2026-08-18",
    }
    return tuple(value for value in sorted(values) if start <= value <= end)


class TradingCalendarDecouplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = DataFetcherConfig(
            data_root=root / "data",
            result_root=root / "result",
            cache_root=root / "data" / "datafetcher-cache",
            ifind_secret_ref=SecretRef("env", "IFIND_REFRESH_TOKEN"),
        ).resolved()
        self.caller = CallerContext(
            tenant_id="tenant-a",
            principal_id="principal-a",
            role="admin",
            capabilities=("data:read", "data:force_refresh"),
            session_id="session-a",
            audience="optionhelper-app",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_calendar_request_ignores_market_fields_but_rejects_other_unknowns(self) -> None:
        request = CalendarRequest.from_mapping({
            "asset_ids": ["000300.SH"],
            "start_date": "2026-08-07",
            "end_date": "2026-08-18",
            "fields": ["open", "close", "adj_close"],
            "adjustment": "forward",
        })
        self.assertEqual(request.asset_ids, ("000300.SH",))
        self.assertNotIn("fields", request.public_dict())
        self.assertNotIn("adjustment", request.public_dict())
        with self.assertRaisesRegex(ValueError, "未知字段"):
            CalendarRequest.from_mapping({**request.public_dict(), "frequency": "1d"})

    @patch("modules.datafetcher.providers.ifind_http._post")
    def test_ifind_calendar_payload_never_contains_market_fields(self, mocked_post) -> None:
        mocked_post.return_value = {"errorcode": 0, "tables": [{"time": ["2026-08-10"]}]}
        calendar_response("opaque", exchange="SSE", start_date="2026-08-10", end_date="2026-08-12")
        payload = mocked_post.call_args.kwargs["payload"]
        self.assertEqual(payload["marketcode"], "212001")
        self.assertEqual(payload["functionpara"], {
            "mode": "1",
            "dateType": "0",
            "period": "D",
            "dateFormat": "0",
        })
        self.assertNotIn("fields", payload)
        self.assertNotIn("indicators", payload)
        self.assertNotIn("adjustment", payload)

    @patch("modules.datafetcher.providers.ifind_http._post")
    def test_szse_uses_ifind_date_query_market_code(self, mocked_post) -> None:
        mocked_post.return_value = {"errorcode": 0, "tables": {"time": ["2026-08-10"]}}
        calendar_response("opaque", exchange="SZSE", start_date="2026-08-10", end_date="2026-11-30")
        self.assertEqual(mocked_post.call_args.kwargs["payload"]["marketcode"], "212100")

    def test_ifind_mapping_tables_shape_is_supported(self) -> None:
        values = calendar_dates({
            "inputParams": {"marketcode": "212100"},
            "tables": {"time": ["2026-08-10", "2026-08-11"]},
        }, exchange="SZSE")
        self.assertEqual(values, ("2026-08-10", "2026-08-11"))

    def test_equivalent_ifind_date_columns_are_not_double_counted(self) -> None:
        values = calendar_dates({
            "inputParams": {"marketcode": "212001"},
            "tables": [{
                "time": ["2026-08-10", "2026-08-11"],
                "table": {"sequencedate": ["2026-08-10", "2026-08-11"]},
            }],
        }, exchange="SSE")
        self.assertEqual(values, ("2026-08-10", "2026-08-11"))

    def test_multi_exchange_calendar_uses_intersection(self) -> None:
        request = {
            "asset_ids": ["000300.SH", "399001.SZ"],
            "start_date": "2026-08-07",
            "end_date": "2026-08-18",
        }
        by_exchange = {
            "SSE": _sessions("2026-08-07", "2026-08-18"),
            "SZSE": tuple(value for value in _sessions("2026-08-07", "2026-08-18") if value != "2026-08-13"),
        }
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value=by_exchange):
            result = fetch_calendar_data(request, self.caller, config=self.config, task_id="calendar-test")
        self.assertTrue(result.ok)
        ref = result.run.data_asset_ref
        self.assertIsNotNone(ref)
        self.assertEqual(ref.schema_id, "trading-calendar")
        self.assertEqual(ref.media_type, "application/json")
        self.assertNotIn("2026-08-13", ref.coverage["sessions"])
        self.assertEqual(ref.normalized_fields, ("session",))
        self.assertFalse(ref.price_convention["contains_market_prices"])

    def test_invalid_calendar_sequences_are_rejected(self) -> None:
        request = CalendarRequest.from_mapping({
            "asset_ids": ["000300.SH"],
            "start_date": "2026-08-07",
            "end_date": "2026-08-18",
        })
        for values in (
            ("2026-08-10", "2026-08-10"),
            ("2026-08-11", "2026-08-10"),
            ("2026-08-06", "2026-08-10"),
            (),
        ):
            with self.subTest(values=values):
                with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": values}):
                    with self.assertRaises(CalendarValidationError):
                        fetch_calendar_asset(request, self.caller, self.config)

    def test_provider_failure_reuses_only_complete_verified_cache(self) -> None:
        request = CalendarRequest.from_mapping({
            "asset_ids": ["000300.SH"],
            "start_date": "2026-08-07",
            "end_date": "2026-08-18",
        })
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": _sessions(request.start_date, request.end_date)}):
            _, original, _, _, _ = fetch_calendar_asset(request, self.caller, self.config)
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", side_effect=ProviderUnavailable("offline")):
            _, cached, _, decision, _ = fetch_calendar_asset(request, self.caller, self.config)
        self.assertEqual(asdict(cached), asdict(original))
        self.assertEqual(decision, "verified_cache_fallback")
        larger = CalendarRequest(("000300.SH",), "2026-08-01", "2026-08-18")
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", side_effect=ProviderUnavailable("offline")):
            with self.assertRaisesRegex(ProviderUnavailable, "交易日历暂不可用"):
                fetch_calendar_asset(larger, self.caller, self.config)

    def test_shorter_refresh_does_not_hide_an_existing_complete_cache(self) -> None:
        broad = CalendarRequest(("000300.SH",), "2026-08-07", "2026-08-18")
        narrow = CalendarRequest(("000300.SH",), "2026-08-10", "2026-08-14")
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": _sessions(broad.start_date, broad.end_date)}):
            _, broad_ref, _, _, _ = fetch_calendar_asset(broad, self.caller, self.config)
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": _sessions(narrow.start_date, narrow.end_date)}):
            fetch_calendar_asset(narrow, self.caller, self.config)
        with patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", side_effect=ProviderUnavailable("offline")):
            _, cached, _, decision, _ = fetch_calendar_asset(broad, self.caller, self.config)
        self.assertEqual(cached, broad_ref)
        self.assertEqual(decision, "verified_cache_fallback")

    def test_wrong_exchange_echo_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "交易所"):
            calendar_dates({
                "errorcode": 0,
                "inputParams": {"marketcode": "212002"},
                "tables": [{"time": ["2026-08-10"]}],
            }, exchange="SSE")

    def test_calendar_asset_is_listed_and_read_through_the_same_public_port(self) -> None:
        request = {
            "asset_ids": ["000300.SH"],
            "start_date": "2026-08-07",
            "end_date": "2026-08-18",
        }
        environment = {
            "OPTIONHELPER_DATA_ROOT": str(self.config.data_root),
            "OPTIONHELPER_RESULT_ROOT": str(self.config.result_root),
            "OPTIONHELPER_RUNTIME_ROOT": str(Path(self.temporary.name) / "runtime"),
        }
        with (
            patch.dict("os.environ", environment, clear=False),
            patch("modules.datafetcher.calendar_service.IFindHttpProvider.fetch_calendar", return_value={"SSE": _sessions("2026-08-07", "2026-08-18")}),
        ):
            result = fetch_calendar_data(request, self.caller, config=self.config, task_id="calendar-read")
            reference = result.run.data_asset_ref
            assets = list_data_assets(caller=self.caller)
            resolved, content = read_data_asset(reference.data_asset_id, caller=self.caller)
        self.assertIn(reference.data_asset_id, [item["data_asset_ref"]["data_asset_id"] for item in assets])
        self.assertEqual(resolved, reference)
        self.assertIn(b'"schema_id":"trading-calendar"', content)


if __name__ == "__main__":
    unittest.main()
