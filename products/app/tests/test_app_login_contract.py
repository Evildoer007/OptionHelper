"""HTTP and storage contract for the App account-login boundary."""

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
from backend.errors import AuthorizationError
from backend.identity.login_handler import LoginHandler, LoginRequest
from backend.identity.password_store import PasswordCredentialStore
from capability_fixture import capability_root


class AppLoginContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="optionhelper-login-")
        self.root = Path(self.temporary.name)
        self.app = AppServer(
            app_data_dir=self.root / "state",
            capability_root=capability_root(),
            allow_unverified_capability=True,
            authentication_mode="local-development",
        )
        self.url = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, path: str, body: object) -> tuple[int, dict[str, object], str | None]:
        payload = json.dumps(body)
        self.connection.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = self.connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        return response.status, value, response.getheader("Set-Cookie")

    def test_development_empty_login_issues_only_a_loopback_local_session(self) -> None:
        status, body, cookie = self.request(
            "/api/auth/login",
            {"account": "", "password": "", "remember": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "local-development")
        self.assertEqual(body["identity"]["principal_id"], "local:User1")
        self.assertEqual(body["identity"]["role"], "admin")
        self.assertIsNotNone(cookie)
        self.assertIn("HttpOnly", str(cookie))
        self.assertIn("SameSite=Strict", str(cookie))
        self.assertNotIn("Max-Age", str(cookie))
        self.assertNotIn("password", json.dumps(body))
        self.assertNotIn("session_id", json.dumps(body))

    def test_remember_only_changes_cookie_lifetime_not_identity_claims(self) -> None:
        status, body, cookie = self.request(
            "/api/auth/login",
            {"account": "", "password": "", "remember": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["identity"]["principal_id"], "local:User1")
        self.assertIn("Max-Age=2592000", str(cookie))

    def test_nonempty_credentials_are_unavailable_until_accounts_exist(self) -> None:
        status, body, cookie = self.request(
            "/api/auth/login",
            {"account": "someone", "password": "not-a-real-password", "remember": True},
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "unavailable")
        self.assertEqual(body["capability"], "account.login")
        self.assertIsNone(cookie)
        self.assertNotIn("not-a-real-password", json.dumps(body))

    def test_login_payload_is_exact_and_typed(self) -> None:
        for body in (
            {"account": "", "password": "", "remember": "yes"},
            {"account": "", "password": "", "remember": False, "role": "admin"},
            {"account": [], "password": "", "remember": False},
        ):
            with self.subTest(body=body):
                status, response, cookie = self.request("/api/auth/login", body)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "invalid_request")
                self.assertIsNone(cookie)

    def test_managed_mode_fails_closed_for_login_and_legacy_local_route(self) -> None:
        managed = AppServer(
            app_data_dir=self.root / "managed-state",
            capability_root=capability_root(),
            allow_unverified_capability=True,
            authentication_mode="managed",
        )
        parsed = urlparse(managed.start_background())
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            for path, body in (
                ("/api/auth/login", {"account": "", "password": "", "remember": False}),
                ("/api/auth/local", {"role": "admin", "principal_label": "User1"}),
            ):
                with self.subTest(path=path):
                    connection.request(
                        "POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    value = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 503)
                    self.assertEqual(value["error"], "unavailable")
                    self.assertIsNone(response.getheader("Set-Cookie"))
        finally:
            connection.close()
            managed.shutdown()

    def test_local_bypass_rejects_a_non_loopback_peer(self) -> None:
        handler = LoginHandler(
            mode="local-development",
            password_store=PasswordCredentialStore(self.root / "passwords.json"),
        )
        request = LoginRequest(account="", password="", remember=False)
        with self.assertRaises(AuthorizationError):
            handler.authenticate(request, client_host="192.0.2.8")

    def test_password_store_never_writes_plaintext(self) -> None:
        path = self.root / "passwords.json"
        store = PasswordCredentialStore(path)
        store.provision("research-admin", "correct horse battery staple", Role.ADMIN, tenant_id="tenant-a")
        stored = path.read_text(encoding="utf-8")
        self.assertNotIn("correct horse battery staple", stored)
        self.assertTrue(store.authenticate("research-admin", "correct horse battery staple"))
        self.assertIsNone(store.authenticate("research-admin", "wrong password"))


if __name__ == "__main__":
    unittest.main()
