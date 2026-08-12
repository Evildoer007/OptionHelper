from __future__ import annotations

from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .. import main

from ..engine.market_data import (
    DailyBar,
    IFindHTTPProvider,
    MarketSnapshotRequest,
    OfflineProvider,
    build_market_snapshot,
    load_market_snapshot,
    save_market_snapshot,
)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers, json=None, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "json": json, "timeout": timeout}
        )
        return self.responses.pop(0)


_CN_SESSIONS_2026 = tuple(
    date.fromisoformat(value)
    for value in """
    2026-01-05 2026-01-06 2026-01-07 2026-01-08 2026-01-09
    2026-01-12 2026-01-13 2026-01-14 2026-01-15 2026-01-16
    2026-01-19 2026-01-20 2026-01-21 2026-01-22 2026-01-23
    2026-01-26 2026-01-27 2026-01-28 2026-01-29 2026-01-30
    2026-02-02 2026-02-03 2026-02-04 2026-02-05 2026-02-06
    2026-02-09 2026-02-10 2026-02-11 2026-02-12 2026-02-13
    2026-02-24 2026-02-25 2026-02-26 2026-02-27 2026-03-02
    2026-03-03 2026-03-04 2026-03-05 2026-03-06 2026-03-09
    2026-03-10 2026-03-11 2026-03-12 2026-03-13 2026-03-16
    2026-03-17 2026-03-18 2026-03-19 2026-03-20 2026-03-23
    2026-03-24 2026-03-25 2026-03-26 2026-03-27 2026-03-30
    2026-03-31 2026-04-01 2026-04-02 2026-04-03 2026-04-07
    2026-04-08 2026-04-09 2026-04-10 2026-04-13 2026-04-14
    2026-04-15 2026-04-16 2026-04-17 2026-04-20 2026-04-21
    2026-04-22 2026-04-23 2026-04-24 2026-04-27 2026-04-28
    2026-04-29 2026-04-30 2026-05-06 2026-05-07 2026-05-08
    2026-05-11 2026-05-12 2026-05-13 2026-05-14 2026-05-15
    2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22
    2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29
    2026-06-01 2026-06-02 2026-06-03 2026-06-04 2026-06-05
    2026-06-08 2026-06-09 2026-06-10 2026-06-11 2026-06-12
    2026-06-15 2026-06-16 2026-06-17 2026-06-18 2026-06-22
    2026-06-23 2026-06-24 2026-06-25 2026-06-26 2026-06-29
    2026-06-30 2026-07-01 2026-07-02 2026-07-03 2026-07-06
    2026-07-07 2026-07-08 2026-07-09 2026-07-10 2026-07-13
    2026-07-14 2026-07-15 2026-07-16 2026-07-17 2026-07-20
    """.split()
)


def _trading_dates(count: int) -> list[date]:
    if not 0 < count <= len(_CN_SESSIONS_2026):
        raise ValueError("测试仅提供显式2026中国交易sessions")
    return list(_CN_SESSIONS_2026[:count])


def _history_payload(code: str, dates: list[date], closes: list[float]):
    return {
        "errorcode": 0,
        "tables": [
            {
                "thscode": code,
                "time": [value.isoformat() for value in dates],
                "table": {
                    "open": [value - 0.1 for value in closes],
                    "high": [value + 0.2 for value in closes],
                    "low": [value - 0.2 for value in closes],
                    "close": closes,
                },
            }
        ],
    }


