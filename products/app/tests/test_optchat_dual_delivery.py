"""Independent regression tests for one-request dual report delivery."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT, APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.recommender.interaction import extract_confirmed_constraints
from backend.agent_runtime.agent_loop import AgentLoop
from backend.agent_runtime.recommender_adapter import AppConversationToolExecutor
from backend.agent_runtime import recommender_adapter as adapter_module
from backend.authorization.roles import Role
from backend.identity.session_identity import SessionIdentity
from backend.stores import _LocalDocumentStore
from backend.task_runtime.task_service import TaskService


class _Context:
    def build(self, _identity, _task_id, message):
        return {
            "latest_message": message,
            "messages": [],
            "tool_catalog": [{"name": "reporter.run"}],
        }


class _NoModel:
    def decide_for(self, *_args, **_kwargs):
        raise AssertionError("an explicit dual delivery must not require another model decision")


class _NoRecommendation:
    def run_fixed(self, *_args, **_kwargs):
        raise AssertionError("an existing-result delivery must not restart recommendation")


class _Reports:
    def __init__(self):
        self.calls = []

    def call(self, _identity, _task_id, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "status": "completed"}


class _RecommendationDeliveries(AppConversationToolExecutor):
    def __init__(self):
        self.dispatched = []

    def _dispatch(self, _identity, _task_id, name, module, payload, *, binding=None):
        self.dispatched.append((name, module, dict(payload), dict(binding or {})))
        return {"ok": True, "status": "completed" if module == "reporter" else "succeeded"}

    def _prepare_report(
        self,
        _identity,
        task_id,
        arguments,
        *,
        report_run_id=None,
        expected_candidate_id=None,
    ):
        return {
            "action": "run",
            "task_id": task_id,
            "kind": arguments["kind"],
            "report_run_id": report_run_id,
            "expected_candidate_id": expected_candidate_id,
        }


class OptChatDualDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = SessionIdentity("principal-dual", "tenant-dual", Role.ADMIN, "session-dual")

    def test_natural_language_preserves_both_requested_deliveries(self) -> None:
        constraints = extract_confirmed_constraints([
            "简单报告和详细报告都给我一份，默认格式即可。",
        ])

        self.assertEqual(constraints["output_type"], "both")

    def test_existing_results_render_both_without_another_model_round(self) -> None:
        reports = _Reports()
        loop = AgentLoop(
            gateway=_NoModel(),
            context_builder=_Context(),
            tool_executor=reports,
            recommender=_NoRecommendation(),
        )

        result = loop.run(self.identity, "task-dual", "简单报告和详细报告都给我一份。")

        self.assertEqual(result["status"], "completed", result)
        self.assertIn("研究简报和完整研究报告已", result["text"])
        self.assertEqual(reports.calls, [
            ("reporter.run", {"kind": "card", "format": "html"}),
            ("reporter.run", {"kind": "report", "format": "html", "html_report_layout": "continuous"}),
        ])

    def test_recommendation_analysis_runs_once_for_both_deliveries(self) -> None:
        executor = _RecommendationDeliveries()

        result = executor.run_recommendation_deliveries(
            self.identity,
            "task-dual",
            analysis_case_id="case-dual",
            catalog_version="v1.0",
            candidate={
                "candidate_id": "candidate-dual",
                "product_id": "2.1",
                "product_name": "看涨期权",
                "underlyings": ["000905.SH"],
            },
            deliveries=(
                {"kind": "card", "format": "html", "html_report_layout": None},
                {"kind": "report", "format": "html", "html_report_layout": "continuous"},
            ),
        )

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual([module for _, module, _, _ in executor.dispatched], ["payoffer", "reporter", "reporter"])
        self.assertEqual([item["kind"] for item in result["deliveries"]], ["card", "report"])

    def test_explicit_recommendation_preserves_dual_choice_in_one_turn(self) -> None:
        class Registry:
            capability_root = ROOT / "products" / "app" / "capability" / "option-helper"
            manifest = {"catalog_version": "v1.0"}

        class Service:
            def __init__(self, **_ports):
                pass

            def recommend_fixed(self, case, *, workflow):
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": workflow},
                    "recommendation_set": {
                        "status": "pending_approval",
                        "primary_candidate_id": "candidate-dual",
                        "candidates": [{
                            "candidate_id": "candidate-dual",
                            "product_id": "2.1",
                            "product_name": "看涨期权",
                            "underlyings": ["000905.SH"],
                            "library_status": "ready",
                        }],
                    },
                }

        class Deliveries:
            def __init__(self):
                self.calls = []

            def run_recommendation_deliveries(self, _identity, _task_id, **kwargs):
                self.calls.append(dict(kwargs))
                return {
                    "status": "completed",
                    "deliveries": [
                        {"kind": "card", "format": "html"},
                        {"kind": "report", "format": "html"},
                    ],
                }

        prompt = (
            "我判断000905.SH未来3个月上涨，最大可承受亏损30%，接受本金波动。"
            "请推荐合适结构，简单报告和详细报告都给我一份。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(self.identity, "双报告推荐")
            tasks.append_message(self.identity, task["task_id"], "user", prompt)
            deliveries = Deliveries()
            with patch.object(adapter_module, "RecommenderService", Service):
                adapter = adapter_module.RecommenderAdapter(
                    gateway=object(),
                    registry=Registry(),
                    task_service=tasks,
                    tool_executor=deliveries,
                )
                first = adapter.run_fixed(self.identity, task["task_id"], prompt, {})

        self.assertEqual(first["recommendation_set"]["status"], "completed")
        self.assertEqual(first["delivery"]["status"], "completed")
        self.assertEqual(len(deliveries.calls), 1)
        self.assertEqual(
            deliveries.calls[0]["deliveries"],
            (
                {"kind": "card", "format": "html", "html_report_layout": None},
                {"kind": "report", "format": "html", "html_report_layout": "continuous"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
