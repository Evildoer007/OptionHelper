"""Backtester交叉审计发现项的独立回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest
from modules.backtester.entry_generator import BacktestInputError
from modules.backtester.historical_data import HistoricalDataError, load_local_historical_data
from modules.backtester.impl.config import BacktestConfigError
from modules.backtester.service import BacktesterRuntime
from modules.backtester import service as backtester_service
from modules.backtester.tests.fixtures import temporary_market_layout, two_asset_history
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import load_registry, resolve_contract


def _official_datafetcher_ref() -> dict[str, object]:
    return {
        "data_asset_id": "official-datafetcher-etf",
        "storage_ref": "data:tenant-a:official-datafetcher-etf:opaque",
        "media_type": "text/csv",
        "schema_id": "market-history-v1",
        "asset_ids": ["512480.SH"],
        "normalized_fields": ["date", "asset_id", "close", "adj_close"],
        "coverage": {
            "start_date": "2024-01-02",
            "end_date": "2024-01-04",
            "sessions": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "calendar_id": "CN-SSE",
            "calendar_version": "explicit_sessions",
            "by_asset": {"512480.SH": {"start_date": "2024-01-02", "end_date": "2024-01-04", "row_count": 3}},
        },
        "row_count": 3,
        "price_convention": {
            "frequency": "1d",
            "requested_adjustment": "auto",
            "field_adjustment_by_asset": {
                "512480.SH": {"close": "unadjusted", "adj_close": "forward_adjusted"},
            },
            "asset_market_conventions": {
                "512480.SH": {
                    "asset_class": "etf",
                    "historical_return_field": "adj_close",
                    "effective_adjustment": "forward",
                },
            },
            "calendar_id": "CN-SSE",
            "calendar_version": "explicit_sessions",
        },
        "content_hash": "a" * 64,
        "lineage": {"provider": "datafetcher-test"},
        "tenant_id": "tenant-a",
        "created_by": "datafetcher-test",
        "access_scope": ["read"],
        "partition_spec": {"frequency": "1d"},
    }


class HistoricalProtocolRegressionTest(unittest.TestCase):
    def test_official_datafetcher_price_convention_is_consumable(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=3, freq="B"),
            "asset_id": "512480.SH",
            "close": [1.00, 1.01, 1.02],
            "adj_close": [0.98, 0.995, 1.02],
        })
        history = HistoricalData.from_frame(frame, data_asset_ref=_official_datafetcher_ref())
        self.assertEqual(history.contract_price_field, "close")
        self.assertEqual(history.contract_adjustment, "unadjusted")
        self.assertEqual(history.hv_price_field, "adj_close")
        self.assertEqual(history.hv_adjustment, "forward")

    def test_invalid_content_hash_is_rejected_at_historical_data_boundary(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=2, freq="B"),
            "asset_id": "000905.SH",
            "close": [100.0, 101.0],
        })
        reference = dict(HistoricalData.from_frame(frame).data_asset_ref)
        reference["content_hash"] = "z" * 64
        with self.assertRaisesRegex(HistoricalDataError, "SHA-256"):
            HistoricalData.from_frame(frame, data_asset_ref=reference)

    def test_legacy_equal_close_fields_require_explicit_alias_declaration(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=2, freq="B"),
            "asset_id": "000905.SH",
            "close": pd.Series([100, 101], dtype="int64"),
            "adj_close": pd.Series([100.0, 101.0], dtype="float64"),
        })
        seed = frame.copy()
        seed["adj_close"] = [99.0, 100.0]
        reference = dict(HistoricalData.from_frame(seed).data_asset_ref)
        reference["price_convention"] = {
            "contract_close_field": "close",
            "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close",
            "hv_adjustment": "forward",
            "close_equals_adj_close": False,
        }
        with self.assertRaisesRegex(HistoricalDataError, "相同仅限明确声明"):
            HistoricalData.from_frame(frame, data_asset_ref=reference)

    def test_local_stock_csv_cannot_infer_equal_close_alias(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=2, freq="B"),
            "asset_id": "600000.SH",
            "close": [10.0, 10.1],
            "adj_close": [10.0, 10.1],
        })
        with tempfile.TemporaryDirectory(prefix="backtester-local-stock-") as temporary:
            path = Path(temporary) / "stock.csv"
            frame.to_csv(path, index=False)
            with self.assertRaisesRegex(HistoricalDataError, "相同仅限明确声明"):
                load_local_historical_data(path, project_root=PROJECT_ROOT, data_root=Path(temporary))


class BacktestConfigRegressionTest(unittest.TestCase):
    def test_complete_tenor_rejects_string_and_numeric_truthiness(self) -> None:
        for value in ("false", "true", 0, 1):
            with self.subTest(value=value), self.assertRaisesRegex(BacktestConfigError, "布尔值"):
                BacktestConfig.from_mapping({"complete_tenor": value})

    def test_hv_bins_require_an_hv_window(self) -> None:
        with self.assertRaisesRegex(BacktestConfigError, "同时提供"):
            BacktestConfig.from_mapping({"entry_hv_bins": [0.2, 0.4]})

    def test_reject_policy_rejects_incomplete_and_missing_explicit_entries(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=4, freq="B"),
            "asset_id": "000905.SH",
            "close": [100.0, 101.0, 102.0, 103.0],
        })
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365})
        history = HistoricalData.from_frame(frame)
        with self.assertRaisesRegex(BacktestInputError, "insufficient_path"):
            backtest(BacktestInput(contract, BacktestConfig(missing_data_policy="reject"), history))
        with self.assertRaisesRegex(BacktestInputError, "entry_date_not_in_aligned_trading_calendar"):
            backtest(BacktestInput(
                contract,
                BacktestConfig(
                    entry_rule="explicit", entry_dates=("2024-01-07",), missing_data_policy="reject",
                ),
                history,
            ))


class MetricProfileRegressionTest(unittest.TestCase):
    def test_worst_of_terminal_profile_uses_worst_underlying(self) -> None:
        dates = pd.date_range("2024-01-02", periods=3, freq="B")
        frame = pd.DataFrame([
            {"date": date, "asset_id": asset, "close": price}
            for asset, prices in (("000905.SH", [100.0, 110.0, 120.0]), ("000300.SH", [100.0, 90.0, 80.0]))
            for date, price in zip(dates, prices)
        ])
        contract = resolve_contract(
            "10.1",
            identity={"underlyings": ["000905.SH", "000300.SH"]},
            term_overrides={"T": 2 / 365},
        )
        result = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(frame),
        ))
        trade = result.trades[0]
        self.assertAlmostEqual(trade.terminal_performance, -0.2)
        self.assertAlmostEqual(result.summary()["specialized_metrics"]["terminal_performance"]["average"], -0.2)

    def test_terminal_profile_uses_contract_case_segments_not_return_sign_as_moneyness(self) -> None:
        frame = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=3, freq="B"),
            "asset_id": "000905.SH",
            "close": [100.0, 103.0, 105.0],
        })
        contract = resolve_contract(
            "2.1",
            identity={"underlyings": ["000905.SH"]},
            term_overrides={"T": 2 / 365, "K": 110.0},
        )
        metrics = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(frame),
        )).summary()["specialized_metrics"]
        self.assertNotIn("terminal_moneyness", metrics)
        self.assertEqual(metrics["terminal_segments"][0]["count"], 1)
        self.assertEqual(metrics["terminal_segments"][0]["domain"], "0 <= S_T <= K")

    def test_event_and_coupon_profiles_include_conditional_cashflow_statistics(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        single_ko = backtest(BacktestInput(
            resolve_contract("5.1", identity={"underlyings": ["000905.SH"]}),
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
            history,
        )).summary()["specialized_metrics"]
        self.assertIn("triggered_pnl", single_ko["trigger_vs_untriggered"])

        coupon = backtest(BacktestInput(
            resolve_contract("9.24", identity={"underlyings": ["000905.SH"]}),
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
            history,
        )).summary()["specialized_metrics"]
        self.assertEqual(coupon["profile_id"], "coupon_autocall")
        self.assertIn("payment_rate", coupon["coupon_payment"])

    def test_metric_profile_hash_and_boolean_monitors_are_auditable(self) -> None:
        result = backtest(BacktestInput(
            resolve_contract("5.1", identity={"underlyings": ["000905.SH"]}),
            BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
            HistoricalData.from_frame(two_asset_history()),
        )).to_dict()
        self.assertRegex(result["metric_profile"]["metric_profile_hash"], r"^[0-9a-f]{64}$")
        observed = result["monitor_summary"]["terminal_out_observed"]
        self.assertEqual(observed["true_count"], 1)
        self.assertNotIn("average", observed)

    def test_execution_fingerprint_binds_complete_price_convention(self) -> None:
        frame = two_asset_history()
        frame = frame[frame["asset_id"] == "000905.SH"].copy()
        forward = HistoricalData.from_frame(frame)
        none_ref = dict(forward.data_asset_ref)
        none_ref["price_convention"] = {
            **none_ref["price_convention"],
            "hv_adjustment": "none",
        }
        unadjusted_alias = HistoricalData.from_frame(frame, data_asset_ref=none_ref)
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        config = BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",))
        forward_result = backtest(BacktestInput(contract, config, forward))
        alias_result = backtest(BacktestInput(contract, config, unadjusted_alias))
        self.assertNotEqual(forward_result.execution_fingerprint, alias_result.execution_fingerprint)


class PageParameterOrderRegressionTest(unittest.TestCase):
    def test_backtest_config_controls_follow_blueprint_order(self) -> None:
        html = (PROJECT_ROOT / "modules" / "backtester" / "page" / "backtester.html").read_text(encoding="utf-8")
        ids = (
            "startDate", "endDate", "entryRule", "entryDates", "completeTenor",
            "missingPolicy", "alignment", "denominator", "statisticsFrequency", "entryHvWindow", "entryHvBins",
        )
        positions = [html.index(f'id="{field_id}"') for field_id in ids]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('id="historyReference"', html)
        self.assertIn("DataAssetRef", html)


class EconomicResultStabilityTest(unittest.TestCase):
    def test_65_product_paths_cashflows_and_returns_do_not_drift(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        rows: dict[str, object] = {}
        for product_id in load_registry()["products"]:
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            trades = backtest(BacktestInput(
                contract,
                BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
                history,
            )).trades
            rows[product_id] = [{
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
                "path_id": trade.path_id,
                "case_id": trade.case_id,
                "cashflows": list(trade.cashflows),
                "pnl": trade.pnl,
                "return_value": trade.return_value,
            } for trade in trades]
        digest = sha256(json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        self.assertEqual(digest, "f31e61e66baee81b9d75ec893546a4912db9fae4db8035ee078b443dec2d021c")

    def test_local_page_run_commits_nonempty_module_run_identity(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="backtester-audit-result-") as temporary,
            temporary_market_layout() as (project_root, data_root, relative),
            patch.object(backtester_service, "PROJECT_ROOT", project_root),
            patch.object(backtester_service, "RUNTIME_PATHS", SimpleNamespace(data_root=data_root)),
        ):
            response = BacktesterRuntime(
                result_store=LocalResultStore(temporary), tenant_id="tenant-a",
            ).run({
                "product_id": "2.1",
                "identity": {"underlyings": ["000905.SH"]},
                "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2021-01-04"]},
                "history_reference": relative,
            })
        self.assertTrue(response["analysis_case_id"])
        self.assertTrue(response["candidate_id"])
        self.assertEqual(response["catalog_version"], response["resolved_contract"]["registry_snapshot_hash"])
        self.assertEqual(response["contract_fingerprint"], response["resolved_contract"]["contract_fingerprint"])
        self.assertEqual(response["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
