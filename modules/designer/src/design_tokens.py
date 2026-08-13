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


DESIGN_SYSTEM_VERSION = "v1.0.0"
DESIGN_SYSTEM_SCHEMA = "optionhelper.design-system/v1.0.0"


@dataclass(frozen=True)
class DesignTokens:
    """Immutable design tokens shared by every Designer consumer."""

    colors: Mapping[str, str]
    fonts: Mapping[str, str]
    type_scale: Mapping[str, str]
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
    def version(self) -> str:
        return DESIGN_SYSTEM_VERSION

    def to_dict(self) -> dict[str, Any]:
        # ``dataclasses.asdict`` deep-copies values and therefore cannot
        # serialize MappingProxyType on the supported Python versions.
        return {
            "version": self.version,
            "colors": dict(self.colors),
            "fonts": dict(self.fonts),
            "type_scale": dict(self.type_scale),
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
    "brand_red_deep": "#890D26",
    "brand_red_soft": "#FBF1F3",
    "paper": "#FFFDFB",
    "ground": "#F5F1F0",
    "surface": "#FFFFFF",
    "ink": "#241D20",
    "ink_soft": "#44383C",
    "muted": "#6E5F63",
    "muted_soft": "#75666A",
    "blue_gray": "#49647D",
    "blue_gray_soft": "#F3F6F8",
    "chart_gray": "#7E8A99",
    "heatmap_low": "#F8E7EA",
    "risk_gold": "#855E22",
    "risk_gold_soft": "#FBF7EE",
    "rule": "#E9DADC",
    "rule_strong": "#DCC1C7",
    "paper_border": "#E7D8DB",
    "red_border_soft": "#E5C5CC",
    "red_surface": "#FFFAFA",
    "red_tag_border": "#EDD5DA",
    "gold_border": "#EADBBD",
    "gold_ink": "#6A5326",
    "table_border": "#DDBCC3",
    "table_head_ink": "#59353D",
    "paper_shadow": "rgb(44 53 62 / .10)",
    "surface_shadow": "rgb(44 53 62 / .08)",
}


TOKENS = DesignTokens(
    colors=_freeze(_COLORS),
    fonts=_freeze(
        {
            "sans": 'Arial, "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif',
            "chinese": '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif',
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
        _COLORS["chart_gray"],
        _COLORS["brand_red_deep"],
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
