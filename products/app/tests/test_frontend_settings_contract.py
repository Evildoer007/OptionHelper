"""Regression checks for the first-run settings and product-owned choices."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "frontend"
PROJECT = Path(__file__).resolve().parents[3]


class FrontendSettingsContractTests(unittest.TestCase):
    def test_first_run_copy_explains_the_required_model_setup(self) -> None:
        page = (ROOT / "settings" / "index.html").read_text(encoding="utf-8")
        for expected in (
            "首次使用只需选择模型服务",
            "OpenAI兼容或自定义",
            "高级选项：Base URL与模型名",
            "测试连接",
            "保存模型设置",
            "高级导出位置标识",
            "这里不是Finder路径选择器",
        ):
            self.assertIn(expected, page)
        self.assertNotIn("Provider名称", page)
        self.assertNotIn("受控目录服务签发", page)

    def test_tenant_credentials_are_admin_managed_and_never_browser_selected(self) -> None:
        page = (ROOT / "settings" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "settings" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("data-admin-only", page)
        self.assertIn("data-user-model-state", page)
        self.assertIn("canManageModel", script)
        self.assertIn("credential_configured", script)
        self.assertNotIn("secret_ref", script)

    def test_ifind_connection_result_uses_the_host_detail_and_status(self) -> None:
        script = (ROOT / "settings" / "settings.js").read_text(encoding="utf-8")
        self.assertIn('value.connection.detail || "iFind连接测试未返回说明。"', script)
        self.assertIn('value.connection.status !== "available"', script)
        self.assertNotIn('value.connection.next_step || "数据服务暂不可用。"', script)

    def test_visible_identity_words_are_neutral(self) -> None:
        page = (ROOT / "login" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name="account"', page)
        self.assertIn('name="password"', page)
        self.assertIn('name="remember"', page)
        self.assertNotIn('name="role"', page)
        self.assertNotIn("销售用户", page)
        self.assertNotIn("产品团队管理员", page)

    def test_first_party_choices_have_keyboard_and_state_contracts(self) -> None:
        shared = (ROOT / "shared" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "shared" / "styles.css").read_text(encoding="utf-8")
        refinement = (ROOT / "shared" / "refinement.css").read_text(encoding="utf-8")
        for expected in ("ArrowDown", "ArrowUp", "Home", "End", "Escape", 'role", "listbox'):
            self.assertIn(expected, shared)
        self.assertIn("label.htmlFor = trigger.id", shared)
        for expected in (".choice-menu", ".choice-option[aria-selected=\"true\"]", ".choice-trigger:disabled", ".choice[data-open=\"true\"]"):
            self.assertIn(expected, styles)
        self.assertIn('.choice-native[aria-invalid="true"]', styles)
        self.assertIn('.choice-trigger[aria-invalid="true"]', refinement)
        self.assertIn('trigger.setAttribute("aria-describedby"', shared)
        self.assertIn('trigger.setAttribute("aria-invalid"', shared)
        self.assertIn('else if (!open && focus)', shared)
        self.assertIn('trigger.focus()', shared)
        for page in ("optchat/index.html", "optdesk/index.html", "settings/index.html"):
            self.assertIn("data-choice", (ROOT / page).read_text(encoding="utf-8"), page)
        self.assertNotIn("data-choice", (ROOT / "login" / "index.html").read_text(encoding="utf-8"))

    def test_module_bridge_enhances_every_module_select(self) -> None:
        bridge = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        self.assertIn("installChoiceControls", bridge)
        self.assertIn('document.querySelectorAll("select")', bridge)
        self.assertIn("oh-choice__menu", bridge)
        self.assertIn("label.htmlFor = trigger.id", bridge)
        self.assertIn('.oh-choice__native[aria-invalid="true"]', bridge)

        desk = (ROOT / "optdesk" / "optdesk.js").read_text(encoding="utf-8")
        hosted_styles = (ROOT / "shared" / "module-host.css").read_text(encoding="utf-8")
        self.assertNotIn('select:not([multiple])', desk)
        self.assertNotIn("enhanceSelects(doc)", desk)
        self.assertIn('.choice-trigger[aria-invalid="true"]', hosted_styles)
        self.assertIn('.choice-option[aria-selected="true"]', hosted_styles)

    def test_module_choice_discovery_cannot_mutate_already_wrapped_controls(self) -> None:
        bridge = (PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        self.assertIn('if (select.closest("[data-oh-choice]")) return;', bridge)
        self.assertIn("record.addedNodes.forEach", bridge)
        self.assertIn('node.matches("select")', bridge)
        self.assertNotIn('new MutationObserver(() => document.querySelectorAll("select").forEach(enhanceSelect))', bridge)

        desk = (ROOT / "optdesk" / "optdesk.js").read_text(encoding="utf-8")
        self.assertNotIn("enhanceSelects", desk)
        self.assertNotIn("dataset.choice", desk)


if __name__ == "__main__":
    unittest.main()
