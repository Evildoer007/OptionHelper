"""Regression gates for the native macOS text-editing responder chain."""

from __future__ import annotations

from pathlib import Path
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]
MACOS_ROOT = APP_ROOT / "desktop" / "macos"


class MacOSPasteShellTests(unittest.TestCase):
    def test_swift_shell_exposes_edit_menu_and_responder_fallback(self) -> None:
        source = (MACOS_ROOT / "OptionHelperApp.swift").read_text(encoding="utf-8")
        for token in (
            'let editMenu = NSMenu(title: "编辑")',
            '#selector(NSResponder.paste(_:))',
            'NSApp.sendAction(action, to: nil, from: self)',
            'window.makeFirstResponder(view)',
        ):
            self.assertIn(token, source)

    def test_objective_c_fallback_matches_swift_shell(self) -> None:
        source = (MACOS_ROOT / "OptionHelperApp.m").read_text(encoding="utf-8")
        for token in (
            '[[NSMenu alloc] initWithTitle:@"编辑"]',
            '@selector(paste:)',
            '[NSApp sendAction:action to:nil from:self]',
            '[self.window makeFirstResponder:webView]',
        ):
            self.assertIn(token, source)


if __name__ == "__main__":
    unittest.main()
