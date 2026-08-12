"""Tests for Designer's published runtime boundary.

This migrated test deliberately uses only the public Config and Tool surface.
It protects the original responsibility: shipped assets are available and the
module can be invoked as a real rendering service.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
source = PROJECT_ROOT / "modules" / "designer" / "src"
if str(source) not in sys.path:
    sys.path.insert(0, str(source))

from modules.designer import call_tool, load_designer_config


class DesignerServiceTest(unittest.TestCase):
    def test_public_config_and_tool_render_a_shipped_payload(self) -> None:
        config = load_designer_config()
        self.assertTrue(config.report_theme_path.is_file())
        self.assertTrue(config.echarts_asset_path.is_file())
        self.assertTrue(config.template_path("card.html").is_file())
        self.assertTrue(config.template_path("report.html").is_file())

        status = call_tool({"action": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "available")
        self.assertEqual(status["module"], "designer")
        self.assertEqual(status["offline_assets"]["report_theme"], "designer-theme.css")

        payload_path = PROJECT_ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json"
        response = call_tool({"action": "render", "payload": json.loads(payload_path.read_text(encoding="utf-8"))})
        self.assertTrue(response["ok"])
        artifact = response["artifact"]
        self.assertEqual(artifact["format"], "html")
        self.assertIn("<html", artifact["html"])
        self.assertIn("data-design-system-version=", artifact["html"])
