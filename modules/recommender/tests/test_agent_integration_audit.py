"""Recommender与App Agent衔接的权限、降级及固定流程回归。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = PROJECT_ROOT / "core" / "src"
for source in (PROJECT_ROOT, CORE_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import modules.recommender as recommender_package
from modules.recommender.evidence_retriever import retrieve_evidence
from modules.recommender.intent_router import route_intent
from modules.recommender.models import ModelCapability, RecommendationCase, RecommendationValidationError
from modules.recommender.service import RecommenderService, call_tool


def case(**overrides: object) -> dict:
    value = {
        "analysis_case_id": "case_1",
        "task_id": "task_1",
        "tenant_id": "tenant_1",
        "prompt": "沪深300看涨，请推荐结构",
        "catalog_version": "v1.0",
        "run_id": "run_1",
    }
    value.update(overrides)
    return value


_COMPLETE_CONSTRAINTS = {
    "underlying": "000300.SH", "horizon": "3个月", "market_view": "上涨+波动率上升",
    "max_loss": "30%", "principal_fluctuation": True,
}


class ScriptedAgent:
    def __init__(self, steps: list[dict] | None = None, *, error: Exception | None = None) -> None:
        self.steps = list(steps or [])
        self.error = error

    def capability(self) -> ModelCapability:
        if self.error:
            raise self.error
        return ModelCapability("model", structured_output=True, tool_calling=True)

    def run_step(self, _role: str, _payload: dict) -> dict:
        if self.error:
            raise self.error
        return self.steps.pop(0)


class RecordingTool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, module: str, payload: dict) -> dict:
        self.calls.append((module, dict(payload)))
        return {
            "ok": True,
            "status": "succeeded",
            "run_id": f"tool_{len(self.calls)}",
            "candidate_id": "run_1_candidate_01",
            "catalog_version": "v1.0",
            "contract_fingerprint": "a" * 64,
        }


class AgentIntegrationAuditTest(unittest.TestCase):
    def test_chat_cannot_call_financial_tools(self) -> None:
        agent = ScriptedAgent([
            {"response": "", "tool_requests": [{"module": "payoffer", "request": {"action": "run"}}]},
            {"response": "你好。", "tool_requests": []},
        ])
        tool = RecordingTool()
        result = RecommenderService(agent_port=agent, tool_port=tool).recommend(case(prompt="你好"))["freeform"]
        self.assertEqual(tool.calls, [])
        self.assertEqual(result["status"], "partial")

    def test_duplicate_freeform_tool_request_runs_once(self) -> None:
        request = {"action": "status"}
        agent = ScriptedAgent([
            {"response": "", "tool_requests": [{"module": "payoffer", "request": request}]},
            {"response": "", "tool_requests": [{"module": "payoffer", "request": request}]},
            {"response": "已获取。", "tool_requests": []},
        ])
        tool = RecordingTool()
        result = RecommenderService(agent_port=agent, tool_port=tool).recommend(
            case(prompt="把产品资料和收益图一起整理一下")
        )["freeform"]
        self.assertEqual(len(tool.calls), 1)
        self.assertTrue(result["tool_results"][1]["reused"])

    def test_freeform_model_failure_returns_audited_unavailable(self) -> None:
        result = RecommenderService(agent_port=ScriptedAgent(error=RuntimeError("model offline"))).recommend(
            case(prompt="你好")
        )["freeform"]
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["response"])
        self.assertTrue(any(event["stage"] == "workflow" for event in result["audit_trail"]))

    def test_fixed_entry_is_public_and_has_tool_action(self) -> None:
        self.assertTrue(callable(getattr(recommender_package, "recommend_fixed", None)))

        class FixedService:
            def recommend_fixed(self, _request: dict, *, workflow: str) -> dict:
                return {
                    "route": {"route": workflow, "fixed_recommendation_workflow": True},
                    "recommendation_set": {"status": "unavailable"},
                }

        with patch("modules.recommender.service._service_from_environment", return_value=FixedService()):
            result = call_tool({"action": "recommend_fixed", "workflow": "professional_report", **case()})
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["route"]["route"], "professional_report")

    def test_nonready_evidence_never_reaches_executor_tool(self) -> None:
        excerpts = {source: f"{source} evidence" for source in ("optionlist", "optionlib", "optionreg_status")}
        evidence = []
        for source, excerpt in excerpts.items():
            row = {
                "evidence_id": f"ev_{source}", "product_id": "2.1", "catalog_version": "v1.0",
                "source": source, "section": "2.1", "library_status": "conflict" if source == "optionlib" else "ready",
                "excerpt": excerpt, "excerpt_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
            }
            if source == "optionlist":
                row["identity"] = {"product_id": "2.1"}
            if source == "optionreg_status":
                row["entry_status"] = True
            evidence.append(row)

        class Knowledge:
            def search(self, payload: dict) -> dict:
                return {"catalog_version": payload["catalog_version"], "evidence": evidence}

        agent = ScriptedAgent([
            {"missing_information": [], "next_question": None, "research_queries": ["看涨"]},
            {"proposals": [{
                "product_id": "2.1", "underlyings": ["000300.SH"], "reason": "x",
                "suitable_for": ["x"], "not_suitable_for": ["x"], "main_risks": ["x"],
                "library_status": "ready", "evidence_ref_ids": ["ev_optionlib"], "missing_inputs": [],
            }]},
            {"reviews": [{"product_id": "2.1", "hard_reject": False}]},
            {"tool_requests": [{"candidate_id": "run_1_candidate_01", "module": "payoffer"}]},
        ])
        tool = RecordingTool()
        result = RecommenderService(agent_port=agent, knowledge_port=Knowledge(), tool_port=tool).recommend_fixed(case(
            confirmed_constraints=_COMPLETE_CONSTRAINTS,
            requested_outputs=["payoff"], approved_candidate_ids=["run_1_candidate_01"],
            candidate_contracts={"run_1_candidate_01": {
                "candidate_id": "run_1_candidate_01", "product_id": "2.1", "underlyings": ["000300.SH"],
                "catalog_version": "v1.0", "evidence_ref_ids": [f"ev_{source}" for source in excerpts],
                "contract_fingerprint": "a" * 64, "resolved_contract": {"contract_fingerprint": "a" * 64},
            }},
        ))["recommendation_set"]
        self.assertEqual(tool.calls, [])
        self.assertEqual(result["candidates"][0]["candidate_status"], "pending_terms")
        self.assertEqual(result["status"], "partial")

    def test_knowledge_port_receives_queries_array(self) -> None:
        class Knowledge:
            def __init__(self) -> None:
                self.payload: dict = {}

            def search(self, payload: dict) -> dict:
                self.payload = dict(payload)
                return {"catalog_version": "v1.0", "evidence": []}

        port = Knowledge()
        retrieve_evidence(port, catalog_version="v1.0", queries=["看涨"])
        self.assertEqual(port.payload["queries"], ["看涨"])
        self.assertNotIn("query", port.payload)

    def test_agent_mode_selection_is_audited(self) -> None:
        agent = ScriptedAgent([{
            "missing_information": ["期限"], "next_question": "期限是多少？", "research_queries": [],
        }])
        result = RecommenderService(agent_port=agent).recommend_fixed(case(confirmed_constraints=_COMPLETE_CONSTRAINTS))["recommendation_set"]
        events = [event for event in result["audit_trail"] if event["stage"] == "agent_mode"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["selected"], "single_agent")

    def test_duplicate_approval_ids_are_rejected(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            RecommendationCase.from_mapping(case(approved_candidate_ids=["candidate_1", "candidate_1"]))

    def test_recommendation_and_direct_execution_language(self) -> None:
        report = route_intent("看涨该买什么期权并做报告")
        self.assertEqual(report.route, "professional_report")
        execution = route_intent("帮我算一下看涨期权价格")
        self.assertEqual(execution.route, "direct_execution")
        explanation = route_intent("解释一下看涨期权如何定价")
        self.assertEqual(explanation.route, "knowledge")


if __name__ == "__main__":
    unittest.main()
