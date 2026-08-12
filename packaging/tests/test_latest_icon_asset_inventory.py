"""Latest OptionHelper logo-set inventory and packaging contracts."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = ROOT / "assets" / "icons"
SOURCE_MAP = ROOT / "packaging" / "skill" / "package-source-map.json"


class LatestIconAssetInventoryTests(unittest.TestCase):
    def test_icon_directory_contains_only_the_current_logo_set_and_generator(self) -> None:
        self.assertEqual(
            {path.name for path in ICON_DIR.iterdir() if path.is_file()},
            {
                "generate_macos_icon.py",
                "optionhelper-logo.svg",
                "optionhelper-mark.svg",
                "optionhelper-app-icon-tile-light.svg",
                "optionhelper-app-icon-tile-light.icns",
                "optionhelper-app-icon-tile-light.ico",
                "optionhelper-app-icon-tile-dark.svg",
                "optionhelper-app-icon-tile-dark.icns",
                "optionhelper-app-icon-tile-dark.ico",
            },
        )
        self.assertFalse((ICON_DIR / "__pycache__").exists())

    def test_skill_mapping_excludes_generated_icon_caches(self) -> None:
        source_map = json.loads(SOURCE_MAP.read_text(encoding="utf-8"))
        icon_tree = next(item for item in source_map["trees"] if item["source"] == "assets/icons")
        self.assertEqual(
            icon_tree["exclude"],
            ["**/__pycache__/**", "**/*.pyc", ".DS_Store"],
        )

    def test_tile_assets_are_not_used_as_sidebar_brand_artwork(self) -> None:
        for relative in ("products/app/frontend/optchat/index.html", "products/app/frontend/optdesk/index.html"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            _, head_end, document_body = source.partition("</head>")
            self.assertTrue(head_end, f"{relative}缺少head结束标签")
            self.assertNotIn("optionhelper-app-icon-tile-light.svg", document_body)
            self.assertNotIn("optionhelper-app-icon-tile-dark.svg", document_body)


if __name__ == "__main__":
    unittest.main()
