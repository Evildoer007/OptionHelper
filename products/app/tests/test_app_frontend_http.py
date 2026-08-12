"""HTTP-level checks for the App-owned static frontend boundary."""

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


class AppFrontendHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # The fixture is deliberately minimal and does not carry a release signature.
        # Production AppServer remains fail-closed for an unverified Capability.
        self.app = AppServer(
            app_data_dir=Path(self.temporary.name),
            capability_root=capability_root(),
            allow_unverified_capability=True,
        )
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, *, cookie: str | None = None, body: dict[str, str] | None = None) -> tuple[http.client.HTTPResponse, bytes]:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = self.connection.getresponse()
        return response, response.read()

    def login(self, role: str) -> str:
        response, _ = self.request("POST", "/api/auth/local", body={"role": role})
        self.assertEqual(response.status, 200)
        return str(response.getheader("Set-Cookie")).split(";", 1)[0]

    def test_login_resources_and_icon_are_public_with_safe_headers(self) -> None:
        for path, content_type in (
            ("/", "text/html"),
            ("/app/frontend/login/login-startup.js", "application/javascript"),
            ("/app/frontend/login/login.js", "application/javascript"),
            ("/app/frontend/shared/vol-surface.js", "application/javascript"),
            ("/app/frontend/shared/styles.css", "text/css"),
            ("/app/frontend/shared/refinement.css", "text/css"),
            ("/app/frontend/shared/module-host.css", "text/css"),
        ):
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200)
            self.assertIn(content_type, str(response.getheader("Content-Type")))
            self.assertIn("script-src 'self'", str(response.getheader("Content-Security-Policy")))
            self.assertTrue(body)
        response, icon = self.request("GET", "/capability/assets/icons/optionhelper-app-icon-tile-light.svg")
        self.assertEqual(response.status, 200)
        self.assertTrue(icon.startswith(b"<svg"))

    def test_static_access_is_whitelisted_and_role_gated(self) -> None:
        sales = self.login("sales")
        response, _ = self.request("GET", "/app/frontend/optchat/index.html", cookie=sales)
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/app/frontend/optdesk/optdesk.js", cookie=sales)
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/app/frontend/settings/settings.js", cookie=sales)
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/app/frontend/../backend/app_server.py", cookie=sales)
        self.assertIn(response.status, {400, 404})
        response, _ = self.request("GET", "/app/frontend/login/README.md")
        self.assertEqual(response.status, 404)

    def test_admin_can_load_all_five_original_module_pages(self) -> None:
        admin = self.login("admin")
        for module in ("datafetcher", "payoffer", "pricer", "backtester", "reporter"):
            response, body = self.request("GET", f"/capability/assets/pages/{module}/{module}.html", cookie=admin)
            self.assertEqual(response.status, 200, module)
            self.assertIn(b"<html", body.lower())


if __name__ == "__main__":
    unittest.main()
