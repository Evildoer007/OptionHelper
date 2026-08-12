"""Contracts for restrained motion adopted from the reviewed UI references."""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
FRONTEND = PROJECT / "products" / "app" / "frontend"
WORKSPACE = (FRONTEND / "optchat" / "optchat.js").read_text(encoding="utf-8")
STYLES = "\n".join(
    (FRONTEND / "shared" / filename).read_text(encoding="utf-8")
    for filename in ("styles.css", "refinement.css")
)


class FrontendMotionComponentTests(unittest.TestCase):
    def test_module_tabs_measure_the_active_control_for_a_sliding_indicator(self) -> None:
        self.assertIn("syncModuleTabIndicator", WORKSPACE)
        self.assertIn("active.offsetLeft", WORKSPACE)
        self.assertIn("active.offsetWidth", WORKSPACE)
        self.assertIn("desk-module-tabs__indicator", STYLES)

    def test_thinking_indicator_only_exists_while_a_real_request_is_pending(self) -> None:
        self.assertIn("appendThinkingIndicator", WORKSPACE)
        self.assertIn("thinkingMessage = appendThinkingIndicator()", WORKSPACE)
        self.assertIn("thinkingMessage?.remove()", WORKSPACE)
        self.assertIn("message--thinking", STYLES)
        self.assertIn("thinking-orbs", STYLES)

    def test_processing_beam_is_limited_to_the_existing_composer_and_respects_motion_preferences(self) -> None:
        self.assertIn('form.classList.toggle("is-generating", sending)', WORKSPACE)
        self.assertIn("markComposerSent", WORKSPACE)
        self.assertIn(".composer form.is-generating", STYLES)
        self.assertIn("composer-border-beam", STYLES)
        self.assertIn("composer-success-check", STYLES)
        self.assertIn("prefers-reduced-motion: reduce", STYLES)


if __name__ == "__main__":
    unittest.main()
