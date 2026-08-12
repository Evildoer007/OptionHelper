"""v1.0 DataFetcher iFinD App闭环：仅用Host SecretPort和内存HTTP夹具。"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlparse


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.errors import UnavailableCapabilityError
from backend.secrets.secret_provider import SecretProvider
from backend.secrets.secret_ref import SecretRef
from capability_fixture import capability_root


class _MemoryKeychain:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def store(self, reference: SecretRef, value: str) -> None:
        self.values[reference.key] = value

    def resolve(self, reference: SecretRef) -> str:
        try:
            return self.values[reference.key]
        except KeyError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "测试夹具未保存iFinD凭据") from error

    def delete(self, reference: SecretRef) -> None:
        self.values.pop(reference.key, None)


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self) -> dict[str, Any]:
        return self._payload


class DataFetcherIFindHostClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="optionhelper-ifind-host-")
        self.root = Path(self.temporary.name)
        self.keychain = _MemoryKeychain()
        self.secrets = SecretProvider({"keychain": self.keychain})
        self.environment = patch.dict(os.environ, {
            "OPTIONHELPER_RUNTIME_ROOT": str(self.root / "runtime"),
            "OPTIONHELPER_DATA_ROOT": str(self.root / "data"),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "result"),
        })
        self.environment.start()
        self.app = self._new_app()
        self.connection = self._connect(self.app)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def _new_app(self) -> AppServer:
        return AppServer(
            app_data_dir=self.root / "app-state",
            capability_root=capability_root(),
            secret_provider=self.secrets,
        )

    @staticmethod
    def _connect(app: AppServer) -> http.client.HTTPConnection:
        location = urlparse(app.start_background())
        return http.client.HTTPConnection(location.hostname, location.port, timeout=10)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], str | None]:
        effective_headers = dict(headers or {})
        payload = None
        if body is not None:
            effective_headers["Content-Type"] = "application/json"
            payload = json.dumps(body)
        if cookie:
            effective_headers["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=effective_headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def login(self) -> str:
        status, _body, cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "ifind-host"})
        self.assertEqual(status, 200)
        return str(cookie).split(";", 1)[0]

    def host_headers(self, cookie: str, request_id: str, task_id: str | None = None) -> dict[str, str]:
        query = f"?{urlencode({'task_id': task_id})}" if task_id else ""
        status, payload, _ = self.request("GET", f"/api/module-host/datafetcher{query}", cookie=cookie)
        self.assertEqual(status, 200)
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(payload["context"], separators=(",", ":")),
            "X-OptionHelper-Request-Id": request_id,
        }

    def fake_ifind_post(self, url: str, **_kwargs: object) -> _Response:
        self.calls.append(url)
        if url.endswith("get_access_token"):
            return _Response({"errorcode": 0, "data": {"access_token": "issued-for-test"}})
        if url.endswith("cmd_history_quotation"):
            return _Response({"errorcode": 0, "tables": [{
                "thscode": "000905.SH",
                "time": ["2026-07-01", "2026-08-01"],
                "table": {
                    "open": [5000.0, 5010.0], "high": [5010.0, 5020.0],
                    "low": [4990.0, 5000.0], "close": [5005.0, 5015.0],
                },
            }]})
        raise AssertionError(f"unexpected iFinD endpoint: {url}")

    def save_ifind_credentials(self, cookie: str, *, refresh: str = "fixture-refresh") -> None:
        status, body, _ = self.request(
            "POST", "/api/settings/data/credential",
            {"provider_name": "ifind-http", "refresh_token": refresh}, cookie,
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["settings"]["data_interface"]["credential_configured"])
        self.assertNotIn(refresh, json.dumps(body))

    def create_task(self, cookie: str) -> str:
        status, body, _ = self.request("POST", "/api/tasks", {"subject": "iFinD数据获取"}, cookie)
        self.assertEqual(status, 201, body)
        return str(body["task"]["task_id"])

    def fetch(self, cookie: str, *, request_id: str, cache_policy: str | None = None) -> dict[str, Any]:
        task_id = self.create_task(cookie)
        request = {
            "asset_id": "000905.SH", "start_date": "2026-07-01", "end_date": "2026-08-01",
            "fields": ["close", "adj_close"],
        }
        if cache_policy is not None:
            request["cache_policy"] = cache_policy
        status, body, _ = self.request(
            "POST", "/api/tools/datafetcher",
            {"action": "fetch", "task_id": task_id, "request": request},
            cookie,
            self.host_headers(cookie, request_id, task_id),
        )
        self.assertEqual(status, 200, body)
        return body["result"]

    def test_saved_credentials_flow_through_host_port_to_status_connection_and_realtime_fetch(self) -> None:
        cookie = self.login()
        self.save_ifind_credentials(cookie)
        with patch("requests.post", new=self.fake_ifind_post):
            status, body, _ = self.request(
                "POST", "/api/tools/datafetcher", {"action": "status"}, cookie,
                self.host_headers(cookie, "ifind-status-1"),
            )
            self.assertEqual(status, 200, body)
            self.assertTrue(body["result"]["configured"])
            self.assertNotIn("fixture-", json.dumps(body))

            status, connection, _ = self.request("POST", "/api/settings/test/ifind", {}, cookie)
            self.assertEqual(status, 200, connection)
            self.assertEqual(connection["connection"]["status"], "available")
            self.assertIn("iFind", connection["connection"]["detail"])
            self.assertNotIn("fixture-", json.dumps(connection))

            first = self.fetch(cookie, request_id="ifind-fetch-1")
            self.assertTrue(first["ok"])
            self.assertEqual(first["data_asset_ref"]["asset_ids"], ["000905.SH"])
            self.assertEqual(first["cache_decision"], "cache_miss_fetched")
            first_http_calls = len(self.calls)
            self.assertGreaterEqual(first_http_calls, 2)
            self.assertGreaterEqual(sum(url.endswith("get_access_token") for url in self.calls), 2)

            self.connection.close()
            self.app.shutdown()
            self.app = self._new_app()
            self.connection = self._connect(self.app)
            refreshed = self.fetch(cookie, request_id="ifind-fetch-after-restart")
            self.assertTrue(refreshed["ok"])
            self.assertEqual(refreshed["cache_decision"], "cache_miss_fetched")
            self.assertGreater(len(self.calls), first_http_calls)
            cached = self.fetch(cookie, request_id="ifind-fetch-explicit-cache", cache_policy="reuse")
            self.assertTrue(cached["ok"])
            self.assertEqual(cached["cache_decision"], "cache_hit")
            self.assertEqual(cached["data_asset_ref"]["content_hash"], refreshed["data_asset_ref"]["content_hash"])
            state = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in self.root.rglob("*.json"))
            self.assertNotIn("fixture-access", state)
            self.assertNotIn("fixture-refresh", state)

    def test_invalid_saved_credentials_return_chinese_action_without_leaking_fixture_values(self) -> None:
        cookie = self.login()
        self.save_ifind_credentials(cookie, refresh="invalid-refresh")

        def rejected_post(url: str, **_kwargs: object) -> _Response:
            self.calls.append(url)
            return _Response({"errorcode": -1, "errmsg": "invalid credential fixture"})

        with patch("requests.post", new=rejected_post):
            status, body, _ = self.request("POST", "/api/settings/test/ifind", {}, cookie)
        self.assertEqual(status, 200, body)
        connection = body["connection"]
        self.assertEqual(connection["status"], "unauthorized")
        self.assertIn("重新保存", connection["detail"])
        self.assertNotIn("invalid-access", json.dumps(body))
        self.assertNotIn("invalid-refresh", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
