"""Focused contracts for continuous login handoff and chat send feedback."""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"
LOGIN = (FRONTEND / "login" / "login.js").read_text(encoding="utf-8")
WORKSPACE = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
STYLES = (FRONTEND / "shared" / "styles.css").read_text(encoding="utf-8")
PAGES = [
    (FRONTEND / "optchat" / "index.html").read_text(encoding="utf-8"),
    (FRONTEND / "optdesk" / "index.html").read_text(encoding="utf-8"),
]


class WorkspaceSendFeedbackTests(unittest.TestCase):
    def test_login_hands_off_to_workspace_with_a_reduced_motion_safe_transition(self) -> None:
        self.assertIn("optionhelper-workspace-enter", LOGIN)
        self.assertIn("login-leaving", LOGIN)
        self.assertIn("location.assign(\"/optchat\")", LOGIN)
        self.assertIn("workspace-entering", WORKSPACE)
        self.assertIn("prefers-reduced-motion: reduce", STYLES)
        self.assertIn("@keyframes login-workspace-leave", STYLES)
        self.assertIn("@keyframes workspace-enter", STYLES)

    def test_send_feedback_appears_before_the_request_and_uses_the_existing_shared_composer(self) -> None:
        self.assertIn("appendPendingMessage", WORKSPACE)
        self.assertIn("setComposerSending", WORKSPACE)
        self.assertIn("pendingMessage = appendPendingMessage(submittedContent)", WORKSPACE)
        self.assertLess(
            WORKSPACE.index("pendingMessage = appendPendingMessage(submittedContent)"),
            WORKSPACE.index("const response = await request(`/api/tasks/"),
        )
        self.assertIn("pendingMessage?.remove()", WORKSPACE)
        self.assertIn("if (!messagePersisted)", WORKSPACE)
        self.assertIn("input.value = submittedContent", WORKSPACE)
        self.assertIn("aria-busy", WORKSPACE)
        self.assertIn("textContent = content", WORKSPACE)
        for page in PAGES:
            self.assertIn('class="stop-square"', page)

    def test_generating_state_keeps_the_send_control_visually_present(self) -> None:
        self.assertIn(".send-control.is-generating:disabled", STYLES)
        self.assertIn(".message--pending", STYLES)
        self.assertIn("composer-stop-in", STYLES)


if __name__ == "__main__":
    unittest.main()
