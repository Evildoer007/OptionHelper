"""HTTP regression tests for immutable Reporter delivery boundaries."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from capability_fixture import capability_root


class AppReportDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=capability_root())
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, *, body: dict[str, object] | None = None, cookie: str | None = None, extra_headers: dict[str, str] | None = None) -> tuple[int, bytes, str | None, str | None]:
        headers: dict[str, str] = dict(extra_headers or {})
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = self.connection.getresponse()
        return response.status, response.read(), response.getheader("Set-Cookie"), response.getheader("Content-Type")

    def login(self) -> tuple[str, object]:
        status, raw, cookie, _ = self.request("POST", "/api/auth/local", body={"role": "admin", "principal_label": "reporter-test"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(cookie)
        identity = self.app.identity_from_cookie(str(cookie).split(";", 1)[0])
        return str(cookie).split(";", 1)[0], identity

    def reporter_context_headers(self, cookie: str) -> dict[str, str]:
        status, raw, _, _ = self.request("GET", "/api/module-host/reporter", cookie=cookie)
        self.assertEqual(status, 200, raw)
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(json.loads(raw)["context"]),
            "X-OptionHelper-Request-Id": "report-delivery-request",
        }

    def test_selection_request_calls_formal_reporter_and_persists_auditable_html_report_run(self) -> None:
        cookie, identity = self.login()
        task = self.app.tasks.create(identity, "受控报告")
        result = {
            "run_id": "pricing-report-run", "ok": True, "status": "completed", "analysis_case_id": "case-report",
            "resolved_contract": {"contract_fingerprint": "a" * 64, "product_version": "v1", "identity": {"product_id": "2.1", "name_zh": "雪球", "underlyings": ["000905.SH"], "currency": "CNY"}},
            "pricing": {"pv": 12.5, "currency": "CNY", "greeks": {"delta": 0.1}},
        }
        self.app.results.commit_module_run(identity, task["task_id"], "pricer", result)
        source = self.app.results.list_report_sources(tenant_id=identity.tenant_id, task_id=task["task_id"])["sources"][0]
        candidate = source["candidates"][0]
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": ["pricing"],
            "delivery_mode": "single",
            "output_type": "report",
            "format": "html",
            "html_report_layout": "continuous",
            "audience": "professional",
            "report_run_id": "report-selection-html",
            "metadata": {"title": "受控报告"},
        }
        status, raw, _, _ = self.request(
            "POST", f"/api/tasks/{task['task_id']}/reports", cookie=cookie, body={"selection": selection},
            extra_headers=self.reporter_context_headers(cookie),
        )
        self.assertEqual(status, 200, raw)
        response = json.loads(raw)
        self.assertEqual(response["result"]["status"], "completed")
        report_ref = response["result"]["report_run_ref"]
        self.assertIn("report_run_id", report_ref)
        self.assertIn("/api/reports/", response["result"]["preview_url"])
        self.assertEqual(response["result"]["output"]["report"], "report.html")
        stored = self.app.results.resolve_report_run(identity, report_ref)
        audit = stored["report_request"].get("reporter_audit", {})
        self.assertIn("report_unit", audit["root"])
        self.assertIn("design_brief", audit["root"])
        self.assertIn("designer_artifact_manifest", audit["root"])
        self.assertTrue(any(item["name"] == "report.html" for item in stored["artifact_manifest"]))
        status, raw, _, _ = self.request("GET", f"/api/tasks/{task['task_id']}/reports", cookie=cookie)
        self.assertEqual(status, 200)
        listed = json.loads(raw)["reports"]
        self.assertEqual(len(listed), 1)

    def test_report_request_without_selected_source_is_unavailable_not_success(self) -> None:
        cookie, identity = self.login()
        task = self.app.tasks.create(identity, "无来源报告")
        status, raw, _, _ = self.request("POST", f"/api/tasks/{task['task_id']}/reports", cookie=cookie, body={"selection": {}})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
