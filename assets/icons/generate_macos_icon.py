#!/usr/bin/env python3
"""Render OptionHelper application icons independently at every required size."""

from __future__ import annotations

import argparse
from io import BytesIO
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from xml.etree import ElementTree

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


CANVAS_SIZE = 1024
RENDER_SIZES = (16, 32, 64, 128, 256, 512, 1024)
WINDOWS_RENDER_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SUPPORTED_RENDER_SIZES = tuple(sorted(set(RENDER_SIZES + WINDOWS_RENDER_SIZES)))
TILE_X = 112
TILE_Y = 112
TILE_SIZE = 800
TILE_RADIUS = 176
# The tile is deliberately a restrained warm-gray material rather than flat
# white: it needs to remain legible against Finder/Explorer white while still
# reading as an icon surface at 16px.  The red mark geometry is frozen below.
class TileTheme:
    """Immutable surface values; kept decorator-free for file-spec test imports."""

    __slots__ = (
        "name", "gradient_start", "gradient_end", "sheen_color", "sheen_strength",
        "edge", "highlight", "shadow_color", "shadow_opacity", "small_gradient_end", "small_sheen",
    )

    def __init__(
        self,
        name: str,
        gradient_start: tuple[int, int, int],
        gradient_end: tuple[int, int, int],
        sheen_color: tuple[int, int, int],
        sheen_strength: float,
        edge: tuple[int, int, int, int],
        highlight: tuple[int, int, int, int],
        shadow_color: tuple[int, int, int],
        shadow_opacity: float,
        small_gradient_end: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]],
        small_sheen: tuple[float, float, float],
    ) -> None:
        self.name = name
        self.gradient_start = gradient_start
        self.gradient_end = gradient_end
        self.sheen_color = sheen_color
        self.sheen_strength = sheen_strength
        self.edge = edge
        self.highlight = highlight
        self.shadow_color = shadow_color
        self.shadow_opacity = shadow_opacity
        self.small_gradient_end = small_gradient_end
        self.small_sheen = small_sheen
    name: str
    gradient_start: tuple[int, int, int]
    gradient_end: tuple[int, int, int]
    sheen_color: tuple[int, int, int]
    sheen_strength: float
    edge: tuple[int, int, int, int]
    highlight: tuple[int, int, int, int]
    shadow_color: tuple[int, int, int]
    shadow_opacity: float
    small_gradient_end: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]
    small_sheen: tuple[float, float, float]


THEMES = {
    "light": TileTheme(
        "light", (252, 253, 251), (241, 242, 239), (255, 255, 255), 0.14,
        (210, 212, 207, 97), (255, 255, 255, 122), (36, 39, 45), 0.10,
        ((249, 250, 248), (247, 248, 245), (244, 245, 242)), (0.10, 0.12, 0.14),
    ),
    "dark": TileTheme(
        "dark", (43, 47, 53), (23, 25, 29), (245, 240, 234), 0.055,
        (190, 197, 205, 41), (255, 255, 255, 20), (2, 3, 4), 0.22,
        ((40, 44, 50), (37, 41, 47), (32, 35, 40)), (0.035, 0.045, 0.055),
    ),
}
# Backward-compatible light aliases used by the existing light-finish tests.
TILE_GRADIENT_START = THEMES["light"].gradient_start
TILE_GRADIENT_END = THEMES["light"].gradient_end
TILE_SHEEN_CENTER = (0.50, 0.43)
TILE_SHEEN_RADIUS = 0.86
TILE_SHEEN_STRENGTH = THEMES["light"].sheen_strength
TILE_EDGE = THEMES["light"].edge
TILE_HIGHLIGHT = THEMES["light"].highlight
SHADOW_COLOR = THEMES["light"].shadow_color
# Keep the red symbol geometrically identical to the approved mark while
# increasing its optical footprint by 10%.  The tile remains unchanged: the
# resulting 96.4px interior margin is still comfortably larger than the
# 1024px tile edge/highlight treatment and prevents Finder clipping.
MARK_SCALE = 2.15625 * 1.10
MARK_DIAMETER = round(256 * MARK_SCALE, 4)
MARK_ORIGIN = round((CANVAS_SIZE - MARK_DIAMETER) / 2, 4)
GRADIENT = (292.0625, 826.8125, 822.5, 279.125)
GRADIENT_START = (169, 0, 34)
GRADIENT_END = (200, 16, 46)

