"""Release gates for the real Card/Report PDF runtime."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packaging" / "skill"))
sys.path.insert(0, str(ROOT / "packaging" / "app" / "macos"))
sys.path.insert(0, str(ROOT / "packaging" / "app" / "windows"))

from build_macos import (
    PDF_RUNTIME_MODULES,
    PDF_RUNTIME_VERSION,
    PILLOW_RUNTIME_VERSION,
    MacOSBuildError,
    backend_build_command as macos_backend_command,
    verify_backend_payload,
)
from build_windows import (
    WindowsBuildError,
    _verify_backend as verify_windows_backend,
    backend_build_command as windows_backend_command,
)


class PdfPackagingContractTests(unittest.TestCase):
    def test_locked_runtime_is_declared_for_skill_and_both_app_builders(self) -> None:
        self.assertIn(f"reportlab=={PDF_RUNTIME_VERSION}", (ROOT / "core" / "requirements.lock").read_text(encoding="utf-8"))
        self.assertIn(f"Pillow=={PILLOW_RUNTIME_VERSION}", (ROOT / "core" / "requirements.lock").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            macos = macos_backend_command(Path(temporary))
            windows = windows_backend_command(Path(temporary))
        for module in PDF_RUNTIME_MODULES:
            self.assertIn(module, macos)
            self.assertIn(module, windows)
        for command in (macos, windows):
            metadata_index = command.index("--copy-metadata")
            self.assertEqual(command[metadata_index + 1], "reportlab")
        self.assertNotIn("--collect-all", macos)
        self.assertNotIn("--collect-all", windows)

    def test_backend_payload_gate_does_not_assume_pure_python_directory_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "OptionHelperBackend"
            (package / "_internal").mkdir(parents=True)
            verify_backend_payload(package)
            verify_windows_backend(package)

    def test_packaged_backend_has_a_runtime_probe(self) -> None:
        source = (ROOT / "products" / "app" / "desktop" / "macos" / "backend_launcher.py").read_text(encoding="utf-8")
        self.assertIn("--probe-pdf-runtime", source)
        self.assertIn("runtime_status", source)
        macos_builder = (ROOT / "packaging" / "app" / "macos" / "build_macos.py").read_text(encoding="utf-8")
        windows_builder = (ROOT / "packaging" / "app" / "windows" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn('[str(backend), "--probe-pdf-runtime"', macos_builder)
        self.assertIn('"--probe-pdf-runtime", "--resource-dir"', windows_builder)


if __name__ == "__main__":
    unittest.main()
