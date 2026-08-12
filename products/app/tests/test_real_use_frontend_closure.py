"""Regression contracts for task-scoped feedback and human-facing module delivery UI."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"
REPORTER_HTML = PROJECT / "modules" / "reporter" / "page" / "reporter.html"
REPORTER_CSS = PROJECT / "modules" / "reporter" / "page" / "reporter.css"
PAYOFFER_HTML = PROJECT / "modules" / "payoffer" / "page" / "payoffer.html"


class RealUseFrontendClosureTests(unittest.TestCase):
    def test_report_feedback_stays_in_the_report_drawer(self) -> None:
        script = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
        for page in ("optchat", "optdesk"):
            html = (FRONTEND / page / "index.html").read_text(encoding="utf-8")
            self.assertIn('id="report-feedback"', html)
            self.assertIn('data-report-actions', html)
        self.assertIn("const reportFeedback = document.querySelector(\"#report-feedback\")", script)
        self.assertIn("showReportFeedback", script)
        self.assertIn("当前任务还没有可用于生成报告的正式结果", script)

    def test_composer_status_is_an_overlay_in_both_modes(self) -> None:
        css = (FRONTEND / "shared" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("#workspace-status", css)
        self.assertIn("pointer-events: none", css)
        self.assertIn(".assistant-panel #workspace-status", css)
        self.assertIn("position: absolute", css)

    def test_payoffer_delivery_dialog_does_not_expose_internal_result_storage(self) -> None:
        html = PAYOFFER_HTML.read_text(encoding="utf-8")
        self.assertNotIn("result/output_payoff/{task_id}/{run_id}", html)
        self.assertNotIn('<span>ResolvedContract</span><textarea id="runtimePayoffInput"', html)
        self.assertNotIn('<span>运行标识</span><input id="runtimeRunId"', html)
        self.assertIn("结果已保存，可生成Card或Report", html)

    def test_reporter_uses_human_copy_and_stable_narrow_rail_choices(self) -> None:
        html = REPORTER_HTML.read_text(encoding="utf-8")
        css = REPORTER_CSS.read_text(encoding="utf-8")
        self.assertIn("结果选择", html)
        self.assertNotIn("RunRef选择", html)
        for token in (
            ".settings-panel .choice {",
            "display:grid",
            "grid-template-columns:16px minmax(0,1fr)",
            "column-gap:8px",
            "min-height:32px",
            "width:16px",
            "height:16px",
        ):
            self.assertIn(token, css)

    def test_report_library_uses_the_persisted_delivery_type(self) -> None:
        script = (FRONTEND / "shared" / "app.js").read_text(encoding="utf-8")
        self.assertIn("report.report_request?.output_type || report.report_request?.kind", script)
        self.assertIn('outputType === "card" ? "Card简报" : "详细报告"', script)


if __name__ == "__main__":
    unittest.main()
