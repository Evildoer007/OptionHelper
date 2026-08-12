"""Regression coverage for DataFetcher App read-only actions.

Read-only metadata and the current caller's local asset index remain usable
before a Data Interface credential is configured.  Only a real fetch needs a
configured Host-owned SecretRef.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlencode, urlparse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from capability_fixture import capability_root


class DataFetcherReadonlyActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="optionhelper-datafetch-readonly-")
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "OPTIONHELPER_RUNTIME_ROOT": str(self.root / "runtime"),
            "OPTIONHELPER_DATA_ROOT": str(self.root / "data"),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "result"),
        })
        self.environment.start()
        self.app = AppServer(app_data_dir=self.root / "app-state", capability_root=capability_root())
        parsed = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], str | None]:
        values = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            values["Content-Type"] = "application/json"
        if cookie:
            values["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=values)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def login_admin(self) -> str:
        status, _body, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "data-readonly"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(cookie)
        return str(cookie).split(";", 1)[0]

    def module_headers(self, cookie: str, *, task_id: str | None = None, request_id: str) -> dict[str, str]:
        query = f"?{urlencode({'task_id': task_id})}" if task_id else ""
        status, payload, _ = self.request("GET", f"/api/module-host/datafetcher{query}", cookie=cookie)
        self.assertEqual(status, 200)
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(payload["context"]),
            "X-OptionHelper-Request-Id": request_id,
        }

    def test_unconfigured_read_actions_are_available_but_fetch_requires_configuration(self) -> None:
        cookie = self.login_admin()
        for index, action in enumerate(("status", "catalog", "list_assets"), start=1):
            status, envelope, _ = self.request(
                "POST",
                "/api/tools/datafetcher",
                {"action": action},
                cookie,
                self.module_headers(cookie, request_id=f"readonly-{index}"),
            )
            self.assertEqual(status, 200, envelope)
            result = envelope["result"]
            self.assertTrue(result["ok"])
            self.assertFalse(result["configured"])
            self.assertEqual(result["credential_status"]["state"], "not_configured")
            self.assertEqual(result["credential_status"]["remote_provider"], "not_configured")
            if action == "list_assets":
                self.assertEqual(result["assets"], [])

        status, created, _ = self.request("POST", "/api/tasks", {"subject": "DataFetcher未配置门禁"}, cookie)
        self.assertEqual(status, 201)
        task_id = created["task"]["task_id"]
        status, error, _ = self.request(
            "POST",
            "/api/tools/datafetcher",
            {
                "action": "fetch",
                "task_id": task_id,
                "request": {
                    "asset_id": "000905.SH",
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-03",
                    "fields": ["close"],
                },
            },
            cookie,
            self.module_headers(cookie, task_id=task_id, request_id="fetch-unconfigured"),
        )
        self.assertEqual(status, 503)
        self.assertEqual(error["capability"], "datafetcher.configuration")

    def test_read_actions_still_reject_tenant_or_plaintext_credential_injection(self) -> None:
        cookie = self.login_admin()
        status, body, _ = self.request(
            "POST",
            "/api/tools/datafetcher",
            {"action": "list_assets", "tenant_id": "tenant-b"},
            cookie,
            self.module_headers(cookie, request_id="tenant-injection"),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

        status, body, _ = self.request(
            "POST",
            "/api/tools/datafetcher",
            {"action": "status", "access_token": "plaintext-must-never-reach-capability"},
            cookie,
            self.module_headers(cookie, request_id="plaintext-injection"),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