HERE = Path(__file__).resolve().parent
SVG_SOURCES = {theme: HERE / f"optionhelper-app-icon-tile-{theme}.svg" for theme in THEMES}
ICNS_OUTPUTS = {theme: HERE / f"optionhelper-app-icon-tile-{theme}.icns" for theme in THEMES}
ICO_OUTPUTS = {theme: HERE / f"optionhelper-app-icon-tile-{theme}.ico" for theme in THEMES}
SVG_SOURCE = SVG_SOURCES["light"]
ICNS_OUTPUT = ICNS_OUTPUTS["light"]
ICO_OUTPUT = ICO_OUTPUTS["light"]

ICNS_LAYOUT = (
    (b"icp4", 16),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic13", 256),
    (b"ic14", 512),
)


def payoff_path_points(samples: int = 96) -> list[tuple[float, float]]:
    """Sample the unchanged SVG payoff path in its local 256-unit space."""
    samples = max(samples, 12)
    first = max(2, samples // 4)
    curve = max(4, samples // 3)
    last = max(2, samples - first - curve)
    points: list[tuple[float, float]] = []
    for index in range(first):
        t = index / first
        points.append((65 + (111 - 65) * t, 173.0))
    for index in range(curve):
        t = index / curve
        inverse = 1 - t
        x = inverse**3 * 111 + 3 * inverse**2 * t * 126 + 3 * inverse * t**2 * 138 + t**3 * 149
        y = inverse**3 * 173 + 3 * inverse**2 * t * 173 + 3 * inverse * t**2 * 169 + t**3 * 160
        points.append((x, y))
    for index in range(last + 1):
        t = index / last
        points.append((149 + (204 - 149) * t, 160 + (111 - 160) * t))
    return points


def _arc_points(samples: int = 180) -> list[tuple[float, float]]:
    """Sample SVG path M181 65 A82 82 0 1 0 181 191."""
    radius = 82.0
    center_x = 128.5
    center_y = 128.0
    start_angle = math.atan2(65 - center_y, 181 - center_x)
    end_angle = math.atan2(191 - center_y, 181 - center_x) - 2 * math.pi
    return [
        (
            center_x + radius * math.cos(start_angle + (end_angle - start_angle) * index / samples),
            center_y + radius * math.sin(start_angle + (end_angle - start_angle) * index / samples),
        )
        for index in range(samples + 1)
    ]


def _supersample(size: int) -> int:
    if size <= 64:
        return 8
    if size <= 256:
        return 4
    return 2


def _scaled_point(point: tuple[float, float], user_scale: float) -> tuple[float, float]:
    return (
        (MARK_ORIGIN + point[0] * MARK_SCALE) * user_scale,
        (MARK_ORIGIN + point[1] * MARK_SCALE) * user_scale,
    )


def _draw_round_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    *,
    fill: tuple[int, int, int, int],
    width: int,
) -> None:
    """Draw a continuous antialiased polyline without sampled-segment pinholes."""
    draw.line(points, fill=fill, width=width, joint="curve")
    radius = width / 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _draw_gradient_circle(canvas: Image.Image, user_scale: float) -> None:
    left = int(math.floor(MARK_ORIGIN * user_scale)) - 2
    top = int(math.floor(MARK_ORIGIN * user_scale)) - 2
    right = int(math.ceil((MARK_ORIGIN + MARK_DIAMETER) * user_scale)) + 2
    bottom = int(math.ceil((MARK_ORIGIN + MARK_DIAMETER) * user_scale)) + 2
    width = right - left
    height = bottom - top

    x_user = (np.arange(width, dtype=np.float32) + left + 0.5) / user_scale
    y_user = (np.arange(height, dtype=np.float32) + top + 0.5) / user_scale
    dx = GRADIENT[2] - GRADIENT[0]
    dy = GRADIENT[3] - GRADIENT[1]
    t = (
        (x_user[np.newaxis, :] - GRADIENT[0]) * dx
        + (y_user[:, np.newaxis] - GRADIENT[1]) * dy
    ) / (dx * dx + dy * dy)
    t = np.clip(t, 0.0, 1.0)
    start = np.asarray(GRADIENT_START, dtype=np.float32)
    end = np.asarray(GRADIENT_END, dtype=np.float32)
    rgb = np.rint(start + t[:, :, np.newaxis] * (end - start)).astype(np.uint8)

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    circle_left = MARK_ORIGIN * user_scale - left
    circle_top = MARK_ORIGIN * user_scale - top
    circle_right = (MARK_ORIGIN + MARK_DIAMETER) * user_scale - left
    circle_bottom = (MARK_ORIGIN + MARK_DIAMETER) * user_scale - top
    mask_draw.ellipse((circle_left, circle_top, circle_right, circle_bottom), fill=255)
    layer = Image.fromarray(rgb, "RGB").convert("RGBA")
    layer.putalpha(mask)
    canvas.alpha_composite(layer, dest=(left, top))


def _tile_material(theme: TileTheme, size: int) -> tuple[tuple[int, int, int], float, float, float, float, float]:
    """Return small-size-aware tile finish parameters in output pixels."""
    if size == 16:
        return theme.small_gradient_end[0], 0.45, 0.25, theme.shadow_opacity * .8, 0.5, theme.small_sheen[0]
    if size == 32:
        return theme.small_gradient_end[1], 0.7, 0.4, theme.shadow_opacity * .9, 0.6, theme.small_sheen[1]
    if size == 64:
        return theme.small_gradient_end[2], 1.0, 0.65, theme.shadow_opacity, 0.75, theme.small_sheen[2]
    scale = size / CANVAS_SIZE
    return (
        theme.gradient_end,
        16 * scale,
        8 * scale,
        theme.shadow_opacity,
        max(0.8, 2.5 * scale),
        theme.sheen_strength,
    )


def _draw_tile_material(canvas: Image.Image, *, theme: TileTheme, size: int, supersample: int, user_scale: float) -> None:
    (
        gradient_end,
        shadow_blur,
        shadow_offset,
        shadow_opacity,
        edge_width,
        sheen_strength,
    ) = _tile_material(theme, size)
    bounds = (
        TILE_X * user_scale,
        TILE_Y * user_scale,
        (TILE_X + TILE_SIZE) * user_scale,
        (TILE_Y + TILE_SIZE) * user_scale,
    )
    radius = TILE_RADIUS * user_scale

    shadow_mask = Image.new("L", canvas.size, 0)
    shadow_draw = ImageDraw.Draw(shadow_mask)
    offset = shadow_offset * supersample
    shadow_draw.rounded_rectangle(
        (bounds[0], bounds[1] + offset, bounds[2], bounds[3] + offset),
        radius=radius,
        fill=255,
    )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(shadow_blur * supersample))
    shadow_alpha = np.rint(np.asarray(shadow_mask, dtype=np.float32) * shadow_opacity).astype(np.uint8)
    shadow_layer = Image.new("RGBA", canvas.size, (*theme.shadow_color, 0))
    shadow_layer.putalpha(Image.fromarray(shadow_alpha, "L"))
    canvas.alpha_composite(shadow_layer)

    work_size = canvas.size[0]
    x_user = (np.arange(work_size, dtype=np.float32) + 0.5) / user_scale
    y_user = (np.arange(work_size, dtype=np.float32) + 0.5) / user_scale
    x_t = np.clip((x_user - TILE_X) / TILE_SIZE, 0.0, 1.0)
    y_t = np.clip((y_user - TILE_Y) / TILE_SIZE, 0.0, 1.0)
    t = (x_t[np.newaxis, :] + y_t[:, np.newaxis]) / 2
    start = np.asarray(theme.gradient_start, dtype=np.float32)
    end = np.asarray(gradient_end, dtype=np.float32)
    rgb = np.rint(start + t[:, :, np.newaxis] * (end - start)).astype(np.uint8)
    # A broad upper-left highlight gives the tile a shallow material plane;
    # it is intentionally too soft to compete with the red focal mark.
    sheen_distance = np.sqrt(
        (x_t[np.newaxis, :] - TILE_SHEEN_CENTER[0]) ** 2
        + (y_t[:, np.newaxis] - TILE_SHEEN_CENTER[1]) ** 2
    ) / TILE_SHEEN_RADIUS
    sheen = np.clip(1.0 - sheen_distance, 0.0, 1.0) ** 1.5 * sheen_strength
    sheen_rgb = np.asarray(theme.sheen_color, dtype=np.float32)
    rgb = np.rint(rgb + (sheen_rgb - rgb) * sheen[:, :, np.newaxis]).astype(np.uint8)
    tile_layer = Image.fromarray(rgb, "RGB").convert("RGBA")
    tile_mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle(bounds, radius=radius, fill=255)
    tile_layer.putalpha(tile_mask)
    canvas.alpha_composite(tile_layer)

    finish = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    finish_draw = ImageDraw.Draw(finish)
    edge_work = max(1, round(edge_width * supersample))
    edge_inset = edge_work / 2
    finish_draw.rounded_rectangle(
        (
            bounds[0] + edge_inset,
            bounds[1] + edge_inset,
            bounds[2] - edge_inset,
            bounds[3] - edge_inset,
        ),
        radius=max(0, radius - edge_inset),
        outline=theme.edge,
        width=edge_work,
    )
    highlight_width = max(1, round(max(0.5, edge_width * 0.65) * supersample))
    highlight_inset = edge_work * 1.5
    finish_draw.rounded_rectangle(
        (
            bounds[0] + highlight_inset,
            bounds[1] + highlight_inset,
            bounds[2] - highlight_inset,
            bounds[3] - highlight_inset,
        ),
        radius=max(0, radius - highlight_inset),
        outline=theme.highlight,
        width=highlight_width,
    )
    canvas.alpha_composite(finish)


