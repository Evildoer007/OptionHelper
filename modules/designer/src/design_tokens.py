"""The single machine-readable visual contract for Designer.

The values in this module are intentionally boring and explicit.  Pages,
cards, reports, charts and the OptChat/OptDesk shell all consume this source;
they must not create a second, page-local palette or typography scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping


DESIGN_SYSTEM_ID = "optionhelper.design-system"
DESIGN_SYSTEM_SCHEMA = DESIGN_SYSTEM_ID


@dataclass(frozen=True)
class DesignTokens:
    """Immutable design tokens shared by every Designer consumer."""

    colors: Mapping[str, str]
    dark_colors: Mapping[str, str]
    fonts: Mapping[str, str]
    type_scale: Mapping[str, str]
    pdf_type_scale: Mapping[str, float]
    spacing: Mapping[str, str]
    radii: Mapping[str, str]
    borders: Mapping[str, str]
    states: Mapping[str, Mapping[str, str]]
    chart_palette: tuple[str, ...]
    chart_line_types: tuple[str, ...]
    chart_symbols: tuple[str, ...]
    breakpoints: Mapping[str, int]
    modules: tuple[str, ...]
    modes: tuple[str, ...]

    @property
    def system_id(self) -> str:
        return DESIGN_SYSTEM_ID

    def to_dict(self) -> dict[str, Any]:
        # ``dataclasses.asdict`` deep-copies values and therefore cannot
        # serialize MappingProxyType on the supported Python versions.
        return {
            "schema": self.system_id,
            "colors": dict(self.colors),
            "dark_colors": dict(self.dark_colors),
            "fonts": dict(self.fonts),
            "type_scale": dict(self.type_scale),
            "pdf_type_scale": dict(self.pdf_type_scale),
            "spacing": dict(self.spacing),
            "radii": dict(self.radii),
            "borders": dict(self.borders),
            "states": {key: dict(value) for key, value in self.states.items()},
            "chart_palette": list(self.chart_palette),
            "chart_line_types": list(self.chart_line_types),
            "chart_symbols": list(self.chart_symbols),
            "breakpoints": dict(self.breakpoints),
            "modules": list(self.modules),
            "modes": list(self.modes),
        }


def _freeze(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Freeze the top-level mapping without exposing mutable defaults."""

    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            frozen[key] = MappingProxyType(dict(item))
        else:
            frozen[key] = item
    return MappingProxyType(frozen)


_COLORS = {
    "brand_red": "#C8102E",
    "on_brand": "#FFFFFF",
    "paper": "#FFFFFF",
    "ground": "#F4F4F4",
    "surface": "#FFFFFF",
    "ink": "#252525",
    "ink_soft": "#404040",
    "muted": "#6B6B6B",
    "muted_soft": "#737373",
    "blue_gray": "#49647D",
    "risk_gold": "#936719",
    "rule": "#E2E2E2",
    "rule_strong": "#C9C9C9",
    # Compatibility names are semantic aliases, not additional colours.
    # Downstream pages may migrate gradually without recreating a second red
    # or the former pink surfaces.
    "brand_red_deep": "#C8102E",
    "brand_red_soft": "#F4F4F4",
    "blue_gray_soft": "#F4F4F4",
    "chart_gray": "#6B6B6B",
    "heatmap_low": "#F4F4F4",
    "risk_gold_soft": "#FCF7EA",
    "paper_border": "#E2E2E2",
    "red_border_soft": "#C9C9C9",
    "red_surface": "#F4F4F4",
    "red_tag_border": "#C9C9C9",
    "gold_border": "#C9C9C9",
    "gold_ink": "#936719",
    "table_border": "#C9C9C9",
    "table_head_ink": "#252525",
    "paper_shadow": "rgb(48 48 48 / .10)",
    "surface_shadow": "rgb(48 48 48 / .08)",
}


_DARK_COLORS = {
    "brand_red": "#C8102E",
    "on_brand": "#FFFFFF",
    "paper": "#181818",
    "ground": "#111111",
    "surface": "#202020",
    "ink": "#F2F2F2",
    "ink_soft": "#D8D8D8",
    "muted": "#A6A6A6",
    "muted_soft": "#8F8F8F",
    "blue_gray": "#8DA6B9",
    "risk_gold": "#D3A348",
    "rule": "#363636",
    "rule_strong": "#4D4D4D",
    "brand_red_deep": "#C8102E",
    "brand_red_soft": "#181818",
    "blue_gray_soft": "#202020",
    "chart_gray": "#A6A6A6",
    "heatmap_low": "#111111",
    "risk_gold_soft": "#181818",
    "paper_border": "#363636",
    "red_border_soft": "#4D4D4D",
    "red_surface": "#202020",
    "red_tag_border": "#4D4D4D",
    "gold_border": "#4D4D4D",
    "gold_ink": "#D3A348",
    "table_border": "#4D4D4D",
    "table_head_ink": "#F2F2F2",
    "paper_shadow": "rgb(0 0 0 / .38)",
    "surface_shadow": "rgb(0 0 0 / .30)",
}


