"""Focused UI contracts for product selection, controls, and the persistent rail."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"


class ModuleProductSelectionTests(unittest.TestCase):
    def test_three_compute_pages_start_without_a_selected_product(self) -> None:
        pages = {
            "payoffer": PROJECT / "modules" / "payoffer" / "page" / "payoffer.html",
            "pricer": PROJECT / "modules" / "pricer" / "page" / "pricer.html",
            "backtester": PROJECT / "modules" / "backtester" / "page" / "backtester.html",
        }
        for module, path in pages.items():
            with self.subTest(module=module):
                html = path.read_text(encoding="utf-8")
                self.assertRegex(
                    html,
                    r'<select[^>]+(?:productSelect|productId)[^>]*>\s*<option value="">请选择产品</option>',
                )
                self.assertIn("new Option('请选择产品','')", html)
                if module == "payoffer":
                    self.assertIn("if (!state.product) {\n          renderIdentity();", html)

    def test_catalog_fallback_is_empty_and_never_forces_2_1(self) -> None:
        for path in (
            PROJECT / "modules" / "payoffer" / "page" / "payoffer.html",
            PROJECT / "modules" / "pricer" / "page" / "pricer.html",
            PROJECT / "modules" / "backtester" / "page" / "backtester.html",
        ):
            html = path.read_text(encoding="utf-8")
            start = re.search(r"async function (?:loadCatalog|catalog)\(\)", html)
            self.assertIsNotNone(start, path)
            body = html[start.start():start.start() + 1800]
            self.assertNotRegex(body, r"['\"]2\.1['\"]")
            self.assertRegex(body, r"(?:select|\$\(['\"]productSelect['\"]\))\.value\s*=.*(?:''|\"\")")
        pricer = (PROJECT / "modules" / "pricer" / "page" / "pricer.html").read_text(encoding="utf-8")
        initial_catalog = pricer.rsplit("catalog().then", 1)[1]
        self.assertNotIn("loadCsi500Demo", initial_catalog)


class ReporterControlTests(unittest.TestCase):
    def test_delivery_choices_are_custom_red_gold_white_controls(self) -> None:
        css = (PROJECT / "modules" / "reporter" / "page" / "reporter.css").read_text(encoding="utf-8")
        required = (
            ".settings-panel .choice input",
            "appearance:none",
            'input[type="radio"]',
            'input[type="checkbox"]',
            ":hover",
            ":focus-visible",
            ":checked",
            ":disabled",
            '[aria-invalid="true"]',
            '[data-visual-state="hover"]',
            '[data-visual-state="focus"]',
            "--red",
            "--gold",
        )
        for token in required:
            self.assertIn(token, css)


class PersistentWorkspaceRailTests(unittest.TestCase):
    def test_workspace_rail_has_no_brand_asset_in_either_mode(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="workspace-rail workspace-rail--unbranded"', html)
            self.assertNotIn("workspace-brand", html)
            self.assertNotIn("optionhelper-logo.svg", html)
            self.assertNotIn("optionhelper-mark.svg", html)

    def test_mode_specific_rules_do_not_reposition_persistent_rail_controls(self) -> None:
        css = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        self.assertIn(".workspace-rail--unbranded", css)
        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr) auto", css)
        self.assertIn('data-has-desk="false"] .workspace-rail--unbranded', css)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", css)
        self.assertIn("padding-top: 20px", css)
        self.assertIn("background: var(--red-rail)", css)
        for selector in ("rail-mode-area", "rail-new-task"):
            self.assertNotRegex(
                css,
                rf'\[data-mode="(?:chat|desk)"\][^{{}}]*\.{selector}\s*\{{[^}}]*(?:width|height|padding|margin|top|right|bottom|left|transform):',
            )

    def test_compact_rail_keeps_one_continuous_transparent_new_task_action(self) -> None:
        css = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        for expected in (
            "grid-template-rows: minmax(0, 1fr)",
            "width: 100%",
            "margin: 10px 0 0",
            "border: 0",
            "background: transparent",
            "border-radius: 0",
        ):
            self.assertIn(expected, css)

    def test_workspace_keeps_the_conversation_as_the_primary_column(self) -> None:
        css = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        for expected in (
            "--rail-width: 248px",
            "--conversation-column-max: 1040px",
            ".rail-section-label",
            "grid-template-columns: 15px minmax(0, 1fr) auto",
            "width: min(var(--conversation-column-max), calc(100% - 18px))",
        ):
            self.assertIn(expected, css)

    def test_rail_has_a_compact_drag_range_and_a_single_edge_control(self) -> None:
        app = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn('minimum: 176, maximum: 384, fallback: 248', app)
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="rail-collapse-toggle"', html)
            self.assertNotIn('<rect x="1.2" y="1"', html)

    def test_messages_use_chat_style_bubbles_without_identity_labels(self) -> None:
        app = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        chat = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        render_messages = app[app.index("export function renderMessages"):app.index("export function renderReports")]
        pending_message = chat[chat.index("const appendPendingMessage"):chat.index("const appendThinkingIndicator")]
        self.assertNotIn("message__identity", render_messages)
        self.assertNotIn("message__identity", pending_message)

    def test_motion_uses_the_three_referenced_patterns_without_new_dependencies(self) -> None:
        css = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        chat = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        for expected in (
            "--oh-panel-open-duration",
            "transitions.dev panel-reveal",
            "transitions.dev input-clear-dissolve",
            "transitions.dev tabs-sliding",
            "composer-border-beam",
            ".thinking-orbs",
        ):
            self.assertIn(expected, css)
        self.assertIn("dissolveComposerInput", chat)


if __name__ == "__main__":
    unittest.main()