def render_icon(size: int, theme: str = "light") -> Image.Image:
    """Render one size directly from geometry, never from another bitmap layer."""
    if size not in SUPPORTED_RENDER_SIZES:
        raise ValueError(f"Unsupported icon size: {size}")
    if theme not in THEMES:
        raise ValueError(f"Unsupported icon theme: {theme}")
    supersample = _supersample(size)
    work_size = size * supersample
    user_scale = work_size / CANVAS_SIZE
    canvas = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))

    _draw_tile_material(canvas, theme=THEMES[theme], size=size, supersample=supersample, user_scale=user_scale)
    _draw_gradient_circle(canvas, user_scale)

    payoff = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    payoff_draw = ImageDraw.Draw(payoff)
    payoff_width = max(1, round(19 * MARK_SCALE * user_scale))
    payoff_points = [_scaled_point(point, user_scale) for point in payoff_path_points()]
    _draw_round_polyline(payoff_draw, payoff_points, fill=(255, 255, 255, 255), width=payoff_width)
    canvas.alpha_composite(payoff)

    arc = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    arc_draw = ImageDraw.Draw(arc)
    arc_width = max(1, round(8 * MARK_SCALE * user_scale))
    arc_points = [_scaled_point(point, user_scale) for point in _arc_points()]
    arc_color = (255, 255, 255, round(255 * 0.28))
    _draw_round_polyline(arc_draw, arc_points, fill=arc_color, width=arc_width)
    canvas.alpha_composite(arc)

    if supersample != 1:
        canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    return canvas


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def build_icns_bytes(theme: str = "light") -> bytes:
    rendered = {size: _png_bytes(render_icon(size, theme)) for size in RENDER_SIZES}
    chunks = [(chunk_type, rendered[size]) for chunk_type, size in ICNS_LAYOUT]
    toc = b"".join(chunk_type + struct.pack(">I", len(payload) + 8) for chunk_type, payload in chunks)
    chunks.insert(0, (b"TOC ", toc))
    body = b"".join(
        chunk_type + struct.pack(">I", len(payload) + 8) + payload
        for chunk_type, payload in chunks
    )
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def build_ico_bytes(theme: str = "light") -> bytes:
    """Build Windows ICO layers from independent renders, not a master bitmap."""
    rendered = {size: render_icon(size, theme) for size in WINDOWS_RENDER_SIZES}
    buffer = BytesIO()
    rendered[256].save(
        buffer,
        format="ICO",
        sizes=[(size, size) for size in WINDOWS_RENDER_SIZES],
        append_images=[
            rendered[size]
            for size in WINDOWS_RENDER_SIZES
            if size != 256
        ],
    )
    return buffer.getvalue()


