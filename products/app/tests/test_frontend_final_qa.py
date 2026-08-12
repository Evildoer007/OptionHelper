"""Focused final QA contracts for App access, choices and local storage copy."""

from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"


class FrontendFinalQATests(unittest.TestCase):
    def test_user_role_has_no_visible_mode_switch_placeholder(self) -> None:
        styles = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        self.assertIn('[data-has-desk="false"] .rail-mode-area', styles)
        self.assertIn("display: none", styles)

    def test_workspace_rail_geometry_is_invariant_while_desk_keeps_original_palette(self) -> None:
        styles = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        self.assertIn("rail keeps identical geometry across modes", styles)
        self.assertIn("grid-template-rows: 62px minmax(0, 1fr) 164px;", styles)
        self.assertIn("min-block-size: 164px;", styles)
        self.assertIn('.workspace-body .workspace-shell.workspace-shell--desk[data-workspace-shell] .workspace-rail', styles)
        self.assertIn('[data-mode="chat"] .workspace-rail', styles)
        self.assertIn('[data-mode="desk"] .workspace-rail', styles)
        self.assertIn("background: #f6f7f6", styles)

    def test_settings_navigation_keeps_the_workspace_transition_small_and_explicit(self) -> None:
        shared = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn('shell.dataset.navigating = "settings"', shared)
        self.assertIn("location.href = settingsLink.href", shared)

    def test_custom_choices_use_the_explicit_form_label(self) -> None:
        shared = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        bridge = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        for source in (shared, bridge):
            self.assertIn('querySelectorAll("label[for]")', source)
            self.assertIn("htmlFor === select.id", source)
        self.assertIn('if (select.closest("[data-oh-choice]")) return;', bridge)

    def test_settings_state_the_real_local_data_lifecycle(self) -> None:
        page = (FRONTEND / "settings" / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND / "settings" / "settings.js").read_text(encoding="utf-8")
        for expected in (
            "不会散落到软件安装目录",
            "Application Support/OptionHelper/local-state",
            "卸载或删除App不会自动删除",
        ):
            self.assertIn(expected, page)
        self.assertIn("AppData/Local/OptionHelper/local-state", script)

    def test_module_host_hides_its_header_and_owns_one_choice_wrapper(self) -> None:
        bridge = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        self.assertIn("body.optionhelper-embedded .topbar { display: none !important; }", bridge)
        self.assertIn('if (select.closest("[data-oh-choice]")) return;', bridge)

    def test_embedded_module_catalog_waits_for_its_signed_context(self) -> None:
        """Initial catalog requests must not escape to the App root as GET /api/catalog."""
        bridge = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        self.assertIn("const hostedInDesk = embeddedInDesk && window.parent !== window;", bridge)
        self.assertIn("const hostContextReady = new Promise", bridge)
        self.assertIn("function waitForHostContext(signal)", bridge)
        self.assertIn("if (!context && hostedInDesk) await waitForHostContext(signal);", bridge)
        self.assertIn('return asResponse(503, {ok: false, message: "模块页面尚未收到运行上下文，请稍后重试。"});', bridge)
        self.assertIn("if (hostedInDesk) window.fetch = hostedFetch;", bridge)
        self.assertIn("acceptHostContext?.(context);", bridge)
        self.assertLess(
            bridge.index("if (hostedInDesk) window.fetch = hostedFetch;"),
            bridge.index('window.addEventListener("message"'),
        )

    def test_all_hosted_pages_load_the_bridge_before_their_module_logic(self) -> None:
        for module in ("datafetcher", "payoffer", "pricer", "backtester", "reporter"):
            page = (PROJECT / "modules" / module / "page" / f"{module}.html").read_text(encoding="utf-8")
            bridge = page.index('src="../module-host-bridge.js"')
            if module in {"payoffer", "pricer", "backtester"}:
                self.assertLess(bridge, page.index("/api/catalog"), module)
            else:
                self.assertLess(bridge, page.index(f'./{module}.js'), module)

    def test_desk_dialog_and_module_tabs_are_keyboard_safe(self) -> None:
        script = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn("assistantPanel.inert", script)
        self.assertIn("assistantToggle.focus()", script)
        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(key, script)
        self.assertIn("button.tabIndex = active ? 0 : -1", script)


if __name__ == "__main__":
    unittest.main()
