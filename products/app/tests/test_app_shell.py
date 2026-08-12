"""App-shell regressions that do not inspect or copy Capability page source."""

from __future__ import annotations

import http.client
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


class AppShellTests(unittest.TestCase):
    def test_optchat_user_can_load_shared_brand_mark_without_module_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            location = urlparse(app.start_background())
            connection = http.client.HTTPConnection(location.hostname, location.port, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/api/auth/local",
                    body='{"role":"sales","principal_label":"shell-test"}',
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                cookie = str(response.getheader("Set-Cookie")).split(";", 1)[0]
                connection.request(
                    "GET",
                    "/capability/assets/icons/optionhelper-mark.svg",
                    headers={"Cookie": cookie},
                )
                logo = connection.getresponse()
                self.assertEqual(logo.status, 200)
                self.assertTrue(logo.read().startswith(b"<svg"))
                connection.request(
                    "GET",
                    "/capability/assets/pages/pricer/pricer.html",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(connection.getresponse().status, 403)
            finally:
                connection.close()
                app.shutdown()


if __name__ == "__main__":
    unittest.main()
