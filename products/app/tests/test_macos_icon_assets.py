"""macOS application icon geometry, container, and bundle-binding checks."""

from __future__ import annotations

import plistlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from xml.etree import ElementTree


APP_ROOT = Path(__file__).resolve().parents[1]
ROOT = APP_ROOT.parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "app" / "macos"))
from build_macos import APP_ICON, copy_app_icon, write_info_plist


class MacOSIconAssetTests(unittest.TestCase):
    def test_optical_safe_zone_is_frozen(self) -> None:
        svg = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg"
        root = ElementTree.parse(svg).getroot()
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        tile = root.find("svg:rect", namespace)
        mark = root.find("svg:g", namespace)
        self.assertIsNotNone(tile)
        self.assertIsNotNone(mark)
        self.assertEqual(root.attrib["viewBox"], "0 0 1024 1024")
        self.assertEqual(
            {key: tile.attrib[key] for key in ("x", "y", "width", "height", "rx", "fill")},
            {"x": "112", "y": "112", "width": "800", "height": "800", "rx": "176", "fill": "url(#tile)"},
        )
        self.assertEqual(mark.attrib["transform"], "translate(208.4 208.4) scale(2.371875)")
        self.assertAlmostEqual(800 / 1024, 0.78125)
        self.assertAlmostEqual((256 * 2.371875) / 1024, 0.59296875)
        self.assertAlmostEqual((800 - 256 * 2.371875) / 2, 96.4)

    def test_icns_container_and_bundle_binding(self) -> None:
        expected = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns"
        self.assertEqual(APP_ICON, expected)
        data = expected.read_bytes()
        self.assertEqual(data[:4], b"icns")
        self.assertEqual(struct.unpack(">I", data[4:8])[0], len(data))
        offset = 8
        chunk_types: set[bytes] = set()
        while offset < len(data):
            chunk_type = data[offset:offset + 4]
            chunk_length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            self.assertGreaterEqual(chunk_length, 8)
            chunk_types.add(chunk_type)
            offset += chunk_length
        self.assertEqual(offset, len(data))
        self.assertTrue({b"TOC ", b"ic07", b"ic08", b"ic09", b"ic10", b"ic11", b"ic12", b"ic13", b"ic14"}.issubset(chunk_types))

        with tempfile.TemporaryDirectory() as temporary_name:
            bundle = Path(temporary_name) / "OptionHelper.app"
            (bundle / "Contents" / "Resources").mkdir(parents=True)
            copy_app_icon(bundle)
            write_info_plist(bundle, "v1.0")
            copied = bundle / "Contents" / "Resources" / "OptionHelper.icns"
            self.assertEqual(copied.read_bytes(), data)
            with (bundle / "Contents" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
            self.assertEqual(info["CFBundleIconFile"], "OptionHelper.icns")


if __name__ == "__main__":
    unittest.main()
