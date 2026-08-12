from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from build_skill import build_skill


_NEEDLE = "Machine" + "Learning"
_EXCLUDED_TOP_LEVEL = {".git", "dist", "versions", "OHCopy", "blueprint"}
_EXCLUDED_PREFIXES = (
    Path("products/app/capability"),
    Path("result/build-candidates"),
    Path("assets/payoffer/figures"),
)


def _files_under(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in _EXCLUDED_TOP_LEVEL:
            continue
        if any(relative.is_relative_to(prefix) for prefix in _EXCLUDED_PREFIXES):
            continue
        files.append(path)
    return sorted(files)


def _matches(root: Path, *, markdown_only: bool = False) -> list[str]:
    matches: list[str] = []
    for path in _files_under(root):
        if markdown_only and path.suffix.lower() != ".md":
            continue
        if _NEEDLE in path.read_text(encoding="utf-8", errors="ignore"):
            matches.append(path.relative_to(root).as_posix())
    return matches


class PortablePythonGateTest(unittest.TestCase):
    def test_development_and_build_inputs_do_not_name_a_local_environment(self) -> None:
        self.assertEqual(_matches(ROOT), [])

    def test_markdown_has_no_local_environment_name(self) -> None:
        self.assertEqual(_matches(ROOT, markdown_only=True), [])

    def test_launchers_and_builders_validate_a_portable_interpreter(self) -> None:
        macos_builder = (ROOT / "build-optionhelper-macos.command").read_text(encoding="utf-8")
        windows_builder = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
        macos_pages = (ROOT / "core" / "start-pages.command").read_text(encoding="utf-8")
        windows_pages = (ROOT / "core" / "start-pages.bat").read_text(encoding="utf-8")
        for source in (macos_builder, windows_builder, macos_pages, windows_pages):
            with self.subTest(source=source[:32]):
                self.assertIn("OPTIONHELPER_PYTHON", source)
                self.assertIn("environment_check.py", source)
                self.assertNotIn(_NEEDLE, source)
        self.assertIn("python3", macos_builder)
        self.assertIn("python3", macos_pages)
        self.assertIn("where python", windows_builder)
        self.assertIn("where python", windows_pages)
        self.assertNotIn("status=$?", macos_builder)
        self.assertIn("build_exit_code=$?", macos_builder)

    def test_build_tools_enforce_app_python_and_pyinstaller_contract(self) -> None:
        checker = (ROOT / "packaging" / "skill" / "environment_check.py").read_text(encoding="utf-8")
        macos_builder = (ROOT / "build-optionhelper-macos.command").read_text(encoding="utf-8")
        windows_builder = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
        self.assertIn("sys.version_info >= (3, 11)", checker)
        build_lock = (ROOT / "packaging" / "build-requirements.lock").read_text(encoding="utf-8")
        self.assertIn("PyInstaller==6.21.0", build_lock)
        self.assertIn("build-requirements.lock", macos_builder)
        self.assertIn("build-requirements.lock", windows_builder)
        for source in (windows_builder, windows_pages := (ROOT / "core" / "start-pages.bat").read_text(encoding="utf-8")):
            with self.subTest(source=source[:32]):
                self.assertIn("%CANDIDATE:~2,1%", source)
                self.assertIn("\\NUL", source)

    def test_candidate_skill_has_no_local_environment_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # 端口绑定属于端到端探测，单元门禁只验证实际构建输入及候选内容。
            # 发布前的完整候选构建仍会运行真实probe_runtime。
            with patch("build_skill.probe_runtime", return_value=[]):
                skill_root = build_skill(Path(temporary), candidate=True)
            self.assertEqual(_matches(skill_root), [])


if __name__ == "__main__":
    unittest.main()
