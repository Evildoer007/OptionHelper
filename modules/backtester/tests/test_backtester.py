"""Backtester正式链路回归：HistoricalData→BacktestInput→BacktestResult。"""

# ruff: noqa: E402

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest  # noqa: E402
from modules.backtester.historical_data import (
    DataFetcherPortUnavailable,
    HistoricalDataError,
    load_local_historical_data,
    load_port_historical_data,
)  # noqa: E402
from modules.backtester.impl.engine import BacktestInputError, ZeroValidSamplesError  # noqa: E402
from modules.backtester.metric_profile_map import METRIC_PROFILE_MAP, validate_metric_profile_coverage  # noqa: E402
from modules.backtester.service import BacktesterRuntime  # noqa: E402
from modules.backtester import service as backtester_service  # noqa: E402
from modules.backtester.impl.config import BacktestConfigError  # noqa: E402
from runtime.adapters.local_store import LocalDataStore  # noqa: E402
from runtime.contracts.contract_api import load_registry, resolve_contract  # noqa: E402
from .fixtures import temporary_market_layout, two_asset_history  # noqa: E402


def _frame(prices: list[float], *, start: str = "2024-01-02", adj_prices: list[float] | None = None) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range(start, periods=len(prices), freq="B"),
        "asset_id": "000905.SH",
        "close": prices,
        "adj_close": adj_prices or [price * 1.01 for price in prices],
    })


def _input(
    product_id: str,
    prices: list[float],
    overrides: dict[str, float] | None = None,
    *,
    start: str = "2024-01-02",
    **config: object,
) -> BacktestInput:
    contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH"]}, term_overrides=overrides or {})
    return BacktestInput(
        contract,
        BacktestConfig(entry_rule="explicit", entry_dates=(start,), **config),
        HistoricalData.from_frame(_frame(prices, start=start)),
    )


