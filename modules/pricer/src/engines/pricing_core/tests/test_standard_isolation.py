from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from .support import path_accumulator_parameters


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class StandardIsolationTest(unittest.TestCase):
    def test_public_api_has_no_mode_or_comparison_entry(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("compare_option", functions)
        price_arguments = functions["price_option"].args
        names = [
            argument.arg
            for argument in price_arguments.args + price_arguments.kwonlyargs
        ]
        self.assertNotIn("mode", names)
        self.assertIn("solve_option", functions)

    def test_active_engine_contains_no_retired_runtime_types_or_adapters(self):
        self.assertFalse((ROOT / "engine" / "derivatives" / "adapters").exists())
        forbidden = (
            "CompatibilityMode",
            "LegacyUnit",
            "legacy_raw",
            "convert_legacy_value",
            "history/legacy",
            "history\\legacy",
        )
        for path in sorted((ROOT / "engine").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(path=path.relative_to(ROOT), marker=marker):
                    self.assertNotIn(marker, text)

    def test_catalog_has_exact_standard_routes_only(self):
        namespace = {}
        catalog = ROOT / "engine" / "catalog.py"
        exec(compile(catalog.read_text(encoding="utf-8"), str(catalog), "exec"), namespace)
        routes = namespace["ENGINE_ROUTES"]
        self.assertEqual(len(routes), 8)
        self.assertTrue(all(name.startswith("STANDARD_") for name in routes))
        self.assertTrue(all("mode" not in route for route in routes.values()))

    def test_runtime_prices_when_history_is_absent(self):
        parameters = path_accumulator_parameters()
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory) / "pricing_core"
            isolated.mkdir()
            shutil.copy2(ROOT / "main.py", isolated / "main.py")
            shutil.copytree(
                ROOT / "engine",
                isolated / "engine",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(ROOT / "data", isolated / "data")
            self.assertFalse((isolated / "history").exists())
            script = """
import importlib.util, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
parameters = json.loads(sys.argv[2])
spec = importlib.util.spec_from_file_location('isolated_standard_main', root / 'main.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
run = module.price_option(
    'ACCUMULATOR', 'PATH_ACCUMULATOR', parameters,
    'MONTE_CARLO_CPU', output='NONE',
)
assert run.result.pv_points_100 is not None
assert set(run.result.greeks) == {'Delta', 'Gamma', 'Theta', 'Vega', 'Rho'}
"""
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [PYTHON, "-c", script, str(isolated), json.dumps(parameters)],
                cwd=Path(directory),
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_import_from_different_cwd_is_silent_and_preserves_cwd(self):
        script = """
import importlib.util, os, pathlib, sys
before = os.getcwd()
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('cwd_safety_main', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert os.getcwd() == before
"""
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [PYTHON, "-c", script, str(ROOT / "main.py")],
                cwd=directory,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
