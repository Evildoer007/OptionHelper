from __future__ import annotations

from datetime import date
import hashlib
from types import SimpleNamespace
import unittest

from runtime.contracts.contract_api import resolve_contract
from runtime.contracts.input_adapter import prepare_compute_request
from runtime.protocol.models import DataAssetRef

from modules.pricer.config import PricingConfig
from modules.pricer.engines.pricing_core.engine.derivatives.standard.optionreg_path import _session_dates
from modules.pricer.models import HistoricalData, PricingInput, TradingCalendarData
from modules.pricer.valuation_solver import _calendar_sessions_for_contract
from modules.pricer.valuation_solver import price


class FutureCalendarDecouplingTests(unittest.TestCase):
    def test_non_trading_valuation_and_weekend_maturity_use_prior_sessions(self) -> None:
        calendar = {
            "sessions": (
                "2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12",
                "2026-08-13", "2026-08-14", "2026-08-17",
            ),
        }
        contract = SimpleNamespace(
            identity={"contract_end_date": "2026-08-16"},
            terms={"T": 9 / 365},
        )
        effective, terminal = _calendar_sessions_for_contract(
            calendar,
            requested_valuation_date="2026-08-09",
            contract=contract,
            configured_time_to_maturity=None,
        )
        self.assertEqual(effective, "2026-08-07")
        self.assertEqual(terminal, "2026-08-14")

    def test_current_trading_day_can_disclose_prior_completed_market_session(self) -> None:
        calendar = {
            "sessions": ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"),
        }
        contract = SimpleNamespace(
            identity={"contract_end_date": "2026-08-14"},
            terms={"T": 4 / 365},
        )
        effective, terminal = _calendar_sessions_for_contract(
            calendar,
            requested_valuation_date="2026-08-12",
            contract=contract,
            configured_time_to_maturity=None,
            available_market_date="2026-08-11",
        )
        self.assertEqual(effective, "2026-08-11")
        self.assertEqual(terminal, "2026-08-14")

    def test_path_engine_keeps_full_calendar_instead_of_sampling_n_obs(self) -> None:
        sessions = tuple(
            day.isoformat()
            for day in (
                date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12),
                date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17),
                date(2026, 8, 18), date(2026, 8, 19),
            )
        )
        instrument = SimpleNamespace(
            calendar_id="CN-SSE",
            calendar_version="calendar-hash",
            trading_sessions=sessions,
            resolved_contract=SimpleNamespace(terms={"T": 9 / 365, "n_obs": 2}),
        )
        values, times = _session_dates(
            instrument,
            SimpleNamespace(as_of=date(2026, 8, 10)),
        )
        self.assertEqual(tuple(value.isoformat() for value in values), sessions)
        self.assertEqual(len(times), len(sessions))

    def test_core_classifies_history_and_calendar_by_schema_not_position(self) -> None:
        contract = resolve_contract("2.1", identity={
            "underlyings": ["000300.SH"],
            "contract_start_date": "2026-08-07",
            "reference_prices": {"000300.SH": 4000.0},
        })
        history = _data_ref("history", "market-history-v1", "text/csv")
        calendar = _data_ref("calendar", "trading-calendar", "application/json")
        prepared = prepare_compute_request(
            "pricer",
            {
                "action": "run",
                "product_id": "2.1",
                "identity": dict(contract.identity),
                "term_overrides": {},
                "pricing_config": {"valuation_date": "2026-08-09", "model_method": "black_scholes"},
            },
            data_refs=(calendar, history),
            resolved_contract=contract.to_protocol_dict(),
        )["request"]
        self.assertEqual(prepared["market_data_refs"][0]["schema_id"], "market-history-v1")
        self.assertEqual(prepared["trading_calendar_ref"]["schema_id"], "trading-calendar")

    def test_pricer_discloses_requested_and_effective_valuation_dates(self) -> None:
        rows = tuple(
            {"date": value, "asset_id": "000300.SH", "close": 4000.0 + index, "adj_close": 4000.0 + index}
            for index, value in enumerate((
                "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10",
                "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
                "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
                "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
                "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
            ))
        )
        history_hash = hashlib.sha256(b"history").hexdigest()
        history = HistoricalData(
            source_ref="data:history",
            rows=rows,
            content_hash=history_hash,
            asset_ids=("000300.SH",),
            coverage={
                "start_date": rows[0]["date"], "end_date": rows[-1]["date"],
                "sessions": [row["date"] for row in rows],
                "calendar_id": "CN-SSE", "calendar_version": "history-only",
                "by_asset": {"000300.SH": {"start_date": rows[0]["date"], "end_date": rows[-1]["date"], "row_count": len(rows)}},
            },
        )
        history_ref = DataAssetRef(
            data_asset_id="history", storage_ref=history.source_ref, media_type="text/csv",
            schema_id="market-history-v1", asset_ids=("000300.SH",),
            normalized_fields=history.normalized_fields, coverage=history.coverage or {}, row_count=len(rows),
            price_convention={"adjustment": "close_and_adj_close"}, content_hash=history_hash,
            lineage={"provider": "test"},
        )
        calendar_sessions = ("2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14")
        calendar_hash = hashlib.sha256(b"calendar").hexdigest()
        calendar = TradingCalendarData(
            source_ref="data:calendar", asset_ids=("000300.SH",), sessions=calendar_sessions,
            sessions_by_exchange={"SSE": calendar_sessions}, asset_exchange={"000300.SH": "SSE"},
            requested_start_date="2026-08-07", requested_end_date="2026-08-16", content_hash=calendar_hash,
        )
        calendar_ref = DataAssetRef(
            data_asset_id="calendar", storage_ref=calendar.source_ref, media_type="application/json",
            schema_id="trading-calendar", asset_ids=("000300.SH",), normalized_fields=("session",),
            coverage={"start_date": "2026-08-07", "end_date": "2026-08-16", "sessions": list(calendar_sessions), "calendar_id": "CN-SSE", "calendar_version": "calendar-hash"},
            row_count=len(calendar_sessions), price_convention={"contains_market_prices": False},
            content_hash=calendar_hash, lineage={"provider": "ifind_http"},
        )
        contract = resolve_contract("2.1", identity={
            "underlyings": ["000300.SH"], "contract_start_date": "2026-08-07",
            "contract_end_date": "2026-08-16", "reference_prices": {"000300.SH": rows[-1]["close"]},
        })
        result = price(PricingInput(
            contract=contract,
            pricing_config=PricingConfig.from_mapping({
                "valuation_date": "2026-08-09", "model_method": "black_scholes",
                "risk_free_rate": 0.02, "dividend_yield": 0.0,
            }),
            historical_data=history,
            market_data_refs=(history_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
        ))
        self.assertEqual(result.market_snapshot["requested_valuation_date"], "2026-08-09")
        self.assertEqual(result.market_snapshot["effective_valuation_session"], "2026-08-07")
        self.assertEqual(result.market_snapshot["effective_maturity_session"], "2026-08-14")

    def test_path_monte_carlo_runs_with_history_ending_before_future_calendar(self) -> None:
        history, history_ref, calendar, calendar_ref = _synthetic_market_inputs()
        contract = resolve_contract("5.1", identity={
            "underlyings": ["000300.SH"], "contract_start_date": "2026-08-07",
            "contract_end_date": "2026-08-16", "reference_prices": {"000300.SH": 4024.0},
        }, term_overrides={"T": 9 / 365})
        result = price(PricingInput(
            contract=contract,
            pricing_config=PricingConfig.from_mapping({
                "valuation_date": "2026-08-09", "model_method": "monte_carlo",
                "path_count": 1000, "risk_free_rate": 0.02, "dividend_yield": 0.0,
            }),
            historical_data=history,
            market_data_refs=(history_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
        ))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.market_snapshot["effective_valuation_session"], "2026-08-07")
        self.assertEqual(result.market_snapshot["effective_maturity_session"], "2026-08-14")

    def test_path_monte_carlo_never_reuses_history_sessions_as_future_calendar(self) -> None:
        history, history_ref, _calendar, _calendar_ref = _synthetic_market_inputs()
        contract = resolve_contract("5.1", identity={
            "underlyings": ["000300.SH"], "contract_start_date": "2026-08-07",
            "contract_end_date": "2026-08-16", "reference_prices": {"000300.SH": 4024.0},
        }, term_overrides={"T": 9 / 365})
        with self.assertRaisesRegex(ValueError, "trading-calendar"):
            price(PricingInput(
                contract=contract,
                pricing_config=PricingConfig.from_mapping({
                    "valuation_date": "2026-08-09", "model_method": "monte_carlo",
                    "path_count": 1000, "risk_free_rate": 0.02, "dividend_yield": 0.0,
                }),
                historical_data=history,
                market_data_refs=(history_ref,),
            ))


def _data_ref(label: str, schema_id: str, media_type: str) -> dict[str, object]:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return {
        "data_asset_id": label, "storage_ref": f"data:{label}", "media_type": media_type,
        "schema_id": schema_id, "asset_ids": ["000300.SH"],
        "normalized_fields": ["session"] if schema_id == "trading-calendar" else ["date", "asset_id", "close", "adj_close"],
        "coverage": {"start_date": "2026-08-07", "end_date": "2026-08-16", "sessions": ["2026-08-07"], "calendar_id": "CN-SSE", "calendar_version": "test"},
        "row_count": 1, "price_convention": {"contains_market_prices": False} if schema_id == "trading-calendar" else {"adjustment": "close_and_adj_close"},
        "content_hash": digest, "lineage": {"provider": "test"}, "tenant_id": "local",
        "created_by": "test", "access_scope": ["read"], "partition_spec": {},
    }


def _synthetic_market_inputs() -> tuple[HistoricalData, DataAssetRef, TradingCalendarData, DataAssetRef]:
    dates = (
        "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10",
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
        "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    )
    rows = tuple(
        {"date": value, "asset_id": "000300.SH", "close": 4000.0 + index, "adj_close": 4000.0 + index}
        for index, value in enumerate(dates)
    )
    history_hash = hashlib.sha256(b"path-history").hexdigest()
    coverage = {
        "start_date": dates[0], "end_date": dates[-1], "sessions": list(dates),
        "calendar_id": "CN-SSE", "calendar_version": "history-only",
        "by_asset": {"000300.SH": {"start_date": dates[0], "end_date": dates[-1], "row_count": len(dates)}},
    }
    history = HistoricalData(
        source_ref="data:path-history", rows=rows, content_hash=history_hash,
        asset_ids=("000300.SH",), coverage=coverage,
    )
    history_ref = DataAssetRef(
        data_asset_id="path-history", storage_ref=history.source_ref, media_type="text/csv",
        schema_id="market-history-v1", asset_ids=("000300.SH",), normalized_fields=history.normalized_fields,
        coverage=coverage, row_count=len(rows), price_convention={"adjustment": "close_and_adj_close"},
        content_hash=history_hash, lineage={"provider": "test"},
    )
    sessions = ("2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14")
    calendar_hash = hashlib.sha256(b"path-calendar").hexdigest()
    calendar = TradingCalendarData(
        source_ref="data:path-calendar", asset_ids=("000300.SH",), sessions=sessions,
        sessions_by_exchange={"SSE": sessions}, asset_exchange={"000300.SH": "SSE"},
        requested_start_date="2026-08-07", requested_end_date="2026-08-16", content_hash=calendar_hash,
    )
    calendar_ref = DataAssetRef(
        data_asset_id="path-calendar", storage_ref=calendar.source_ref, media_type="application/json",
        schema_id="trading-calendar", asset_ids=("000300.SH",), normalized_fields=("session",),
        coverage={"start_date": "2026-08-07", "end_date": "2026-08-16", "sessions": list(sessions), "calendar_id": "CN-SSE", "calendar_version": "calendar-hash"},
        row_count=len(sessions), price_convention={"contains_market_prices": False},
        content_hash=calendar_hash, lineage={"provider": "ifind_http"},
    )
    return history, history_ref, calendar, calendar_ref


if __name__ == "__main__":
    unittest.main()
