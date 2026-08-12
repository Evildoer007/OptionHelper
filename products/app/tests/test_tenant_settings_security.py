"""Failure-safe tests for tenant settings and Host-owned credential references."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer, _model_from
from backend.errors import UnavailableCapabilityError, ValidationError
from backend.model_gateway.deepseek_provider import complete_openai_compatible
from backend.secrets.secret_provider import SecretProvider
from backend.secrets.secret_ref import SecretRef
from backend.settings.settings_models import ModelServiceSettings
from capability_fixture import capability_root


class MemoryKeychain:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def store(self, reference: SecretRef, value: str) -> None:
        self.values[reference.key] = value

    def resolve(self, reference: SecretRef) -> str:
        try:
            return self.values[reference.key]
        except KeyError as error:
            raise UnavailableCapabilityError("本机凭据缺失", "测试凭据不存在") from error

    def delete(self, reference: SecretRef) -> None:
        self.values.pop(reference.key, None)


class TenantSettingsSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.keychain = MemoryKeychain()
        self.secrets = SecretProvider({"keychain": self.keychain})
        self.app = AppServer(
            app_data_dir=Path(self.temporary.name),
            capability_root=capability_root(),
            secret_provider=self.secrets,
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
        body: dict[str, object] | None = None,
        cookie: str | None = None,
    ) -> tuple[int, dict[str, object], str | None]:
        headers: dict[str, str] = {}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body)
        if cookie:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def login(self, role: str, label: str) -> str:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": role, "principal_label": label})
        self.assertEqual(status, 200)
        self.assertIsNotNone(cookie)
        return str(cookie).split(";", 1)[0]

    def test_browser_cannot_submit_or_receive_secret_reference(self) -> None:
        admin = self.login("admin", "tenant-admin")
        status, body, _ = self.request(
            "POST",
            "/api/settings/model",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://api.example.invalid/v1",
                "model_name": "example-model",
                "secret_ref": {"provider": "keychain", "key": "attacker-selected"},
            },
            admin,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")
        status, _, _ = self.request(
            "POST", "/api/settings/model",
            {"provider_name": "openai-compatible", "endpoint": "https://api.example.invalid/v1", "model_name": "example-model", "Secret_Ref": {"key": "attacker-selected"}},
            admin,
        )
        self.assertEqual(status, 400)
        status, body, _ = self.request("GET", "/api/settings", cookie=admin)
        self.assertEqual(status, 200)
        self.assertNotIn("secret_ref", body["settings"]["model_service"])
        self.assertNotIn("secret_ref", body["settings"]["data_interface"])

    def test_only_admin_can_write_tenant_model_or_data_credentials(self) -> None:
        user = self.login("sales", "tenant-user")
        status, body, _ = self.request(
            "POST",
            "/api/settings/model/credential",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://api.example.invalid/v1",
                "model_name": "example-model",
                "api_key": "test-only-key",
            },
            user,
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "settings.model.write")
        status, body, _ = self.request(
            "POST",
            "/api/settings/data/credential",
            {"provider_name": "ifind-http", "refresh_token": "test-refresh"},
            user,
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["capability"], "settings.data.write")

    def test_admin_tenant_model_is_visible_and_usable_by_user_in_same_tenant(self) -> None:
        admin = self.login("admin", "shared-admin")
        user = self.login("sales", "shared-user")
        status, _, _ = self.request(
            "POST",
            "/api/settings/model/credential",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://api.example.invalid/v1",
                "model_name": "shared-model",
                "api_key": "test-shared-key",
            },
            admin,
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/settings", cookie=user)
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["model_service"]["model_name"], "shared-model")
        self.assertTrue(body["settings"]["model_service"]["credential_configured"])

        observed: dict[str, str] = {}

        def completion(settings: ModelServiceSettings, reference: SecretRef, messages: list[dict[str, str]]) -> str:
            del messages
            observed["model"] = settings.model_name
            observed["secret"] = self.secrets.resolve(reference, "模型服务凭据")
            return "OK"

        self.app.provider_registry.register("openai-compatible", completion)
        user_identity = self.app.identity_from_cookie(user)
        self.assertEqual(self.app.model_gateway.complete_for(user_identity, "task-test", "测试"), "OK")
        self.assertEqual(observed, {"model": "shared-model", "secret": "test-shared-key"})

    def test_saved_model_key_cannot_be_reused_for_another_origin(self) -> None:
        admin = self.login("admin", "origin-admin")
        status, _, _ = self.request(
            "POST",
            "/api/settings/model/credential",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://models-a.example.invalid/v1",
                "model_name": "model-a",
                "api_key": "test-origin-key",
            },
            admin,
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request(
            "POST",
            "/api/settings/model",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://models-b.example.invalid/v1",
                "model_name": "model-b",
            },
            admin,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

        status, _, _ = self.request(
            "POST",
            "/api/settings/model",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://models-a.example.invalid/compatible/v1",
                "model_name": "model-b",
            },
            admin,
        )
        self.assertEqual(status, 200)

    def test_references_are_purpose_and_tenant_bound(self) -> None:
        first = self.app.secret_reference("tenant-a", "model")
        second = self.app.secret_reference("tenant-b", "model")
        data = self.app.secret_reference("tenant-a", "ifind")
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.key, data.key)
        self.assertNotIn("tenant-a", first.key)

    def test_legacy_kimi_without_model_name_is_rejected_instead_of_defaulted(self) -> None:
        admin_cookie = self.login("admin", "legacy-admin")
        identity = self.app.identity_from_cookie(admin_cookie)
        reference = self.app.secret_reference(identity.tenant_id, "model")
        current = self.app.settings_for(identity)
        self.app.settings.save(
            self.app._tenant_settings_key(identity.tenant_id),
            replace(current, role="admin", model_service=ModelServiceSettings(
                "kimi-compatible", "https://api.moonshot.cn/v1", reference,
            )),
        )
        snapshot = self.app.settings_for(identity)
        self.assertEqual(snapshot.model_service.provider_name, "unconfigured")
        self.assertIsNone(snapshot.model_service.secret_ref)

    def test_secret_provider_rejects_unapproved_or_wrong_purpose_reference(self) -> None:
        reference = SecretRef("keychain", "server-generated-model")
        with self.assertRaises(ValidationError):
            self.secrets.store(reference, "test", "模型服务凭据")
        self.secrets.allow_reference(reference, "模型服务凭据")
        self.secrets.store(reference, "test", "模型服务凭据")
        with self.assertRaises(ValidationError):
            self.secrets.resolve(reference, "iFind数据凭据")

    def test_failed_settings_save_restores_previous_key(self) -> None:
        admin_cookie = self.login("admin", "rollback-admin")
        identity = self.app.identity_from_cookie(admin_cookie)
        self.app.settings_for(identity)
        reference = self.app.secret_reference(identity.tenant_id, "model")
        self.secrets.allow_reference(reference, "模型服务凭据")
        self.secrets.store(reference, "old-test-key", "模型服务凭据")
        original_save = self.app.settings.save

        def fail_tenant_save(key: str, settings: object) -> None:
            if key.startswith("tenant:"):
                raise RuntimeError("simulated settings failure")
            original_save(key, settings)

        self.app.settings.save = fail_tenant_save  # type: ignore[method-assign]
        status, _, _ = self.request(
            "POST",
            "/api/settings/model/credential",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://api.example.invalid/v1",
                "model_name": "example-model",
                "api_key": "new-test-key",
            },
            admin_cookie,
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.secrets.resolve(reference, "模型服务凭据"), "old-test-key")


class ModelInputSafetyTests(unittest.TestCase):
    def test_backend_rejects_unsafe_base_urls_and_unknown_provider(self) -> None:
        base = {"provider_name": "openai-compatible", "model_name": "model-a"}
        for endpoint in (
            "http://api.example.com/v1",
            "https:///v1",
            "https://user:pass@api.example.com/v1",
            "https://api.example.com/v1?tenant=x",
            "https://api.example.com/v1#fragment",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                _model_from({**base, "endpoint": endpoint})
        with self.assertRaises(ValidationError):
            _model_from({"provider_name": "vendor-specific", "endpoint": "https://api.example.com/v1", "model_name": "model-a"})

    def test_generic_adapter_never_chooses_a_vendor_model_default(self) -> None:
        with self.assertRaises(ValidationError):
            complete_openai_compatible(
                ModelServiceSettings("openai-compatible", "https://api.example.com/v1"),
                SecretRef("keychain", "model/test"),
                [{"role": "user", "content": "测试"}],
                resolve_secret=lambda _: "test-key",
            )


if __name__ == "__main__":
    unittest.main()
