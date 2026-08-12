"""OptChat's Card and Report buttons use the same controlled delivery path as chat."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlparse
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import build_skill as skill_builder

_RUNTIME_PROBE = skill_builder.probe_runtime

def _no_port_runtime_probe(root: Path) -> list[str]:
    return _RUNTIME_PROBE(root, page_probe=lambda _root: [])


class OptChatReportButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capability_temporary = tempfile.TemporaryDirectory(prefix="optionhelper-report-button-")
        with patch.object(skill_builder, "probe_runtime", _no_port_runtime_probe):
            cls.capability_root = skill_builder.build_skill(
                Path(cls.capability_temporary.name) / "skill", candidate=True, repo_root=ROOT,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.capability_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "OPTIONHELPER_RUNTIME_ROOT": str(root / "runtime"),
            "OPTIONHELPER_DATA_ROOT": str(root / "data"),
            "OPTIONHELPER_RESULT_ROOT": str(root / "result"),
        })
        self.environment.start()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=self.capability_root)
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: dict | None = None, cookie: str | None = None) -> tuple[int, dict, str | None]:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def request_bytes(self, path: str, cookie: str) -> tuple[int, bytes]:
        self.connection.request("GET", path, headers={"Cookie": cookie})
        response = self.connection.getresponse()
        return response.status, response.read()

    def host_headers(self, module: str, task_id: str, cookie: str, sequence: int) -> dict[str, str]:
        status, context, _ = self.request(
            "GET", f"/api/module-host/{module}?{urlencode({'task_id': task_id})}", cookie=cookie,
        )
        self.assertEqual(status, 200, context)
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(context["context"]),
            "X-OptionHelper-Request-Id": f"optchat-report-{module}-{sequence}",
        }

    def hosted_request(self, module: str, task_id: str, body: dict, cookie: str, sequence: int) -> tuple[int, dict]:
        headers = {**self.host_headers(module, task_id, cookie, sequence), "Content-Type": "application/json", "Cookie": cookie}
        self.connection.request("POST", f"/api/tools/{module}", body=json.dumps(body), headers=headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_card_button_generates_a_real_card_from_current_task_evidence_without_browser_selection(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "card-button"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "Card按钮")
        self.app.results.commit_module_run(identity, task["task_id"], "pricer", {
            "run_id": "card-button-price", "ok": True, "status": "completed", "analysis_case_id": "case-card-button",
            "resolved_contract": {"contract_fingerprint": "b" * 64, "product_version": "v1", "identity": {"product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY"}},
            "pricing": {"pv": 12.5, "currency": "CNY", "greeks": {"delta": 0.1}},
        })

        status, body, _ = self.request("POST", f"/api/tasks/{task['task_id']}/reports", {"kind": "card"}, cookie)

        self.assertEqual(status, 200, body)
        result = body["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"]["report"], "report.html")
        self.assertIn("/api/reports/", result["preview_url"])

    def test_optchat_report_request_preserves_html_layout_on_current_verified_evidence(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "report-layout"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "Report参数透传")
        self.app.results.commit_module_run(identity, task["task_id"], "pricer", {
            "run_id": "report-layout-price", "ok": True, "status": "completed", "analysis_case_id": "case-report-layout",
            "resolved_contract": {"contract_fingerprint": "c" * 64, "product_version": "v1.0", "identity": {"product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY"}},
            "pricing": {"pv": 12.5, "currency": "CNY", "greeks": {"delta": 0.1}},
        })

        status, body, _ = self.request(
            "POST", f"/api/tasks/{task['task_id']}/reports",
            {"kind": "report", "format": "html", "html_report_layout": "continuous"}, cookie,
        )

        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["status"], "completed", body)
        record = self.app.results.list_report_runs(identity, task["task_id"])[0]
        request = record["report_request"]
        self.assertEqual(request["output_type"], "report")
        self.assertEqual(request["format"], "html")
        self.assertEqual(request["html_report_layout"], "continuous")

    def test_optchat_rejects_a_report_layout_for_card_before_dispatch(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "card-layout"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "Card版式边界")

        status, body, _ = self.request(
            "POST", f"/api/tasks/{task['task_id']}/reports",
            {"kind": "card", "format": "html", "html_report_layout": "with_toc"}, cookie,
        )

        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "invalid_request")
        self.assertNotIn("html_report_layout", str(body))

    def test_optchat_rejects_a_non_string_report_layout_without_internal_error(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "invalid-layout"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "Report版式输入边界")

        status, body, _ = self.request(
            "POST", f"/api/tasks/{task['task_id']}/reports",
            {"kind": "report", "format": "html", "html_report_layout": ["continuous"]}, cookie,
        )

        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "invalid_request")
        self.assertNotIn("TypeError", str(body))

    def test_card_button_without_evidence_returns_a_public_next_step(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "card-button-empty"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "空Card按钮")

        status, body, _ = self.request("POST", f"/api/tasks/{task['task_id']}/reports", {"kind": "card"}, cookie)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["status"], "needs_input")
        self.assertNotIn("RunRef", body["result"]["message"])
        self.assertNotIn("source_id", body["result"]["message"])

    def test_payoffer_module_run_is_listed_for_its_current_task_then_generates_card_and_report(self) -> None:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "payoffer-reporter"})
        self.assertEqual(status, 200)
        assert cookie is not None
        cookie = cookie.split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "2.1看涨期权报告闭环")
        task_id = task["task_id"]

        status, payoffer = self.hosted_request("payoffer", task_id, {
            "action": "run",
            "task_id": task_id,
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": 100.0},
                "contract_start_date": "2026-08-10",
            },
            "term_overrides": {"K": 100.0, "T": 1.0, "Pi_0": 0.0},
        }, cookie, 1)
        self.assertEqual(status, 200, payoffer)
        payoff_result = payoffer["result"]
        self.assertIn(payoff_result["status"], {"succeeded", "partial"}, payoff_result)
        self.assertEqual(payoff_result["module_run_ref"]["module"], "payoffer")
        run_directory = self.app.results.resolve_owned_module_run(identity, payoff_result["module_run_ref"])
        committed_manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
        committed_result = json.loads((run_directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(committed_manifest["semantic_result_hash"], payoff_result["module_run_ref"]["expected_semantic_result_hash"])
        self.assertNotIn("semantic_result_hash", committed_result)

        status, listed = self.hosted_request("reporter", task_id, {"action": "list_report_sources", "task_id": task_id}, cookie, 2)
        self.assertEqual(status, 200, listed)
        sources = listed["result"]["sources"]
        self.assertEqual(len(sources), 1, listed)
        source = sources[0]
        self.assertEqual(source["task_id"], task_id)
        candidate = source["candidates"][0]
        self.assertEqual(candidate["product_id"], "2.1")
        self.assertEqual(candidate["underlyings"], ["000905.SH"])
        self.assertEqual(candidate["module_run_refs"]["payoff"]["run_id"], payoff_result["module_run_ref"]["run_id"])

        def render(kind: str, sequence: int) -> str:
            selection = {
                "source_id": source["source_id"],
                "candidate_ids": [candidate["candidate_id"]],
                "selected_modules": ["payoff"],
                "delivery_mode": "single",
                "output_type": kind,
                "format": "html",
                "html_report_layout": "continuous" if kind == "report" else None,
                "audience": identity.audience,
                "report_run_id": f"payoffer-{kind}-{sequence}",
                "metadata": {"title": "看涨期权"},
            }
            response_status, reply = self.hosted_request("reporter", task_id, {"action": "run", "selection": selection}, cookie, 10 + sequence)
            self.assertEqual(response_status, 200, reply)
            delivery = reply["result"]
            self.assertEqual(delivery["status"], "completed", delivery)
            report_id = delivery["report_run_ref"]["report_run_id"]
            report_name = delivery["output"]["report"]
            artifact_status, content = self.request_bytes(f"/api/reports/{report_id}/artifacts/{report_name}", cookie)
            self.assertEqual(artifact_status, 200)
            return content.decode("utf-8")

        card_html = render("card", 1)
        card_record = self.app.results.list_report_runs(identity, task_id)[-1]
        card_unit = card_record["report_request"]["reporter_audit"]["root"]["report_unit"]
        self.assertEqual(card_unit["content"]["payoff"]["status"], "ready", card_unit["content"]["payoff"])
        self.assertNotIn("<svg", card_html.lower())
        self.assertNotIn("echarts", card_html.lower())
        self.assertIn("推荐结构", card_html)
        self.assertIn("估值定价", card_html)
        self.assertIn("历史回测", card_html)
        self.assertIn("本次尚未形成可引用的估值结果。", card_html)
        self.assertIn("本次尚未形成可引用的回测结果。", card_html)

        report_html = render("report", 2)
        self.assertIn("核心结论", report_html)
        self.assertIn("研究逻辑", report_html)
        self.assertIn("收益结构", report_html)
        self.assertIn("风险提示", report_html)

if __name__ == "__main__":
    unittest.main()
