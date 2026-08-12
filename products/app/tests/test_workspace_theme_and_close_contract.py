"""Runtime contracts for the fixed-light workspace and native close lifecycle."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"
THEME = FRONTEND / "shared" / "theme.js"
NODE = shutil.which("node")
SWIFT = PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.swift"
OBJC = PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.m"


class WorkspaceThemeAndCloseContractTests(unittest.TestCase):
    def test_workspace_surfaces_are_fixed_light_while_login_remains_themeable(self) -> None:
        login = (FRONTEND / "login" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('data-theme-scope="fixed-light"', login)
        self.assertIn('name="color-scheme" content="light dark"', login)

        for page in ("optchat", "optdesk", "settings"):
            content = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-theme-scope="fixed-light"', content)
            self.assertIn('name="color-scheme" content="light"', content)

        settings = (FRONTEND / "settings" / "index.html").read_text(encoding="utf-8")
        self.assertIn("启动动画、登录页与App图标", settings)
        self.assertNotIn("统一登录、工作台与设置中心", settings)

    def test_dark_preference_keeps_workspace_light_and_updates_native_icon(self) -> None:
        self.assertIsNotNone(NODE, "Node.js is required for the theme runtime test")
        encoded = base64.b64encode(THEME.read_bytes()).decode("ascii")
        harness = textwrap.dedent(
            """
            const source = Buffer.from("__SOURCE__", "base64").toString("utf8");
            const values = new Map([["oh-theme", "dark"]]);
            const root = { dataset: { themeScope: "fixed-light" } };
            const icon = { href: "" };
            let nativeMessage = null;
            globalThis.document = {
              documentElement: root,
              querySelector(selector) { return selector === 'link[rel="icon"]' ? icon : null; },
              querySelectorAll() { return []; },
              dispatchEvent() {},
            };
            globalThis.window = {
              matchMedia() { return { matches: false, addEventListener() {} }; },
              webkit: { messageHandlers: { optionhelperTheme: { postMessage(value) { nativeMessage = value; } } } },
            };
            globalThis.localStorage = {
              getItem(key) { return values.get(key) ?? null; },
              setItem(key, value) { values.set(key, String(value)); },
            };
            globalThis.CustomEvent = class CustomEvent {
              constructor(type, init) { this.type = type; this.detail = init?.detail; }
            };
            await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
            process.stdout.write(JSON.stringify({
              pageTheme: root.dataset.theme,
              preference: root.dataset.themePref,
              favicon: icon.href,
              nativeMessage,
            }));
            """
        ).replace("__SOURCE__", encoded)
        completed = subprocess.run(
            [str(NODE), "--input-type=module", "-e", harness],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout)
        self.assertEqual(state["pageTheme"], "light")
        self.assertEqual(state["preference"], "dark")
        self.assertTrue(state["favicon"].endswith("optionhelper-app-icon-tile-dark.svg"))
        self.assertEqual(state["nativeMessage"], {"theme": "dark", "preference": "dark"})

    def test_closing_the_last_native_window_terminates_app_and_backend(self) -> None:
        for source in (SWIFT.read_text(encoding="utf-8"), OBJC.read_text(encoding="utf-8")):
            self.assertIn("applicationShouldTerminateAfterLastWindowClosed", source)
            self.assertTrue("true" in source or "YES" in source)
            self.assertIn("applicationWillTerminate", source)
            self.assertIn("terminate", source)


if __name__ == "__main__":
    unittest.main()
