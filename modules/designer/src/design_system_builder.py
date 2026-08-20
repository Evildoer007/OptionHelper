"""Build the Designer design system from one token source.

This module deliberately has no dependency on a page, Reporter, or financial
module.  It emits serializable data that can be embedded by the renderer or
consumed by the five operation pages and the OptChat/OptDesk shell.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .config import load_designer_config
from .design_tokens import DESIGN_SYSTEM_SCHEMA, DESIGN_SYSTEM_ID, token_dict, token_hash


@dataclass(frozen=True)
class DesignSystem:
    """A frozen, portable design-system bundle."""

    schema: str
    design_system_id: str
    token_hash: str
    tokens: Mapping[str, Any]
    css_variables: str
    component_rules: Mapping[str, Mapping[str, Any]]
    echarts_theme: Mapping[str, Any]
    svg_theme: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "design_system_id": self.design_system_id,
            "token_hash": self.token_hash,
            "tokens": dict(self.tokens),
            "css_variables": self.css_variables,
            "component_rules": {key: dict(value) for key, value in self.component_rules.items()},
            "echarts_theme": dict(self.echarts_theme),
            "svg_theme": dict(self.svg_theme),
        }


def theme_path() -> Path:
    """Return the checked-in CSS artifact used for report portability."""

    return load_designer_config().report_theme_path


def _css_variables(tokens: Mapping[str, Any]) -> str:
    lines = [":root {"]
    for name, value in tokens["colors"].items():
        lines.append(f"  --color-{name.replace('_', '-')}: {value};")
    for name, value in tokens["fonts"].items():
        lines.append(f"  --font-{name.replace('_', '-')}: {value};")
    for name, value in tokens["type_scale"].items():
        lines.append(f"  --type-{name.replace('_', '-')}: {value};")
    for name, value in tokens["spacing"].items():
        lines.append(f"  --space-{name}: {value};")
    for name, value in tokens["radii"].items():
        lines.append(f"  --radius-{name}: {value};")
    for name, value in tokens["borders"].items():
        lines.append(f"  --border-{name}: {value};")
    for name, state in tokens["states"].items():
        css_name = name.replace("_", "-")
        lines.append(f"  --state-{css_name}-color: {state['color']};")
        lines.append(f"  --state-{css_name}-surface: {state['surface']};")
    for position, value in enumerate(tokens["chart_palette"], start=1):
        lines.append(f"  --chart-color-{position}: {value};")
    for name, value in tokens["breakpoints"].items():
        lines.append(f"  --breakpoint-{name}: {value}px;")
    lines.append("}")
    return "\n".join(lines)


def build_app_token_stylesheet() -> str:
    """Build the managed light and dark token file consumed by App pages.

    Reports remain light by default because they never set ``data-theme``.
    App surfaces opt into the same semantic token names with
    ``[data-theme="dark"]`` rather than maintaining a page-local palette.
    """

    tokens = token_dict()
    lines = [
        "/* Generated from modules/designer/src/design_tokens.py. */",
        f"/* designer-token-hash:{token_hash()} */",
        _css_variables(tokens),
        "",
        '[data-theme="dark"] {',
    ]
    for name, value in tokens["dark_colors"].items():
        lines.append(f"  --color-{name.replace('_', '-')}: {value};")
    for name, state in tokens["states"].items():
        state_name = name.replace("_", "-")
        if name == "ready":
            color_name, surface_name = "blue-gray", "blue-gray-soft"
        elif name == "failed":
            color_name, surface_name = "brand-red", "brand-red-soft"
        else:
            color_name, surface_name = "risk-gold", "risk-gold-soft"
        lines.append(f"  --state-{state_name}-color: var(--color-{color_name});")
        lines.append(f"  --state-{state_name}-surface: var(--color-{surface_name});")
    for position, color_name in enumerate(("brand-red", "blue-gray", "risk-gold", "muted", "ink-soft"), start=1):
        lines.append(f"  --chart-color-{position}: var(--color-{color_name});")
    lines.append("}")
    lines.extend(
        (
            "",
            ":root {",
            "  --red: var(--color-brand-red);",
            "  --red-deep: var(--color-brand-red);",
            "  --on-brand: var(--color-on-brand);",
            "  --gold: var(--color-risk-gold);",
            "  --ink: var(--color-ink);",
            "  --muted: var(--color-muted);",
            "  --rule: var(--color-rule);",
            "  --rule-soft: var(--color-rule);",
            "  --paper: var(--color-paper);",
            "  --surface: var(--color-surface);",
            "  --surface-muted: var(--color-ground);",
            "  --desk: var(--color-ground);",
            "  --wash: var(--color-blue-gray-soft);",
            "  --blue: var(--color-blue-gray);",
            "  --ok: var(--color-blue-gray);",
            "  --warning: var(--color-risk-gold);",
            "  --danger: var(--color-brand-red);",
            "  --font: var(--font-sans);",
            "  --mono: var(--font-sans);",
            "}",
        )
    )
    return "\n".join(lines) + "\n"


def _component_rules(tokens: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    colors = tokens["colors"]
    return {
        "module_page": {
            "background": colors["ground"],
            "surface": colors["paper"],
            "accent": colors["brand_red"],
            "title_size": tokens["type_scale"]["module_title"],
            "label_size": tokens["type_scale"]["module_label"],
        },
        "optchat_shell": {
            "background": colors["paper"],
            "surface": colors["surface"],
            "accent": colors["brand_red"],
            "muted": colors["muted"],
        },
        "optdesk_shell": {
            "background": colors["ground"],
            "surface": colors["paper"],
            "accent": colors["brand_red"],
            "secondary": colors["blue_gray"],
        },
        "card": {
            "background": colors["paper"],
            "rule": colors["rule"],
            "accent": colors["brand_red"],
            "body_size": tokens["type_scale"]["body"],
        },
        "report": {
            "background": colors["paper"],
            "accent": colors["brand_red"],
            "body_size": tokens["type_scale"]["body"],
        },
        "formula": {
            "font_family": tokens["fonts"]["math"],
            "color": colors["ink_soft"],
            "surface": colors["blue_gray_soft"],
        },
        "status": {key: dict(value) for key, value in tokens["states"].items()},
    }


def build_design_system() -> DesignSystem:
    """Build a deterministic system bundle from :mod:`design_tokens`."""

    tokens = token_dict()
    colors = tokens["colors"]
    return DesignSystem(
        schema=DESIGN_SYSTEM_SCHEMA,
        design_system_id=DESIGN_SYSTEM_ID,
        token_hash=token_hash(),
        tokens=tokens,
        css_variables=_css_variables(tokens),
        component_rules=_component_rules(tokens),
        echarts_theme={
            "color": list(tokens["chart_palette"]),
            "lineTypes": list(tokens["chart_line_types"]),
            "symbols": list(tokens["chart_symbols"]),
            "textStyle": {
                "fontFamily": tokens["fonts"]["sans"],
                "color": colors["ink_soft"],
                "fontSize": int(tokens["type_scale"]["meta"].removesuffix("px")),
            },
            "axis": {"line": colors["rule_strong"], "label": colors["muted"], "split": colors["rule"]},
            "tooltip": {"background": colors["ink"], "border": colors["rule_strong"], "text": colors["paper"]},
            "heatmap": {"low": colors["heatmap_low"]},
        },
        svg_theme={
            "stroke": colors["brand_red"],
            "secondary_stroke": colors["blue_gray"],
            "risk_stroke": colors["risk_gold"],
            "axis": colors["ink_soft"],
            "grid": colors["rule"],
            "paper": colors["paper"],
            "font_family": tokens["fonts"]["sans"],
        },
    )


def build_design_system_json() -> str:
    """Serialize the bundle for manifest or a page bootstrap payload."""

    return json.dumps(build_design_system().to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["DESIGN_SYSTEM_SCHEMA", "DESIGN_SYSTEM_ID", "DesignSystem", "build_app_token_stylesheet", "build_design_system", "build_design_system_json", "theme_path"]