class HistoricalDataTest(unittest.TestCase):
    def test_close_is_required_and_duplicate_dates_are_rejected(self) -> None:
        with self.assertRaisesRegex(HistoricalDataError, "close"):
            HistoricalData.from_frame(_frame([100.0]).drop(columns=["close"]))
        duplicated = pd.concat([_frame([100.0]), _frame([100.0])], ignore_index=True)
        with self.assertRaisesRegex(HistoricalDataError, "重复"):
            HistoricalData.from_frame(duplicated)

    def test_controlled_local_data_runs_without_datafetcher_import(self) -> None:
        loaded_before = {
            name for name in sys.modules
            if name == "modules.datafetcher" or name.startswith("modules.datafetcher.")
        }
        with temporary_market_layout() as (project_root, data_root, relative), patch.dict(
            sys.modules, {"modules.datafetcher.service": None},
        ):
            history = load_local_historical_data(relative, project_root=project_root, data_root=data_root)
            self.assertEqual(history.contract_price_field, "close")
            self.assertEqual(history.contract_adjustment, "unadjusted")
            with self.assertRaises(DataFetcherPortUnavailable):
                load_port_historical_data({}, project_root=PROJECT_ROOT, data_root=PROJECT_ROOT / "data", call_port=None)
        loaded_after = {
            name for name in sys.modules
            if name == "modules.datafetcher" or name.startswith("modules.datafetcher.")
        }
        self.assertEqual(loaded_after, loaded_before)

    def test_uncontrolled_local_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(HistoricalDataError, "受控data目录"):
            load_local_historical_data("/tmp/not-allowed.csv", project_root=PROJECT_ROOT, data_root=PROJECT_ROOT / "data")

    def test_injected_data_port_returns_a_controlled_data_asset(self) -> None:
        with temporary_market_layout() as (project_root, data_root, relative):
            local = load_local_historical_data(relative, project_root=project_root, data_root=data_root)
            history = load_port_historical_data(
                {"asset_id": "000905.SH"},
                project_root=project_root,
                data_root=data_root,
                call_port=lambda _: {"ok": True, "data_asset_ref": local.data_asset_ref},
            )
        self.assertEqual(history.data_asset_ref["content_hash"], local.data_asset_ref["content_hash"])

    def test_datafetcher_asset_ref_uses_injected_datastore_without_module_import(self) -> None:
        payload = b"date,asset_id,close,adj_close\n2024-01-02,000905.SH,100,101\n2024-01-03,000905.SH,101,102\n"
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalDataStore(temporary)
            reference = store.put_bytes(
                tenant_id="local",
                data_asset_id="backtester-port-test",
                payload=payload,
                media_type="text/csv",
                schema_id="market-history-v1",
                asset_ids=("000905.SH",),
                normalized_fields=("date", "asset_id", "close", "adj_close"),
                coverage={"by_asset": {"000905.SH": {"start_date": "2024-01-02", "end_date": "2024-01-03", "row_count": 2}}},
                row_count=2,
                price_convention={"contract_close_field": "close", "contract_adjustment": "unadjusted", "hv_close_field": "adj_close", "hv_adjustment": "forward", "close_equals_adj_close": False},
                lineage={"provider": "port-test"},
            )
            history = load_port_historical_data(
                {"asset_id": "000905.SH"},
                project_root=PROJECT_ROOT,
                data_root=PROJECT_ROOT / "data",
                call_port=lambda _: {"ok": True, "data_asset_ref": asdict(reference)},
                data_store=store,
            )
        self.assertEqual(history.data_asset_ref["storage_ref"], reference.storage_ref)
        self.assertEqual(history.data_asset_ref["content_hash"], reference.content_hash)

    def test_equal_close_and_adj_close_need_explicit_index_declaration(self) -> None:
        equal = _frame([100.0, 101.0], adj_prices=[100.0, 101.0])
        with self.assertRaisesRegex(HistoricalDataError, "明确声明"):
            HistoricalData.from_frame(equal)
        history = HistoricalData.from_frame(equal, data_asset_ref={
            "price_convention": {"contract_close_field": "close", "contract_adjustment": "unadjusted", "hv_close_field": "adj_close", "hv_adjustment": "forward", "close_equals_adj_close": True},
        })
        self.assertTrue(history.data_asset_ref["price_convention"]["close_equals_adj_close"])

    def test_close_only_history_runs_contract_but_marks_hv_unavailable(self) -> None:
        history = HistoricalData.from_frame(_frame([100.0, 101.0, 102.0, 103.0]).drop(columns=["adj_close"]))
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365, "Pi_0": 1.0})
        result = backtest(BacktestInput(contract, BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",), entry_hv_window=5), history))
        self.assertEqual(result.trades[0].entry_features["reason"], "adj_close_not_provided")
        self.assertIsNone(result.to_dict()["price_convention_evidence"]["entry_hv"]["field"])


class BacktestEngineTest(unittest.TestCase):
    def test_backtest_config_accepts_only_real_iso_dates(self) -> None:
        config = BacktestConfig.from_mapping({"start_date": "2024-02-29", "entry_rule": "explicit", "entry_dates": ["2024-03-01"]})
        self.assertEqual(config.start_date, "2024-02-29")
        with self.assertRaises(BacktestConfigError):
            BacktestConfig.from_mapping({"start_date": "2024/02/29"})
        with self.assertRaises(BacktestConfigError):
            BacktestConfig.from_mapping({"start_date": "2024-02-30"})
        grouped = BacktestConfig.from_mapping({"entry_hv_window": 20, "entry_hv_bins": [0.2, 0.4]})
        self.assertEqual(grouped.entry_hv_bins, (0.2, 0.4))
        with self.assertRaises(BacktestConfigError):
            BacktestConfig.from_mapping({"entry_hv_window": 21})

    def test_single_entry_uses_shared_cashflow_and_freezes_trade_contract(self) -> None:
        result = backtest(_input("2.1", [100.0, 105.0, 110.0, 110.0], {"T": 2 / 365, "K": 100.0, "Pi_0": 1.0}))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.pnl, 9.0)
        self.assertEqual(trade.actual_calendar_days, 2)
        self.assertEqual(trade.entry_normalized_spots, {"000905.SH": 100.0})
        self.assertEqual(trade.historical_contract.reference_prices, {"000905.SH": 100.0})
        self.assertEqual(trade.trade_contract_fingerprint, trade.historical_contract.trade_contract_fingerprint)
        payload = result.to_dict()
        self.assertEqual(payload["data_asset_ref"]["price_convention"]["contract_adjustment"], "unadjusted")
        self.assertEqual(payload["trade_ledger_ref"]["ledger_hash"], result.ledger_hash)
        self.assertFalse("nav_curve" in payload)

    def test_contract_replay_uses_unadjusted_close_not_hv_adj_close(self) -> None:
        contract = resolve_contract("5.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 3 / 365, "H_KO": 102.0, "Pi_0": 1.0})
        history = HistoricalData.from_frame(_frame([100.0, 103.0, 103.0, 103.0], adj_prices=[200.0, 200.0, 200.0, 200.0]))
        result = backtest(BacktestInput(contract, BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)), history))
        self.assertEqual(result.trades[0].exit_date, "2024-01-03")
        evidence = result.to_dict()["price_convention_evidence"]
        self.assertEqual(evidence["contract_settlement"], {"field": "close", "adjustment": "unadjusted"})

    def test_entry_hv_uses_only_pre_entry_adj_close_without_lookahead(self) -> None:
        prices = [100.0 + index for index in range(12)]
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365, "Pi_0": 1.0})
        first = HistoricalData.from_frame(_frame(prices, adj_prices=[100 + index * 1.02 for index in range(12)]))
        changed = _frame(prices, adj_prices=[100 + index * 1.02 if index <= 6 else 10_000 + index for index in range(12)])
        second = HistoricalData.from_frame(changed)
        config = BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-10",), entry_hv_window=5, entry_hv_bins=(0.1, 0.3))
        first_feature = backtest(BacktestInput(contract, config, first)).trades[0].entry_features
        second_feature = backtest(BacktestInput(contract, config, second)).trades[0].entry_features
        self.assertEqual(first_feature, second_feature)
        self.assertEqual(first_feature["as_of"], "2024-01-10")
        self.assertEqual(first_feature["price_field"], "adj_close")

    def test_event_profile_uses_shared_monitor_and_actual_calendar_days(self) -> None:
        result = backtest(_input("5.1", [100.0, 101.0, 103.0, 104.0], {"T": 3 / 365, "H_KO": 102.0, "Pi_0": 1.0}))
        trade = result.trades[0]
        self.assertIsNotNone(trade.events["tau_out"])
        self.assertEqual(trade.actual_calendar_days, 2)
        specialized = result.summary()["specialized_metrics"]
        self.assertEqual(specialized["profile_id"], "single_knock_out")
        self.assertIn("tau_out", specialized["events"])

    def test_statistics_frequency_controls_yearly_summary_and_ledger_hash_is_stable(self) -> None:
        source = _input("2.1", [100.0, 105.0, 110.0, 110.0], {"T": 2 / 365, "Pi_0": 1.0})
        first = backtest(source)
        second = backtest(source)
        self.assertEqual(first.ledger_hash, second.ledger_hash)
        self.assertEqual(first.summary()["annual_summary"], [])
        yearly = backtest(BacktestInput(
            source.contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",), statistics_frequency="year"),
            source.historical_data,
        ))
        self.assertEqual(yearly.summary()["annual_summary"][0]["year"], 2024)

    def test_maturity_uses_last_trading_day_on_or_before_contract_date(self) -> None:
        prices = [100.0] * 270
        result = backtest(_input("2.1", prices, {"T": 1.0, "Pi_0": 1.0}, start="2021-01-08"))
        trade = result.trades[0]
        self.assertEqual(trade.exit_date, "2022-01-07")
        self.assertEqual(trade.actual_calendar_days, 364)
        self.assertLessEqual(trade.actual_time_years, 1.0)

    def test_early_termination_truncates_later_events_and_freezes_actual_schedule(self) -> None:
        dates = pd.date_range("2024-01-02", periods=80, freq="B")
        prices = [100.0] * len(dates)
        prices[dates.get_loc("2024-01-31")] = 104.0
        prices[dates.get_loc("2024-02-01")] = 60.0
        contract = resolve_contract("9.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 0.2})
        result = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(_frame(prices)),
        ))
        trade = result.trades[0]
        self.assertEqual(trade.exit_date, "2024-01-31")
        self.assertIsNone(trade.events["tau_in"])
        schedules = trade.historical_contract.resolved_schedules
        self.assertEqual(schedules["O_KO"]["status"], "resolved")
        self.assertEqual(schedules["O_KO"]["dates"], ["2024-01-31"])

    def test_multi_knock_out_event_is_counted_as_knock_out_outcome(self) -> None:
        dates = pd.date_range("2024-01-02", periods=80, freq="B")
        prices = [100.0] * len(dates)
        prices[dates.get_loc("2024-01-31")] = 104.0
        contract = resolve_contract("9.10", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 0.2})
        result = backtest(BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(_frame(prices)),
        ))
        outcomes = {item["code"]: item["count"] for item in result.summary()["specialized_metrics"]["three_outcome_summary"]}
        self.assertEqual(outcomes["ko"], 1)

    def test_mutated_historical_frame_is_rejected_before_replay(self) -> None:
        source = _input("2.1", [100.0, 105.0, 110.0, 110.0], {"T": 2 / 365, "Pi_0": 1.0})
        source.historical_data.frame.loc[0, "close"] = 200.0
        with self.assertRaisesRegex(HistoricalDataError, "内容已变化"):
            backtest(source)

    def test_mutated_data_asset_ref_is_rejected_before_replay(self) -> None:
        source = _input("2.1", [100.0, 105.0, 110.0, 110.0], {"T": 2 / 365, "Pi_0": 1.0})
        source.historical_data.data_asset_ref["content_hash"] = "0" * 64
        with self.assertRaisesRegex(HistoricalDataError, "data_asset_ref已变化"):
            backtest(source)

    def test_uncompleted_tenor_is_not_silently_valued(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        incomplete = BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",), complete_tenor=False),
            HistoricalData.from_frame(_frame([100.0, 101.0, 102.0])),
        )
        with self.assertRaises(ZeroValidSamplesError):
            backtest(incomplete)

    def test_monthly_trigger_contract_can_be_replayed(self) -> None:
        contract = resolve_contract("9.26", identity={"underlyings": ["000905.SH"]})
        input_value = BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(_frame([100.0] * 160)),
        )
        result = backtest(input_value)
        self.assertGreater(len(result.trades), 0)
        self.assertEqual(result.metric_profile_spec.profile_id, "dual_knock_autocall")

    def test_multi_underlying_never_reuses_single_asset_history(self) -> None:
        contract = resolve_contract("10.1", identity={"underlyings": ["000905.SH", "000300.SH"]})
        input_value = BacktestInput(
            contract,
            BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",)),
            HistoricalData.from_frame(_frame([100.0, 101.0, 102.0, 103.0])),
        )
        with self.assertRaisesRegex(BacktestInputError, "000300.SH"):
            backtest(input_value)

    def test_two_asset_fixture_runs_registered_multi_underlying_contracts(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        for product_id in ("9.18", "10.1"):
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            result = backtest(BacktestInput(contract, BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)), history))
            self.assertEqual(set(result.trades[0].entry_market_spots), {"000905.SH", "000300.SH"})

    def test_all_65_products_run_on_two_asset_fixture(self) -> None:
        history = HistoricalData.from_frame(two_asset_history())
        failures: dict[str, str] = {}
        for product_id in load_registry()["products"]:
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            try:
                backtest(BacktestInput(contract, BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)), history))
            except Exception as error:  # 收集产品级精确拒绝理由，断言不得静默遗漏。
                failures[product_id] = str(error)
        self.assertEqual(failures, {})

    def test_range_and_variance_profiles_have_structure_metrics(self) -> None:
        range_result = backtest(_input("10.8", [100.0, 105.0, 120.0, 120.0], {
            "T": 2 / 365, "n_obs": 3, "N": 1_000.0, "Hlow": 90.0, "Hup": 110.0,
        }))
        self.assertEqual(range_result.summary()["specialized_metrics"]["profile_id"], "range_accrual")
        self.assertIn("range_observations", range_result.summary()["specialized_metrics"])
        variance_result = backtest(_input("10.4", [100.0, 102.0, 101.0, 101.0], {"T": 2 / 365}))
        self.assertEqual(variance_result.summary()["specialized_metrics"]["profile_id"], "variance_swap")
        self.assertIn("realized_variance", variance_result.summary()["specialized_metrics"])
        self.assertEqual(variance_result.summary()["specialized_metrics"]["realized_volatility_vs_strike"]["strike_volatility"], 13.0)

    def test_metric_profile_map_covers_all_registered_products(self) -> None:
        registered = load_registry()["products"]
        self.assertEqual(len(registered), 65)
        self.assertEqual(len(METRIC_PROFILE_MAP), 65)
        self.assertEqual(validate_metric_profile_coverage(registered), ())
        self.assertTrue(METRIC_PROFILE_MAP["9.26"].supported)


