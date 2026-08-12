"""iFind settings expose one durable Refresh Token, not two required fields."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from capability_fixture import capability_root


def test_admin_can_save_refresh_token_without_access_token() -> None:
    with tempfile.TemporaryDirectory(prefix="optionhelper-ifind-refresh-") as temporary:
        app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
        parsed = urlparse(app.start_background())
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        try:
            connection.request(
                "POST", "/api/auth/local",
                body=json.dumps({"role": "admin", "principal_label": "User1"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            cookie = str(response.getheader("Set-Cookie")).split(";", 1)[0]

            connection.request(
                "POST", "/api/settings/data/credential",
                body=json.dumps({"provider_name": "ifind-http", "refresh_token": "refresh-fixture"}),
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            assert response.status == 200, body
            assert body["credential_status"] == "stored"
            assert body["settings"]["data_interface"]["credential_configured"] is True
            assert "refresh-fixture" not in json.dumps(body)

            reference = app.secret_reference("local-development", "ifind")
            stored = json.loads(app.secret_provider.resolve(reference, "iFind数据凭据"))
            assert stored == {"refresh_token": "refresh-fixture"}
        finally:
            connection.close()
            app.shutdown()


def test_settings_page_has_one_ifind_credential_field_and_neutral_provider_label() -> None:
    html = (APP_ROOT / "frontend" / "settings" / "index.html").read_text(encoding="utf-8")
    script = (APP_ROOT / "frontend" / "settings" / "settings.js").read_text(encoding="utf-8")
    assert 'name="refresh_token"' in html
    assert 'name="access_token"' not in html
    assert "iFind HTTP" not in html
    assert "iFind HTTPS" not in html
    assert "dataForm.elements.access_token" not in script
    service = (ROOT / "modules" / "datafetcher" / "src" / "service.py").read_text(encoding="utf-8")
    assert "重新保存Access Token" not in service
    assert "iFinD HTTP" not in service


def test_access_token_is_rejected_by_the_credential_endpoint() -> None:
    with tempfile.TemporaryDirectory(prefix="optionhelper-ifind-refresh-") as temporary:
        app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
        parsed = urlparse(app.start_background())
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        try:
            connection.request(
                "POST", "/api/auth/local",
                body=json.dumps({"role": "admin", "principal_label": "User1"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            cookie = str(response.getheader("Set-Cookie")).split(";", 1)[0]
            connection.request(
                "POST", "/api/settings/data/credential",
                body=json.dumps({
                    "provider_name": "ifind-http",
                    "refresh_token": "refresh-fixture",
                    "access_token": "must-not-be-accepted",
                }),
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 400
            assert payload["error"] == "invalid_request"
            reference = app.secret_reference("local-development", "ifind")
            try:
                app.secret_provider.resolve(reference, "iFind数据凭据")
            except Exception:
                pass
            else:
                raise AssertionError("rejected credential must not be stored")
        finally:
            connection.close()
            app.shutdown()


def test_public_validation_copy_uses_provider_name_not_transport_name() -> None:
    from backend.app_server import _data_from
    from backend.errors import ValidationError

    try:
        _data_from({"provider_name": "wind"})
    except ValidationError as exc:
        message = str(exc)
    else:
        raise AssertionError("unsupported provider must be rejected")

    assert message == "当前仅支持iFind数据服务"
    assert "HTTP" not in message
    assert "HTTPS" not in message
