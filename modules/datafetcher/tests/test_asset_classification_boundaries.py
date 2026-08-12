"""中国A股、ETF、指数支持边界回归，不调用任何Provider。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "datafetcher" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.datafetcher.market_conventions import UnsupportedChinaAsset, china_market_convention


class AssetClassificationBoundaryTest(unittest.TestCase):
    def test_supported_asset_classes_use_explicit_code_ranges(self) -> None:
        expected = {
            "000905.SH": "index",
            "399001.SZ": "index",
            "510300.SH": "etf",
            "159919.SZ": "etf",
            "600000.SH": "stock",
            "000001.SZ": "stock",
            "688001.SH": "stock",
            "300750.SZ": "stock",
        }
        for asset_id, asset_class in expected.items():
            with self.subTest(asset_id=asset_id):
                self.assertEqual(china_market_convention(asset_id)["asset_class"], asset_class)

    def test_non_supported_security_categories_are_rejected(self) -> None:
        unsupported = (
            "900901.SH", "200002.SZ", "110059.SH", "123001.SZ",
            "501001.SH", "180101.SZ", "689009.SH",
        )
        for asset_id in unsupported:
            with self.subTest(asset_id=asset_id):
                with self.assertRaises(UnsupportedChinaAsset):
                    china_market_convention(asset_id)


if __name__ == "__main__":
    unittest.main()
