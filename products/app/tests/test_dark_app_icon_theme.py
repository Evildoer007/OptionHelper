"""Paired light/dark OptionHelper app-icon contracts."""

from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import unittest
from xml.etree import ElementTree

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
ICON_DIR = ROOT / "assets" / "icons"
GENERATOR_PATH = ICON_DIR / "generate_macos_icon.py"
SPEC = importlib.util.spec_from_file_location("generate_paired_app_icon", GENERATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load icon generator: {GENERATOR_PATH}")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
NAMESPACE = {"svg": "http://www.w3.org/2000/svg"}


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise AssertionError("mask is empty")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class DarkAppIconThemeTests(unittest.TestCase):
    def test_light_and_dark_sources_share_the_exact_red_symbol_geometry(self) -> None:
        marks = []
        for theme in ("light", "dark"):
            source = ICON_DIR / f"optionhelper-app-icon-tile-{theme}.svg"
            GENERATOR.verify_svg_source(source, theme)
            mark = ElementTree.parse(source).getroot().find("svg:g", NAMESPACE)
            self.assertIsNotNone(mark)
            marks.append(ElementTree.tostring(mark, encoding="unicode"))
        self.assertEqual(marks[0], marks[1])

    def test_theme_pair_keeps_every_safe_boundary_and_symbol_center_aligned(self) -> None:
        for size in GENERATOR.RENDER_SIZES:
            with self.subTest(size=size):
                rendered = {theme: np.asarray(GENERATOR.render_icon(size, theme)) for theme in ("light", "dark")}
                bounds = {theme: _bbox(image[:, :, 3] >= 128) for theme, image in rendered.items()}
                self.assertEqual(bounds["light"], bounds["dark"])
                self.assertLessEqual(abs((bounds["light"][2] - bounds["light"][0]) - round(size * 800 / 1024)), 1)
                red_bounds = {}
                for theme, image in rendered.items():
                    red = (
                        (image[:, :, 3] >= 128)
                        & (image[:, :, 0].astype(np.uint16) >= 120)
                        & (image[:, :, 0].astype(np.uint16) >= image[:, :, 1].astype(np.uint16) * 2)
                        & (image[:, :, 0].astype(np.uint16) >= image[:, :, 2].astype(np.uint16) * 2)
                    )
                    red_bounds[theme] = _bbox(red)
                for light, dark in zip(red_bounds["light"], red_bounds["dark"]):
                    self.assertLessEqual(abs(light - dark), 1)

    def test_dark_tile_is_layered_near_black_not_flat_black_or_a_gray_blob(self) -> None:
        image = np.asarray(GENERATOR.render_icon(256, "dark"))
        luminance = image[:, :, :3].mean(axis=2)
        tile = luminance[40:216, 40:216]
        self.assertGreater(float(np.percentile(tile, 90)), 31)
        self.assertLess(float(np.percentile(tile, 10)), 44)
        self.assertGreater(float(luminance[40, 128] - luminance[216, 128]), 2)
        self.assertLess(int(image[0, 0, 3]), 8)

    def test_committed_dark_icns_ico_and_png_previews_are_reproducible(self) -> None:
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                icns = ICON_DIR / f"optionhelper-app-icon-tile-{theme}.icns"
                ico = ICON_DIR / f"optionhelper-app-icon-tile-{theme}.ico"
                self.assertEqual(icns.read_bytes(), GENERATOR.build_icns_bytes(theme))
                self.assertEqual(ico.read_bytes(), GENERATOR.build_ico_bytes(theme))
                GENERATOR.verify_icns(icns, theme=theme)
                GENERATOR.verify_ico(ico, theme=theme)
                for size in GENERATOR.RENDER_SIZES:
                    preview = ICON_DIR / "previews" / theme / f"optionhelper-app-icon-tile-{theme}-{size}.png"
                    self.assertTrue(preview.is_file())
                    self.assertEqual(Image.open(preview).convert("RGBA").tobytes(), GENERATOR.render_icon(size, theme).tobytes())

        preview = ICON_DIR / "previews" / "optionhelper-app-icon-themes-preview.png"
        self.assertTrue(preview.is_file())
        with Image.open(preview) as sheet:
            self.assertEqual(sheet.size, (2264, 1244))


if __name__ == "__main__":
    unittest.main()
