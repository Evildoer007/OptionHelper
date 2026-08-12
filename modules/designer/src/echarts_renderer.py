"""Offline ECharts theme and asset helpers.

Designer receives series and values from Reporter or a calculation module. It
only supplies a stable visual theme and never calculates or transforms data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_designer_config
from .design_system_builder import build_design_system


def asset_path() -> Path:
    """Return the configured offline ECharts asset location."""

    return load_designer_config().echarts_asset_path


def echarts_theme() -> dict[str, Any]:
    """Return a copy of the fixed offline ECharts theme."""

    return dict(build_design_system().echarts_theme)


def chart_colors() -> tuple[str, ...]:
    return tuple(build_design_system().tokens["chart_palette"])


__all__ = ["asset_path", "chart_colors", "echarts_theme"]
