"""Pricer交易日历、演示精度与市场资产边界回归。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import DataAssetRef
from modules.pricer import HistoricalData, PricingConfig, PricingInput, price
from modules.pricer.market_resolver import market_snapshot_from_history
from modules.pricer.observed_state import ObservedContractState
from modules.pricer.product_pricing_adapter import ProductPricingAdapter
from .pricer_test_fixtures import calendar_asset, explicit_test_sessions


def _sessions(count: int = 620) -> tuple[str, ...]:
    """Use the self-contained test session sequence, never a repository market file."""
    values = [value for value in explicit_test_sessions() if value >= "2024-01-02"]
    if len(values) < count:
        raise AssertionError("受控市场资产没有足够的显式交易sessions")
    return tuple(values[:count])


def _historical_asset() -> tuple[HistoricalData, DataAssetRef, tuple[str, ...]]:
    sessions = _sessions()
    rows = tuple(
        {
            "date": session,
            "asset_id": "A",
            "close": 100.0 + index * 0.1,
            "adj_close": 100.0 * (1.0005 ** index),
        }
        for index, session in enumerate(sessions[:30])
    )
    coverage = {
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "sessions": sessions,
        "calendar_id": "CN-SSE",
        "calendar_version": "fixture-2026-08-07",
        "by_asset": {"A": {"start": sessions[0], "end": sessions[29]}},
    }
    historical = HistoricalData(
        "memory://market/A",
        rows,
        asset_ids=("A",),
        coverage=coverage,
    )
    return historical, DataAssetRef(
        data_asset_id="fixture-A",
        storage_ref=historical.source_ref,
        media_type="text/csv",
        schema_id="market-history-v1",
        asset_ids=("A",),
        normalized_fields=("date", "asset_id", "close", "adj_close"),
        coverage=coverage,
        row_count=len(rows),
        price_convention={},
        content_hash=historical.content_hash,
        lineage={},
    ), sessions


class CalendarPrecisionGateTest(unittest.TestCase):
    def _contract(self, product_id: str):
        return resolve_contract(product_id, identity={
            "underlyings": ("A",),
            "contract_reference_spots": {"A": 100.0},
        })

    def test_mc10_is_not_a_quote_without_explicit_demo_mode(self) -> None:
        calendar, calendar_ref = calendar_asset(("A",))
        result = price(PricingInput(
            self._contract("5.1"),
            PricingConfig(spot=100.0, historical_volatility=.2, model_method="monte_carlo", path_count=10),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
        ))
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.precision_status, "not_priced")
        self.assertFalse(result.quote_eligible)
        self.assertIn("MC10仅允许显式demo_mode", result.messages[0])

    def test_demo_result_has_machine_precision_gate(self) -> None:
        historical, ref, sessions = _historical_asset()
        result = price(PricingInput(
            self._contract("2.1"),
            PricingConfig(
                valuation_date=sessions[29],
                model_method="monte_carlo",
                path_count=10,
                demo_mode=True,
                risk_free_rate=.02,
            ),
            historical,
            (ref,),
        ))
        self.assertEqual(result.status, "priced")
        self.assertEqual(result.precision_status, "demo_only")
        self.assertFalse(result.quote_eligible)
        self.assertEqual(result.path_count, 10)

    def test_discrete_path_uses_injected_sessions_and_act_365(self) -> None:
        historical, ref, sessions = _historical_asset()
        snapshot = market_snapshot_from_history(
            __import__("pandas").DataFrame(historical.rows),
            ("A",),
            valuation_date=sessions[29],
            hv_window=20,
            risk_free_rate=.02,
            dividend_yield=0.0,
            trading_calendar={
                "calendar_id": ref.coverage["calendar_id"],
                "calendar_version": ref.coverage["calendar_version"],
                "sessions": sessions,
                "verified_cn_sessions": True,
                "source": "host-injected",
            },
        )
        adapter = ProductPricingAdapter(
            self._contract("5.1"),
            PricingConfig(
                valuation_date=sessions[29], spot=100.0, historical_volatility=.2,
                model_method="monte_carlo", path_count=10, demo_mode=True, risk_free_rate=.02,
                time_to_maturity=.25,
            ),
            snapshot,
            ObservedContractState.from_value(None, valuation_date=sessions[29]),
        )
        result = adapter.reprice()
        calendar = result.diagnostics["calendar"]
        self.assertEqual(calendar["calendar_id"], "CN-SSE")
        self.assertEqual(calendar["time_basis"], "ACT/365 from injected trading sessions")
        self.assertNotIn("weekday", " ".join(result.warnings).lower())

    def test_data_asset_hash_mismatch_is_rejected(self) -> None:
        historical, ref, sessions = _historical_asset()
        bad_ref = DataAssetRef(
            data_asset_id=ref.data_asset_id,
            storage_ref=ref.storage_ref,
            media_type=ref.media_type,
            schema_id=ref.schema_id,
            asset_ids=ref.asset_ids,
            normalized_fields=ref.normalized_fields,
            coverage=ref.coverage,
            row_count=ref.row_count,
            price_convention=ref.price_convention,
            content_hash="0" * 64,
            lineage=ref.lineage,
        )
        with self.assertRaisesRegex(ValueError, "content_hash"):
            price(PricingInput(
                self._contract("2.1"),
                PricingConfig(valuation_date=sessions[29], risk_free_rate=.02),
                historical,
                (bad_ref,),
            ))


if __name__ == "__main__":
    unittest.main()
