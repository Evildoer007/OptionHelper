"""Backtester页面公共资源约定回归。"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester.service import BRAND_ASSET_DIR, PAGE, page_asset_path  # noqa: E402


class BacktesterPageAssetTests(unittest.TestCase):
    def test_logo_resolves_in_release_layout_without_copying_asset(self) -> None:
        html = PAGE.read_text(encoding="utf-8")
        match = re.search(r'<img class="brand-logo" src="([^"]+)"', html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "../../icons/optionhelper-logo.svg")
        self.assertTrue((BRAND_ASSET_DIR / "optionhelper-logo.svg").is_file())

        # 发行页固定落在assets/pages/backtester；相对路径必须回到同一份assets/icons。
        release_page = PROJECT_ROOT / "assets" / "pages" / "backtester" / "backtester.html"
        release_logo = (release_page.parent / match.group(1)).resolve()
        self.assertEqual(release_logo, (PROJECT_ROOT / "assets" / "icons" / "optionhelper-logo.svg").resolve())

    def test_development_host_exposes_only_declared_brand_assets(self) -> None:
        self.assertEqual(page_asset_path("/icons/optionhelper-logo.svg"), BRAND_ASSET_DIR / "optionhelper-logo.svg")
        self.assertEqual(page_asset_path("/icons/optionhelper-app-icon-tile-light.svg"), BRAND_ASSET_DIR / "optionhelper-app-icon-tile-light.svg")
        self.assertIsNone(page_asset_path("/icons/not-allowed.svg"))
        self.assertIsNone(page_asset_path("/../references/optionreg.py"))


if __name__ == "__main__":
    unittest.main()
