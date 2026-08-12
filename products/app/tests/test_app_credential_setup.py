"""Credential-setup regression tests: paste once, persist only opaque references."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.errors import UnavailableCapabilityError
from backend.model_gateway.deepseek_provider import complete
from backend.secrets.secret_provider import SecretProvider
from backend.secrets.secret_ref import SecretRef
from backend.settings.settings_models import ModelServiceSettings
from desktop.macos.keychain import MacOSKeychain
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


class CredentialSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.keychain = MemoryKeychain()
        self.app = AppServer(
            app_data_dir=Path(self.temporary.name),
            capability_root=capability_root(),
            secret_provider=SecretProvider({"keychain": self.keychain}),
        )
        location = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: dict[str, object], cookie: str | None = None) -> tuple[int, dict[str, object], str | None]:
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        self.connection.request(method, path, body=json.dumps(body), headers=headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")

    def login(self, role: str) -> str:
        status, _, cookie = self.request("POST", "/api/auth/local", {"role": role, "principal_label": "credential-test"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(cookie)
        return str(cookie).split(";", 1)[0]

    def test_pasted_model_key_is_written_only_to_keychain(self) -> None:
        cookie = self.login("admin")
        secret = "test-deepseek-key-not-in-settings"
        status, body, _ = self.request(
            "POST", "/api/settings/model/credential",
            {"provider_name": "openai-compatible", "endpoint": "https://api.deepseek.com/v1", "model_name": "deepseek-chat", "api_key": secret},
            cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["credential_status"], "stored")
        reference = self.app.secret_reference("local-development", "model")
        self.assertEqual(self.keychain.values[reference.key], secret)
        self.assertNotIn(secret, json.dumps(self.app._documents.read("settings")))
        self.assertNotIn(secret, json.dumps(body))

    def test_generic_model_connection_resolves_keychain_before_provider(self) -> None:
        cookie = self.login("admin")
        status, _, _ = self.request(
            "POST", "/api/settings/model/credential",
            {
                "provider_name": "openai-compatible",
                "endpoint": "https://models.example.invalid/v1",
                "model_name": "chosen-model",
                "api_key": "test-model-key",
            },
            cookie,
        )
        self.assertEqual(status, 200)
        received: dict[str, object] = {}

        def completion(settings: ModelServiceSettings, reference: SecretRef, messages: list[dict[str, str]]) -> str:
            received["model_name"] = settings.model_name
            received["reference"] = reference.redacted()
            received["messages"] = messages
            self.assertEqual(self.app.secret_provider.resolve(reference, "模型服务凭据"), "test-model-key")
            return "OK"

        self.app.provider_registry.register("openai-compatible", completion)
        status, body, _ = self.request("POST", "/api/settings/test/model", {}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["connection"]["status"], "available")
        self.assertEqual(received["model_name"], "chosen-model")
        self.assertEqual(received["reference"], self.app.secret_reference("local-development", "model").redacted())

    def test_missing_model_key_is_distinct_from_provider_network_failure(self) -> None:
        cookie = self.login("admin")
        identity = self.app.identity_from_cookie(cookie)
        reference = self.app.secret_reference(identity.tenant_id, "model")
        current = self.app.settings_for(identity)
        self.app.save_settings(
            identity,
            replace(current, model_service=ModelServiceSettings(
                "openai-compatible", "https://models.example.invalid/v1", reference, "chosen-model",
            )),
            "settings.model.write",
        )
        status, body, _ = self.request("POST", "/api/settings/test/model", {}, cookie)
        self.assertEqual(status, 503)
        self.assertEqual(body["capability"], "模型服务凭据")
        self.assertIn("重新粘贴", body["next_step"])

    def test_pasted_ifind_refresh_token_is_atomic_and_not_written_to_settings(self) -> None:
        cookie = self.login("admin")
        refresh = "test-ifind-refresh"
        status, body, _ = self.request(
            "POST", "/api/settings/data/credential",
            {"provider_name": "ifind-http", "refresh_token": refresh},
            cookie,
        )
        self.assertEqual(status, 200)
        reference = self.app.secret_reference("local-development", "ifind")
        stored = json.loads(self.keychain.values[reference.key])
        self.assertEqual(stored, {"refresh_token": refresh})
        serialized = json.dumps(self.app._documents.read("settings"))
        self.assertNotIn(refresh, serialized)
        self.assertNotIn(refresh, json.dumps(body))

    def test_plaintext_remains_rejected_on_normal_settings_endpoint(self) -> None:
        cookie = self.login("admin")
        status, body, _ = self.request(
            "POST", "/api/settings/model",
            {"provider_name": "deepseek-compatible", "endpoint": "https://api.deepseek.com/v1", "api_key": "must-reject"},
            cookie,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

    def test_deepseek_adapter_uses_bearer_header_without_exposing_key(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            def read(self) -> bytes:
                return '{"choices":[{"message":{"content":"  正常返回  "}}]}'.encode("utf-8")

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

        def opener(request: object, timeout: int) -> Response:
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

        result = complete(
            ModelServiceSettings("openai-compatible", "https://api.deepseek.com/v1", model_name="deepseek-chat"),
            SecretRef("keychain", "optionhelper/model-service"),
            [{"role": "user", "content": "你好"}],
            resolve_secret=lambda _: "test-key",
            opener=opener,
        )
        self.assertEqual(result, "正常返回")
        self.assertEqual(captured["authorization"], "Bearer test-key")

    def test_compatible_adapter_classifies_network_and_upstream_failures(self) -> None:
        settings = ModelServiceSettings("openai-compatible", "https://api.example.invalid/v1", model_name="test-model")
        reference = SecretRef("keychain", "model/test")
        with self.assertRaises(UnavailableCapabilityError) as network:
            complete(settings, reference, [{"role": "user", "content": "测试"}], resolve_secret=lambda _: "test-key", opener=lambda *_, **__: (_ for _ in ()).throw(URLError("offline")))
        self.assertEqual(network.exception.capability, "模型服务网络")
        with self.assertRaises(UnavailableCapabilityError) as upstream:
            complete(settings, reference, [{"role": "user", "content": "测试"}], resolve_secret=lambda _: "test-key", opener=lambda *_, **__: (_ for _ in ()).throw(HTTPError("https://api.example.invalid", 429, "rate", {}, None)))
        self.assertEqual(upstream.exception.capability, "模型服务上游")

if __name__ == "__main__":
    unittest.main()
