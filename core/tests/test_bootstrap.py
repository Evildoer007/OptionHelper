from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.bootstrap import BootstrapError, bootstrap_runtime


class BootstrapTest(unittest.TestCase):
    def _release_root(self, parent: Path) -> Path:
        root = parent / "option-helper"
        (root / "scripts" / "knowledger").mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: option-helper\n---\n", encoding="utf-8")
        (root / "scripts" / "knowledger" / "optionreg.py").write_text("REGISTRY = {}\n", encoding="utf-8")
        return root

    def test_release_requires_an_explicit_external_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            release = self._release_root(parent)
            project = parent / "research-project"
            project.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(project)
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(BootstrapError, "必须由App注入运行根"):
                        bootstrap_runtime(release, mutate_sys_path=False)
            finally:
                os.chdir(previous)
            with patch.dict(os.environ, {"OPTIONHELPER_RUNTIME_ROOT": str(parent / "runtime")}, clear=True):
                paths = bootstrap_runtime(release, mutate_sys_path=False)
            self.assertEqual(paths.mode, "release")
            self.assertEqual(paths.data_root, (parent / "runtime" / "data").resolve())
            self.assertEqual(paths.result_root, (parent / "runtime" / "result").resolve())

    def test_release_never_defaults_stores_inside_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = self._release_root(Path(temporary))
            previous = Path.cwd()
            try:
                os.chdir(release)
                with patch.dict(os.environ, {"OPTIONHELPER_RUNTIME_ROOT": str(release)}, clear=True):
                    with self.assertRaisesRegex(BootstrapError, "安装目录"):
                        bootstrap_runtime(release, mutate_sys_path=False)
            finally:
                os.chdir(previous)

    def test_release_rejects_installation_store_and_shared_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            release = self._release_root(parent)
            external = parent / "external"
            with patch.dict(os.environ, {
                "OPTIONHELPER_DATA_ROOT": str(release / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(external / "result"),
            }, clear=True):
                with self.assertRaisesRegex(BootstrapError, "安装目录"):
                    bootstrap_runtime(release, mutate_sys_path=False)
            with patch.dict(os.environ, {
                "OPTIONHELPER_DATA_ROOT": str(external),
                "OPTIONHELPER_RESULT_ROOT": str(external),
            }, clear=True):
                with self.assertRaisesRegex(BootstrapError, "不同根目录"):
                    bootstrap_runtime(release, mutate_sys_path=False)

    def test_launchers_do_not_embed_a_local_python_path(self) -> None:
        command = (ROOT / "core" / "start-pages.command").read_text(encoding="utf-8")
        batch = (ROOT / "core" / "start-pages.bat").read_text(encoding="utf-8")
        self.assertNotIn("/opt/anaconda", command)
        self.assertIn("OPTIONHELPER_PYTHON", command)
        self.assertIn("environment_check.py", command)
        self.assertIn("python3", command)
        self.assertIn("OPTIONHELPER_PYTHON", batch)
        self.assertIn("environment_check.py", batch)
        self.assertIn("where python", batch)


if __name__ == "__main__":
    unittest.main()