def read_icns_png_chunks(path: Path) -> dict[bytes, bytes]:
    data = path.read_bytes()
    if data[:4] != b"icns" or len(data) < 8:
        raise ValueError(f"Not an ICNS file: {path}")
    if struct.unpack(">I", data[4:8])[0] != len(data):
        raise ValueError(f"Invalid ICNS container length: {path}")
    chunks: dict[bytes, bytes] = {}
    offset = 8
    while offset < len(data):
        chunk_type = data[offset:offset + 4]
        chunk_length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        if chunk_length < 8 or offset + chunk_length > len(data):
            raise ValueError(f"Invalid {chunk_type!r} chunk in {path}")
        if chunk_type != b"TOC ":
            chunks[chunk_type] = data[offset + 8:offset + chunk_length]
        offset += chunk_length
    if offset != len(data):
        raise ValueError(f"Trailing ICNS data: {path}")
    return chunks


def verify_svg_source(path: Path | None = None, theme: str = "light") -> None:
    if theme not in THEMES:
        raise ValueError(f"Unsupported icon theme: {theme}")
    theme_spec = THEMES[theme]
    path = path or SVG_SOURCES[theme]
    root = ElementTree.parse(path).getroot()
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    tile = root.find("svg:rect", namespace)
    mark = root.find("svg:g", namespace)
    gradient = root.find("svg:defs/svg:linearGradient[@id='red']", namespace)
    tile_gradient = root.find("svg:defs/svg:linearGradient[@id='tile']", namespace)
    shadow = root.find("svg:defs/svg:filter[@id='tileShadow']", namespace)
    if tile is None or mark is None or gradient is None or tile_gradient is None or shadow is None:
        raise ValueError("SVG source is missing the tile, mark, or gradient")
    expected_tile = {
        "x": str(TILE_X),
        "y": str(TILE_Y),
        "width": str(TILE_SIZE),
        "height": str(TILE_SIZE),
        "rx": str(TILE_RADIUS),
        "fill": "url(#tile)",
        "stroke": "#D2D4CF" if theme == "light" else "#BEC5CD",
        "stroke-opacity": ".38" if theme == "light" else ".16",
        "stroke-width": "2.5",
    }
    actual_tile = {key: tile.attrib.get(key) for key in expected_tile}
    if actual_tile != expected_tile:
        raise ValueError(f"SVG tile geometry differs from generator: {actual_tile}")
    expected_transform = f"translate({MARK_ORIGIN} {MARK_ORIGIN}) scale({MARK_SCALE})"
    if mark.attrib.get("transform") != expected_transform:
        raise ValueError(f"SVG mark transform differs from generator: {mark.attrib.get('transform')}")
    actual_gradient = tuple(float(gradient.attrib[key]) for key in ("x1", "y1", "x2", "y2"))
    if actual_gradient != GRADIENT:
        raise ValueError(f"SVG gradient differs from generator: {actual_gradient}")
    tile_stops = tile_gradient.findall("svg:stop", namespace)
    expected_stops = [
        "#%02X%02X%02X" % theme_spec.gradient_start,
        "#%02X%02X%02X" % theme_spec.gradient_end,
    ]
    if [stop.attrib.get("stop-color") for stop in tile_stops] != expected_stops:
        raise ValueError("SVG tile material differs from generator")
    sheen = root.find("svg:defs/svg:radialGradient[@id='tileSheen']", namespace)
    if sheen is None or {
        "cx": sheen.attrib.get("cx"),
        "cy": sheen.attrib.get("cy"),
        "r": sheen.attrib.get("r"),
    } != {"cx": "50%", "cy": "43%", "r": "86%"}:
        raise ValueError("SVG tile sheen differs from generator")
    sheen_stops = sheen.findall("svg:stop", namespace)
    expected_sheen = ".14" if theme == "light" else ".055"
    expected_sheen_color = "#FFFFFF" if theme == "light" else "#F5F0EA"
    if not sheen_stops or {
        "stop-color": sheen_stops[0].attrib.get("stop-color"),
        "stop-opacity": sheen_stops[0].attrib.get("stop-opacity"),
    } != {"stop-color": expected_sheen_color, "stop-opacity": expected_sheen}:
        raise ValueError("SVG tile sheen strength differs from generator")
    shadow_drop = shadow.find("svg:feDropShadow", namespace)
    expected_shadow_color = "#24272D" if theme == "light" else "#020304"
    if shadow_drop is None or {
        "dx": shadow_drop.attrib.get("dx"),
        "dy": shadow_drop.attrib.get("dy"),
        "stdDeviation": shadow_drop.attrib.get("stdDeviation"),
        "flood-color": shadow_drop.attrib.get("flood-color"),
        "flood-opacity": shadow_drop.attrib.get("flood-opacity"),
    } != {
        "dx": "0", "dy": "8", "stdDeviation": "16", "flood-color": expected_shadow_color,
        "flood-opacity": ".10" if theme == "light" else ".22",
    }:
        raise ValueError("SVG tile shadow differs from generator")
    rects = root.findall("svg:rect", namespace)
    if len(rects) < 3 or rects[1].attrib.get("fill") != "url(#tileSheen)":
        raise ValueError("SVG is missing the tile sheen layer")
    expected_highlight = {
        "x": "116",
        "y": "116",
        "width": "792",
        "height": "792",
        "rx": "172",
        "fill": "none",
        "stroke": "#FFFFFF",
        "stroke-opacity": ".48" if theme == "light" else ".08",
        "stroke-width": "1.5",
    }
    if {key: rects[2].attrib.get(key) for key in expected_highlight} != expected_highlight:
        raise ValueError("SVG inner highlight differs from generator")


