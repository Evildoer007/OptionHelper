"""DataFetcher页面的开发Host与发行目录资源约定测试。"""

from __future__ import annotations

from pathlib import Path
import posixpath
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAGE = PROJECT_ROOT / "modules" / "datafetcher" / "page" / "datafetcher.html"
LOGO = "../../icons/optionhelper-logo.svg"
FAVICON = "../../icons/optionhelper-app-icon-tile-light.svg"


class DataFetcherPageResourcesTest(unittest.TestCase):
    def test_page_uses_single_release_compatible_logo_and_favicon_path(self) -> None:
        source = PAGE.read_text(encoding="utf-8")
        self.assertIn(f'src="{LOGO}"', source)
        self.assertIn(f'href="{FAVICON}"', source)
        self.assertIn("../../../assets/icons/optionhelper-logo.svg", source)
        self.assertIn("../../../assets/icons/optionhelper-app-icon-tile-light.svg", source)
        self.assertNotIn('src="/icons/optionhelper-logo-background.svg"', source)

    def test_release_relative_path_resolves_to_shared_icon_directory(self) -> None:
        self.assertEqual(posixpath.normpath(f"assets/pages/datafetcher/{LOGO}"), "assets/icons/optionhelper-logo.svg")
        self.assertEqual(posixpath.normpath(f"assets/pages/datafetcher/{FAVICON}"), "assets/icons/optionhelper-app-icon-tile-light.svg")
        self.assertTrue((PROJECT_ROOT / "assets" / "icons" / "optionhelper-logo.svg").is_file())
        self.assertTrue((PROJECT_ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg").is_file())


if __name__ == "__main__":
    unittest.main()
