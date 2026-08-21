#!/usr/bin/env python3
"""Regenerate Designer's checked-in HTML visual baselines.

Samples are derived from the public renderer and the canonical fixture. They
are deliberately not hand-authored presentation files: the asset test verifies
byte equality with a fresh render before any release.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.designer import render
from modules.designer.design_system_builder import build_app_token_stylesheet
from modules.designer.models import DesignerInput


def main() -> None:
    fixture = PROJECT_ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json"
    assets = PROJECT_ROOT / "modules" / "designer" / "assets"
    samples = assets / "samples"
    (assets / "themes" / "designer-token-vars.css").write_text(
        build_app_token_stylesheet(), encoding="utf-8"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for output_type in ("report", "card", "quote"):
        artifact = render(DesignerInput(payload=payload, output_type=output_type, asset_mode="portable"))
        (samples / f"{output_type}.html").write_text(artifact["html"], encoding="utf-8")


if __name__ == "__main__":
    main()
