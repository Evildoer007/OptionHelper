"""Pricer正式协议、存续状态和ResultStore闭环的独立回归。"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "pricer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalDataStore, LocalResultStore
from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import ModuleRunRef, PricingInput as ProtocolPricingInput
from core import tool_entry
from modules.pricer import PricingConfig, price
from modules.pricer.models import PricingInput as InternalPricingInput
from modules.pricer import service as pricer_service

from .pricer_test_fixtures import calendar_asset, market_asset, market_csv_payload, modulehost_v2_scope


ASSET = "000905.SH"
VALUATION_DATE = "2023-07-28"


def _vanilla_contract():
    return resolve_contract(
        "2.1",
        identity={
            "underlyings": (ASSET,),
            "reference_prices": {ASSET: 6_043.2438},
            "contract_start_date": VALUATION_DATE,
        },
        term_overrides={"K": 5_500.0, "T": 0.5, "Pi_0": 100.0},
    )


def _snowball_contract():
    return resolve_contract(
        "9.1",
        identity={
            "underlyings": ("A",),
            "reference_prices": {"A": 100.0},
            "contract_start_date": "2023-01-30",
        },
    )


def _snowball_input(state: dict) -> InternalPricingInput:
    historical, data_ref = market_asset(("A",))
    calendar, calendar_ref = calendar_asset(("A",))
    return InternalPricingInput(
        contract=_snowball_contract(),
        pricing_config=PricingConfig(
            valuation_date=VALUATION_DATE,
            spot=100.0,
            historical_volatility=0.20,
            dividend_yield=0.0,
            risk_free_rate=0.02,
            model_method="monte_carlo",
            path_count=10,
            random_seed=20240101,
            demo_mode=True,
        ),
        historical_data=historical,
        market_data_refs=(data_ref,),
        trading_calendar_data=calendar,
        trading_calendar_ref=calendar_ref,
        observed_contract_state=state,
    )


def _active_state(*, knocked_in: bool = False, realized_cashflows: tuple[dict, ...] = ()) -> dict:
    events: list[dict] = [{
        "event_type": "observation_checkpoint",
        "event_date": "2023-07-27",
        "observation_stage": 120,
    }]
    if knocked_in:
        events.append({
            "event_type": "knock_in",
            "event_date": "2023-06-16",
            "observation_stage": 92,
        })
    return {
        "valuation_date": VALUATION_DATE,
        "lifecycle_status": "active",
        "occurred_events": events,
        "realized_cashflows": list(realized_cashflows),
        "source_refs": ["module-run:test-observed-state"],
    }


class FormalToolAndResultStoreTest(unittest.TestCase):
    def _formal_fixture(self, temporary: str) -> tuple[ProtocolPricingInput, LocalDataStore, LocalResultStore]:
        payload = market_csv_payload(ASSET)
        rows = list(csv.DictReader(payload.decode("utf-8").splitlines()))
        sessions = tuple(row["date"] for row in rows)
        data_store = LocalDataStore(Path(temporary) / "data")
        data_ref = data_store.put_bytes(
            tenant_id="local",
            data_asset_id="pricer-formal-market",
            payload=payload,
            media_type="text/csv",
            schema_id="market-history-v1",
            asset_ids=(ASSET,),
            normalized_fields=("date", "asset_id", "close", "adj_close"),
            coverage={
                "start": sessions[0],
                "end": sessions[-1],
                "sessions": sessions,
                "calendar_id": "CN-SSE",
                "calendar_version": "formal-protocol-fixture-v1",
                "by_asset": {ASSET: {"start": sessions[0], "end": sessions[-1]}},
            },
            row_count=len(rows),
            price_convention={
                "adjustment": "close_and_adj_close",
                "close": "unadjusted",
                "adj_close": "adjusted",
            },
            lineage={"fixture": "formal-protocol-state-store"},
            created_by="host-test",
        )
        pricing_input = ProtocolPricingInput(
            contract=_vanilla_contract(),
            pricing_config={
                "valuation_date": VALUATION_DATE,
                "spot": 6_043.2438,
                "historical_volatility": 0.20,
                "dividend_yield": 0.0,
                "risk_free_rate": 0.02,
                "model_method": "black_scholes",
            },
            market_data_refs=(data_ref,),
        )
        return pricing_input, data_store, LocalResultStore(Path(temporary) / "result")

    def _run_formal(self, temporary: str, pricing_config: dict | None = None) -> tuple[dict, LocalResultStore]:
        pricing_input, data_store, result_store = self._formal_fixture(temporary)
        if pricing_config is not None:
            pricing_input = replace(pricing_input, pricing_config=pricing_config)
        contract = pricing_input.contract
        caller, context = modulehost_v2_scope(contract)
        request = {
            "contract": contract,
            "pricing_config": pricing_input.pricing_config,
            "market_data_refs": pricing_input.market_data_refs,
        }
        with (
            patch.object(pricer_service, "LocalDataStore", return_value=data_store),
            patch.object(pricer_service, "LocalResultStore", return_value=result_store, create=True),
            patch.object(pricer_service, "RESULT_ROOT", Path(temporary) / "result"),
            patch.object(
                pricer_service,
                "resolve_contract",
                side_effect=AssertionError("正式Pricer不得按product_id重新解析冻结合同"),
            ),
        ):
            output = tool_entry.call_tool(
                "pricer",
                request,
                caller_context=caller,
                host_context=context,
                result_store=result_store,
                data_store=data_store,
            )
        self.assertIsInstance(output, dict)
        return output, result_store

    def test_formal_tool_consumes_shared_pricing_input_without_product_id_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self._run_formal(temporary)

        self.assertEqual(output["module"], "pricer")
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(
            output["input_snapshot"]["contract"]["contract_fingerprint"],
            _vanilla_contract().contract_fingerprint,
        )
        self.assertEqual(output["data_refs"][0]["data_asset_id"], "pricer-formal-market")
        self.assertIn("by_asset", output["data_refs"][0]["coverage"])
        self.assertEqual(output["data_refs"][0]["price_convention"]["adjustment"], "close_and_adj_close")
        self.assertIsInstance(output["limitations"], list)

    def test_data_backed_mc10_never_claims_explicit_demo_snapshot(self) -> None:
        config = {
            "valuation_date": VALUATION_DATE,
            "spot": 6_043.2438,
            "historical_volatility": 0.20,
            "volatility_override": 0.20,
            "time_to_maturity": 0.5,
            "dividend_yield": 0.0,
            "risk_free_rate": 0.02,
            "model_method": "monte_carlo",
            "path_count": 10,
            "demo_mode": True,
            "demo_calendar": {
                "calendar_id": "demo-european-vanilla",
                "calendar_version": "v1",
                "sessions": [VALUATION_DATE],
                "discrete_path": False,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            output, _ = self._run_formal(temporary, config)

        self.assertEqual(output["pricing"]["precision_status"], "demo_only")
        self.assertEqual(output["data_refs"][0]["data_asset_id"], "pricer-formal-market")
        self.assertNotEqual(output["market_snapshot"]["source"], "explicit_demo_market_snapshot")
        self.assertNotEqual(output["market_snapshot"].get("data_lineage", {}).get("mode"), "explicit_demo_only")

    def test_legacy_product_id_shape_is_not_a_formal_tool_entry(self) -> None:
        caller, context = modulehost_v2_scope(_vanilla_contract())
        with tempfile.TemporaryDirectory() as temporary, patch.object(pricer_service.PricerRuntime, "run") as page_run:
            with self.assertRaisesRegex(
                (TypeError, pricer_service.PricerWebInputError),
                "PricingInput|正式.*协议|contract.*pricing_config.*market_data_refs",
            ):
                tool_entry.call_tool(
                    "pricer",
                    {
                        "action": "run",
                        "product_id": "2.1",
                        "identity": {"underlyings": [ASSET]},
                        "pricing_config": {"model_method": "black_scholes"},
                        "market_data_refs": [],
                    },
                    caller_context=caller,
                    host_context=context,
                    data_store=LocalDataStore(Path(temporary) / "data"),
                )
            page_run.assert_not_called()

    def test_formal_run_is_atomically_committed_and_legacy_json_csv_are_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, result_store = self._run_formal(temporary)
            reference = ModuleRunRef(**output["module_run_ref"])
            run_dir = result_store.resolve_module_run(reference, tenant_id="local")

            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            artifact_manifest = json.loads(
                (run_dir / "artifacts" / "artifact_manifest.json").read_text(encoding="utf-8")
            )
            commit_marker = json.loads((run_dir / "commit_marker.json").read_text(encoding="utf-8"))
            input_snapshot = json.loads((run_dir / "input_snapshot.json").read_text(encoding="utf-8"))
            data_refs = json.loads((run_dir / "data_refs.json").read_text(encoding="utf-8"))
            limitations = json.loads((run_dir / "limitations.json").read_text(encoding="utf-8"))
            artifact_files = tuple(path for path in (run_dir / "artifacts").rglob("*") if path.is_file())

        for field in ("status", "data_refs", "input_snapshot", "limitations", "artifact_manifest", "commit_marker"):
            self.assertIn(field, output)
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["result"], "result.json")
        self.assertEqual(manifest["artifact_manifest"], "artifacts/artifact_manifest.json")
        self.assertEqual(manifest["input_snapshot"], "input_snapshot.json")
        self.assertEqual(manifest["data_refs"], "data_refs.json")
        self.assertEqual(manifest["limitations"], "limitations.json")
        self.assertEqual(input_snapshot, json.loads(json.dumps(output["input_snapshot"])))
        self.assertEqual(data_refs, json.loads(json.dumps({"data_refs": output["data_refs"]})))
        self.assertEqual(limitations, json.loads(json.dumps({"limitations": output["limitations"]})))
        for name in ("input_snapshot.json", "data_refs.json", "limitations.json"):
            self.assertIn(name, artifact_manifest["file_hashes"])
        self.assertTrue(commit_marker["committed"])
        self.assertEqual(
            artifact_manifest["semantic_result_hash"],
            reference.expected_semantic_result_hash,
        )
        self.assertTrue(any(path.suffix == ".json" and path.name != "artifact_manifest.json" for path in artifact_files))
        self.assertTrue(any(path.suffix == ".csv" for path in artifact_files))


class ObservedContractStateValuationTest(unittest.TestCase):
    def test_already_knocked_in_changes_snowball_value_and_uses_remaining_sessions(self) -> None:
        active = price(_snowball_input(_active_state()))
        knocked_in = price(_snowball_input(_active_state(knocked_in=True)))

        self.assertEqual(active.status, "priced")
        self.assertEqual(knocked_in.status, "priced")
        self.assertNotAlmostEqual(active.pv_amount, knocked_in.pv_amount, places=8)
        self.assertTrue(knocked_in.observed_contract_state["occurred_events"])
        calendar = knocked_in.diagnostics["calendar"]
        self.assertEqual(calendar["session_start"], VALUATION_DATE)
        self.assertLess(float(knocked_in.market_snapshot["time_to_maturity"]), float(_snowball_contract().terms["T"]))

    def test_already_knocked_out_safely_terminates_instead_of_restarting_paths(self) -> None:
        state = _active_state()
        state["lifecycle_status"] = "terminated"
        state["occurred_events"].append({
            "event_type": "knock_out",
            "event_date": "2023-07-27",
            "observation_stage": 120,
        })

        result = price(_snowball_input(state))

        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(result.pv_amount)
        self.assertFalse(result.quote_eligible)
        self.assertRegex(" ".join(result.messages), "已.*敲出|knock.?out|终止")

    def test_paid_realized_cashflow_is_audited_but_not_double_counted_in_remaining_pv(self) -> None:
        knocked_in = _active_state(knocked_in=True)
        paid = _active_state(
            knocked_in=True,
            realized_cashflows=({
                "cashflow_id": "coupon-20230727",
                "payment_date": "2023-07-27",
                "amount": 100_000.0,
                "currency": "CNY",
                "status": "paid",
            },),
        )

        without_paid_cashflow = price(_snowball_input(knocked_in))
        with_paid_cashflow = price(_snowball_input(paid))

        self.assertEqual(without_paid_cashflow.status, "priced")
        self.assertEqual(with_paid_cashflow.status, "priced")
        self.assertAlmostEqual(without_paid_cashflow.pv_amount, with_paid_cashflow.pv_amount, places=8)
        self.assertEqual(
            with_paid_cashflow.observed_contract_state["realized_cashflows"],
            paid["realized_cashflows"],
        )

    def test_accumulator_requires_and_applies_historical_quantity_not_observation_count(self) -> None:
        contract = resolve_contract(
            "8.1",
            identity={
                "underlyings": ("A",),
                "reference_prices": {"A": 100.0},
                "contract_start_date": "2023-01-30",
            },
        )
        historical, data_ref = market_asset(("A",))
        calendar, calendar_ref = calendar_asset(("A",))
        state = _active_state()
        state["occurred_events"][0].update({"accumulated_count": 25})
        missing_quantity = price(InternalPricingInput(
            contract=contract,
            pricing_config=_snowball_input(state).pricing_config,
            historical_data=historical,
            market_data_refs=(data_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
            observed_contract_state=state,
        ))
        self.assertEqual(missing_quantity.status, "unsupported")
        self.assertRegex(" ".join(missing_quantity.messages), "accumulated_quantity|累计数量")

        state["occurred_events"][0]["accumulated_quantity"] = 2_500.0
        valued = price(InternalPricingInput(
            contract=contract,
            pricing_config=_snowball_input(state).pricing_config,
            historical_data=historical,
            market_data_refs=(data_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
            observed_contract_state=state,
        ))
        self.assertEqual(valued.status, "priced")
        applied = valued.diagnostics["valuation_state_applied"]
        self.assertEqual(applied["accumulated_count"], 25)
        self.assertEqual(applied["accumulated_quantity"], 2_500.0)


if __name__ == "__main__":
    unittest.main()
