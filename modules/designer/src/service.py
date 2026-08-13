"""Thin Designer service adapter.

The adapter accepts an explicit DesignerInput mapping and delegates all
presentation work to the same ``render`` function used by Reporter. It never
discovers result directories or chooses a latest run.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from .config import DesignerConfig, DesignerConfigurationError, load_designer_config
from .design_renderer import DesignerDependencyError, render
from .design_system_builder import DESIGN_SYSTEM_SCHEMA, build_design_system
from .models import DESIGNER_ARTIFACT_MANIFEST_SCHEMA, DESIGNER_PAYLOAD_SCHEMA, DesignerInput
from .pdf_renderer import runtime_status as pdf_runtime_status


def _profile() -> dict[str, Any]:
    theme = build_design_system()
    return {
        "module": "designer",
        "version": theme.design_system_version,
        "design_system_schema": DESIGN_SYSTEM_SCHEMA,
        "payload_schema": DESIGNER_PAYLOAD_SCHEMA,
        "artifact_manifest_schema": DESIGNER_ARTIFACT_MANIFEST_SCHEMA,
        "design_system_hash": theme.token_hash,
        "output_types": ["card", "report"],
        "modules": list(theme.tokens["modules"]),
        "modes": list(theme.tokens["modes"]),
    }


def capability(config: DesignerConfig | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe the public Tool capability without exposing internal paths."""

    try:
        config = load_designer_config(config)
    except DesignerConfigurationError as error:
        return {
            **_profile(),
            "ok": False,
            "status": "misconfigured",
            "error": "invalid_configuration",
            "message": str(error),
        }
    pdf_runtime = pdf_runtime_status()
    pdf_available = bool(pdf_runtime.get("available")) and config.allow_pdf
    return {
        **_profile(),
        "ok": True,
        "status": "available",
        "offline_assets": {
            "report_theme": config.report_theme_path.name,
            "echarts": config.echarts_asset_path.name,
            "templates": ["card.html", "report.html"],
        },
        "asset_modes": ["shared", "portable"] if config.allow_portable_assets else ["shared"],
        "default_asset_mode": config.default_asset_mode,
        "formats": ["html", "pdf"] if pdf_available else ["html"],
        "pdf_runtime": pdf_runtime,
    }


def call_tool(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Render one explicit payload or return the capability contract."""

    value = dict(request)
    action = str(value.get("action", "status")).strip().lower()
    try:
        config = load_designer_config(value.pop("config", None))
    except DesignerConfigurationError as error:
        return {
            **_profile(),
            "action": action,
            "ok": False,
            "status": "misconfigured",
            "error": "invalid_configuration",
            "message": str(error),
        }
    profile = capability(config)
    if action == "status":
        return {**profile, "action": action}
    if not profile["ok"]:
        return {**profile, "action": action}
    if action != "render":
        return {**profile, "action": action, "ok": False, "error": "unsupported_action"}
    value.pop("action", None)
    if "echarts_path" in value:
        return {
            **profile,
            "action": action,
            "ok": False,
            "error": "invalid_request",
            "message": "ECharts资源由Designer配置固定，Tool请求不得传入物理路径或外链。",
        }
    value.setdefault("asset_mode", config.default_asset_mode)
    try:
        artifact = render(DesignerInput.from_mapping(value), config=config)
    except (TypeError, ValueError) as error:
        return {**profile, "action": action, "ok": False, "error": "invalid_payload", "message": str(error)}
    except (DesignerConfigurationError, DesignerDependencyError) as error:
        error_code = "invalid_configuration" if isinstance(error, DesignerConfigurationError) else "missing_dependency"
        return {**profile, "action": action, "ok": False, "error": error_code, "message": str(error)}
    result: dict[str, Any] = {key: value for key, value in artifact.items() if key not in {"pdf"}}
    if artifact.get("pdf") is not None:
        result["pdf_base64"] = base64.b64encode(artifact["pdf"]).decode("ascii")
    return {**profile, "action": action, "artifact": result}


__all__ = ["DesignerDependencyError", "call_tool", "capability"]
