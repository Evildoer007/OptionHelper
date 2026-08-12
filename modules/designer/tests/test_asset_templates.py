"""Keep Designer's shipped runtime templates lean and honest."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "modules" / "designer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer import render
from modules.designer.config import DesignerConfig, DesignerConfigurationError, load_designer_config
from modules.designer.models import DesignerInput
class DesignerAssetTemplateTests(unittest.TestCase):
    def test_runtime_reads_shipped_card_and_report_template_assets(self) -> None:
        config = load_designer_config()
        card = config.read_template("card.html")
        report = config.read_template("report.html")
        self.assertIn("__CARD_BODY__", card)
        self.assertIn('class="designer-card"', card)
        self.assertIn("__CONTENT__", report)
        self.assertIn('class="report-document"', report)

    def test_assets_contain_only_the_governed_runtime_delivery_files(self) -> None:
        assets = ROOT / "modules/designer/assets"
        expected = {
            "templates": {"card.html", "report.html"},
            "themes": {"designer-theme.css"},
            "vendor": {"echarts.min.js"},
        }
        self.assertEqual(
            {path.name for path in assets.iterdir() if path.name != ".DS_Store"},
            set(expected),
        )
        for directory, names in expected.items():
            self.assertEqual(
                {path.name for path in (assets / directory).iterdir() if path.name != ".DS_Store"},
                names,
            )

    def test_configuration_rejects_ungoverned_static_samples(self) -> None:
        source = ROOT / "modules" / "designer" / "assets"
        with tempfile.TemporaryDirectory(prefix="designer-assets-") as directory:
            staged = Path(directory) / "assets"
            shutil.copytree(source, staged)
            (staged / "examples").mkdir()
            with self.assertRaisesRegex(DesignerConfigurationError, "包含未治理资源examples"):
                DesignerConfig(asset_root=staged)

    def test_rendered_fixture_is_the_authoritative_sample_baseline(self) -> None:
        payload_path = ROOT / "modules/designer/tests/fixtures/report-payload.example.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        artifact = render(DesignerInput(payload=payload))
        html = artifact["html"]
        self.assertIn('class="report-profile-brief"', html)
        self.assertIn(load_designer_config().read_report_theme(), html)
        self.assertNotIn('class="report-toc"', html)
        self.assertNotIn("报告目录", html)


if __name__ == "__main__":
    unittest.main()
