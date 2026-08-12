"""Executable fallback tests for the login launch-screen boundary.

The startup script is deliberately exercised alone: no ES module (including
``login.js``) is evaluated in this harness.  A failed module graph must never
leave the native launch screen above an inert login form.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[3]
STARTUP_SCRIPT = PROJECT / "products" / "app" / "frontend" / "login" / "login-startup.js"
NODE = shutil.which("node")


class LoginStartupRuntimeTests(unittest.TestCase):
    def run_startup_only(self, *, reduced_motion: bool, vol_ready: bool) -> dict[str, object]:
        self.assertIsNotNone(NODE, "Node.js is required for the isolated login-startup runtime test")
        source = json.dumps(STARTUP_SCRIPT.read_text(encoding="utf-8"))
        harness = textwrap.dedent(
            """
            const vm = require("node:vm");
            const source = __SOURCE__;
            const reducedMotion = __REDUCED_MOTION__;
            const volReady = __VOL_READY__;

            function makeClassList(initial = []) {
              const values = new Set(initial);
              const additions = new Map();
              return {
                add(...names) {
                  names.forEach((name) => {
                    additions.set(name, (additions.get(name) || 0) + 1);
                    values.add(name);
                  });
                },
                remove(...names) { names.forEach((name) => values.delete(name)); },
                contains(name) { return values.has(name); },
                additions(name) { return additions.get(name) || 0; },
              };
            }

            function makeElement() {
              const attributes = new Map();
              const listeners = new Map();
              return {
                classList: makeClassList(),
                removed: 0,
                focusCount: 0,
                addEventListener(type, callback) { listeners.set(type, callback); },
                removeEventListener(type) { listeners.delete(type); },
                setAttribute(name, value) { attributes.set(name, String(value)); },
                removeAttribute(name) { attributes.delete(name); },
                toggleAttribute(name, force) {
                  if (force) attributes.set(name, "");
                  else attributes.delete(name);
                },
                hasAttribute(name) { return attributes.has(name); },
                remove() { this.removed += 1; },
                focus() { this.focusCount += 1; },
              };
            }

            const root = { classList: makeClassList(["login-boot"]) };
            const splash = makeElement();
            const account = makeElement();
            const wrap = makeElement();
            const environment = makeElement();
            const theme = makeElement();
            [wrap, environment, theme].forEach((element) => {
              element.setAttribute("inert", "");
              element.setAttribute("aria-hidden", "true");
            });
            const selectors = new Map([
              ["#login-splash", splash], ["#login-account", account],
              [".login-wrap", wrap], [".login-environment", environment], [".login-theme", theme],
            ]);
            const documentListeners = new Map();
            const document = {
              documentElement: root,
              querySelector(selector) { return selectors.get(selector) || null; },
              addEventListener(type, callback) { documentListeners.set(type, callback); },
              removeEventListener(type) { documentListeners.delete(type); },
            };
            const timers = [];
            let nextTimer = 1;
            function setTimeout(callback, delay) {
              const timer = { id: nextTimer++, callback, delay, cleared: false };
              timers.push(timer);
              return timer.id;
            }
            function clearTimeout(id) {
              const timer = timers.find((candidate) => candidate.id === id);
              if (timer) timer.cleared = true;
            }
            let volCalls = 0;
            const windowObject = {};
            if (volReady) windowObject.__volIn = () => { volCalls += 1; };
            const location = { href: "http://127.0.0.1:4179/?app_startup=launch-token" };
            let replacedUrl = null;
            const context = {
              window: windowObject,
              document,
              location,
              history: { replaceState(_state, _title, url) { replacedUrl = url; } },
              URL,
              matchMedia() { return { matches: reducedMotion }; },
              setTimeout,
              clearTimeout,
            };
            vm.createContext(context);
            // Intentionally do not load login.js or any ES-module dependency.
            vm.runInContext(source, context, { filename: "login-startup.js" });
            function runTimers(delay) {
              timers.filter((timer) => timer.delay === delay && !timer.cleared)
                .forEach((timer) => { timer.cleared = true; timer.callback(); });
            }
            runTimers(1833);
            runTimers(470);
            runTimers(2600);
            runTimers(470);
            const shielded = [wrap, environment, theme];
            process.stdout.write(JSON.stringify({
              boot: root.classList.contains("login-boot"),
              splashRemoved: splash.removed,
              splashLeavingAdds: splash.classList.additions("is-leaving"),
              revealedAdds: wrap.classList.additions("is-revealing"),
              interactive: shielded.every((element) => !element.hasAttribute("inert") && !element.hasAttribute("aria-hidden")),
              accountFocuses: account.focusCount,
              volRequested: windowObject.__optionhelperVolInRequested === true,
              volCalls,
              replacedUrl,
              scheduledDelays: timers.map((timer) => timer.delay),
            }));
            """
        ).replace("__SOURCE__", source).replace("__REDUCED_MOTION__", json.dumps(reduced_motion)).replace("__VOL_READY__", json.dumps(vol_ready))
        completed = subprocess.run(
            [str(NODE), "-e", harness],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_startup_script_releases_login_when_the_module_graph_never_runs(self) -> None:
        state = self.run_startup_only(reduced_motion=False, vol_ready=False)

        self.assertFalse(state["boot"])
        self.assertEqual(state["splashRemoved"], 1)
        self.assertEqual(state["splashLeavingAdds"], 1)
        self.assertEqual(state["revealedAdds"], 1)
        self.assertTrue(state["interactive"])
        self.assertEqual(state["accountFocuses"], 1)
        self.assertTrue(state["volRequested"])
        self.assertEqual(state["volCalls"], 0)
        self.assertIn(1833, state["scheduledDelays"])
        self.assertIn(2600, state["scheduledDelays"])
        self.assertNotIn("app_startup", str(state["replacedUrl"]))

    def test_normal_launch_calls_the_vol_surface_once_even_with_the_watchdog(self) -> None:
        state = self.run_startup_only(reduced_motion=False, vol_ready=True)

        self.assertEqual(state["splashLeavingAdds"], 1)
        self.assertEqual(state["splashRemoved"], 1)
        self.assertEqual(state["revealedAdds"], 1)
        self.assertEqual(state["volCalls"], 1)

    def test_reduced_motion_reveals_login_synchronously_without_scheduling_splash_exit(self) -> None:
        state = self.run_startup_only(reduced_motion=True, vol_ready=True)

        self.assertFalse(state["boot"])
        self.assertEqual(state["splashRemoved"], 1)
        self.assertEqual(state["splashLeavingAdds"], 0)
        self.assertEqual(state["revealedAdds"], 1)
        self.assertTrue(state["interactive"])
        self.assertEqual(state["accountFocuses"], 1)
        self.assertTrue(state["volRequested"])
        self.assertEqual(state["volCalls"], 1)
        self.assertEqual(state["scheduledDelays"], [])


if __name__ == "__main__":
    unittest.main()
