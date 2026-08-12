"""Regression coverage for the three public Skill execution routes."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
for source in (ROOT, ROOT / "core" / "src"):
    if str(source) not in os.sys.path:
        os.sys.path.insert(0, str(source))

from core import tool_entry
from modules.recommender.models import ModelCapability


class _RecommendationModel:
    def capability(self) -> ModelCapability:
        return ModelCapability(model_id="route-fixture", structured_output=True, tool_calling=False)

    def run_step(self, role: str, payload: dict) -> dict:
        data = payload["input"]
        if role.endswith("Intent"):
            return {
                "confirmed_constraints": dict(data["confirmed_constraints"]),
                "missing_information": [],
                "next_question": None,
                "research_queries": ["看涨期权 上涨 波动率上升"],
            }
        if role.endswith("Research"):
            evidence = [item for item in data["evidence"] if item["product_id"] == "2.1"]
            return {"proposals": [{
                "product_id": "2.1", "product_name": "看涨期权", "underlyings": ["000300.SH"],
                "reason": "上涨和波动率上升的观点与多头看涨结构一致。",
                "suitable_for": ["预期上涨且波动率上升"],
                "not_suitable_for": ["预期横盘或波动率下降"],
                "main_risks": ["到期未上涨可能损失全部期权费"],
                "library_status": "ready", "evidence_ref_ids": [item["evidence_id"] for item in evidence],
                "missing_inputs": [],
            }]}
        if role.endswith("Critic"):
            return {"reviews": [{
                "product_id": "2.1", "hard_reject": False, "rejection_reason": None,
                "additional_not_suitable_for": [], "additional_risks": [], "rank_adjustment": 0,
            }]}
        raise AssertionError(role)


class SkillExecutionRoutesTest(unittest.TestCase):
    def test_recommendation_route_returns_candidates_without_data_compute_or_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-recommend-") as temporary:
            events: list[dict] = []
            result = tool_entry.run_recommendation_request(
                {"prompt": "000300.SH未来上涨且波动变大，期限改为3个月，推荐期权结构", "constraints": {"max_loss": "30%", "principal_fluctuation": True}},
                project_root=Path(temporary), agent_port=_RecommendationModel(),
                progress=lambda event: events.append(dict(event)),
            )
            project_files = list(Path(temporary).rglob("*"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["constraints"]["horizon"], "3个月")
        self.assertEqual(result["recommendation"]["candidates"][0]["product_id"], "2.1")
        self.assertFalse(any(event["stage"] in {"datafetcher", "payoffer", "pricer", "backtester", "reporter"} for event in events))
        self.assertFalse(any(path.suffix in {".html", ".pdf", ".py"} for path in project_files))

    def test_module_route_recompiles_a_new_contract_for_a_term_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-module-") as temporary:
            result = tool_entry.run_module_request({
                "module": "payoffer", "product_id": "2.1",
                "identity": {"underlyings": ["000300.SH"]}, "horizon": "3个月",
            }, project_root=Path(temporary))

        self.assertTrue(result["ok"])
        self.assertEqual(result["module"], "payoffer")
        self.assertEqual(result["result"]["module"], "payoffer")

    def test_native_report_selection_is_verified_without_a_second_model(self) -> None:
        paths = tool_entry.bootstrap_runtime()
        selection = tool_entry._agent_native_candidate({
            "product_id": "2.1",
            "underlyings": ["000300.SH"],
            "reason": "上涨观点与多头看涨结构一致。",
            "main_risks": ["到期未上涨可能损失期权费。"],
        }, paths=paths, task_hash="native-selection")

        self.assertEqual(selection["product_id"], "2.1")
        self.assertEqual(selection["candidate_id"], "host-native-selection")
        self.assertEqual(selection["underlyings"], ["000300.SH"])
        with self.assertRaises(tool_entry.ProjectRequestError):
            tool_entry._agent_native_candidate({
                "product_id": "2.1", "underlyings": ["000300.SH"], "candidate_id": "caller-controlled",
            }, paths=paths, task_hash="native-selection")

    def test_native_report_preflight_requires_data_not_a_second_model_gateway(self) -> None:
        with patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "fixture-refresh"}, clear=True):
            configuration = tool_entry._configuration(require_model=False, require_ifind=True)

        self.assertEqual(configuration["mode"], "direct")
        self.assertEqual(configuration["ifind"], "fixture-refresh")
        self.assertNotIn("base_url", configuration)
        self.assertNotIn("model", configuration)

    def test_native_report_route_never_constructs_a_second_model_gateway(self) -> None:
        request = {
            "prompt": "沪深300未来上涨，生成完整研究报告",
            "selection": {"product_id": "2.1", "underlyings": ["000300.SH"]},
        }
        with tempfile.TemporaryDirectory(prefix="optionhelper-native-report-") as temporary, \
             patch.object(tool_entry, "_configuration", return_value={"mode": "direct", "ifind": "fixture-refresh"}) as configuration, \
             patch.object(tool_entry, "_fetch_local_data_asset", side_effect=tool_entry.ProjectRequestError("datafetcher", "fixture stop")):
            with self.assertRaisesRegex(tool_entry.ProjectRequestError, "fixture stop"):
                tool_entry.run_project_request(
                    request, project_root=Path(temporary), ifind_probe=lambda token: None,
                )

        self.assertEqual(configuration.call_args.kwargs, {"require_model": False, "require_ifind": True})

if __name__ == "__main__":
    unittest.main()
