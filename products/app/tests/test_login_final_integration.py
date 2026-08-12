"""Focused contracts for the approved login surface and startup boundary."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"
LOGIN_HTML = FRONTEND / "login" / "index.html"
LOGIN_JS = FRONTEND / "login" / "login.js"
LOGIN_STARTUP_JS = FRONTEND / "login" / "login-startup.js"
VOL_SURFACE_JS = FRONTEND / "shared" / "vol-surface.js"
SHARED_JS = FRONTEND / "shared" / "app.js"
SHARED_CSS = FRONTEND / "shared" / "styles.css"
LOGO = PROJECT / "assets" / "icons" / "optionhelper-logo.svg"


class LoginFinalIntegrationTests(unittest.TestCase):
    def test_approved_copy_and_controls_are_present_without_inline_code(self) -> None:
        page = LOGIN_HTML.read_text(encoding="utf-8")
        for text in (
            "登录OptionHelper",
            "本机开发模式",
            "账号",
            "工号或邮箱",
            "密码",
            "忘记密码",
            "记住此设备",
            "登录后任务与报告仅保存在此Mac。",
        ):
            self.assertIn(text, page)
        for control in ("account", "password", "remember"):
            self.assertRegex(page, rf'name="{control}"')
        self.assertNotIn("<style", page.lower())
        self.assertNotRegex(page, re.compile(r"<script(?![^>]*\bsrc=)", re.IGNORECASE))
        self.assertNotIn("<select", page.lower())

    def test_splash_uses_its_own_complete_drawable_logo(self) -> None:
        page = LOGIN_HTML.read_text(encoding="utf-8")
        splash = page.split('<div id="login-splash"', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("optionhelper-logo.svg#", splash)
        self.assertIn('<linearGradient id="login-splash-red-gradient"', splash)
        self.assertIn('class="login-splash__disc"', splash)
        self.assertIn('class="login-splash__payoff"', splash)
        self.assertIn('class="login-splash__dot login-splash__dot--start"', splash)
        self.assertIn('class="login-splash__dot login-splash__dot--end"', splash)
        self.assertIn('class="login-splash__arc"', splash)
        self.assertIn('class="login-splash__candidates"', splash)
        self.assertIn('class="login-splash__sweep"', splash)
        self.assertIn('class="login-splash__ticks"', splash)
        self.assertIn('class="login-splash__glow"', splash)
        self.assertIn('class="login-splash__word login-splash__word--option"', splash)
        self.assertIn('class="login-splash__word login-splash__word--helper"', splash)
        self.assertIn('class="login-splash__rule"', splash)
        self.assertIn("Option", splash)
        self.assertIn("Helper", splash)

        self.assertIn('class="login-logo"', page)
        self.assertIn('id="login-header-red-gradient"', page)
        self.assertIn('class="login-logo__word"', page)

    def test_login_background_is_an_external_reduced_motion_safe_surface(self) -> None:
        page = LOGIN_HTML.read_text(encoding="utf-8")
        script = LOGIN_JS.read_text(encoding="utf-8")
        surface = VOL_SURFACE_JS.read_text(encoding="utf-8")
        styles = SHARED_CSS.read_text(encoding="utf-8")

        self.assertIn('id="login-vol-surface"', page)
        self.assertIn('initializeVolSurface', script)
        self.assertIn('window.__volIn = () => volSurface?.reveal()', script)
        self.assertIn('window.__volIn()', script)
        self.assertIn('Float32Array', surface)
        self.assertIn('Math.min(window.devicePixelRatio || 1, 1.25)', surface)
        self.assertIn('visibilitychange', surface)
        self.assertIn('#login-vol-surface', styles)
        self.assertIn('prefers-reduced-motion: reduce', surface)

    def test_login_uses_shared_request_layer_and_exact_payload(self) -> None:
        script = LOGIN_JS.read_text(encoding="utf-8")
        self.assertIn('request("/api/auth/login"', script)
        self.assertIn("safeJson(", script)
        self.assertIn('allowSecrets: new Set(["password"])', script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("enhanceSelects", script)
        for field in ("account", "password", "remember"):
            self.assertIn(field, script)

    def test_shared_json_guard_has_narrow_secret_allowlist(self) -> None:
        script = SHARED_JS.read_text(encoding="utf-8")
        self.assertIn("allowSecrets", script)
        self.assertIn("secretKeys.has(key) && !allowSecrets.has(key)", script)

    def test_splash_uses_native_startup_parameter_not_route_hash(self) -> None:
        page = LOGIN_HTML.read_text(encoding="utf-8")
        script = LOGIN_STARTUP_JS.read_text(encoding="utf-8")
        self.assertIn('/app/frontend/login/login-startup.js', page)
        self.assertLess(page.index('/app/frontend/login/login-startup.js'), page.index('/app/frontend/login/login.js'))
        self.assertIn('launchUrl.searchParams.get("app_startup")', script)
        self.assertNotIn("#nosplash", script)
        self.assertNotIn("location.hash", script)
        self.assertIn('timer = setTimeout(finish, 1833)', script)
        self.assertIn('setTimeout(finish, 2600)', script)
        self.assertIn('root.classList.remove("login-boot")', script)
        swift = (PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.swift").read_text(encoding="utf-8")
        objc = (PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.m").read_text(encoding="utf-8")
        self.assertIn("app_startup", swift)
        self.assertIn("app_startup", objc)
        self.assertIn("UUID().uuidString", swift)
        self.assertIn("[NSUUID UUID].UUIDString", objc)
        self.assertIn("WKUserScript", swift)
        self.assertIn(".atDocumentStart", swift)
        self.assertIn("if (location.pathname === '/') document.documentElement.classList.add('login-boot')", swift)
        self.assertIn("WKUserScript", objc)
        self.assertIn("WKUserScriptInjectionTimeAtDocumentStart", objc)
        self.assertIn("if (location.pathname === '/') document.documentElement.classList.add('login-boot')", objc)

    def test_blank_local_login_is_not_blocked_and_storage_failure_cannot_lock_the_form(self) -> None:
        page = LOGIN_HTML.read_text(encoding="utf-8")
        script = LOGIN_JS.read_text(encoding="utf-8")

        for selector in ('class="login-theme"', 'class="login-environment"', 'class="login-wrap"'):
            element = page.split(selector, 1)[1].split(">", 1)[0]
            self.assertIn("inert", element)
            self.assertIn('aria-hidden="true"', element)
        self.assertIn("function saveSessionValue(key, value)", script)
        startup = LOGIN_STARTUP_JS.read_text(encoding="utf-8")
        self.assertIn('element.removeAttribute("aria-hidden")', startup)
        self.assertIn('element.setAttribute("aria-hidden", "true")', startup)
        self.assertNotIn("if (!account.value.trim())", script)
        self.assertNotIn("if (!password.value)", script)
        self.assertIn("if (hasAccount !== hasPassword)", script)

    def test_splash_timing_and_login_states_live_in_shared_styles(self) -> None:
        styles = SHARED_CSS.read_text(encoding="utf-8")
        for token in (
            ".login-page",
            ".login-card",
            ".login-field.is-invalid",
            ".login-eye:hover",
            ".login-submit:disabled",
            "0.458s",
            "2.083s",
            "1.833s",
            "visibility: hidden",
        ):
            self.assertIn(token, styles)

    def test_distributed_logo_text_is_path_geometry(self) -> None:
        logo = LOGO.read_text(encoding="utf-8")
        self.assertNotIn("<text", logo)
        self.assertNotIn("Avenir", logo)
        for component in (
            "optionhelper-disc",
            "optionhelper-payoff",
            "optionhelper-dot-start",
            "optionhelper-dot-end",
            "optionhelper-arc",
            "optionhelper-word-option",
            "optionhelper-word-helper",
            "optionhelper-rule",
        ):
            self.assertIn(f'id="{component}"', logo)


if __name__ == "__main__":
    unittest.main()
