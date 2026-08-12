"""Payoffer默认资产在开发、Skill与App Capability布局中的路径回归。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = PROJECT_ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.bootstrap import RuntimePaths
from modules.payoffer.asset_resolver import FIGURES_DIR, RUNTIME_PATHS, _figures_dir
from modules.payoffer.service import call_tool


def _tree_hash(root: Path) -> str:
    digest = sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _runtime_paths(root: Path, mode: str) -> RuntimePaths:
    return RuntimePaths(
        project_root=root,
        knowledger_root=root / "references",
        result_root=root / "result",
        data_root=root / "data",
        module_source_roots=(),
        mode=mode,
    )


class PayofferAssetRuntimePathTests(unittest.TestCase):
    def test_development_preview_reads_module_figures_without_mutation(self) -> None:
        expected = PROJECT_ROOT / "modules" / "payoffer" / "figures"
        self.assertEqual(RUNTIME_PATHS.mode, "development")
        self.assertEqual(FIGURES_DIR, expected)
        before = _tree_hash(expected)

        result = call_tool({"action": "preview", "product_id": "2.1"})

        self.assertTrue(result["ok"])
        self.assertIn("<svg", result["svg"])
        self.assertEqual(before, _tree_hash(expected))

    def test_skill_and_app_capability_resolve_packaged_figures(self) -> None:
        skill_root = Path("/opt/option-helper")
        self.assertEqual(
            _figures_dir(_runtime_paths(skill_root, "release")),
            skill_root / "assets" / "payoffer" / "figures",
        )

        app_root = PROJECT_ROOT / "products" / "app" / "capability" / "option-helper"
        app_figures = _figures_dir(_runtime_paths(app_root, "release"))
        self.assertEqual(app_figures, app_root / "assets" / "payoffer" / "figures")
        self.assertEqual(len(list((app_figures / "json").glob("*.json"))), 65)
        self.assertEqual(len(list((app_figures / "svg").glob("*.svg"))), 65)


if __name__ == "__main__":
    unittest.main()
