"""Focused accessibility and continuity contracts for the App workspace shell."""

from __future__ import annotations

from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
CORE_BRIDGE = Path(__file__).resolve().parents[3] / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js"


class FrontendAccessibilityRefinementTests(unittest.TestCase):
    def test_closed_report_drawer_is_hidden_from_assistive_technology(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('id="task-context" aria-label="报告库" aria-hidden="true" inert', html)

        script = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const contextPanel = shell.querySelector("#task-context")', script)
        self.assertIn('contextPanel.setAttribute("aria-hidden", String(!open))', script)
        self.assertIn("contextPanel.inert = !open", script)

    def test_module_tabs_and_floating_assistant_expose_relationships(self) -> None:
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertEqual(html.count('aria-controls="module-mount"'), 5)
            self.assertIn('id="module-mount" role="tabpanel"', html)
            self.assertIn('aria-controls="assistant-panel"', html)
            self.assertIn('id="assistant-panel" role="dialog" aria-modal="false"', html)

    def test_empty_task_mode_switch_migrates_transient_state(self) -> None:
        script = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        self.assertIn("migrateTransient", script)
        self.assertIn("readTransient(fromTaskId)", script)
        self.assertIn('migrateTransient("new", task.task_id)', script)
        self.assertIn("transientKey(taskId)", script)

    def test_small_auxiliary_copy_is_readable(self) -> None:
        styles = (FRONTEND / "shared" / "refinement.css").read_text(encoding="utf-8")
        for selector in (
            ".conversation-starter span { font-size: 12px; }",
            ".conversation-start__note { margin-top: 12px; color: var(--muted); font-size: 12px; }",
            ".assistant-panel__head span { color: var(--muted); font-size: 12px; }",
        ):
            self.assertIn(selector, styles)
        self.assertIn(".message__body { line-height: 1.65; }", styles)

    def test_module_choice_closes_when_it_becomes_disabled(self) -> None:
        bridge = CORE_BRIDGE.read_text(encoding="utf-8")
        self.assertIn("if (trigger.disabled) setChoiceOpen(choice, false);", bridge)


if __name__ == "__main__":
    unittest.main()
