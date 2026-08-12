"""HTTP proof that the App persists real Card and Report PDFs."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from backend.app_server import AppServer
from capability_fixture import capability_root


class AppPdfDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=capability_root())
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=10)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, *, body: dict[str, object] | None = None, cookie: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, str | None]:
        all_headers = dict(headers or {})
        if body is not None:
            all_headers["Content-Type"] = "application/json"
        if cookie:
            all_headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=all_headers)
        response = self.connection.getresponse()
        return response.status, response.read(), response.getheader("Set-Cookie")

    def test_card_and_report_pdf_are_persisted_by_the_real_app_adapter(self) -> None:
        status, _, set_cookie = self.request("POST", "/api/auth/local", body={"role": "admin", "principal_label": "pdf-test"})
        self.assertEqual(status, 200)
        cookie = str(set_cookie).split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        task = self.app.tasks.create(identity, "PDF交付")
        self.app.results.commit_module_run(identity, task["task_id"], "pricer", {
            "run_id": "pricing-pdf", "ok": True, "status": "completed", "analysis_case_id": "case-pdf",
            "resolved_contract": {"contract_fingerprint": "b" * 64, "product_version": "v1", "identity": {"product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY"}},
            "pricing": {"pv": 12.5, "currency": "CNY", "greeks": {"delta": 0.1}},
        })
        status, raw, _ = self.request("GET", "/api/module-host/reporter", cookie=cookie)
        self.assertEqual(status, 200, raw)
        headers = {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(json.loads(raw)["context"]),
            "X-OptionHelper-Request-Id": "pdf-delivery-request",
        }
        source = self.app.results.list_report_sources(tenant_id=identity.tenant_id, task_id=task["task_id"])["sources"][0]
        candidate_id = source["candidates"][0]["candidate_id"]
        for output_type in ("card", "report"):
            with self.subTest(output_type=output_type):
                selection = {
                    "source_id": source["source_id"], "candidate_ids": [candidate_id], "selected_modules": ["pricing"],
                    "delivery_mode": "single", "output_type": output_type, "format": "pdf",
                    "audience": "professional", "report_run_id": f"{output_type}-pdf", "metadata": {"title": "PDF交付"},
                }
                status, raw, _ = self.request("POST", f"/api/tasks/{task['task_id']}/reports", cookie=cookie, body={"selection": selection}, headers=headers)
                self.assertEqual(status, 200, raw)
                result = json.loads(raw)["result"]
                stored = self.app.results.resolve_report_run(identity, result["report_run_ref"])
                artifact = next(item for item in stored["artifact_manifest"] if item["name"] == "report.pdf")
                self.assertEqual(artifact["content_type"], "application/pdf")
                report_bytes, _content_type = self.app.results.read_report_artifact(identity, result["report_run_ref"]["report_run_id"], "report.pdf")
                self.assertTrue(report_bytes.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
