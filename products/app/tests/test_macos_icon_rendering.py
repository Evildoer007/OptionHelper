"""Independent-size rendering and ICNS source-consistency tests."""

from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import struct
import unittest

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = ROOT / "assets" / "icons" / "generate_macos_icon.py"
SPEC = importlib.util.spec_from_file_location("generate_macos_icon", GENERATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load icon generator: {GENERATOR_PATH}")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def _threshold_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise AssertionError("Mask is empty")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class MacOSIconRenderingTests(unittest.TestCase):
    def test_svg_source_locks_the_refined_tile_material(self) -> None:
        GENERATOR.verify_svg_source()

    def test_tile_material_stays_visible_without_becoming_gray(self) -> None:
        image = GENERATOR.render_icon(256)
        pixels = np.asarray(image)
        alpha = pixels[:, :, 3]
        luminance = pixels[:, :, :3].mean(axis=2)

        # The low-alpha halo makes the white tile legible on Finder white.
        shadow = (alpha > 0) & (alpha < 96)
        self.assertGreater(int(shadow.sum()), 300)
        self.assertLess(int(alpha[0:20, 0:20].max()), 8)

        # The tile remains neutral light gray, with a deliberately restrained
        # center light that reads as a surface rather than a plastic gradient.
        tile_core = np.concatenate(
            (luminance[38:54, 64:192].ravel(), luminance[202:218, 64:192].ravel())
        )
        self.assertGreaterEqual(float(np.percentile(tile_core, 90)), 247)
        self.assertGreaterEqual(float(np.percentile(tile_core, 10)), 233)
        slope = float(luminance[40, 128] - luminance[216, 128])
        self.assertGreaterEqual(slope, 4)
        self.assertLessEqual(slope, 8)
        tile_rgb = pixels[38:48, 64:192, :3]
        self.assertLessEqual(int(np.max(np.ptp(tile_rgb, axis=2))), 3)

        # A one-pixel-class edge remains visible when composited on pure white.
        white = Image.new("RGBA", image.size, (255, 255, 255, 255))
        white.alpha_composite(image)
        on_white = np.asarray(white)[:, :, :3].mean(axis=2)
        self.assertGreaterEqual(float(255 - on_white[28, 128]), 6)

        for size in (16, 32, 64):
            with self.subTest(size=size):
                small = np.asarray(GENERATOR.render_icon(size))
                small_alpha = small[:, :, 3]
                small_luminance = small[:, :, :3].mean(axis=2)
                core = small_luminance[
                    round(size * 0.2):round(size * 0.8),
                    round(size * 0.2):round(size * 0.8),
                ]
                self.assertGreaterEqual(float(np.percentile(core, 90)), 245)
                self.assertLess(int(small_alpha[0, 0]), 8)

    def test_each_required_size_is_rendered_from_geometry(self) -> None:
        self.assertEqual(GENERATOR.RENDER_SIZES, (16, 32, 64, 128, 256, 512, 1024))
        master = GENERATOR.render_icon(1024)
        for size in GENERATOR.RENDER_SIZES:
            with self.subTest(size=size):
                image = GENERATOR.render_icon(size)
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.size, (size, size))

                pixels = np.asarray(image)
                alpha_bbox = _threshold_bbox(pixels[:, :, 3] >= 128)
                tile_width = alpha_bbox[2] - alpha_bbox[0]
                self.assertLessEqual(abs(tile_width - round(size * 800 / 1024)), 1)
                self.assertLessEqual(abs((alpha_bbox[0] + alpha_bbox[2]) - size), 1)

                red_channel = pixels[:, :, 0].astype(np.uint16)
                green_channel = pixels[:, :, 1].astype(np.uint16)
                blue_channel = pixels[:, :, 2].astype(np.uint16)
                red = (
                    (pixels[:, :, 3] >= 128)
                    & (red_channel >= 120)
                    & (red_channel >= green_channel * 2)
                    & (red_channel >= blue_channel * 2)
                )
                red_bbox = _threshold_bbox(red)
                red_width = red_bbox[2] - red_bbox[0]
                self.assertLessEqual(abs(red_width - round(size * 607.2 / 1024)), 2)
                self.assertLessEqual(abs((red_bbox[0] + red_bbox[2]) - size), 1)

                if size in (16, 32, 64):
                    resized_master = master.resize((size, size), Image.Resampling.BICUBIC)
                    self.assertNotEqual(image.tobytes(), resized_master.tobytes())

    def test_payoff_path_remains_visible_at_small_sizes(self) -> None:
        for size in (16, 32, 64):
            with self.subTest(size=size):
                pixels = np.asarray(GENERATOR.render_icon(size))
                origin = size * 208.4 / 1024
                scale = size * 2.371875 / 1024
                sample_points = GENERATOR.payoff_path_points(96)
                hits = 0
                checked_columns: set[int] = set()
                for x_local, y_local in sample_points:
                    x = int(round(origin + x_local * scale))
                    y = int(round(origin + y_local * scale))
                    if x in checked_columns:
                        continue
                    checked_columns.add(x)
                    x0, x1 = max(0, x - 1), min(size, x + 2)
                    y0, y1 = max(0, y - 1), min(size, y + 2)
                    patch = pixels[y0:y1, x0:x1]
                    white = (
                        (patch[:, :, 3] >= 96)
                        & (patch[:, :, 0] >= 190)
                        & (patch[:, :, 1] >= 120)
                        & (patch[:, :, 2] >= 120)
                    )
                    hits += bool(white.any())
                self.assertGreaterEqual(hits / len(checked_columns), 0.9)
                self.assertGreaterEqual(len(checked_columns), round(size * 0.11))

    def test_committed_icns_contains_exact_independent_layers(self) -> None:
        expected_sizes = {
            b"icp4": 16,
            b"ic11": 32,
            b"ic12": 64,
            b"ic07": 128,
            b"ic08": 256,
            b"ic09": 512,
            b"ic10": 1024,
            b"ic13": 256,
            b"ic14": 512,
        }
        icon = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns"
        chunks = GENERATOR.read_icns_png_chunks(icon)
        self.assertEqual(set(chunks), set(expected_sizes))
        for chunk_type, size in expected_sizes.items():
            with self.subTest(chunk=chunk_type.decode("ascii")):
                image = Image.open(BytesIO(chunks[chunk_type])).convert("RGBA")
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.tobytes(), GENERATOR.render_icon(size).tobytes())

        data = icon.read_bytes()
        self.assertEqual(data[:4], b"icns")
        self.assertEqual(struct.unpack(">I", data[4:8])[0], len(data))


if __name__ == "__main__":
    unittest.main()
