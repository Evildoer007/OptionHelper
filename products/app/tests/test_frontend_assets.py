"""Static front-end resource contract for the AppServer whitelist."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from capability_fixture import capability_root


ROOT = Path(__file__).resolve().parents[1] / "frontend"
PAGES = ("login", "optchat", "optdesk", "settings")
PROJECT = Path(__file__).resolve().parents[3]
MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")


class FrontendAssetTests(unittest.TestCase):
    def test_pages_have_independent_html_entries(self) -> None:
        for page in PAGES:
            entry = ROOT / page / "index.html"
            self.assertTrue(entry.is_file(), entry)
            content = entry.read_text(encoding="utf-8")
            self.assertIn('/app/frontend/shared/styles.css', content)
            self.assertIn('/app/frontend/shared/refinement.css', content)
            self.assertNotIn("<style", content)

    def test_executable_pages_do_not_embed_javascript(self) -> None:
        for page in PAGES:
            content = (ROOT / page / "index.html").read_text(encoding="utf-8")
            script_name = "login.js" if page == "login" else f"{page}.js"
            self.assertIn(f'/app/frontend/{page}/{script_name}', content)
            self.assertNotRegex(content, re.compile(r"<script(?![^>]*\bsrc=)", re.IGNORECASE))

    def test_static_resource_references_use_whitelisted_prefix(self) -> None:
        for asset in ROOT.rglob("*"):
            if asset.suffix not in {".html", ".js", ".css"}:
                continue
            content = asset.read_text(encoding="utf-8")
            self.assertNotIn('"/frontend/', content, asset)
            self.assertNotIn("'/frontend/", content, asset)
            for reference in re.findall(r'(?:(?:src=)|(?:from\s+))["\'](/[^"\']+)', content):
                self.assertTrue(reference.startswith("/app/frontend/") or reference.startswith("/app/assets/") or reference.startswith("/capability/"), f"{asset}: {reference}")

    def test_branded_entries_use_the_declared_capability_icon_path(self) -> None:
        settings = (ROOT / "settings" / "index.html").read_text(encoding="utf-8")
        login = (ROOT / "login" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/capability/assets/icons/optionhelper-logo.svg", settings)
        self.assertIn('class="login-logo"', login)
        for page in ("login", "optchat", "optdesk", "settings"):
            content = (ROOT / page / "index.html").read_text(encoding="utf-8")
            self.assertIn("/app/frontend/shared/theme-bootstrap.js", content)
            self.assertIn("/app/frontend/shared/theme.js", content)

    def test_workspace_entries_do_not_repeat_the_product_brand(self) -> None:
        expected = "/capability/assets/icons/optionhelper-logo.svg"
        for page in ("optchat", "optdesk"):
            self.assertNotIn(expected, (ROOT / page / "index.html").read_text(encoding="utf-8"))

    def test_desk_mounts_registered_capability_pages_without_copies(self) -> None:
        content = (ROOT / "optchat" / "optchat.js").read_text(encoding="utf-8")
        desk_entry = (ROOT / "optdesk" / "optdesk.js").read_text(encoding="utf-8")
        self.assertIn('startWorkspace("desk")', desk_entry)
        self.assertIn("/capability/assets/pages/", content)
        self.assertIn("datafetcher", content)
        self.assertIn("payoffer", content)
        self.assertIn("pricer", content)
        self.assertIn("backtester", content)
        self.assertIn("reporter", content)
        self.assertIn("/app/frontend/shared/module-host.css", content)
        self.assertFalse(any((ROOT / module).exists() for module in ("datafetcher", "payoffer", "pricer", "backtester", "reporter")))

    def test_five_module_pages_use_one_host_bridge(self) -> None:
        bridge = PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js"
        self.assertTrue(bridge.is_file())
        self.assertIn("X-OptionHelper-Module-Context", bridge.read_text(encoding="utf-8"))
        for module in MODULES:
            page = PROJECT / "modules" / module / "page" / f"{module}.html"
            self.assertIn('../module-host-bridge.js', page.read_text(encoding="utf-8"), page)

    def test_embedded_bridge_is_the_signed_host_scope_implementation(self) -> None:
        source = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        # The checked-in App bundle is a prior immutable candidate.  Exercise
        # the verified Capability built from the current source instead: this
        # is the exact tree that development, tests, and final packaging use.
        embedded = (capability_root() / "assets" / "pages" / "module-host-bridge.js").read_text(encoding="utf-8")
        self.assertEqual(embedded, source)

    def test_built_capability_keeps_reporter_delivery_controls_in_sync(self) -> None:
        """A rebuild must carry the reviewed Reporter control states into App assets."""
        source_root = PROJECT / "modules" / "reporter" / "page"
        built_root = capability_root() / "assets" / "pages" / "reporter"
        for filename in ("reporter.html", "reporter.css", "reporter.js"):
            self.assertEqual(
                (built_root / filename).read_bytes(),
                (source_root / filename).read_bytes(),
                f"Capability重建后Reporter资产漂移：{filename}",
            )
        css = (built_root / "reporter.css").read_text(encoding="utf-8")
        for token in (
            "appearance:none",
            ":hover",
            ":focus-visible",
            ":checked",
            ":disabled",
            "aria-invalid",
            "grid-template-columns:16px minmax(0,1fr)",
        ):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
