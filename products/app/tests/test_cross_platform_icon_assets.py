"""Cross-platform application-icon source and layer consistency checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = ROOT / "assets" / "icons" / "generate_macos_icon.py"
SPEC = importlib.util.spec_from_file_location("generate_app_icon", GENERATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load icon generator: {GENERATOR_PATH}")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def _threshold_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise AssertionError("Mask is empty")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class CrossPlatformIconAssetTests(unittest.TestCase):
    def test_windows_ico_contains_exact_independent_layers(self) -> None:
        expected_sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
        self.assertEqual(GENERATOR.WINDOWS_RENDER_SIZES, expected_sizes)
        icon = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
        with Image.open(icon) as image:
            self.assertEqual(
                image.info["sizes"],
                {(size, size) for size in expected_sizes},
            )
            for size in expected_sizes:
                with self.subTest(size=size):
                    layer = image.ico.getimage((size, size)).convert("RGBA")
                    expected = GENERATOR.render_icon(size)
                    self.assertEqual(layer.size, (size, size))
                    self.assertEqual(layer.tobytes(), expected.tobytes())

    def test_windows_and_macos_share_the_approved_optical_bounds(self) -> None:
        macos = GENERATOR.render_icon(256)
        with Image.open(
            ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
        ) as icon:
            windows = icon.ico.getimage((256, 256)).convert("RGBA")
        self.assertEqual(windows.tobytes(), macos.tobytes())

        pixels = np.asarray(windows)
        tile_bbox = _threshold_bbox(pixels[:, :, 3] >= 128)
        red = (
            (pixels[:, :, 3] >= 128)
            & (pixels[:, :, 0].astype(np.uint16) >= 120)
            & (
                pixels[:, :, 0].astype(np.uint16)
                >= pixels[:, :, 1].astype(np.uint16) * 2
            )
            & (
                pixels[:, :, 0].astype(np.uint16)
                >= pixels[:, :, 2].astype(np.uint16) * 2
            )
        )
        red_bbox = _threshold_bbox(red)
        self.assertLessEqual(abs((tile_bbox[2] - tile_bbox[0]) - 200), 1)
        self.assertLessEqual(abs((red_bbox[2] - red_bbox[0]) - 152), 2)

    def test_windows_ico_is_reproducible(self) -> None:
        icon = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
        self.assertEqual(icon.read_bytes(), GENERATOR.build_ico_bytes())


if __name__ == "__main__":
    unittest.main()
