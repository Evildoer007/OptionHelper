"""Payoffer SVG presentation theme.

The SVG payload itself is owned by Payoffer. Designer can supply these values
to an SVG renderer, but never edits paths, labels, points or economic rules.
"""

from __future__ import annotations

from typing import Any

from .design_system_builder import build_design_system


def svg_theme() -> dict[str, Any]:
    return dict(build_design_system().svg_theme)


def svg_attributes() -> dict[str, str]:
    theme = svg_theme()
    return {
        "data-design-system": build_design_system().design_system_id,
        "data-primary-stroke": str(theme["stroke"]),
        "data-secondary-stroke": str(theme["secondary_stroke"]),
        "data-risk-stroke": str(theme["risk_stroke"]),
    }


__all__ = ["svg_attributes", "svg_theme"]
