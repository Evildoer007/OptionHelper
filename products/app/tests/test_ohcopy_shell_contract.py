"""OHCopy壳层关键交互不得在App适配中回退。"""

from __future__ import annotations

from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class OHCopyShellContractTests(unittest.TestCase):
    def test_chat_composers_keep_model_picker_without_settings_link(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            form = html.split('id="workspace-form"', 1)[1].split("</form>", 1)[0]
            self.assertIn('id="workspace-model-picker"', form)
            self.assertIn("model-picker", form)
            self.assertNotIn("设置中心", form)

    def test_desk_mode_is_hidden_until_authorized(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-has-desk="false"', html)
            self.assertIn('data-mode-switch', html)
            self.assertNotIn('button type="button" data-mode="desk"', html)
        script = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.createElement("button")', script)
        self.assertIn('deskButton.remove()', script)
        self.assertIn('shell.dataset.hasDesk', script)

    def test_desk_assistant_keeps_ohcopy_conversation_shell(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="workspace-content"', html)
            self.assertIn('class="chat-surface', html)
            self.assertIn('class="desk-surface', html)
            self.assertEqual(html.count('id="conversation-stream"'), 1)
            self.assertEqual(html.count('id="workspace-form"'), 1)
            self.assertIn("任务对话", html)
            self.assertIn("当前任务的连续对话", html)
        chat_html = (FRONTEND / "optchat" / "index.html").read_text(encoding="utf-8")
        desk_html = (FRONTEND / "optdesk" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="desk-surface" aria-label="研究模块" aria-hidden="true" inert', chat_html)
        self.assertIn('id="chat-surface" aria-label="任务对话" aria-hidden="true" inert', desk_html)
        script = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn("开始一项结构化产品研究", script)
        self.assertIn("先描述研究目标，随后可进入条款、估值、回测和报告。", script)
        self.assertIn("筛选候选结构", script)
        self.assertIn("设计产品条款", script)
        self.assertIn("准备估值输入", script)
        self.assertIn("建立回测方案", script)
        self.assertIn("assistantTranscript.append(stream)", workspace)
        self.assertIn("chatScrollStage.append(stream)", workspace)
        self.assertIn("chatSurface.inert", workspace)
        self.assertIn("deskSurface.inert", workspace)

    def test_desktop_desk_layout_keeps_one_content_canvas(self) -> None:
        css = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-rows: 62px minmax(0, 1fr) var(--assistant-height);", css)
        self.assertIn("--assistant-height: 0px", css)

    def test_chat_and_desk_keep_one_task_during_mode_switch(self) -> None:
        shell = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        bridge = (Path(__file__).resolve().parents[3] / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
        self.assertIn("history.pushState", workspace)
        self.assertIn('next.searchParams.set("task", currentTask.task_id)', workspace)
        self.assertNotIn("location.assign", shell)
        self.assertIn("location.replace", shell)
        self.assertIn('`?task_id=${encodeURIComponent(currentTask.task_id)}`', workspace)
        self.assertIn('hostScope = Object.freeze', bridge)
        self.assertIn('applyHostScope', bridge)
        self.assertNotIn('event.data.task_id', bridge)

    def test_mode_binding_targets_buttons_not_the_root_shell(self) -> None:
        shell = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn('querySelectorAll("button[data-mode]")', shell)
        self.assertIn('querySelectorAll("button[data-mode]")', workspace)
        self.assertNotIn('querySelectorAll("[data-mode]")', shell)

    def test_shared_composer_keyboard_contract(self) -> None:
        shell = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn('event.key !== "Enter" || event.shiftKey || event.isComposing', shell)
        self.assertIn("form.requestSubmit()", shell)
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn("bindComposerKeyboard(form)", workspace)

    def test_mode_switch_preserves_transient_workspace_state(self) -> None:
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        for expected in (
            "optionhelper.workspace.state",
            "draft",
            "chatScrollTop",
            "deskScrollTop",
            "chatScrollRatio",
            "deskScrollRatio",
            "chatAtBottom",
            "deskAtBottom",
            "assistantOpen",
            "sessionStorage",
            'window.addEventListener("popstate"',
            "desiredScrollTop",
            "restoringScroll",
            "finishModeScroll",
            "modeSwitchRevision",
            'event.propertyName !== "transform"',
        ):
            self.assertIn(expected, workspace)
        self.assertIn("desiredScrollRatio * maxScroll", workspace)
        self.assertIn("desiredAtBottom", workspace)
        self.assertIn("if (!restoringScroll) saveTransient()", workspace)

    def test_persisted_user_message_is_not_restored_as_a_draft(self) -> None:
        workspace = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn('last?.role === "user" && last.content === submittedContent', workspace)
        self.assertIn('input.value = ""', workspace)
        self.assertIn("saveTransient()", workspace)
        self.assertIn("error.body?.error?.next_step || error.body?.next_step", workspace)
        self.assertIn("任务内容已保留。", workspace)

    def test_mode_transition_is_bounded_and_respects_reduced_motion(self) -> None:
        styles = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        shared_styles = (FRONTEND / "shared" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('[data-mode="desk"] .desk-surface', styles)
        self.assertIn('[data-mode="desk"] .chat-surface', styles)
        self.assertIn("prefers-reduced-motion: reduce", styles)
        self.assertIn("overflow-anchor: none", shared_styles)


if __name__ == "__main__":
    unittest.main()
