"""Executable regression for App theme controls.

The login module must not be the only path that binds the three theme buttons:
an exception in a page module must never make the application appear usable
while leaving its controls inert.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[3]
THEME = PROJECT / "products" / "app" / "frontend" / "shared" / "theme.js"
NODE = shutil.which("node")


class ThemeControlsRuntimeTests(unittest.TestCase):
    def test_document_theme_controls_bind_and_switch_to_dark(self) -> None:
        self.assertIsNotNone(NODE, "Node.js is required for the theme runtime test")
        encoded = base64.b64encode(THEME.read_bytes()).decode("ascii")
        harness = textwrap.dedent(
            """
            const source = Buffer.from("__SOURCE__", "base64").toString("utf8");
            const values = new Map();
            const listeners = new Map();
            const root = { dataset: {} };
            const buttons = ["auto", "light", "dark"].map((themeSet) => ({
              dataset: { themeSet },
              attributes: new Map(),
              setAttribute(name, value) { this.attributes.set(name, String(value)); },
              closest(selector) { return selector === "[data-theme-set]" ? this : null; },
            }));
            const container = {
              dataset: {},
              querySelectorAll() { return buttons; },
              contains(node) { return buttons.includes(node); },
              addEventListener(name, callback) { listeners.set(name, callback); },
              dispatchEvent() {},
            };
            globalThis.document = {
              documentElement: root,
              querySelector() { return null; },
              querySelectorAll(selector) {
                if (selector === "[data-theme-set]") return buttons;
                return selector === "[data-theme-controls], .login-theme" ? [container] : [];
              },
              dispatchEvent() {},
            };
            globalThis.window = {
              matchMedia() { return { matches: false, addEventListener() {} }; },
              webkit: undefined,
            };
            globalThis.localStorage = {
              getItem(key) { return values.get(key) ?? null; },
              setItem(key, value) { values.set(key, String(value)); },
            };
            globalThis.CustomEvent = class CustomEvent {
              constructor(type, init) { this.type = type; this.detail = init?.detail; }
            };
            await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
            listeners.get("click")({ target: buttons[2] });
            process.stdout.write(JSON.stringify({
              theme: root.dataset.theme,
              preference: root.dataset.themePref,
              stored: values.get("oh-theme"),
              installed: container.dataset.themeControlsInstalled,
              pressed: buttons.map((button) => button.attributes.get("aria-pressed")),
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
        self.assertEqual(state["theme"], "dark")
        self.assertEqual(state["preference"], "dark")
        self.assertEqual(state["stored"], "dark")
        self.assertEqual(state["installed"], "true")
        self.assertEqual(state["pressed"], ["false", "false", "true"])


if __name__ == "__main__":
    unittest.main()
