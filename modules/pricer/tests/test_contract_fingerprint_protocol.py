"""Pricer公开结果与落盘记录的合同指纹协议回归。"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from runtime.contracts.contract_api import load_registry, resolve_contract
from modules.pricer import PricingConfig, PricingInput, price
from modules.pricer import service as pricer_service

from .pricer_test_fixtures import calendar_asset, market_asset, stored_market_asset


def _resolved_call(*, strike: float | None = None):
    overrides = {} if strike is None else {"K": strike}
    return resolve_contract(
        "2.1",
        identity={
            "underlyings": ["000905.SH"],
            "reference_prices": {"000905.SH": 100.0},
        },
        term_overrides=overrides,
        registry=load_registry(),
    )


class ContractFingerprintProtocolTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_sensitive_to_resolved_terms(self) -> None:
        first = _resolved_call()
        second = _resolved_call()
        changed = _resolved_call(strike=105.0)

        self.assertEqual(first.contract_fingerprint, second.contract_fingerprint)
        self.assertNotEqual(first.contract_fingerprint, changed.contract_fingerprint)
        self.assertEqual(first.to_protocol_dict()["contract_fingerprint"], first.contract_fingerprint)

    def test_public_pricing_result_uses_resolved_contract_fingerprint(self) -> None:
        contract = _resolved_call()
        history, data_ref = market_asset()
        result = price(PricingInput(
            contract=contract,
            pricing_config=PricingConfig(
                valuation_date="2023-07-28",
                model_method="black_scholes",
                risk_free_rate=0.01,
            ),
            historical_data=history,
            market_data_refs=(data_ref,),
        ))

        self.assertEqual(result.contract_fingerprint, contract.contract_fingerprint)
        self.assertEqual(result.to_dict()["contract_fingerprint"], contract.contract_fingerprint)

    def test_unsupported_result_keeps_the_same_contract_fingerprint(self) -> None:
        contract = resolve_contract(
            "5.1",
            identity={
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": 100.0},
            },
            registry=load_registry(),
        )
        history, data_ref = market_asset()
        calendar, calendar_ref = calendar_asset(("000905.SH",))
        result = price(PricingInput(
            contract=contract,
            pricing_config=PricingConfig(
                valuation_date="2023-07-28",
                model_method="black_scholes",
                risk_free_rate=0.01,
            ),
            historical_data=history,
            market_data_refs=(data_ref,),
            trading_calendar_data=calendar,
            trading_calendar_ref=calendar_ref,
        ))

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.contract_fingerprint, contract.contract_fingerprint)

    def test_tool_output_and_saved_run_share_one_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pricer-fingerprint-data-") as data_root:
            store, reference = stored_market_asset(Path(data_root))
            request = {
                "product_id": "2.1",
                "task_id": "fingerprint-task",
                "run_id": "fingerprint-run",
                "identity": {"underlyings": ["000905.SH"]},
                "term_overrides": {"T": 2 / 244, "Pi_0": 1.0},
                "pricing_config": {
                    "valuation_date": "2023-07-28",
                    "model_method": "black_scholes",
                    "risk_free_rate": 0.01,
                },
                "market_data_refs": [asdict(reference)],
            }
            with patch.object(pricer_service, "_write_run"):
                output = pricer_service.PricerRuntime(data_port=store).run(request)

        fingerprint = output["contract_fingerprint"]
        self.assertEqual(output["resolved_contract"]["contract_fingerprint"], fingerprint)
        self.assertEqual(output["pricing"]["contract_fingerprint"], fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            result_root = Path(temporary) / "result"
            with patch.object(pricer_service, "RESULT_ROOT", result_root):
                pricer_service._write_run("fingerprint-task", "fingerprint-run", request, output)
            run_root = result_root / "output_pricing" / "fingerprint-task" / "fingerprint-run"
            saved_contract = json.loads((run_root / "resolved_contract.json").read_text(encoding="utf-8"))
            saved_result = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
            saved_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(saved_contract["contract_fingerprint"], fingerprint)
        self.assertEqual(saved_result["contract_fingerprint"], fingerprint)
        self.assertEqual(saved_result["pricing"]["contract_fingerprint"], fingerprint)
        self.assertEqual(saved_manifest["contract_fingerprint"], fingerprint)

    def test_saved_run_rejects_inconsistent_fingerprints_before_writing(self) -> None:
        fingerprint = "a" * 64
        output = {
            "contract_fingerprint": fingerprint,
            "resolved_contract": {"contract_fingerprint": fingerprint},
            "pricing": {"contract_fingerprint": "b" * 64},
        }
        with tempfile.TemporaryDirectory() as temporary:
            result_root = Path(temporary) / "result"
            with patch.object(pricer_service, "RESULT_ROOT", result_root):
                with self.assertRaisesRegex(ValueError, "pricing.contract_fingerprint"):
                    pricer_service._write_run("task", "run", {}, output)
            self.assertFalse(result_root.exists())


if __name__ == "__main__":
    unittest.main()