TOKENS = DesignTokens(
    colors=_freeze(_COLORS),
    dark_colors=_freeze(_DARK_COLORS),
    fonts=_freeze(
        {
            "sans": 'Arial, "PingFang SC", "Noto Sans CJK SC", sans-serif',
            "chinese": '"PingFang SC", "Noto Sans CJK SC", sans-serif',
            "latin": 'Arial, sans-serif',
            "math": '"STIX Two Math", "Cambria Math", "Times New Roman", serif',
        }
    ),
    type_scale=_freeze(
        {
            "report_title": "26px",
            "report_title_a4": "23px",
            "report_title_print": "20px",
            "section": "17px",
            "section_a4": "15px",
            "section_print": "14px",
            "subsection": "14px",
            "subsection_a4": "13px",
            "subsection_print": "12px",
            "report_body": "12.5px",
            "report_body_print": "11.5px",
            "body": "14px",
            "body_print": "13.333px",
            "table": "12px",
            "table_print": "12px",
            "table_compact": "11px",
            "table_compact_print": "11px",
            "table_dense": "10px",
            "table_dense_print": "10px",
            "table_min": "9px",
            "table_min_print": "9px",
            "caption": "13px",
            "meta": "11px",
            "meta_print": "12px",
            "metric": "20px",
            "formula": "15px",
            "report_title_narrow": "21px",
            "module_title": "24px",
            "module_label": "12px",
            "card_brand": "8px",
            "card_title": "16px",
            "card_section": "11px",
            "card_label": "10px",
            "card_structure": "17px",
            "card_meta": "10px",
            # Reasoning is supporting evidence on the compact Card, not a
            # second headline.  Keep it one step below the surrounding data
            # note while retaining a print-readable line height in CSS.
            "card_reason": "9px",
            "card_table": "9px",
            "card_unit": "8px",
            "card_risk": "9.5px",
        }
    ),
    pdf_type_scale=_freeze(
        {
            "report_title": 20.0,
            "card_title": 16.0,
            "report_section": 14.0,
            "card_section": 10.5,
            "report_subsection": 11.5,
            "card_subsection": 9.2,
            "report_body": 10.1,
            "card_body": 9.1,
            "report_caption": 9.3,
            "card_caption": 8.6,
            "report_table": 8.8,
            "card_table": 8.0,
            "report_table_head": 8.9,
            "card_table_head": 8.1,
            "report_table_compact": 8.2,
            "card_table_compact": 7.4,
            "report_table_head_compact": 8.3,
            "card_table_head_compact": 7.5,
            "report_table_dense": 7.6,
            "card_table_dense": 6.9,
            "report_table_head_dense": 7.7,
            "card_table_head_dense": 7.0,
            "metric": 8.2,
        }
    ),
    spacing=_freeze(
        {
            "xs": "4px",
            "sm": "8px",
            "md": "12px",
            "lg": "20px",
            "xl": "32px",
            "xxl": "48px",
        }
    ),
    radii=_freeze({"none": "0", "sm": "4px", "md": "8px", "lg": "12px"}),
    borders=_freeze({"hairline": "1px", "accent": "2px", "topline": "4px"}),
    states=_freeze(
        {
            "ready": {"color": _COLORS["blue_gray"], "surface": _COLORS["blue_gray_soft"], "label": "已接入结果"},
            "partial": {"color": _COLORS["risk_gold"], "surface": _COLORS["risk_gold_soft"], "label": "部分接入"},
            "pending": {"color": _COLORS["risk_gold"], "surface": _COLORS["risk_gold_soft"], "label": "待接入计算"},
            "not_run": {"color": _COLORS["risk_gold"], "surface": _COLORS["risk_gold_soft"], "label": "本次未运行"},
            "unsupported": {"color": _COLORS["risk_gold"], "surface": _COLORS["risk_gold_soft"], "label": "当前不支持"},
            "failed": {"color": _COLORS["brand_red"], "surface": _COLORS["brand_red_soft"], "label": "运行失败"},
        }
    ),
    chart_palette=(
        _COLORS["brand_red"],
        _COLORS["blue_gray"],
        _COLORS["risk_gold"],
        _COLORS["muted"],
        _COLORS["ink_soft"],
    ),
    chart_line_types=("solid", "dashed", "dotted"),
    chart_symbols=("circle", "rect", "triangle", "diamond"),
    breakpoints=_freeze({"narrow": 900, "print": 720}),
    modules=("DataFetcher", "Payoffer", "Pricer", "Backtester", "Reporter"),
    modes=("OptChat", "OptDesk"),
)


def token_dict() -> dict[str, Any]:
    """Return a JSON-serializable copy of the authoritative token source."""

    return TOKENS.to_dict()


def token_hash() -> str:
    """Return a stable hash for manifest and ReportRun provenance."""

    encoded = json.dumps(token_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()
