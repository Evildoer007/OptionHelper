"""v1.0 OptChat结构推荐的提问与交付闭环回归。"""

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


class OptChatRecommendationClarificationTests(unittest.TestCase):
    def test_v10_knowledger_reads_published_registry_and_real_optionlib_chapter(self) -> None:
        from backend.agent_runtime.recommender_adapter import AppKnowledgePort

        port = AppKnowledgePort(ROOT / "products" / "app" / "capability" / "option-helper", "v1.0")
        result = port.search({"catalog_version": "v1.0", "queries": ["000905.SH上涨 看涨"]})
        evidence = result["evidence"]
        optionlib = next(item for item in evidence if item["product_id"] == "2.1" and item["source"] == "optionlib")

        self.assertEqual(optionlib["library_status"], "ready")
        self.assertIn("### 2.1", optionlib["excerpt"])

    def test_unavailable_recommendation_does_not_reask_already_confirmed_fields(self) -> None:
        from backend.agent_runtime.agent_loop import _recommendation_follow_up

        response = _recommendation_follow_up({
            "recommendation_set": {
                "status": "unavailable",
                "candidates": [],
                "limitations": ["模型服务暂不可用"],
            }
        })

        self.assertIsNotNone(response)
        assert response is not None
        self.assertIn("模型", response)
        self.assertNotIn("最大可承受亏损", response)

    def test_fixed_recommender_replays_confirmed_constraints_from_task_history(self) -> None:
        from backend.agent_runtime import recommender_adapter as adapter_module
        from backend.authorization.roles import Role
        from backend.identity.session_identity import SessionIdentity
        from backend.stores import _LocalDocumentStore
        from backend.task_runtime.task_service import TaskService

        class Registry:
            capability_root = ROOT / "products" / "app" / "capability" / "option-helper"
            manifest = {"catalog_version": "v1.0"}

        captured = {}

        class Service:
            def __init__(self, **_ports: object) -> None:
                pass

            def recommend_fixed(self, case, *, workflow: str):
                captured["constraints"] = dict(case.confirmed_constraints)
                captured["outputs"] = case.requested_outputs
                captured["run_id"] = case.run_id
                return {"route": {"fixed_recommendation_workflow": True, "route": workflow}, "recommendation_set": {"status": "pending_approval"}}

        identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(identity, "连续推荐")
            tasks.append_message(identity, task["task_id"], "user", "标的是000905.SH，未来3个月上涨且波动率上升。")
            tasks.append_message(identity, task["task_id"], "assistant", "请确认最大可承受亏损。", status="needs_input")
            tasks.append_message(identity, task["task_id"], "user", "最多30%，接受本金波动，生成HTML简报。")
            with patch.object(adapter_module, "RecommenderService", Service):
                adapter = adapter_module.RecommenderAdapter(gateway=object(), registry=Registry(), task_service=tasks, tool_executor=object())
                adapter.run_fixed(identity, task["task_id"], "最多30%，接受本金波动，生成HTML简报。", {})

        self.assertEqual(captured["constraints"], {
            "underlying": "000905.SH", "horizon": "3个月", "market_view": "上涨+波动率上升",
            "max_loss": "30%", "principal_fluctuation": True, "output_type": "card", "format": "html",
        })
        self.assertEqual(captured["outputs"], ("card",))
        self.assertTrue(str(captured["run_id"]).startswith("recommend-"))

    def test_two_real_user_turns_keep_constraints_and_honor_explicit_card_scope(self) -> None:
        from backend.agent_runtime import recommender_adapter as adapter_module
        from backend.authorization.roles import Role
        from backend.identity.session_identity import SessionIdentity
        from backend.stores import _LocalDocumentStore
        from backend.task_runtime.task_service import TaskService

        class Registry:
            capability_root = ROOT / "products" / "app" / "capability" / "option-helper"
            manifest = {"catalog_version": "v1.0"}

        class Service:
            def __init__(self, **_ports: object) -> None:
                pass

            def recommend_fixed(self, case, *, workflow: str):
                self.constraints = dict(case.confirmed_constraints)
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": workflow},
                    "recommendation_set": {
                        "status": "pending_approval",
                        "primary_candidate_id": "recommend-card_candidate_01",
                        "candidates": [{
                            "candidate_id": "recommend-card_candidate_01", "product_id": "2.1",
                            "product_name": "看涨期权",
                            "underlyings": ["000905.SH"], "library_status": "ready",
                        }],
                    },
                }

        class Deliveries:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_recommendation_delivery(self, _identity: object, _task_id: str, **kwargs: object) -> dict[str, str]:
                self.calls.append(dict(kwargs))
                return {"status": "completed", "kind": "card", "format": "html"}

        identity = SessionIdentity("principal-two-turn", "tenant-two-turn", Role.ADMIN, "session-two-turn")
        first = "我判断未来3个月000905.SH上涨且波动率上升，最大可承受亏损30%，接受本金波动。"
        second = "我已明确未来3个月、最大可承受亏损30%、接受本金波动，请直接推荐并生成HTML简报。"
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(identity, "两轮推荐Card")
            tasks.append_message(identity, task["task_id"], "user", first)
            tasks.append_message(identity, task["task_id"], "assistant", "请补充投资期限、最大可承受亏损，以及是否接受本金波动。", status="needs_input")
            tasks.append_message(identity, task["task_id"], "user", second)
            deliveries = Deliveries()
            with patch.object(adapter_module, "RecommenderService", Service):
                adapter = adapter_module.RecommenderAdapter(gateway=object(), registry=Registry(), task_service=tasks, tool_executor=deliveries)  # type: ignore[arg-type]
                result = adapter.run_fixed(identity, task["task_id"], second, {})

        self.assertEqual(result["recommendation_set"]["status"], "completed")
        self.assertEqual(result["delivery"], {"status": "completed", "kind": "card", "format": "html"})
        self.assertEqual(len(deliveries.calls), 1)
        candidate = deliveries.calls[0]["candidate"]
        self.assertEqual(candidate["product_id"], "2.1")
        self.assertEqual(candidate["underlyings"], ["000905.SH"])

    def test_explicit_recommend_and_card_request_runs_in_one_turn(self) -> None:
        """A unique ready candidate uses the user's original delivery scope."""

        class StructuredGateway:
            def capability_for(self, _identity: object) -> dict[str, object]:
                return {
                    "model_id": "test-structured", "structured_output": True,
                    "tool_calling": True, "multi_agent": False, "max_parallel_agents": 1,
                }

            def decide_for(self, _identity: object, _task_id: str, context: dict[str, object]) -> dict[str, object]:
                role = str(context["role"])
                step = dict(context["input"])
                payload = dict(step["input"])
                if role.endswith("Intent"):
                    return {"action": "final", "result": {
                        "confirmed_constraints": payload["confirmed_constraints"],
                        "missing_information": [], "next_question": None,
                        "research_queries": ["000905.SH 看涨期权"],
                    }}
                if role.endswith("Research"):
                    evidence = list(payload["evidence"])
                    evidence_ids = [str(item["evidence_id"]) for item in evidence if item["product_id"] == "2.1"]
                    return {"action": "final", "result": {"proposals": [{
                        "product_id": "2.1", "product_name": "看涨期权", "underlyings": ["000905.SH"],
                        "reason": "与上涨且波动率上升观点相符。", "suitable_for": ["看涨观点"],
                        "not_suitable_for": ["不接受本金波动"], "main_risks": ["标的下跌"],
                        "library_status": "ready", "evidence_ref_ids": evidence_ids, "missing_inputs": [],
                    }]}}
                if role.endswith("Critic"):
                    return {"action": "final", "result": {"reviews": [{
                        "product_id": "2.1", "hard_reject": False, "rejection_reason": None,
                        "additional_not_suitable_for": [], "additional_risks": [], "rank_adjustment": 0,
                    }]}}
                raise AssertionError(f"unexpected Recommender role: {role}")

        with tempfile.TemporaryDirectory() as temporary:
            packaging_app = ROOT / "packaging" / "app"
            sys.path.insert(0, str(packaging_app))
            from run_development_app import build_development_capability
            from backend.app_server import AppServer
            from backend.authorization.roles import Role
            from backend.identity.session_identity import SessionIdentity

            identity = SessionIdentity("principal-card", "tenant-card", Role.ADMIN, "session-card")
            capability = build_development_capability(Path(temporary) / "candidate", "v1.0")
            app = AppServer(
                app_data_dir=Path(temporary),
                capability_root=capability,
            )
            app.recommender._gateway = StructuredGateway()  # type: ignore[assignment]
            task = app.tasks.create(identity, "完整推荐Card")
            result = app.recommender.run_fixed(
                identity,
                task["task_id"],
                "我判断未来3个月000905.SH上涨且波动率上升，最大可承受亏损30%，接受本金波动。请推荐一个适合的期权产品，并生成HTML简报。",
                {},
            )
            delivery = result["delivery"]
            self.assertEqual(delivery["status"], "completed", delivery)
            self.assertEqual(delivery["kind"], "card")
            self.assertEqual(delivery["format"], "html")
            reports = app.results.list_report_runs(identity, task_id=task["task_id"])
            self.assertEqual(len(reports), 1)
            report = reports[0]
            artifact = report["artifact_manifest"][0]["name"]
            html, content_type = app.results.read_report_artifact(identity, report["report_run_id"], artifact)
            self.assertEqual(content_type, "text/html; charset=utf-8")
            self.assertIn(b"<html", html.lower())

    def test_conversation_response_does_not_expose_internal_run_references(self) -> None:
        from backend.agent_runtime.conversation_service import ConversationService
        from backend.authorization.roles import Role
        from backend.identity.session_identity import SessionIdentity
        from backend.stores import _LocalDocumentStore
        from backend.task_runtime.task_service import TaskService

        class Loop:
            def run(self, _identity: object, _task_id: str, _message: str) -> dict[str, object]:
                return {
                    "status": "completed", "text": "HTML简报已生成，可在当前任务中预览或导出。",
                    "rounds": 1,
                    "observations": [{
                        "tool": "reporter.run", "status": "completed",
                        "report_run_ref": {"run_id": "internal-report"},
                        "module_run_ref": {"run_id": "internal-module"},
                    }],
                }

        identity = SessionIdentity("principal-public", "tenant-public", Role.ADMIN, "session-public")
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(identity, "公开投影")
            response = ConversationService(tasks, gateway=object(), agent_loop=Loop()).respond(identity, task["task_id"], "生成HTML简报")  # type: ignore[arg-type]
        self.assertNotIn("internal-report", str(response))
        self.assertNotIn("internal-module", str(response))


if __name__ == "__main__":
    unittest.main()
