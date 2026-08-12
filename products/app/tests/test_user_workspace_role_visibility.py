"""Live HTTP and browser-behaviour checks for the user workspace boundary."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT = APP_ROOT.parents[1]
FRONTEND = APP_ROOT / "frontend"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.secrets.secret_provider import SecretProvider
from capability_fixture import capability_root


class UserWorkspaceRoleVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = AppServer(
            app_data_dir=Path(self.temporary.name),
            capability_root=capability_root(),
            secret_provider=SecretProvider(),
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

    def test_user_http_page_has_no_desk_control_and_desk_route_is_forbidden(self) -> None:
        cookie = self.login("sales")
        response, body = self.request("GET", "/api/me", cookie=cookie)
        self.assertEqual(response.status, 200)
        self.assertNotIn("optdesk", json.loads(body)["capabilities"])

        response, page = self.request("GET", "/optchat", cookie=cookie)
        self.assertEqual(response.status, 200)
        html = page.decode("utf-8")
        self.assertIn('data-has-desk="false"', html)
        self.assertNotIn('button type="button" data-mode="desk"', html)

        response, _ = self.request("GET", "/optdesk", cookie=cookie)
        self.assertEqual(response.status, 403)

    def test_browser_role_bootstrap_creates_desk_control_only_for_admin(self) -> None:
        for role, expected_labels in (("sales", ["OptChat"]), ("admin", ["OptChat", "OptDesk"])):
            with self.subTest(role=role):
                result = self._run_browser_bootstrap(role)
                self.assertEqual(result["labels"], expected_labels)
                self.assertEqual(result["has_desk"], role == "admin")
                self.assertEqual(result["desk_controls"], 1 if role == "admin" else 0)

    def _run_browser_bootstrap(self, role: str) -> dict[str, object]:
        script = textwrap.dedent(
            """
            import fs from "node:fs";
            import {pathToFileURL} from "node:url";

            const [appPath, pagePath, role] = process.argv.slice(1);
            const initialDesk = fs.readFileSync(pagePath, "utf8").includes('button type="button" data-mode="desk"');

            class Element {
              constructor(tagName = "div") {
                this.tagName = tagName;
                this.dataset = {};
                this.attributes = {};
                this.children = [];
                this.hidden = false;
                this.disabled = false;
                this.style = { setProperty() {} };
                this.classList = { add() {}, remove() {}, toggle() {} };
              }
              append(...children) { this.children.push(...children); }
              after() {}
              addEventListener() {}
              setAttribute(name, value) { this.attributes[name] = String(value); }
              getAttribute(name) { return this.attributes[name] || null; }
              removeAttribute(name) { delete this.attributes[name]; }
              querySelector(selector) {
                if (selector === "[data-mode-switch]") return modeSwitch;
                return null;
              }
              querySelectorAll() { return []; }
              getBoundingClientRect() { return { width: 1280 }; }
            }

            const chat = new Element("button");
            chat.dataset.mode = "chat";
            chat.textContent = "OptChat";
            const modeSwitch = new Element("div");
            modeSwitch.append(chat);
            if (initialDesk) {
              const desk = new Element("button");
              desk.dataset.mode = "desk";
              desk.textContent = "OptDesk";
              modeSwitch.append(desk);
            }
            const shell = new Element("div");
            shell.dataset = { hasDesk: "false" };
            shell.querySelector = (selector) => selector === "[data-mode-switch]" ? modeSwitch : null;

            globalThis.document = {
              documentElement: { dataset: {} },
              querySelector(selector) {
                if (selector === "[data-workspace-shell]") return shell;
                return null;
              },
              querySelectorAll(selector) {
                return selector === "button[data-mode]" ? modeSwitch.children.filter((item) => item.dataset.mode) : [];
              },
              createElement(tagName) { return new Element(tagName); },
              addEventListener() {},
            };
            globalThis.window = { addEventListener() {} };
            globalThis.requestAnimationFrame = (callback) => callback();
            globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
            globalThis.location = { origin: "http://127.0.0.1", search: "", pathname: "/optchat", replace() {} };
            globalThis.fetch = async () => ({
              ok: true,
              json: async () => ({ capabilities: role === "admin" ? ["optchat", "optdesk"] : ["optchat"] }),
            });

            const app = await import(pathToFileURL(appPath).href);
            await app.initializeWorkspace("chat");
            const buttons = modeSwitch.children.filter((item) => item.dataset.mode);
            console.log(JSON.stringify({
              labels: buttons.map((item) => item.textContent),
              desk_controls: buttons.filter((item) => item.dataset.mode === "desk").length,
              has_desk: shell.dataset.hasDesk === "true",
            }));
            """
        )
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                str(FRONTEND / "shared" / "app.js"),
                str(FRONTEND / "optchat" / "index.html"),
                role,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
