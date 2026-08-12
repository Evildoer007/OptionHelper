from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from environment_check import check_external_stores


class ProjectStoreDefaultsTest(unittest.TestCase):
    def test_project_level_skill_defaults_to_project_data_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            skill = project / ".claude" / "skills" / "option-helper"
            skill.mkdir(parents=True)
            previous = Path.cwd()
            try:
                os.chdir(project)
                report = check_external_stores(skill, None, None)
            finally:
                os.chdir(previous)

        self.assertTrue(report["ok"])
        self.assertEqual(Path(report["stores"]["data_root"]["path"]), (project / "data").resolve())
        self.assertEqual(Path(report["stores"]["result_root"]["path"]), (project / "result").resolve())

    def test_skill_directory_is_never_accepted_as_project_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "option-helper"
            skill.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(skill)
                report = check_external_stores(skill, None, None)
            finally:
                os.chdir(previous)

        self.assertFalse(report["ok"])
        self.assertEqual(report["stores"]["data_root"]["status"], "inside_installation")
        self.assertEqual(report["stores"]["result_root"]["status"], "inside_installation")


if __name__ == "__main__":
    unittest.main()