class IFindHTTPProviderTest(unittest.TestCase):
    def test_refresh_auth_and_raw_adjusted_history_requests(self):
        dates = _trading_dates(130)
        raw = [100.0 + index * 0.1 for index in range(130)]
        adjusted = [80.0 * math.exp(index * 0.001) for index in range(130)]
        session = _FakeSession(
            [
                _FakeResponse({"errorcode": 0, "data": {"access_token": "test-access"}}),
                _FakeResponse(_history_payload("510300.SH", dates, raw)),
                _FakeResponse(_history_payload("510300.SH", dates, adjusted)),
            ]
        )
        with patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "test-refresh"}, clear=True):
            provider = IFindHTTPProvider.from_environment(session=session)
            bars = provider.history_quotes(
                "510300.SH", dates[0], dates[-1], asset_type="ETF"
            )
        self.assertEqual(len(bars), 130)
        self.assertEqual(bars[-1].close, raw[-1])
        self.assertEqual(bars[-1].adjusted_close, adjusted[-1])
        self.assertEqual(session.calls[1]["json"]["functionpara"]["CPS"], "1")
        self.assertEqual(session.calls[2]["json"]["functionpara"]["CPS"], "2")

    def test_missing_credentials_fail_before_network(self):
        session = _FakeSession([])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "IFIND_REFRESH_TOKEN"):
                IFindHTTPProvider.from_environment(session=session)
        self.assertEqual(session.calls, [])


class OfflineMarketSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.dates = _trading_dates(130)
        self.bars = tuple(
            DailyBar(
                trading_date=trading_date,
                code="510300.SH",
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.5 + index,
                adjusted_open=80.0 * math.exp(index * 0.001),
                adjusted_high=80.2 * math.exp(index * 0.001),
                adjusted_low=79.8 * math.exp(index * 0.001),
                adjusted_close=80.0 * math.exp(index * 0.001),
            )
            for index, trading_date in enumerate(self.dates)
        )

    def _snapshot(self):
        return build_market_snapshot(
            OfflineProvider({"510300.SH": self.bars}),
            MarketSnapshotRequest(
                code="510300.SH",
                as_of=self.dates[-1],
                asset_type="ETF",
                volatility_window=20,
                risk_free_rate=0.02,
                dividend_yield=0.01,
            ),
        )

    def test_unadjusted_close_is_spot_and_adjusted_log_return_is_hv(self):
        snapshot = self._snapshot()
        self.assertAlmostEqual(snapshot.spot, self.bars[-1].close, places=15)
        self.assertAlmostEqual(snapshot.volatility, 0.0, places=14)
        self.assertEqual(set(dict(snapshot.historical_volatility)), {10, 20, 60, 122})
        self.assertRegex(snapshot.data_sha256, r"^[0-9a-f]{64}$")
        market = snapshot.to_market_parameters()
        self.assertEqual(market["spot"], snapshot.spot)
        self.assertEqual(market["volatility"], snapshot.volatility)

    def test_snapshot_round_trip_excludes_credentials_and_full_history(self):
        snapshot = self._snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_market_snapshot(snapshot, path)
            text = path.read_text(encoding="utf-8").casefold()
            self.assertNotIn("token", text)
            self.assertNotIn("password", text)
            self.assertNotIn("bars", text)
            self.assertEqual(load_market_snapshot(path), snapshot)

    def test_insufficient_history_is_rejected(self):
        provider = OfflineProvider({"510300.SH": self.bars[-20:]})
        with self.assertRaisesRegex(ValueError, "122"):
            build_market_snapshot(
                provider,
                MarketSnapshotRequest(
                    code="510300.SH",
                    as_of=self.dates[-1],
                    asset_type="ETF",
                    volatility_window=20,
                    risk_free_rate=0.02,
                ),
            )

    def test_root_entry_can_persist_and_reload_snapshot(self):
        provider = OfflineProvider({"510300.SH": self.bars})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "market_snapshot.json"
            with patch.object(
                main._market_data().IFindHTTPProvider,
                "from_environment",
                return_value=provider,
            ):
                snapshot = main.fetch_ifind_market_snapshot(
                    "510300.SH",
                    self.dates[-1].isoformat(),
                    asset_type="ETF",
                    volatility_window=20,
                    risk_free_rate=0.02,
                    dividend_yield=0.01,
                    output_path=output,
                )
            self.assertEqual(main.load_market_snapshot(output), snapshot)


if __name__ == "__main__":
    unittest.main()
