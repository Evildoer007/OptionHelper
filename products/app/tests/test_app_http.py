"""Real HTTP smoke tests for the local OptionHelper App platform."""

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
from backend.capability_service import capability_import_scope
from capability_fixture import capability_root


class AppHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=capability_root())
        self.url = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: dict[str, object] | None = None, cookie: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object], str | None]:
        headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie is not None:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=headers)
        response = self.connection.getresponse()
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw), response.getheader("Set-Cookie")

    def module_headers(self, module: str, cookie: str) -> dict[str, str]:
        status, body, _ = self.request("GET", f"/api/module-host/{module}", cookie=cookie)
        self.assertEqual(status, 200)
        return {"Origin": self.app.url, "X-OptionHelper-Module-Context": json.dumps(body["context"]), "X-OptionHelper-Request-Id": f"test-{module}"}

    def local_login(self, role: str) -> str:
        status, response, cookie = self.request("POST", "/api/auth/local", {"role": role, "principal_label": f"{role}-tester"})
        self.assertEqual(status, 200)
        self.assertEqual(response["identity"]["role"], role)
        self.assertIsNotNone(cookie)
        return str(cookie).split(";", 1)[0]

    def test_health_exposes_verified_capability_without_copying_pages(self) -> None:
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        pages = body["capability"]["pages"]
        self.assertEqual({page["module"] for page in pages}, {"datafetcher", "payoffer", "pricer", "backtester", "reporter"})
        self.assertTrue(all(page["path"].startswith("assets/pages/") for page in pages))
        integrity = body["capability"]["integrity"]
        self.assertEqual(integrity["page_hashes"], "verified")
        self.assertEqual(integrity["declared_file_hashes"], "verified")
        self.assertIn(integrity["content_tree_hash"], {"verified", "mismatch"})
        self.assertEqual(
            integrity["release_ready"],
            integrity["content_tree_hash"] == "verified" and not integrity["untracked_files"],
        )

    def test_sales_cannot_enter_desk_or_module_page(self) -> None:
        cookie = self.local_login("sales")
        status, body, _ = self.request("GET", "/optdesk", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        status, body, _ = self.request("GET", "/capability/assets/pages/pricer/pricer.html", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "module.page")
        status, body, _ = self.request("POST", "/api/tools/pricer", {}, cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "module.run")

    def test_admin_mounts_original_capability_page_with_a_session_bound_bridge(self) -> None:
        cookie = self.local_login("admin")
        status, body, _ = self.request("GET", "/api/module-host/pricer", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["context"]["module"], "pricer")
        self.assertNotIn("optionhelper_session", json.dumps(body))
        self.connection.request("GET", "/capability/assets/pages/pricer/pricer.html", headers={"Cookie": cookie})
        response = self.connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("估值定价", html)
        self.connection.request("GET", "/capability/assets/icons/optionhelper-logo.svg", headers={"Cookie": cookie})
        response = self.connection.getresponse()
        logo = response.read()
        self.assertEqual(response.status, 200)
        self.assertTrue(logo.startswith(b"<svg"))

    def test_admin_receives_a_declared_module_bridge_context(self) -> None:
        cookie = self.local_login("admin")
        status, task_body, _ = self.request("POST", "/api/tasks", {"subject": "Capability网关"}, cookie)
        self.assertEqual(status, 201)
        status, body, _ = self.request("GET", "/api/module-host/payoffer", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["context"]["module"], "payoffer")

    def test_later_module_context_is_bound_to_the_task_frozen_contract(self) -> None:
        cookie = self.local_login("admin")
        status, task_body, _ = self.request("POST", "/api/tasks", {"subject": "共享合同上下文"}, cookie)
        self.assertEqual(status, 201)
        task_id = task_body["task"]["task_id"]
        identity = self.app.identity_from_cookie(cookie)
        scripts_root = str(self.app.registry.capability_root / "scripts")
        with capability_import_scope(scripts_root):
            tool_entry = self.app.gateway._load_tool_entry(scripts_root)
            prepared = tool_entry.prepare_compute_request("payoffer", {
                "product_id": "2.1",
                "identity": {
                    "underlyings": ["000905.SH"],
                    "reference_prices": {"000905.SH": 100.0},
                    "contract_start_date": "2023-01-03",
                },
                "term_overrides": {"K": 100.0},
            })
        binding = self.app.contracts.bind(identity, task_id, prepared, catalog_version=str(self.app.registry.manifest["catalog_version"]))
        status, body, _ = self.request("GET", f"/api/module-host/pricer?task_id={task_id}", cookie=cookie)
        self.assertEqual(status, 200)
        context = body["context"]
        self.assertEqual(context["contract_fingerprint"], binding["contract_fingerprint"])
        self.assertEqual(context["catalog_version"], binding["catalog_version"])
        self.assertEqual(context["analysis_case_id"], f"case-{binding['contract_fingerprint'][:24]}")
        self.assertEqual(context["candidate_id"], f"candidate-{binding['contract_fingerprint'][:24]}")

    def test_preferences_persist_and_external_model_returns_unavailable(self) -> None:
        cookie = self.local_login("sales")
        status, body, _ = self.request("POST", "/api/settings/preferences", {"language": "en-US", "html_report_layout": "continuous"}, cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/settings", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["preferences"]["html_report_layout"], "continuous")
        status, body, _ = self.request("POST", "/api/tasks", {"subject": "本机持久化测试"}, cookie)
        self.assertEqual(status, 201)
        task_id = body["task"]["task_id"]
        status, body, _ = self.request("POST", f"/api/tasks/{task_id}/messages", {"content": "请分析这个任务"}, cookie)
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "unavailable")
        status, body, _ = self.request("GET", f"/api/tasks/{task_id}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["messages"][0]["content"], "请分析这个任务")

    def test_report_requests_require_selection_and_enforce_role(self) -> None:
        sales_cookie = self.local_login("sales")
        status, body, _ = self.request("POST", "/api/tasks", {"subject": "报告请求"}, sales_cookie)
        task_id = body["task"]["task_id"]
        status, body, _ = self.request("POST", f"/api/tasks/{task_id}/reports", {"selection": {"output_type": "card"}}, sales_cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")
        status, body, _ = self.request("POST", f"/api/tasks/{task_id}/reports", {"selection": {"output_type": "report"}}, sales_cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "report.full.request")

    def test_local_state_survives_server_restart_and_wind_probe_is_unavailable(self) -> None:
        cookie = self.local_login("admin")
        status, body, _ = self.request("POST", "/api/settings/preferences", {"language": "zh-CN", "html_report_layout": "with_toc"}, cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", "/api/tasks", {"subject": "重启后仍存在"}, cookie)
        self.assertEqual(status, 201)
        task_id = body["task"]["task_id"]
        self.app.shutdown()
        self.connection.close()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=capability_root())
        self.url = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=5)
        status, body, _ = self.request("GET", f"/api/tasks/{task_id}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["subject"], "重启后仍存在")
        status, body, _ = self.request("POST", "/api/settings/test/wind", {}, cookie)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "unavailable")
        self.assertEqual(body["message"], "相关功能暂不可用。请确认本机服务、当前任务和数据配置后重试。")

    def test_settings_reject_client_secret_reference_and_plaintext(self) -> None:
        cookie = self.local_login("admin")
        status, body, _ = self.request("POST", "/api/settings/model", {"provider_name": "openai-compatible", "endpoint": "https://example.invalid/v1", "model_name": "example-model", "secret_ref": {"provider": "keychain", "key": "model/default", "version": "1"}}, cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")
        status, body, _ = self.request("POST", "/api/settings/model", {"provider_name": "bad", "api_key": "plaintext"}, cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
