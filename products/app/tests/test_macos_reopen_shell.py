"""Source contracts for reopening the already-running macOS App window."""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
SWIFT = PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.swift"
OBJC = PROJECT / "products" / "app" / "desktop" / "macos" / "OptionHelperApp.m"


class MacOSReopenShellTests(unittest.TestCase):
    def test_both_native_shells_restore_a_closed_window_without_restarting_backend(self) -> None:
        for source in (SWIFT.read_text(encoding="utf-8"), OBJC.read_text(encoding="utf-8")):
            self.assertIn("applicationShouldHandleReopen", source)
            self.assertIn("hasVisibleWindows", source)
            self.assertIn("makeKeyAndOrderFront", source)
            self.assertTrue(
                "activateIgnoringOtherApps" in source or "activate(ignoringOtherApps" in source,
            )

    def test_reopen_path_is_separate_from_backend_lifecycle(self) -> None:
        sources = (
            (SWIFT.read_text(encoding="utf-8"), "private func startBackend"),
            (OBJC.read_text(encoding="utf-8"), "- (void)startBackend"),
        )
        for source, next_method in sources:
            reopen = source.split("applicationShouldHandleReopen", 1)[1].split(next_method, 1)[0]
            self.assertNotIn("startBackend", reopen)
            self.assertNotIn("terminate", reopen)


if __name__ == "__main__":
    unittest.main()
