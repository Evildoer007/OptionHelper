"""Standalone Skill reports default to the invoking project's result directory."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.config import default_report_output_root
from runtime.bootstrap import RuntimePaths


class ProjectResultDefaultTest(unittest.TestCase):
    def test_release_report_writes_directly_below_external_project_result_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            result = project / "result"
            paths = RuntimePaths(
                project_root=Path(temporary) / "installed-skill",
                knowledger_root=Path(temporary) / "installed-skill" / "scripts" / "knowledger",
                result_root=result,
                data_root=project / "data",
                module_source_roots=(),
                mode="release",
            )

            self.assertEqual(default_report_output_root(paths), result.resolve())
            self.assertNotIn("installed-skill", str(default_report_output_root(paths)))

    def test_development_report_keeps_named_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = RuntimePaths(
                project_root=root,
                knowledger_root=root / "references",
                result_root=root / "result",
                data_root=root / "data",
                module_source_roots=(),
                mode="development",
            )

            self.assertEqual(default_report_output_root(paths), (root / "result" / "output_report").resolve())


if __name__ == "__main__":
    unittest.main()
