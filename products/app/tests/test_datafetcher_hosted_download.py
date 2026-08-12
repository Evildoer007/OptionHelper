"""HTTP behavior for DataFetcher downloads hosted inside OptDesk."""

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
from backend.authorization.roles import Role
from backend.identity.session_identity import SessionIdentity
ASSET_ID = "data-abc_01"
CSV = b"date,asset_id,close\n2026/08/10,000905.SH,100.0\n"
CAPABILITY_ROOT = APP_ROOT / "capability" / "option-helper"


def _asset() -> dict[str, object]:
    return {
        "data_asset_id": ASSET_ID,
        "storage_ref": "assetref-abc-01",
        "media_type": "text/csv",
        "schema_id": "optionhelper.data-asset/v1",
        "asset_ids": ["000905.SH"],
        "normalized_fields": ["close"],
        "coverage": {"start": "2026/08/10", "end": "2026/08/10"},
        "row_count": 1,
        "price_convention": {"adjustment": "forward"},
        "content_hash": "a" * 64,
        "lineage": {"provider": "local-test"},
    }


class HostedDataAssetDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(
            app_data_dir=Path(self.temporary.name),
            capability_root=CAPABILITY_ROOT,
            allow_unverified_capability=True,
        )
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        cookie: str | None = None,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[http.client.HTTPResponse, bytes]:
        effective_headers = dict(headers or {})
        if cookie:
            effective_headers["Cookie"] = cookie
        payload = None
        if body is not None:
            payload = json.dumps(body)
            effective_headers["Content-Type"] = "application/json"
        self.connection.request(method, path, body=payload, headers=effective_headers)
        response = self.connection.getresponse()
        return response, response.read()

    def login(self, label: str) -> tuple[str, SessionIdentity]:
        response, payload = self.request("POST", "/api/auth/local", body={"role": "admin", "principal_label": label})
        self.assertEqual(response.status, 200)
        cookie = str(response.getheader("Set-Cookie")).split(";", 1)[0]
        value = json.loads(payload)
        identity = SessionIdentity(value["identity"]["principal_id"], value["identity"]["tenant_id"], Role.ADMIN, cookie.split("=", 1)[1])
        return cookie, identity

    def host_headers(self, cookie: str, task_id: str, request_id: str) -> dict[str, str]:
        response, payload = self.request("GET", f"/api/module-host/datafetcher?task_id={task_id}", cookie=cookie)
        self.assertEqual(response.status, 200)
        context = json.loads(payload)["context"]
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(context, separators=(",", ":")),
            "X-OptionHelper-Request-Id": request_id,
        }

    def test_download_requires_host_context_and_returns_only_the_registered_asset(self) -> None:
        cookie, identity = self.login("download-owner")
        task = self.app.tasks.create(identity, "受控数据下载")
        registered = self.app.data_assets.register(identity, _asset())
        calls: list[tuple[str, str, str]] = []

        def read_asset(data_asset_id: str, principal: SessionIdentity, *, request_id: str) -> tuple[dict[str, object], bytes]:
            calls.append((data_asset_id, principal.tenant_id, request_id))
            return registered, CSV

        self.app.datafetcher.read_asset = read_asset  # type: ignore[method-assign]

        response, _ = self.request("POST", f"/api/assets/{ASSET_ID}/download", cookie=cookie, body={})
        self.assertEqual(response.status, 403)

        headers = self.host_headers(cookie, task["task_id"], "download-request-1")
        response, body = self.request("POST", f"/api/assets/{ASSET_ID}/download", cookie=cookie, body={}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, CSV)
        self.assertEqual(response.getheader("Content-Type"), "text/csv; charset=utf-8")
        self.assertEqual(response.getheader("Content-Disposition"), f'attachment; filename="{ASSET_ID}.csv"')
        self.assertEqual(calls, [(ASSET_ID, identity.tenant_id, "download-request-1")])
        self.assertNotIn(b"assetref-abc-01", body)

        blocked_headers = dict(headers)
        blocked_headers["X-OptionHelper-Request-Id"] = "download-request-path"
        response, _ = self.request(
            "POST",
            f"/api/assets/{ASSET_ID}/download",
            cookie=cookie,
            body={"local_csv": "/private/tmp/input.csv"},
            headers=blocked_headers,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(len(calls), 1)

    def test_download_rejects_other_principal_unknown_ids_and_path_inputs(self) -> None:
        first_cookie, first = self.login("first-owner")
        first_task = self.app.tasks.create(first, "第一用户")
        self.app.data_assets.register(first, _asset())
        second_cookie, second = self.login("second-owner")
        second_task = self.app.tasks.create(second, "第二用户")
        self.app.datafetcher.read_asset = lambda *_args, **_kwargs: (_asset(), CSV)  # type: ignore[method-assign]

        second_headers = self.host_headers(second_cookie, second_task["task_id"], "download-other-tenant")
        response, _ = self.request("POST", f"/api/assets/{ASSET_ID}/download", cookie=second_cookie, body={}, headers=second_headers)
        self.assertEqual(response.status, 403)

        first_headers = self.host_headers(first_cookie, first_task["task_id"], "download-missing")
        response, _ = self.request("POST", "/api/assets/not-registered/download", cookie=first_cookie, body={}, headers=first_headers)
        self.assertEqual(response.status, 404)
        response, _ = self.request("POST", "/api/assets/%2Fprivate%2Finput.csv/download", cookie=first_cookie, body={}, headers=first_headers)
        self.assertIn(response.status, {400, 404})


if __name__ == "__main__":
    unittest.main()
