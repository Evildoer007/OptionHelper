"""Security and role-boundary tests for the local App HTTP runtime."""

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


class AppSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(app_data_dir=Path(self.temporary.name), capability_root=capability_root())
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

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
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def login(self, role: str) -> tuple[dict[str, object], str]:
        status, body, set_cookie = self.request("POST", "/api/auth/local", {"role": role, "principal_label": f"{role}-security"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(set_cookie)
        return body, str(set_cookie).split(";", 1)[0]

    def test_session_cookie_is_not_exposed_in_module_context(self) -> None:
        body, cookie = self.login("admin")
        cookie_token = cookie.split("=", 1)[1]
        self.assertNotIn("session_id", body["identity"])
        self.assertNotIn(cookie_token, json.dumps(body))
        status, context_body, _ = self.request("GET", "/api/module-host/pricer", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(context_body["context"]["module"], "pricer")
        self.assertNotIn(cookie_token, json.dumps(context_body))

    def test_sales_cannot_write_tenant_model_or_data_settings(self) -> None:
        _, cookie = self.login("sales")
        model = {"provider_name": "openai-compatible", "endpoint": "https://example.invalid/v1", "model_name": "example-model"}
        status, body, _ = self.request("POST", "/api/settings/model", model, cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "settings.model.write")
        status, body, _ = self.request("POST", "/api/settings/data", {"provider_name": "ifind-http"}, cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "settings.data.write")

    def test_configured_model_reference_returns_real_unavailable_without_keychain_item(self) -> None:
        _, cookie = self.login("admin")
        model = {"provider_name": "openai-compatible", "endpoint": "https://example.invalid/v1", "model_name": "example-model"}
        status, _, _ = self.request("POST", "/api/settings/model", model, cookie)
        self.assertEqual(status, 200)
        status, task_body, _ = self.request("POST", "/api/tasks", {"subject": "模型适配器门禁"}, cookie)
        self.assertEqual(status, 201)
        status, response, _ = self.request("POST", f"/api/tasks/{task_body['task']['task_id']}/messages", {"content": "测试外部模型"}, cookie)
        self.assertEqual(status, 503)
        self.assertEqual(response["error"]["capability"], "模型服务凭据")
        self.assertIn("管理员", response["error"]["next_step"])

    def test_admin_module_context_does_not_create_a_run_on_its_own(self) -> None:
        _, cookie = self.login("admin")
        status, task_body, _ = self.request("POST", "/api/tasks", {"subject": "模块运行留痕"}, cookie)
        self.assertEqual(status, 201)
        task_id = task_body["task"]["task_id"]
        status, body, _ = self.request("GET", "/api/module-host/payoffer", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["context"]["module"], "payoffer")
        status, persisted_task, _ = self.request("GET", f"/api/tasks/{task_id}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(persisted_task["task"]["run_refs"], [])

    def test_datafetcher_context_does_not_create_a_fake_data_asset(self) -> None:
        _, cookie = self.login("admin")
        status, task_body, _ = self.request("POST", "/api/tasks", {"subject": "数据获取门禁"}, cookie)
        self.assertEqual(status, 201)
        status, body, _ = self.request("GET", "/api/module-host/datafetcher", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["context"]["module"], "datafetcher")
        self.assertEqual(self.app._documents.read("data_assets"), {})

    def test_module_execution_is_rejected_without_host_context(self) -> None:
        _, admin = self.login("admin")
        status, body, _ = self.request("POST", "/api/tools/payoffer", {"action": "catalog"}, admin)
        self.assertEqual(status, 403)
        status, body, _ = self.request("GET", "/api/module-host/payoffer", cookie=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["context"]["module"], "payoffer")


if __name__ == "__main__":
    unittest.main()
