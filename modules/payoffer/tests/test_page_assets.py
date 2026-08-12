"""Payoffer页面公共资源约定回归。"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.payoffer.service import BRAND_ASSET_DIR, PAGE, page_asset_path


class PayofferPageAssetTests(unittest.TestCase):
    def test_logo_uses_five_page_release_relative_convention(self) -> None:
        html = PAGE.read_text(encoding="utf-8")
        match = re.search(r'<img class="brand-logo" src="([^"]+)"', html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "../../icons/optionhelper-logo.svg")
        self.assertTrue((BRAND_ASSET_DIR / "optionhelper-logo.svg").is_file())

        # 发行包页面位于assets/pages/payoffer，两个父级正好回到assets。
        release_page = PROJECT_ROOT / "assets" / "pages" / "payoffer" / "payoffer.html"
        release_logo = (release_page.parent / match.group(1)).resolve()
        self.assertEqual(release_logo, (PROJECT_ROOT / "assets" / "icons" / "optionhelper-logo.svg").resolve())
        self.assertTrue(release_logo.is_file())

    def test_local_host_exposes_only_the_declared_brand_asset(self) -> None:
        logo = page_asset_path("/icons/optionhelper-logo.svg")
        self.assertEqual(logo, BRAND_ASSET_DIR / "optionhelper-logo.svg")
        self.assertEqual(page_asset_path("/icons/optionhelper-app-icon-tile-light.svg"), BRAND_ASSET_DIR / "optionhelper-app-icon-tile-light.svg")
        self.assertIsNone(page_asset_path("/icons/not-allowed.svg"))
        self.assertIsNone(page_asset_path("/../references/optionreg.py"))


if __name__ == "__main__":
    unittest.main()
