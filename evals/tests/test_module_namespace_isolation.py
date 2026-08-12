"""Regression coverage for Eval isolation from preloaded module services."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

from evals.runner import run


ROOT = Path(__file__).resolve().parents[2]


def test_formal_pricer_eval_uses_its_own_runtime_when_service_is_preloaded() -> None:
    """A preceding module test must not pin Eval to another temporary Store."""

    previous_path = list(sys.path)
    previous_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "modules" or name.startswith("modules.")
    }
    try:
        source_root = str(ROOT / "modules" / "pricer" / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        preloaded = importlib.import_module("modules.pricer.service")

        outcome = run("compute.pricer.formal-run")

        assert outcome["failed"] == 0, outcome
        assert sys.modules["modules.pricer.service"] is preloaded
    finally:
        for name in tuple(sys.modules):
            if name == "modules" or name.startswith("modules."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        sys.path[:] = previous_path
