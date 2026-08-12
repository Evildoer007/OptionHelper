"""Pricer日期与S₀Raw默认值的DataAssetRef回归。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from hashlib import sha256
import csv
import io
import inspect
import unittest

from core import tool_entry
from runtime.protocol.models import DataAssetRef

from modules.pricer.input_defaults import (
    PricerInputDefaultError,
    compile_pricer_input_defaults,
)
from modules.pricer import service


class _ReadOnlyAssetPort:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes:
        self.calls.append((ref.data_asset_id, tenant_id))
        return self.payload


def _asset() -> tuple[DataAssetRef, bytes]:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=("date", "asset_id", "close", "adj_close"))
    writer.writeheader()
    for day, close in (("2026-08-06", 9.0), ("2026-08-07", 10.0), ("2026-08-10", 11.0)):
        writer.writerow({"date": day, "asset_id": "000905.SH", "close": close, "adj_close": close})
    payload = stream.getvalue().encode("utf-8")
    ref = DataAssetRef(
        data_asset_id="fixture-csi500", storage_ref="memory://fixture-csi500", media_type="text/csv",
        schema_id="market-history-v1", asset_ids=("000905.SH",),
        normalized_fields=("date", "asset_id", "close", "adj_close"),
        coverage={
            "start": "2026-08-06", "end": "2026-08-10",
            "sessions": ("2026-08-06", "2026-08-07", "2026-08-10"),
            "calendar_id": "CN-SSE", "calendar_version": "fixture-v1",
            "by_asset": {"000905.SH": {"start": "2026-08-06", "end": "2026-08-10"}},
        },
        row_count=3, price_convention={"adjustment": "close_and_adj_close"},
        content_hash=sha256(payload).hexdigest(), lineage={"fixture": "input-defaults"},
        tenant_id="tenant-fixture", created_by="fixture", access_scope=("read",), partition_spec={},
    )
    return ref, payload


def _calendar_asset() -> DataAssetRef:
    payload = b'{"sessions":["2026-08-07","2026-08-10"]}'
    return DataAssetRef(
        data_asset_id="fixture-calendar", storage_ref="memory://fixture-calendar",
        media_type="application/json", schema_id="trading-calendar",
        asset_ids=("000905.SH",), normalized_fields=("session",),
        coverage={
            "start_date": "2026-08-07", "end_date": "2026-08-10",
            "sessions": ("2026-08-07", "2026-08-10"),
            "calendar_id": "CN-SSE", "calendar_version": "fixture-calendar",
        },
        row_count=2, price_convention={"contains_market_prices": False},
        content_hash=sha256(payload).hexdigest(), lineage={"fixture": "calendar"},
        tenant_id="tenant-fixture", created_by="fixture", access_scope=("read",),
        partition_spec={},
    )


class PricerInputDefaultsDataAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ref, payload = _asset()
        self.port = _ReadOnlyAssetPort(payload)
        self.request = {
            "action": "run", "product_id": "2.1",
            "identity": {"underlyings": ["000905.SH"]},
            "term_overrides": {},
            "pricing_config": {"model_method": "black_scholes", "risk_free_rate": 0.02},
        }

    def test_new_issue_uses_device_today_and_previous_trading_close(self) -> None:
        compiled = compile_pricer_input_defaults(
            self.request, data_refs=(self.ref,), data_store=self.port, today=date(2026, 8, 9),
        )
        self.assertEqual(compiled["pricing_config"]["valuation_date"], "2026-08-09")
        self.assertEqual(compiled["identity"]["contract_start_date"], "2026-08-09")
        self.assertEqual(compiled["identity"]["contract_reference_spots"], {"000905.SH": 10.0})
        self.assertEqual(self.port.calls, [("fixture-csi500", "tenant-fixture")])

    def test_midlife_start_uses_its_own_close_and_rejects_valuation_spot(self) -> None:
        request = {
            **self.request,
            "identity": {
                "underlyings": ["000905.SH"], "contract_start_date": "2026-08-07",
            },
            "pricing_config": {"model_method": "black_scholes", "valuation_date": "2026-08-10"},
        }
        compiled = compile_pricer_input_defaults(request, data_refs=(self.ref,), data_store=self.port)
        self.assertEqual(compiled["identity"]["contract_reference_spots"], {"000905.SH": 10.0})
        request["identity"]["contract_reference_spots"] = {"000905.SH": 11.0}
        with self.assertRaisesRegex(PricerInputDefaultError, "合同起始参考价"):
            compile_pricer_input_defaults(request, data_refs=(self.ref,), data_store=self.port)

    def test_regular_pricing_without_asset_is_blocked(self) -> None:
        with self.assertRaisesRegex(PricerInputDefaultError, "绑定真实DataAssetRef"):
            compile_pricer_input_defaults(self.request)

    def test_only_explicit_csi500_mc10_demo_can_omit_data(self) -> None:
        request = {
            **self.request,
            "pricing_config": {
                "model_method": "monte_carlo", "path_count": 10, "demo_mode": True,
                "valuation_date": "2026-08-09", "spot": 7443.4332, "volatility_override": .20,
                "time_to_maturity": 1.0, "risk_free_rate": .02, "dividend_yield": 0.0,
                "demo_calendar": {
                    "calendar_id": "demo-european-vanilla", "calendar_version": "v1",
                    "sessions": ["2026-08-09"], "discrete_path": False,
                },
            },
        }
        compiled = compile_pricer_input_defaults(request)
        self.assertEqual(compiled["identity"]["contract_start_date"], "2026-08-09")
        self.assertEqual(compiled["identity"]["contract_reference_spots"], {"000905.SH": 7443.4332})

    def test_app_compiler_freezes_data_backed_reference_before_contract_resolution(self) -> None:
        request = {
            **self.request,
            "pricing_config": {**self.request["pricing_config"], "valuation_date": "2026-08-09"},
        }
        prepared = tool_entry.prepare_compute_request(
            "pricer", request, data_refs=(asdict(self.ref),), data_store=self.port,
        )
        identity = prepared["resolved_contract"]["identity"]
        self.assertEqual(identity["contract_start_date"], "2026-08-09")
        self.assertEqual(identity["reference_prices"]["000905.SH"], 10.0)

    def test_app_compiler_classifies_history_and_calendar_before_resolving_s0raw(self) -> None:
        calendar = _calendar_asset()
        request = {
            **self.request,
            "pricing_config": {**self.request["pricing_config"], "valuation_date": "2026-08-09"},
        }
        prepared = tool_entry.prepare_compute_request(
            "pricer", request,
            data_refs=(asdict(calendar), asdict(self.ref)),
            data_store=self.port,
        )
        identity = prepared["resolved_contract"]["identity"]
        self.assertEqual(identity["reference_prices"]["000905.SH"], 10.0)
        self.assertEqual(self.port.calls, [("fixture-csi500", "tenant-fixture")])
        formal = prepared["request"]
        self.assertEqual(formal["market_data_refs"][0]["schema_id"], "market-history-v1")
        self.assertEqual(formal["trading_calendar_ref"]["schema_id"], "trading-calendar")

    def test_app_compiler_rejects_ambiguous_or_unknown_ref_schemas(self) -> None:
        calendar = _calendar_asset()
        with self.assertRaisesRegex(PricerInputDefaultError, "唯一market-history-v1"):
            compile_pricer_input_defaults(
                self.request, data_refs=(calendar,), data_store=self.port,
            )
        with self.assertRaisesRegex(PricerInputDefaultError, "最多绑定一个trading-calendar"):
            compile_pricer_input_defaults(
                self.request, data_refs=(self.ref, calendar, calendar), data_store=self.port,
            )
        unknown = DataAssetRef(**{
            **asdict(calendar),
            "data_asset_id": "fixture-unknown",
            "schema_id": "unknown-calendar",
        })
        with self.assertRaisesRegex(PricerInputDefaultError, "不支持的DataAssetRef.schema_id"):
            compile_pricer_input_defaults(
                self.request, data_refs=(self.ref, unknown), data_store=self.port,
            )

    def test_formal_host_dependency_signatures_keep_datastore_private_and_ordered(self) -> None:
        compiler = inspect.signature(tool_entry.prepare_compute_request)
        self.assertEqual(
            tuple(compiler.parameters),
            ("module", "request", "data_refs", "resolved_contract", "data_store"),
        )
        tool = inspect.signature(service.call_tool)
        self.assertEqual(
            tuple(tool.parameters),
            ("request", "host_context", "result_store", "tenant_id", "data_store"),
        )


if __name__ == "__main__":
    unittest.main()
