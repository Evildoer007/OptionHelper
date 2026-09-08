"""Resolve Windows fonts from the current installation, without a drive assumption."""
from __future__ import annotations

import os
from pathlib import Path


def windows_font_candidates(*names: str) -> tuple[Path, ...]:
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not system_root:
        return ()
    font_root = Path(system_root) / "Fonts"
    return tuple(font_root / name for name in names)
