"""Regression: a confirmed OptChat recommendation must resume one persisted candidate."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT / "core" / "src", APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.app_server import AppServer
from capability_fixture import capability_root


class _StructuredGateway:
    def __init__(self) -> None:
        self.roles: list[str] = []

    def capability_for(self, _identity: object) -> dict[str, object]:
        return {
            "model_id": "test-structured", "structured_output": True,
            "tool_calling": True, "multi_agent": False, "max_parallel_agents": 1,
        }

    def decide_for(self, _identity: object, _task_id: str, context: dict[str, object]) -> dict[str, object]:
        role = str(context["role"])
        self.roles.append(role)
        payload = dict(dict(context["input"])["input"])
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
                "reason": "与已确认的上涨观点相符。", "suitable_for": ["看涨观点"],
                "not_suitable_for": ["不接受本金波动"], "main_risks": ["标的下跌"],
                "library_status": "ready", "evidence_ref_ids": evidence_ids, "missing_inputs": [],
            }]}}
        if role.endswith("Critic"):
            return {"action": "final", "result": {"reviews": [{
                "product_id": "2.1", "hard_reject": False, "rejection_reason": None,
                "additional_not_suitable_for": [], "additional_risks": [], "rank_adjustment": 0,
            }]}}
        raise AssertionError(f"unexpected Recommender role: {role}")


class OptChatPendingApprovalDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="optionhelper-chat-approval-")
        root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
            "OPTIONHELPER_DATA_ROOT": str(root / "data"),
            "OPTIONHELPER_RESULT_ROOT": str(root / "result"),
        })
        self.environment.start()
        self.app = AppServer(app_data_dir=root / "app-state", capability_root=capability_root())
        parsed = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, str | None]:
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if cookie:
            request_headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def request_bytes(self, path: str, cookie: str) -> tuple[int, bytes]:
        self.connection.request("GET", path, headers={"Cookie": cookie})
        response = self.connection.getresponse()
        return response.status, response.read()

    def test_explicit_recommend_and_report_request_completes_in_one_turn(self) -> None:
        """The original request authorizes one unique ready candidate and its report."""
        gateway = _StructuredGateway()
        self.app.recommender._gateway = gateway  # type: ignore[assignment]
        status, _, cookie = self.request("POST", "/api/auth/login", {"account": "", "password": "", "remember": False})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        status, created, _ = self.request("POST", "/api/tasks", {"subject": "OptChat完整报告"}, cookie)
        self.assertEqual(status, 201, created)
        task_id = created["task"]["task_id"]

        first_request = (
            "我判断000905.SH未来3个月上涨且波动率上升，最大可承受亏损30%，"
            "接受本金波动。请推荐合适结构并生成HTML完整报告。"
        )
        status, first, _ = self.request(
            "POST", f"/api/tasks/{task_id}/messages", {"content": first_request}, cookie,
            {"X-Request-Id": "chat-report-resume-1"},
        )
        self.assertEqual(status, 200, first)
        self.assertIn(first["status"], {"completed", "partial"}, first)
        self.assertIn("报告已生成", first["text"])
        self._assert_no_orchestration_identifiers(first)

        identity = self.app.identity_from_cookie(cookie)
        self.assertEqual(len(self.app.results.list_report_runs(identity, task_id)), 1)

        status, task_view, _ = self.request("GET", f"/api/tasks/{task_id}", cookie=cookie)
        self.assertEqual(status, 200, task_view)
        self.assertNotIn("recommendation_state", str(task_view))

        self.assertEqual(len([role for role in gateway.roles if role.endswith("Research")]), 1)

        reports = self.app.results.list_report_runs(identity, task_id)
        self.assertEqual(len(reports), 1, reports)
        request = reports[0]["report_request"]
        self.assertEqual(request["output_type"], "report")
        self.assertEqual(request["format"], "html")
        self.assertEqual(request["html_report_layout"], "continuous")
        artifact_name = reports[0]["artifact_manifest"][0]["name"]
        artifact_status, html = self.request_bytes(
            f"/api/reports/{reports[0]['report_run_id']}/artifacts/{artifact_name}", cookie,
        )
        self.assertEqual(artifact_status, 200)
        self.assertIn(b"<html", html.lower())
        status, task_view, _ = self.request("GET", f"/api/tasks/{task_id}", cookie=cookie)
        self.assertEqual(status, 200, task_view)
        self.assertNotIn("recommendation_state", json.dumps(task_view, ensure_ascii=False))

    def test_explicit_recommend_and_card_request_completes_in_one_turn(self) -> None:
        """The original request authorizes one unique ready candidate and its Card."""
        gateway = _StructuredGateway()
        self.app.recommender._gateway = gateway  # type: ignore[assignment]
        status, _, cookie = self.request("POST", "/api/auth/login", {"account": "", "password": "", "remember": False})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        status, created, _ = self.request("POST", "/api/tasks", {"subject": "OptChat简报"}, cookie)
        self.assertEqual(status, 201, created)
        task_id = created["task"]["task_id"]

        request = (
            "我判断000905.SH未来3个月上涨且波动率上升，最大可承受亏损30%，"
            "接受本金波动。请推荐合适结构并生成HTML简报。"
        )
        status, pending, _ = self.request(
            "POST", f"/api/tasks/{task_id}/messages", {"content": request}, cookie,
            {"X-Request-Id": "chat-card-resume-1"},
        )
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["status"], "completed", pending)
        self._assert_no_orchestration_identifiers(pending)
        identity = self.app.identity_from_cookie(cookie)
        reports = self.app.results.list_report_runs(identity, task_id)
        self.assertEqual(len(reports), 1, reports)
        report_request = reports[0]["report_request"]
        self.assertEqual(report_request["output_type"], "card")
        self.assertEqual(report_request["format"], "html")
        self.assertIsNone(report_request["html_report_layout"])
        self.assertEqual(len([role for role in gateway.roles if role.endswith("Research")]), 1)

    def test_confirmed_candidate_requires_a_delivery_choice_before_reporter_runs(self) -> None:
        """A structure choice and a delivery choice are separate user decisions."""
        gateway = _StructuredGateway()
        self.app.recommender._gateway = gateway  # type: ignore[assignment]
        status, _, cookie = self.request("POST", "/api/auth/login", {"account": "", "password": "", "remember": False})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        status, created, _ = self.request("POST", "/api/tasks", {"subject": "OptChat先推荐后交付"}, cookie)
        self.assertEqual(status, 201, created)
        task_id = created["task"]["task_id"]
        request = (
            "我判断000905.SH未来3个月上涨且波动率上升，最大可承受亏损30%，"
            "接受本金波动。请推荐合适结构。"
        )
        status, first, _ = self.request(
            "POST", f"/api/tasks/{task_id}/messages", {"content": request}, cookie,
            {"X-Request-Id": "chat-delivery-choice-1"},
        )
        self.assertEqual(status, 200, first)
        self.assertEqual(first["status"], "pending_approval", first)
        identity = self.app.identity_from_cookie(cookie)
        self.assertEqual(self.app.results.list_report_runs(identity, task_id), [])

        status, choice, _ = self.request(
            "POST", f"/api/tasks/{task_id}/messages", {"content": "确认这个候选。"}, cookie,
            {"X-Request-Id": "chat-delivery-choice-2"},
        )
        self.assertEqual(status, 200, choice)
        self.assertEqual(choice["status"], "needs_input", choice)
        self.assertIn("研究简报", choice["text"])
        self.assertIn("完整研究报告", choice["text"])
        self.assertNotIn("Card", choice["text"])
        self.assertNotIn("Report", choice["text"])
        self.assertEqual(self.app.results.list_report_runs(identity, task_id), [])

        status, completed, _ = self.request(
            "POST", f"/api/tasks/{task_id}/messages", {"content": "详细报告。"}, cookie,
            {"X-Request-Id": "chat-delivery-choice-3"},
        )
        self.assertEqual(status, 200, completed)
        self.assertIn(completed["status"], {"completed", "partial"}, completed)
        self.assertIn("研究报告已生成", completed["text"])
        reports = self.app.results.list_report_runs(identity, task_id)
        self.assertEqual(len(reports), 1, reports)
        report_request = reports[0]["report_request"]
        self.assertEqual(report_request["output_type"], "report")
        self.assertEqual(report_request["format"], "html")
        self.assertEqual(report_request["html_report_layout"], "continuous")
        self.assertEqual(len([role for role in gateway.roles if role.endswith("Research")]), 1)

    def _assert_no_orchestration_identifiers(self, response: dict) -> None:
        rendered = json.dumps(response, ensure_ascii=False)
        for forbidden in ("candidate_id", "source_id", "run_ref", "report_run_ref", "module_run_ref"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
