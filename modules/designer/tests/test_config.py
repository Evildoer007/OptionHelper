"""Tests for the published Designer runtime configuration and Tool boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import DesignerConfigurationError, call_tool, load_designer_config
from modules.designer.design_system_builder import theme_path
from modules.designer.echarts_renderer import asset_path


class DesignerConfigTest(unittest.TestCase):
    def test_single_runtime_config_resolves_checked_in_assets(self) -> None:
        config = load_designer_config()
        self.assertTrue(config.report_theme_path.is_file())
        self.assertTrue(config.echarts_asset_path.is_file())
        self.assertTrue(config.template_path("card.html").is_file())
        self.assertTrue(config.template_path("report.html").is_file())
        self.assertIn("__CARD_BODY__", config.read_template("card.html"))
        self.assertIn("__CONTENT__", config.read_template("report.html"))
        self.assertEqual(theme_path(), config.report_theme_path)
        self.assertEqual(asset_path(), config.echarts_asset_path)
        self.assertEqual(config.relative_echarts_path(config.asset_root), "vendor/echarts.min.js")

    def test_public_tool_entry_is_available_without_internal_import(self) -> None:
        response = call_tool({"action": "status"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["module"], "designer")
        self.assertEqual(
            response["offline_assets"],
            {"report_theme": "designer-theme.css", "echarts": "echarts.min.js", "templates": ["card.html", "report.html"]},
        )

    def test_public_tool_renders_a_mapping_handoff(self) -> None:
        payload = json.loads((ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8"))
        response = call_tool({"action": "render", "designer_payload": payload, "html_report_layout": "continuous"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["artifact"]["layout"], "brief")
        self.assertIn("<html", response["artifact"]["html"])

    def test_invalid_policy_configuration_is_rejected_by_loader_and_tool(self) -> None:
        with self.assertRaises(DesignerConfigurationError):
            load_designer_config({"unexpected": True})
        response = call_tool({"action": "render", "payload": {}, "config": {"allow_pdf": "yes"}})
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "misconfigured")
        self.assertEqual(response["error"], "invalid_configuration")

    def test_policy_is_applied_to_mapping_request(self) -> None:
        payload = json.loads((ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8"))
        response = call_tool({
            "action": "render",
            "payload": payload,
            "asset_mode": "portable",
            "config": {"allow_portable_assets": False},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "invalid_configuration")

    def test_status_advertises_effective_policy(self) -> None:
        response = call_tool({"action": "status", "config": {"allow_pdf": False, "allow_portable_assets": False}})
        self.assertTrue(response["ok"])
        self.assertEqual(response["formats"], ["html"])
        self.assertEqual(response["asset_modes"], ["shared"])

    def test_portable_can_be_the_default_only_when_allowed(self) -> None:
        config = load_designer_config({"default_asset_mode": "portable"})
        self.assertEqual(config.default_asset_mode, "portable")
        with self.assertRaises(DesignerConfigurationError):
            load_designer_config({"default_asset_mode": "portable", "allow_portable_assets": False})


if __name__ == "__main__":
    unittest.main()