def verify_icns(path: Path, *, theme: str = "light", system_extract: bool = False) -> None:
    expected = dict(ICNS_LAYOUT)
    chunks = read_icns_png_chunks(path)
    if set(chunks) != set(expected):
        raise ValueError(f"Unexpected ICNS chunks: {sorted(chunks)}")
    for chunk_type, size in expected.items():
        decoded = Image.open(BytesIO(chunks[chunk_type])).convert("RGBA")
        if decoded.size != (size, size):
            raise ValueError(f"{chunk_type!r} has size {decoded.size}, expected {size}x{size}")
        if decoded.tobytes() != render_icon(size, theme).tobytes():
            raise ValueError(f"{chunk_type!r} does not match the independent {size}px render")
    if system_extract and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory(prefix="optionhelper-icon-") as temporary_name:
            destination = Path(temporary_name) / "OptionHelper.iconset"
            subprocess.run(
                ["iconutil", "-c", "iconset", str(path), "-o", str(destination)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            extracted = {image.size[0] for image_path in destination.glob("*.png") for image in [Image.open(image_path)]}
            if not set(RENDER_SIZES).issubset(extracted):
                raise ValueError(f"iconutil extraction is missing sizes: {set(RENDER_SIZES) - extracted}")


def verify_ico(path: Path, *, theme: str = "light") -> None:
    with Image.open(path) as icon:
        expected_sizes = {(size, size) for size in WINDOWS_RENDER_SIZES}
        if icon.info.get("sizes") != expected_sizes:
            raise ValueError(f"Unexpected ICO sizes: {icon.info.get('sizes')}")
        for size in WINDOWS_RENDER_SIZES:
            decoded = icon.ico.getimage((size, size)).convert("RGBA")
            if decoded.tobytes() != render_icon(size, theme).tobytes():
                raise ValueError(f"ICO {size}px layer does not match its independent render")


def write_icon(output: Path, *, theme: str = "light") -> None:
    verify_svg_source(theme=theme)
    output.write_bytes(build_icns_bytes(theme))
    verify_icns(output, theme=theme, system_extract=True)


def write_windows_icon(output: Path, *, theme: str = "light") -> None:
    verify_svg_source(theme=theme)
    output.write_bytes(build_ico_bytes(theme))
    verify_ico(output, theme=theme)


def write_theme_comparison_preview(output: Path) -> None:
    """Create a deterministic light/dark comparison sheet for visual QA."""
    side = 1024
    gutter = 72
    label_height = 76
    preview = Image.new("RGBA", (side * 2 + gutter * 3, side + label_height + gutter * 2), (236, 238, 240, 255))
    for index, theme in enumerate(("light", "dark")):
        icon = render_icon(side, theme)
        panel_x = gutter + index * (side + gutter)
        panel = Image.new("RGBA", (side, side), (255, 255, 255, 255) if theme == "light" else (29, 32, 37, 255))
        panel.alpha_composite(icon)
        preview.alpha_composite(panel, dest=(panel_x, gutter + label_height))
    output.parent.mkdir(parents=True, exist_ok=True)
    preview.convert("RGB").save(output, format="PNG", compress_level=9, optimize=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--theme", choices=tuple(THEMES), default="light", help="Surface theme to render")
    parser.add_argument("--windows", action="store_true", help="Generate or check the Windows ICO")
    parser.add_argument("--check", action="store_true", help="Check that the committed icon is reproducible")
    parser.add_argument("--preview-dir", type=Path, help="Optionally write temporary PNG previews for visual QA")
    parser.add_argument("--comparison-preview", type=Path, help="Write a deterministic light/dark 1024px comparison PNG")
    arguments = parser.parse_args()

    theme = arguments.theme
    verify_svg_source(theme=theme)
    output = arguments.output or (ICO_OUTPUTS[theme] if arguments.windows else ICNS_OUTPUTS[theme])
    build = build_ico_bytes if arguments.windows else build_icns_bytes
    verify = verify_ico if arguments.windows else verify_icns
    if arguments.check:
        expected = build(theme)
        if not output.exists() or output.read_bytes() != expected:
            raise SystemExit(f"Icon is stale; run {Path(__file__).name}")
        if arguments.windows:
            verify(output, theme=theme)
        else:
            verify(output, theme=theme, system_extract=True)
    else:
        if arguments.windows:
            write_windows_icon(output, theme=theme)
        else:
            write_icon(output, theme=theme)

    if arguments.preview_dir:
        arguments.preview_dir.mkdir(parents=True, exist_ok=True)
        preview_sizes = WINDOWS_RENDER_SIZES if arguments.windows else RENDER_SIZES
        for size in preview_sizes:
            render_icon(size, theme).save(arguments.preview_dir / f"optionhelper-app-icon-tile-{theme}-{size}.png")
    if arguments.comparison_preview:
        write_theme_comparison_preview(arguments.comparison_preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
