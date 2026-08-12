"""macOS bundle preflight and frozen-resource launcher checks."""

from __future__ import annotations

import json
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen


APP_ROOT = Path(__file__).resolve().parents[1]
ROOT = APP_ROOT.parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "app" / "macos"))
from build_macos import (
    MAX_APP_BUNDLE_BYTES,
    MAX_DMG_BYTES,
    MacOSBuildError,
    app_manifest,
    copy_tree,
    finder_conflicts,
    platform_release_manifest,
    prepare_dmg_layout,
    verify_platform_release_manifest,
    write_json,
)
from capability_fixture import capability_root


class MacOSPackagingTests(unittest.TestCase):
    def test_dmg_layout_supports_drag_to_applications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            bundle = temporary / "OptionHelper.app"
            (bundle / "Contents").mkdir(parents=True)
            layout = prepare_dmg_layout(bundle, temporary / "layout")
            self.assertTrue((layout / "OptionHelper.app" / "Contents").is_dir())
            self.assertTrue((layout / "Applications").is_symlink())
            self.assertEqual(os.readlink(layout / "Applications"), "/Applications")

    def test_delivery_size_gates_are_explicit(self) -> None:
        self.assertEqual(MAX_APP_BUNDLE_BYTES, 750 * 1024 * 1024)
        self.assertEqual(MAX_DMG_BYTES, 300 * 1024 * 1024)

    def test_resource_copy_preserves_files_and_reports_only_true_sibling_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source"
            source.mkdir()
            (source / "service.py").write_text("canonical\n", encoding="utf-8")
            (source / "service 2.py").write_text("stale\n", encoding="utf-8")
            (source / "scripts").mkdir()
            (source / "scripts 2").mkdir()
            (source / "chapter 2.md").write_text("legitimate numbered resource\n", encoding="utf-8")
            destination = temporary / "destination"
            copy_tree(source, destination)
            self.assertTrue((destination / "service.py").is_file())
            self.assertTrue((destination / "service 2.py").is_file())
            self.assertTrue((destination / "scripts 2").is_dir())
            self.assertTrue((destination / "chapter 2.md").is_file())
            conflicts = {path.relative_to(destination).as_posix() for path in finder_conflicts(destination)}
            self.assertEqual(conflicts, {"scripts 2", "service 2.py"})

    def test_manifest_locks_verified_capability_without_release_id(self) -> None:
        manifest_path = capability_root() / "capability-manifest.json"
        capability = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = sha256(manifest_path.read_bytes()).hexdigest()
        manifest = app_manifest(
            "v1.0",
            capability,
            digest,
            shell_language="objective-c",
            build_tool="clang",
        )
        self.assertEqual(manifest["app_version"], "v1.0")
        self.assertEqual(manifest["capability_version"], "candidate")
        self.assertEqual(manifest["capability_content_tree_hash"], capability["content_tree_hash"])
        self.assertNotIn("release_id", manifest)
        self.assertEqual(manifest["shell_language"], "objective-c")
        self.assertEqual(manifest["build_tool"], "clang")

    def test_platform_release_manifest_is_ad_hoc_candidate_and_locks_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            app_manifest_path = temporary / "app-manifest.json"
            dmg = temporary / "OptionHelper-v1.0-macOS-arm64.dmg"
            app_manifest_path.write_text('{"app_version":"v1.0","capability_manifest_hash":"capability-manifest","capability_content_tree_hash":"capability-tree"}\n', encoding="utf-8")
            dmg.write_bytes(b"immutable-dmg")
            value = platform_release_manifest(
                app_version="v1.0", app_manifest_path=app_manifest_path, dmg_path=dmg,
                capability={"capability_manifest_hash": "capability-manifest", "capability_content_tree_hash": "capability-tree"},
                created_at="2026-08-08T00:00:00Z",
            )
            release = temporary / "platform-release-manifest.json"
            write_json(release, value)
            verified = verify_platform_release_manifest(release, app_manifest_path=app_manifest_path, dmg_path=dmg)
            self.assertEqual(verified["release_status"], "local_candidate")
            self.assertEqual(verified["formal_distribution_status"], "formal_distribution_unavailable")
            self.assertFalse(verified["notarized"])
            dmg.write_bytes(b"changed")
            with self.assertRaises(MacOSBuildError):
                verify_platform_release_manifest(release, app_manifest_path=app_manifest_path, dmg_path=dmg)

    def test_frozen_resource_launcher_starts_verified_backend_on_loopback(self) -> None:
        launcher = APP_ROOT / "desktop" / "macos" / "backend_launcher.py"
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            resources = temporary / "Resources"
            shutil.copytree(APP_ROOT / "frontend", resources / "frontend")
            shutil.copytree(ROOT / "assets" / "icons", resources / "assets" / "icons")
            # Exercise the same current, session-built Capability used by
            # packaging rather than the intentionally stale local cache.
            shutil.copytree(capability_root(), resources / "capability" / "option-helper")
            shutil.copytree(ROOT / "core" / "src" / "runtime", resources / "runtime")
            data_dir = temporary / "state"
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(APP_ROOT)}
            for key in ("OPTIONHELPER_RUNTIME_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT"):
                environment.pop(key, None)
            process = subprocess.Popen(
                [sys.executable, str(launcher), "--resource-dir", str(resources), "--data-dir", str(data_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment,
            )
            try:
                deadline, url = time.monotonic() + 10, None
                startup_output: list[str] = []
                while time.monotonic() < deadline and process.poll() is None:
                    line = process.stdout.readline()
                    startup_output.append(line)
                    if line.startswith("OPTIONHELPER_URL="):
                        url = line.strip().split("=", 1)[1]
                        break
                if url is None:
                    output = process.stdout.read() if process.poll() is not None else ""
                    self.fail("backend launcher did not publish a loopback URL\n" + "".join(startup_output) + output)
                with urlopen(f"{url}/api/health", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                expected_tree_hash = json.loads((resources / "capability" / "option-helper" / "capability-manifest.json").read_text(encoding="utf-8"))["content_tree_hash"]
                self.assertEqual(payload["capability"]["content_tree_hash"], expected_tree_hash)
                request = Request(
                    f"{url}/api/auth/login",
                    data=json.dumps({"account": "", "password": "", "remember": False}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    login = json.loads(response.read().decode("utf-8"))
                self.assertEqual(login["mode"], "local-development")
                self.assertEqual(login["identity"]["principal_id"], "local:User1")
                self.assertEqual(login["identity"]["role"], "admin")
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