class BacktesterServiceTest(unittest.TestCase):
    def test_service_runs_controlled_local_data_and_returns_protocol_contract(self) -> None:
        with temporary_market_layout() as (project_root, data_root, relative):
            request = {
                "product_id": "2.1",
                "identity": {"underlyings": ["000905.SH"]},
                "term_overrides": {"T": 2 / 365, "Pi_0": 1.0},
                "history_reference": relative,
                "backtest_config": {
                    "start_date": "2024-01-04",
                    "end_date": "2024-01-10",
                    "entry_rule": "daily",
                },
            }
            with (
                patch.object(backtester_service, "PROJECT_ROOT", project_root),
                patch.object(backtester_service, "RUNTIME_PATHS", SimpleNamespace(data_root=data_root)),
                patch("modules.backtester.service._write_run"),
            ):
                result = BacktesterRuntime().run(request)
        self.assertEqual(result["module"], "backtester")
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("contract_fingerprint", result["resolved_contract"])
        self.assertEqual(result["backtest"]["sample_count"], 4)
        self.assertEqual(result["backtest"]["data_asset_ref"]["price_convention"]["contract_close_field"], "close")

    def test_service_port_without_injection_does_not_fallback_to_datafetcher(self) -> None:
        request = {"product_id": "2.1", "data_request": {"asset_id": "000905.SH"}}
        with self.assertRaises(DataFetcherPortUnavailable):
            BacktesterRuntime().run(request)


if __name__ == "__main__":
    unittest.main()
