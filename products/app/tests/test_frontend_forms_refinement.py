"""Focused contracts for the settings form and product-owned choice controls."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1] / "frontend"


class FrontendFormsRefinementTests(unittest.TestCase):
    def test_every_settings_control_explains_purpose_example_and_default(self) -> None:
        page = (ROOT / "settings" / "index.html").read_text(encoding="utf-8")
        specs = re.findall(r'<div class="field[^>]*data-field-spec[^>]*>(.*?)</div>', page, re.DOTALL)
        self.assertGreaterEqual(len(specs), 7)
        for spec in specs:
            self.assertIn("data-purpose", spec)
            self.assertIn("data-example", spec)
            self.assertIn("data-default", spec)
        self.assertIn('class="advanced-options__body"', page)

    def test_settings_errors_are_linked_to_the_exact_control(self) -> None:
        script = (ROOT / "settings" / "settings.js").read_text(encoding="utf-8")
        self.assertIn('error.id = `${control.id || control.name}-error`', script)
        self.assertIn('error.setAttribute("role", "alert")', script)
        self.assertIn('control.setAttribute("aria-describedby"', script)
        self.assertIn('control.addEventListener("input", () => clearInvalid(control))', script)

    def test_invalid_advanced_field_is_revealed_before_focus(self) -> None:
        script = (ROOT / "settings" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("details.open = true", script)
        self.assertIn("focusFirstInvalid(modelForm)", script)

    def test_data_settings_navigation_is_permission_gated(self) -> None:
        page = (ROOT / "settings" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "settings" / "settings.js").read_text(encoding="utf-8")
        self.assertIn('data-settings-data-link hidden', page)
        self.assertIn('querySelector("[data-settings-data-link]").hidden = false', script)

    def test_choice_visual_states_are_explicit(self) -> None:
        styles = (ROOT / "shared" / "styles.css").read_text(encoding="utf-8")
        for state in (
            '.choice:hover .choice-trigger',
            '.choice:focus-within .choice-trigger',
            '.choice[data-open="true"] .choice-trigger',
            '.choice-option[aria-selected="true"]',
            '.choice-trigger:disabled',
            '.choice-trigger[aria-invalid="true"]',
        ):
            self.assertIn(state, styles)

    def test_login_controls_have_explicit_labels_and_help(self) -> None:
        page = (ROOT / "login" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "login" / "login.js").read_text(encoding="utf-8")
        for control in ("login-account", "login-password"):
            self.assertIn(f'for="{control}"', page)
            self.assertIn(f'id="{control}"', page)
        self.assertIn('id="login-remember"', page)
        self.assertIn('aria-describedby="login-account-tip"', page)
        self.assertIn('aria-describedby="login-password-tip"', page)
        self.assertNotIn("haoran", page)
        self.assertIn('request("/api/auth/login"', script)
        self.assertNotIn("enhanceSelects", script)


if __name__ == "__main__":
    unittest.main()
