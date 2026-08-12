"""Static release checks for lightweight, repeatable desktop delivery."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "packaging" / "app" / "macos"))
sys.path.insert(0, str(ROOT / "packaging" / "app" / "windows"))

from build_macos import (
    EXCLUDED_BACKEND_MODULES,
    NUMERIC_RUNTIME_MODULES,
    MacOSBuildError,
    backend_build_command,
    copy_backend_bundle,
    verify_backend_payload,
)
from build_windows import WindowsBuildError, backend_build_command as windows_backend_build_command, build_windows
from build_current import CurrentBuildError, build_current


class PackagingSizeTests(unittest.TestCase):
    def test_backend_command_includes_only_required_numeric_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = backend_build_command(Path(temporary))
        for module in NUMERIC_RUNTIME_MODULES:
            self.assertIn(module, command)
        for module in EXCLUDED_BACKEND_MODULES:
            self.assertIn(module, command)
        self.assertNotIn("--collect-all", command)

    def test_forbidden_build_host_dependency_fails_payload_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "OptionHelperBackend" / "_internal" / "tensorflow"
            package.mkdir(parents=True)
            (package / "unrelated.dylib").write_bytes(b"x")
            with self.assertRaises(MacOSBuildError):
                verify_backend_payload(package.parents[1])

    def test_backend_copy_preserves_runtime_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "source"
            package.mkdir()
            (package / "library.dylib").write_bytes(b"runtime")
            (package / "library.1.dylib").symlink_to("library.dylib")
            copied = copy_backend_bundle(package, root / "resources")
            self.assertTrue((copied / "library.1.dylib").is_symlink())

    def test_windows_and_macos_share_the_minimal_backend_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            macos = backend_build_command(Path(temporary))
            windows = windows_backend_build_command(Path(temporary))
        for module in (*NUMERIC_RUNTIME_MODULES, *EXCLUDED_BACKEND_MODULES):
            self.assertIn(module, macos)
            self.assertIn(module, windows)

    def test_one_click_builders_have_fixed_non_destructive_v10_default(self) -> None:
        macos = (ROOT / "build-optionhelper-macos.command").read_text(encoding="utf-8")
        windows = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
        self.assertIn("OPTIONHELPER_VERSION:-v1.0", macos)
        self.assertIn("OPTIONHELPER_VERSION", windows)
        self.assertIn("--platform windows", windows)
        self.assertIn("build_current.py", macos)
        self.assertIn("build_current.py", windows)

    def test_windows_shell_is_a_real_webview2_host(self) -> None:
        source = (ROOT / "products" / "app" / "desktop" / "windows" / "Program.cs").read_text(encoding="utf-8")
        project = (ROOT / "products" / "app" / "desktop" / "windows" / "OptionHelper.Windows.csproj").read_text(encoding="utf-8")
        builder = (ROOT / "packaging" / "app" / "windows" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn("WebView2", source)
        self.assertIn("OPTIONHELPER_URL=", source)
        self.assertIn("Kill(entireProcessTree: true)", source)
        self.assertIn("started && !process.HasExited", source)
        self.assertIn("后端错误", source)
        self.assertIn("Microsoft.Web.WebView2", project)
        self.assertIn("dotnet", builder)
        self.assertIn("--self-contained", builder)
        self.assertIn('"true"', builder)
        self.assertIn("--check-webview2", builder)
        self.assertIn("if path.is_dir()", builder)

    def test_platform_builders_pin_the_python_and_pyinstaller_contract(self) -> None:
        macos = (ROOT / "packaging" / "app" / "macos" / "build_macos.py").read_text(encoding="utf-8")
        windows = (ROOT / "packaging" / "app" / "windows" / "build_windows.py").read_text(encoding="utf-8")
        for source in (macos, windows):
            with self.subTest(source=source[:32]):
                self.assertIn("MINIMUM_BUILD_PYTHON = (3, 11)", source)
                self.assertIn('PYINSTALLER_VERSION = "6.21.0"', source)
                self.assertIn('metadata.version("PyInstaller")', source)

    def test_current_builder_rejects_any_unapproved_version_before_writing(self) -> None:
        with self.assertRaises(CurrentBuildError):
            build_current("v2.0", "macos")

    def test_platform_builders_also_reject_unapproved_versions(self) -> None:
        from build_macos import build_macos

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(MacOSBuildError):
                build_macos("v1.1", root)
            with self.assertRaises(WindowsBuildError):
                build_windows("v1.1", root, dist_root=root / "dist")

    def test_windows_builder_refuses_cross_platform_execution_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "dist"
            with self.assertRaises(WindowsBuildError):
                build_windows("v1.0", Path(temporary) / "capability", dist_root=stage)
            self.assertFalse(stage.exists())

    def test_windows_one_click_builder_uses_portable_python_resolution(self) -> None:
        source = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
        self.assertIn("OPTIONHELPER_PYTHON", source)
        self.assertIn("where python3", source)
        self.assertIn("environment_check.py", source)
        self.assertNotIn("conda run", source)

    def test_windows_shell_reports_webview_initialization_failure(self) -> None:
        source = (ROOT / "products" / "app" / "desktop" / "windows" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn("EnsureCoreWebView2Async", source)
        self.assertIn("请安装Microsoft Edge WebView2 Runtime后重试", source)
        self.assertIn("catch (Exception error)", source)
        self.assertIn("backendErrorTask", source)
        self.assertIn("backendError[^4000..]", source)


if __name__ == "__main__":
    unittest.main()
