"""Contract tests for the shared manual theme and final login surface."""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"


class LoginThemeIntegrationTests(unittest.TestCase):
    def test_every_app_entry_resolves_theme_before_its_stylesheet(self) -> None:
        for page in ("login", "optchat", "optdesk", "settings"):
            content = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            bootstrap = content.index('/app/frontend/shared/theme-bootstrap.js')
            stylesheet = content.index('/app/frontend/shared/styles.css')
            self.assertLess(bootstrap, stylesheet, page)
            self.assertIn('id="app-favicon"', content, page)
            expected_scheme = "light dark" if page == "login" else "light"
            self.assertIn(f'meta name="color-scheme" content="{expected_scheme}"', content, page)

    def test_theme_controller_is_subscribable_and_does_not_follow_system_unless_auto(self) -> None:
        source = (FRONTEND / "shared" / "theme.js").read_text(encoding="utf-8")
        bootstrap = (FRONTEND / "shared" / "theme-bootstrap.js").read_text(encoding="utf-8")
        for token in (
            'const THEME_KEY = "oh-theme"',
            "export function onThemeChange",
            "export function currentTheme()",
            "export function setThemePreference",
            'currentThemePreference() === "auto"',
            "optionhelperTheme",
            "optionhelper-app-icon-tile-dark.svg",
        ):
            self.assertIn(token, source)
        self.assertIn('let preference = "light"', bootstrap)
        self.assertIn('root.dataset.themePref', bootstrap)

    def test_login_allows_the_local_blank_login_and_keeps_partial_credentials_specific(self) -> None:
        page = (FRONTEND / "login" / "index.html").read_text(encoding="utf-8")
        source = (FRONTEND / "login" / "login.js").read_text(encoding="utf-8")
        for token in ('role="alert"', 'aria-invalid="false"', 'data-theme-set="auto"', 'data-theme-set="light"', 'data-theme-set="dark"'):
            self.assertIn(token, page)
        for token in (
            '"账号或密码不正确"',
            '登录服务返回${error.status}',
            '"无法连接本机服务，请确认AppServer已启动。"',
            'request("/api/auth/login"',
            "safeJson(",
        ):
            self.assertIn(token, source)
        self.assertIn("if (hasAccount !== hasPassword)", source)
        self.assertNotIn("if (!account.value.trim())", source)
        self.assertNotIn("if (!password.value)", source)
        self.assertNotIn("fetch(", source)

    def test_theme_is_a_real_preference_and_packaged_icon_contract(self) -> None:
        models = (PROJECT / "products" / "app" / "backend" / "settings" / "settings_models.py").read_text(encoding="utf-8")
        server = (PROJECT / "products" / "app" / "backend" / "app_server.py").read_text(encoding="utf-8")
        service = (PROJECT / "products" / "app" / "backend" / "settings" / "settings_service.py").read_text(encoding="utf-8")
        build = (PROJECT / "packaging" / "app" / "macos" / "build_macos.py").read_text(encoding="utf-8")
        launcher = (PROJECT / "products" / "app" / "desktop" / "macos" / "backend_launcher.py").read_text(encoding="utf-8")
        for source in (models, server, service):
            self.assertIn("theme", source)
        self.assertIn('"optionhelper-app-icon-tile-dark.svg"', server)
        self.assertIn('brand_assets_root=resources / "assets" / "icons"', launcher)
        self.assertIn('authentication_mode="local-development"', launcher)
        self.assertNotIn('authentication_mode="managed"', launcher)
        self.assertIn("def copy_theme_icon_assets", build)
        self.assertIn("optionhelper-app-icon-tile-dark.icns", build)

    def test_login_surface_and_volume_background_use_theme_subscription(self) -> None:
        styles = (FRONTEND / "shared" / "styles.css").read_text(encoding="utf-8")
        surface = (FRONTEND / "shared" / "vol-surface.js").read_text(encoding="utf-8")
        self.assertIn('[data-theme="dark"] .login-page', styles)
        self.assertIn('[data-theme="dark"] .login-logo__word', styles)
        self.assertIn("onThemeChange", surface)
        self.assertNotIn("__volTheme", surface)


if __name__ == "__main__":
    unittest.main()
