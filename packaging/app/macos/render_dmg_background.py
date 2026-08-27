"""Render the checked-in Finder background used by the macOS installer image.

The build consumes the PNG as a static resource.  Keeping the renderer next to
the resource makes the visual contract reproducible without requiring Finder
automation during a release build.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Finder lays the window out in logical points. The checked-in bitmap is
# rendered at 2x so text and fine rules remain sharp on Retina displays.
WINDOW_SIZE = (760, 460)
RENDER_SCALE = 2
PIXEL_SIZE = tuple(dimension * RENDER_SCALE for dimension in WINDOW_SIZE)
OUTPUT_DPI = 72 * RENDER_SCALE
BACKGROUND = (247, 244, 240)
BRAND_RED = (200, 16, 46)
TEXT = (44, 43, 42)
MUTED = (126, 123, 119)
RULE = (224, 219, 214)
TITLE = "将OptionHelper拖入Applications完成安装"
FOOTER = "拖动OptionHelper到Applications后即可开始使用"
SIMPLIFIED_HEITI_INDEX = 1
ARROW_START_X = 255
ARROW_BASE_X = 491
ARROW_TIP_X = 505


def scale(value: int) -> int:
    return value * RENDER_SCALE


def scale_point(point: tuple[int, int]) -> tuple[int, int]:
    return scale(point[0]), scale(point[1])


def _font(size: int, *, light: bool = False) -> ImageFont.FreeTypeFont:
    if light:
        # STHeiti Light index 0 is Heiti TC on macOS; index 1 is Heiti SC.
        return ImageFont.truetype(
            "/System/Library/Fonts/STHeiti Light.ttc",
            size=scale(size),
            index=SIMPLIFIED_HEITI_INDEX,
        )
    return ImageFont.truetype(
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        size=scale(size),
        index=0,
    )


def _centered(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = (PIXEL_SIZE[0] - (right - left)) // 2
    draw.text((x, scale(y) - top), text, font=font, fill=fill)


def _radial_glow(image: Image.Image, center: tuple[int, int], color: tuple[int, int, int], radius: int, opacity: int) -> None:
    """Add a restrained glow without changing the neutral page background."""
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    pixels = glow.load()
    cx, cy = center
    for y in range(max(0, cy - radius), min(image.height, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(image.width, cx + radius + 1)):
            distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if distance >= radius:
                continue
            alpha = int(opacity * (1 - distance / radius) ** 2)
            pixels[x, y] = (*color, alpha)
    image.alpha_composite(glow)


def render_background(destination: Path) -> None:
    image = Image.new("RGBA", PIXEL_SIZE, (*BACKGROUND, 255))
    _radial_glow(image, scale_point((185, 235)), (200, 16, 46), scale(145), 23)
    _radial_glow(image, scale_point((575, 235)), (58, 112, 177), scale(145), 21)

    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, PIXEL_SIZE[0], scale(3) - 1), fill=BRAND_RED)
    title_font = _font(22, light=True)
    _centered(draw, TITLE, 36, title_font, TEXT)
    draw.line(
        (scale(58), scale(82), PIXEL_SIZE[0] - scale(58), scale(82)),
        fill=RULE,
        width=scale(1),
    )

    # The Finder icons are supplied by the DMG entries.  The arrow keeps the
    # same 11px clearance from both 128px icon boxes.
    arrow_y = scale(235)
    draw.line(
        (scale(ARROW_START_X), arrow_y, scale(ARROW_BASE_X), arrow_y),
        fill=BRAND_RED,
        width=scale(2),
    )
    draw.polygon(
        (
            (scale(ARROW_BASE_X), arrow_y - scale(7)),
            (scale(ARROW_TIP_X), arrow_y),
            (scale(ARROW_BASE_X), arrow_y + scale(7)),
        ),
        fill=BRAND_RED,
    )

    draw.line(
        (scale(58), scale(390), PIXEL_SIZE[0] - scale(58), scale(390)),
        fill=RULE,
        width=scale(1),
    )
    footer_font = _font(13, light=True)
    _centered(draw, FOOTER, 412, footer_font, MUTED)
    image.convert("RGB").save(
        destination,
        format="PNG",
        optimize=True,
        dpi=(OUTPUT_DPI, OUTPUT_DPI),
    )


if __name__ == "__main__":
    render_background(Path(__file__).with_name("assets") / "optionhelper-dmg-background.png")
