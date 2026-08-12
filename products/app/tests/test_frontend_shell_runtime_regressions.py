"""Focused regressions for App-owned interactive frontend boundaries."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"


class FrontendShellRuntimeRegressionTests(unittest.TestCase):
    def test_mode_transition_has_a_webkit_safe_completion_path(self) -> None:
        script = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn('createTransitionScope', script)
        self.assertIn('activeModeTransition?.dispose()', script)
        self.assertIn('transition.delay(finishModeScroll, 640)', script)
        self.assertIn('transition.listen(targetSurface, "transitionend", onTransitionEnd)', script)

    def test_workspace_surfaces_are_light_and_do_not_repeat_branding(self) -> None:
        for page in ("optchat", "optdesk", "settings"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-theme-scope="fixed-light"', html)
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("workspace-brand", html)

    def test_reporter_native_choice_rows_have_stable_host_geometry(self) -> None:
        css = (FRONTEND / "shared" / "module-host.css").read_text(encoding="utf-8")
        for token in (
            ".settings-panel .choice",
            "grid-template-columns: 16px minmax(0, 1fr)",
            "column-gap: 8px",
            "min-height: 32px",
            "input[type=\"radio\"]",
            "input[type=\"checkbox\"]",
            "accent-color: var(--red, #c8102e)",
        ):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
