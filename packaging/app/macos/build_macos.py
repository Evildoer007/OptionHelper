#!/usr/bin/env python3
"""Build the immutable macOS OptionHelper App from one verified Capability."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
APP_ICON = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns"
DIST_ROOT = ROOT / "dist"
VERSIONS_ROOT = ROOT / "versions"
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from verify_skill import content_tree_entries, tree_hash, verify_skill
from release_contract import RELEASE_VERSION, require_release_version


class MacOSBuildError(RuntimeError):
    pass


def _progress(message: str) -> None:
    print(f"[platform] {message}", flush=True)


NUMERIC_RUNTIME_MODULES = ("numpy", "pandas", "scipy", "numba")
"""Modules used by the shipped Capability's pricing, payoff, and data flows."""
PDF_RUNTIME_MODULES = (
    "reportlab",
    "reportlab.pdfgen.canvas",
    "reportlab.pdfbase.pdfmetrics",
    "reportlab.pdfbase.cidfonts",
    "PIL",
    "PIL.Image",
)
PDF_RUNTIME_VERSION = "5.0.0"
PILLOW_RUNTIME_VERSION = "12.3.0"
"""The minimal deterministic runtime used for Card and Report PDF delivery."""

# The build host is a broad research environment.  These packages are neither
# imported by the App Host nor required by the verified Capability.  Explicit
# exclusions prevent PyInstaller from following optional test/example imports
# into the host's ML and desktop stacks.
EXCLUDED_BACKEND_MODULES = (
    "IPython", "PySide6", "cv2", "datasets", "debugpy", "h5py",
    "jedi", "keras", "matplotlib", "pyarrow", "pytest", "sklearn", "tensorflow",
    "tensorboard", "torch", "torchaudio", "torchvision", "transformers",
    "pandas.tests", "numpy.tests", "scipy.tests", "numba.tests",
)
MAX_BACKEND_BYTES = 600 * 1024 * 1024
MAX_APP_BUNDLE_BYTES = 750 * 1024 * 1024
MAX_DMG_BYTES = 300 * 1024 * 1024
MINIMUM_BUILD_PYTHON = (3, 11)
PYINSTALLER_VERSION = "6.21.0"
FINDER_COPY_PATTERN = re.compile(r"^(?P<base>.+) (?P<copy>\d+)(?P<suffix>(?:\.[^.]+)*)$")


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise MacOSBuildError(f"命令失败：{' '.join(command)}\n{completed.stdout}")


def prepare_dmg_layout(bundle: Path, destination: Path) -> Path:
    """Create the conventional drag-to-Applications DMG layout."""
    if not bundle.is_dir() or bundle.name != "OptionHelper.app":
        raise MacOSBuildError("DMG布局缺少OptionHelper.app")
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(bundle, destination / bundle.name, symlinks=True)
    (destination / "Applications").symlink_to("/Applications", target_is_directory=True)
    return destination


def verify_dmg_install_layout(
    dmg_path: Path,
    *,
    expected_content_tree_hash: str,
    expected_manifest_hash: str,
) -> None:
    """Mount the image and verify install layout plus the embedded Capability."""
    if not dmg_path.is_file():
        raise MacOSBuildError("DMG安装物不存在")
    with tempfile.TemporaryDirectory(prefix="optionhelper-dmg-verify-") as temporary_name:
        mountpoint = Path(temporary_name) / "mounted"
        mountpoint.mkdir()
        attached = False
        try:
            run([
                "hdiutil", "attach", "-readonly", "-nobrowse",
                "-mountpoint", str(mountpoint), str(dmg_path),
            ])
            attached = True
            app = mountpoint / "OptionHelper.app"
            applications = mountpoint / "Applications"
            if not app.is_dir():
                raise MacOSBuildError("DMG打开后缺少OptionHelper.app")
            if not applications.is_symlink() or os.readlink(applications) != "/Applications":
                raise MacOSBuildError("DMG打开后缺少指向/Applications的拖拽入口")
            embedded = app / "Contents" / "Resources" / "capability" / "option-helper"
            embedded_manifest = verified_capability(embedded)
            if embedded_manifest.get("content_tree_hash") != expected_content_tree_hash:
                raise MacOSBuildError("DMG内置Capability content_tree_hash与当次Skill不一致")
            if file_hash(embedded / "capability-manifest.json") != expected_manifest_hash:
                raise MacOSBuildError("DMG内置Capability Manifest哈希与当次Skill不一致")
        finally:
            if attached:
                run(["hdiutil", "detach", str(mountpoint)])


def _commit_release_artifacts(
    *,
    dmg: Path,
    output_dmg: Path,
    history_stage: Path,
    release_root: Path,
    archive_names: tuple[str, ...],
    transaction_stage: bool = False,
) -> bool:
    """Commit the candidate DMG and its archive together, or restore both stages."""
    created_root = False
    if release_root.exists():
        if not transaction_stage:
            raise MacOSBuildError("App版本已存在；请提高app_version")
    else:
        release_root.mkdir(parents=False, exist_ok=False)
        created_root = True
    if any((release_root / name).exists() for name in archive_names):
        raise MacOSBuildError("App版本已存在；请提高app_version")
    try:
        for name in archive_names:
            staged = history_stage / name
            archived = release_root / name
            os.replace(staged, archived)
        os.replace(dmg, output_dmg)
    except BaseException:
        _rollback_committed_release_artifacts(
            dmg=dmg,
            output_dmg=output_dmg,
            history_stage=history_stage,
            release_root=release_root,
            archive_names=archive_names,
            created_root=created_root,
        )
        raise
    return created_root


def _rollback_committed_release_artifacts(
    *,
    dmg: Path,
    output_dmg: Path,
    history_stage: Path,
    release_root: Path,
    archive_names: tuple[str, ...],
    created_root: bool,
) -> None:
    """Restore only this App's files after a failed transaction-stage check."""
    if output_dmg.exists() and not dmg.exists():
        os.replace(output_dmg, dmg)
    for name in reversed(archive_names):
        archived = release_root / name
        staged = history_stage / name
        if archived.exists() and not staged.exists():
            os.replace(archived, staged)
    if created_root and release_root.exists() and not any(release_root.iterdir()):
        release_root.rmdir()


def _validate_transaction_stage(release_root: Path) -> None:
    """Require the Catalog and Capability already staged by the release transaction."""
    required = (
        release_root / "knowledger" / "catalog-version.json",
        release_root / "option-helper.zip",
        release_root / "capability-manifest.json",
    )
    if not release_root.is_dir() or any(not path.is_file() for path in required):
        raise MacOSBuildError("macOS事务版本根缺少已签发Catalog或Capability")


def verified_capability(capability_root: Path) -> dict[str, Any]:
    errors = verify_skill(capability_root)
    if errors:
        raise MacOSBuildError("Capability未通过verify_skill：\n" + "\n".join(errors))
    manifest_path = capability_root / "capability-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_tree_hash = tree_hash(content_tree_entries(capability_root))
    if actual_tree_hash != manifest.get("content_tree_hash"):
        raise MacOSBuildError("Capability目录树哈希与Manifest不一致")
    return manifest


def assert_staged_capability(source: Path, staged: Path) -> dict[str, Any]:
    manifest = verified_capability(source)
    if not staged.is_dir():
        raise MacOSBuildError("products/app中缺少已stage的Capability")
    if content_tree_entries(source) != content_tree_entries(staged):
        raise MacOSBuildError("App内置Capability不是版本目录的逐字节副本")
    if (source / "capability-manifest.json").read_bytes() != (staged / "capability-manifest.json").read_bytes():
        raise MacOSBuildError("App内置Capability Manifest不是逐字节副本")
    return manifest


def app_manifest(
    app_version: str,
    capability: dict[str, Any],
    capability_manifest_hash: str,
    *,
    shell_language: str,
    build_tool: str,
) -> dict[str, Any]:
    return {
        "app_version": app_version,
        "bundle_id": "com.optionhelper.app",
        "platform": "macos-arm64",
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": capability_manifest_hash,
        "capability_content_tree_hash": capability["content_tree_hash"],
        "protocol_id": capability["protocol_id"],
        "design_system_id": capability["design_system_id"],
        "shell_language": shell_language,
        "build_tool": build_tool,
        "signing": {"method": "ad-hoc", "notarized": False},
    }


def platform_release_manifest(
    *,
    app_version: str,
    app_manifest_path: Path,
    dmg_path: Path,
    capability: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Describe one immutable macOS installer without claiming Apple release approval."""
    if not app_manifest_path.is_file() or not dmg_path.is_file():
        raise MacOSBuildError("PlatformReleaseManifest缺少App Manifest或DMG")
    return {
        "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}",
        "app_version": app_version,
        "platform": "macos",
        "architecture": "arm64",
        "app_manifest_hash": file_hash(app_manifest_path),
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": capability["capability_manifest_hash"],
        "capability_content_tree_hash": capability["capability_content_tree_hash"],
        "protocol_id": capability["protocol_id"],
        "design_system_id": capability["design_system_id"],
        "installer": {"filename": dmg_path.name, "sha256": file_hash(dmg_path), "size": dmg_path.stat().st_size},
        "installer_hash": file_hash(dmg_path),
        "signing_identity": "ad-hoc",
        "signature_status": "ad-hoc_validated",
        "notarized": False,
        "notarization_status": "unavailable",
        "release_status": "local_candidate",
        "formal_distribution_status": "formal_distribution_unavailable",
        "gatekeeper_distribution": "unavailable",
        "created_at": created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def verify_platform_release_manifest(path: Path, *, app_manifest_path: Path, dmg_path: Path) -> dict[str, Any]:
    """Verify the external release record against immutable local artifacts."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MacOSBuildError("PlatformReleaseManifest不是有效JSON") from error
    if not isinstance(value, dict):
        raise MacOSBuildError("PlatformReleaseManifest必须是对象")
    try:
        app_manifest = json.loads(app_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MacOSBuildError("App Manifest不是有效JSON") from error
    if not isinstance(app_manifest, dict):
        raise MacOSBuildError("App Manifest必须是对象")
    installer = value.get("installer")
    if not isinstance(installer, dict):
        raise MacOSBuildError("PlatformReleaseManifest缺少installer")
    checks = {
        "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}",
        "platform": "macos",
        "architecture": "arm64",
        "app_version": app_manifest.get("app_version"),
        "app_manifest_hash": file_hash(app_manifest_path),
        "capability_version": app_manifest.get("capability_version"),
        "catalog_version": app_manifest.get("catalog_version"),
        "capability_manifest_hash": app_manifest.get("capability_manifest_hash"),
        "capability_content_tree_hash": app_manifest.get("capability_content_tree_hash"),
        "protocol_id": app_manifest.get("protocol_id"),
        "design_system_id": app_manifest.get("design_system_id"),
        "installer_hash": file_hash(dmg_path),
        "signing_identity": "ad-hoc",
        "signature_status": "ad-hoc_validated",
        "notarized": False,
        "notarization_status": "unavailable",
        "release_status": "local_candidate",
        "formal_distribution_status": "formal_distribution_unavailable",
        "gatekeeper_distribution": "unavailable",
    }
    for key, expected in checks.items():
        if value.get(key) != expected:
            raise MacOSBuildError(f"PlatformReleaseManifest字段不匹配：{key}")
    if installer != {"filename": dmg_path.name, "sha256": file_hash(dmg_path), "size": dmg_path.stat().st_size}:
        raise MacOSBuildError("PlatformReleaseManifest安装物哈希不匹配")
    if not isinstance(value.get("created_at"), str) or not value["created_at"]:
        raise MacOSBuildError("PlatformReleaseManifest缺少created_at")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def assert_build_python() -> None:
    if sys.version_info < MINIMUM_BUILD_PYTHON:
        raise MacOSBuildError("App构建需要Python 3.11或更高版本")
    try:
        actual = metadata.version("PyInstaller")
    except metadata.PackageNotFoundError as error:
        raise MacOSBuildError("当前Python环境缺少PyInstaller") from error
    if actual != PYINSTALLER_VERSION:
        raise MacOSBuildError(f"PyInstaller必须固定为{PYINSTALLER_VERSION}，当前为{actual}")
    try:
        pdf_version = metadata.version("reportlab")
    except metadata.PackageNotFoundError as error:
        raise MacOSBuildError(f"App构建缺少PDF运行组件ReportLab {PDF_RUNTIME_VERSION}") from error
    if pdf_version != PDF_RUNTIME_VERSION:
        raise MacOSBuildError(f"ReportLab必须固定为{PDF_RUNTIME_VERSION}，当前为{pdf_version}")
    try:
        from PIL import __version__ as pillow_version
    except ImportError as error:
        raise MacOSBuildError(f"App构建缺少PDF图像组件Pillow {PILLOW_RUNTIME_VERSION}") from error
    if pillow_version != PILLOW_RUNTIME_VERSION:
        raise MacOSBuildError(f"Pillow必须固定为{PILLOW_RUNTIME_VERSION}，当前为{pillow_version}")


def copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return set(shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")(directory, names))

    shutil.copytree(source, destination, ignore=ignore)


def copy_licenses(destination: Path, capability_root: Path) -> None:
    destination.mkdir(parents=True)
    copy_tree(capability_root / "LICENSES", destination / "capability")
    pyinstaller_license = Path(__file__).resolve().parents[4] / ".missing"
    assert_build_python()
    try:
        import PyInstaller
        pyinstaller_license = Path(PyInstaller.__file__).resolve().parent.parent / f"pyinstaller-{PYINSTALLER_VERSION}.dist-info" / "licenses" / "COPYING.txt"
    except ImportError as error:
        raise MacOSBuildError("当前Python环境缺少PyInstaller") from error
    if not pyinstaller_license.is_file():
        candidates = list(Path(sys.prefix).glob("lib/python*/site-packages/pyinstaller-*.dist-info/licenses/COPYING.txt"))
        if len(candidates) != 1:
            raise MacOSBuildError("无法定位PyInstaller许可证")
        pyinstaller_license = candidates[0]
    shutil.copy2(pyinstaller_license, destination / "PyInstaller-COPYING.txt")
    (destination / "THIRD_PARTY.md").write_text(
        "# 第三方许可证\n\n"
        "- PyInstaller 6.21.0：见`PyInstaller-COPYING.txt`。\n"
        "- ReportLab 5.0.0：见`capability/ReportLab-LICENSE.txt`。\n"
        "- Pillow 12.3.0：见`capability/Pillow-LICENSE.txt`。\n"
        "- ECharts许可证随Capability页面供应，Apache-2.0声明保留在对应vendor文件头。\n"
        "- Capability自身归档见`capability/`。\n",
        encoding="utf-8",
    )


def backend_build_command(workspace: Path) -> list[str]:
    """Return the reproducible, minimal PyInstaller command for App Host."""
    launcher = APP_ROOT / "desktop" / "macos" / "backend_launcher.py"
    dist = workspace / "pyinstaller-dist"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "OptionHelperBackend", "--distpath", str(dist), "--workpath", str(workspace / "pyinstaller-work"),
        "--specpath", str(workspace / "pyinstaller-spec"), "--paths", str(APP_ROOT),
        "--exclude-module", "runtime", "--exclude-module", "modules",
    ]
    for module in NUMERIC_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    for module in PDF_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    # Designer verifies the pinned runtime through importlib.metadata inside the
    # frozen process; preserve that distribution metadata alongside the PYZ.
    command.extend(("--copy-metadata", "reportlab"))
    for module in EXCLUDED_BACKEND_MODULES:
        command.extend(("--exclude-module", module))
    command.append(str(launcher))
    return command


def _backend_size(package: Path) -> int:
    return sum(path.stat().st_size for path in package.rglob("*") if path.is_file())


def verify_backend_payload(package: Path) -> None:
    """Reject accidental collection of the build machine's unrelated stacks."""
    if not package.is_dir():
        raise MacOSBuildError("PyInstaller未生成OptionHelperBackend目录")
    size = _backend_size(package)
    if size > MAX_BACKEND_BYTES:
        raise MacOSBuildError(
            f"App Host体积{size / 1024 / 1024:.1f}MB超过{MAX_BACKEND_BYTES // 1024 // 1024}MB上限；"
            "请检查PyInstaller是否收集了构建环境的无关依赖"
        )
    internal = package / "_internal"
    forbidden = [module for module in EXCLUDED_BACKEND_MODULES if "." not in module and (internal / module).exists()]
    if forbidden:
        raise MacOSBuildError("App Host包含禁止的无关依赖：" + ", ".join(forbidden))
    # Pure-Python packages are stored in PyInstaller's PYZ archive and do not
    # necessarily materialize as ``_internal/<package>`` directories.  The
    # authoritative PDF gate is the frozen executable's runtime probe executed
    # after the verified Capability and runtime resources have been staged.


def copy_backend_bundle(package: Path, resources: Path) -> Path:
    """Preserve PyInstaller's shared-library symlinks inside the signed App."""
    target = resources / "backend" / "OptionHelperBackend"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, target, symlinks=True)
    return target


def build_backend(workspace: Path, resources: Path) -> Path:
    dist = workspace / "pyinstaller-dist"
    command = backend_build_command(workspace)
    environment = dict(os.environ)
    environment["PYINSTALLER_CONFIG_DIR"] = str(workspace / "pyinstaller-config")
    run(command, cwd=ROOT, env=environment)
    package = dist / "OptionHelperBackend"
    executable = package / "OptionHelperBackend"
    if not executable.is_file():
        raise MacOSBuildError("PyInstaller未生成OptionHelperBackend")
    verify_backend_payload(package)
    copied = copy_backend_bundle(package, resources)
    return copied / "OptionHelperBackend"


def finder_conflicts(root: Path) -> list[Path]:
    """Find numbered siblings only when their canonical sibling also exists."""
    conflicts: list[Path] = []
    for path in root.rglob("*"):
        match = FINDER_COPY_PATTERN.fullmatch(path.name)
        if not match:
            continue
        canonical = path.with_name(f"{match.group('base')}{match.group('suffix')}")
        if canonical.exists() or canonical.is_symlink():
            conflicts.append(path)
    return sorted(conflicts)


def assert_no_finder_conflicts(root: Path) -> None:
    conflicts = finder_conflicts(root)
    if conflicts:
        names = ", ".join(path.relative_to(root).as_posix() for path in conflicts)
        raise MacOSBuildError(f"App构建目录出现同步冲突副本：{names}")


def write_info_plist(bundle: Path, app_version: str) -> None:
    plist = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": "OptionHelper",
        "CFBundleIconFile": "OptionHelper.icns",
        "CFBundleIdentifier": "com.optionhelper.app",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "OptionHelper",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": app_version.removeprefix("v"),
        "CFBundleVersion": app_version.removeprefix("v"),
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)


def copy_app_icon(bundle: Path) -> None:
    if not APP_ICON.is_file():
        raise MacOSBuildError(f"缺少macOS应用图标：{APP_ICON}")
    shutil.copy2(APP_ICON, bundle / "Contents" / "Resources" / "OptionHelper.icns")


def copy_theme_icon_assets(resources: Path) -> None:
    source_root = ROOT / "assets" / "icons"
    target_root = resources / "assets" / "icons"
    target_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "optionhelper-app-icon-tile-light.svg",
        "optionhelper-app-icon-tile-dark.svg",
        "optionhelper-app-icon-tile-light.icns",
        "optionhelper-app-icon-tile-dark.icns",
    ):
        source = source_root / name
        if not source.is_file():
            raise MacOSBuildError(f"缺少主题图标资源：{source}")
        shutil.copy2(source, target_root / name)


def compile_shell(bundle: Path) -> tuple[Path, str, str]:
    output = bundle / "Contents" / "MacOS" / "OptionHelper"
    output.parent.mkdir(parents=True)
    swift = [
        "xcrun", "swiftc", str(APP_ROOT / "desktop" / "macos" / "OptionHelperApp.swift"),
        "-framework", "Cocoa", "-framework", "WebKit", "-o", str(output),
    ]
    try:
        run(swift)
        return output, "swift", "swiftc"
    except MacOSBuildError:
        if output.exists():
            output.unlink()
    objc = [
        "xcrun", "clang", "-fobjc-arc", str(APP_ROOT / "desktop" / "macos" / "OptionHelperApp.m"),
        "-framework", "Cocoa", "-framework", "WebKit", "-o", str(output),
    ]
    run(objc)
    return output, "objective-c", "clang"


def verify_bundle(bundle: Path, expected_manifest: dict[str, Any]) -> None:
    resources = bundle / "Contents" / "Resources"
    required = [
        bundle / "Contents" / "Info.plist", bundle / "Contents" / "MacOS" / "OptionHelper",
        resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend", resources / "app" / "backend" / "app_server.py",
        resources / "frontend" / "optchat" / "index.html", resources / "capability" / "option-helper" / "capability-manifest.json",
        resources / "runtime" / "protocol" / "models.py", resources / "LICENSES", resources / "app-manifest.json",
        resources / "OptionHelper.icns",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-dark.svg",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-dark.icns",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise MacOSBuildError("Bundle缺少资源：" + ", ".join(missing))
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        bundle_info = plistlib.load(handle)
    if bundle_info.get("CFBundleIconFile") != "OptionHelper.icns":
        raise MacOSBuildError("Info.plist未绑定OptionHelper.icns")
    actual = json.loads((resources / "app-manifest.json").read_text(encoding="utf-8"))
    if actual != expected_manifest:
        raise MacOSBuildError("Bundle app-manifest不匹配")
    capability = resources / "capability" / "option-helper"
    if verify_skill(capability):
        raise MacOSBuildError("Bundle内置Capability未通过verify_skill")
    if actual["capability_manifest_hash"] != file_hash(capability / "capability-manifest.json"):
        raise MacOSBuildError("Bundle Capability Manifest哈希不匹配")
    if actual["capability_content_tree_hash"] != tree_hash(content_tree_entries(capability)):
        raise MacOSBuildError("Bundle Capability目录树哈希不匹配")


def build_macos(
    app_version: str,
    capability_root: Path,
    *,
    dist_root: Path = DIST_ROOT,
    versions_root: Path = VERSIONS_ROOT,
    transaction_stage: bool = False,
) -> dict[str, Path]:
    try:
        require_release_version(app_version)
    except ValueError as error:
        raise MacOSBuildError(str(error)) from error
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise MacOSBuildError("macOS arm64构建必须在本机arm64 macOS执行")
    assert_build_python()
    dist_root = dist_root.resolve()
    versions_root = versions_root.resolve()
    output_dmg = dist_root / f"OptionHelper-{app_version}-macOS-arm64.dmg"
    release_root = versions_root / app_version
    if output_dmg.exists():
        raise MacOSBuildError("App版本已存在；请提高app_version")
    if release_root.exists():
        if not transaction_stage:
            raise MacOSBuildError("App版本已存在；请提高app_version")
        if versions_root == VERSIONS_ROOT.resolve():
            raise MacOSBuildError("macOS事务版本根不得使用正式versions目录")
        _validate_transaction_stage(release_root)
    elif transaction_stage:
        raise MacOSBuildError("macOS事务版本根不存在")

    _progress("正在验证当次Capability")
    source = capability_root.resolve()
    capability = verified_capability(source)
    capability_manifest_hash = file_hash(source / "capability-manifest.json")

    with tempfile.TemporaryDirectory(prefix="optionhelper-macos-") as temporary_name:
        temporary = Path(temporary_name)
        _progress("正在组装应用资源")
        bundle = temporary / "OptionHelper.app"
        resources = bundle / "Contents" / "Resources"
        resources.mkdir(parents=True)
        copy_app_icon(bundle)
        copy_theme_icon_assets(resources)
        write_info_plist(bundle, app_version)
        _shell, shell_language, build_tool = compile_shell(bundle)
        manifest = app_manifest(
            app_version,
            capability,
            capability_manifest_hash,
            shell_language=shell_language,
            build_tool=build_tool,
        )
        copy_tree(APP_ROOT / "backend", resources / "app" / "backend")
        copy_tree(APP_ROOT / "config", resources / "app" / "config")
        copy_tree(APP_ROOT / "frontend", resources / "frontend")
        copy_tree(source, resources / "capability" / "option-helper")
        assert_staged_capability(source, resources / "capability" / "option-helper")
        copy_tree(ROOT / "core" / "src" / "runtime", resources / "runtime")
        copy_licenses(resources / "LICENSES", source)
        write_json(resources / "app-manifest.json", manifest)
        _progress("正在构建后端运行时")
        backend = build_backend(temporary, resources)
        run([str(backend), "--probe-pdf-runtime", "--resource-dir", str(resources)])
        verify_bundle(bundle, manifest)
        assert_no_finder_conflicts(bundle)
        bundle_size = _backend_size(bundle)
        if bundle_size > MAX_APP_BUNDLE_BYTES:
            raise MacOSBuildError(
                f"macOS App体积{bundle_size / 1024 / 1024:.1f}MB超过"
                f"{MAX_APP_BUNDLE_BYTES // 1024 // 1024}MB上限；请检查重复资源和无关依赖"
            )
        _progress("正在签名并验证应用包")
        run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])
        _progress("正在生成DMG安装物")
        built_dmg = temporary / f"OptionHelper-{app_version}-macOS-arm64.dmg"
        dmg_layout = prepare_dmg_layout(bundle, temporary / "dmg-layout")
        run(["hdiutil", "create", "-volname", "OptionHelper", "-srcfolder", str(dmg_layout), "-format", "UDZO", str(built_dmg)])
        run(["hdiutil", "verify", str(built_dmg)])
        if built_dmg.stat().st_size > MAX_DMG_BYTES:
            raise MacOSBuildError(
                f"macOS安装物体积{built_dmg.stat().st_size / 1024 / 1024:.1f}MB超过"
                f"{MAX_DMG_BYTES // 1024 // 1024}MB上限；请检查重复资源和无关运行时依赖"
            )
        _progress("正在验证安装物")
        verify_dmg_install_layout(
            built_dmg,
            expected_content_tree_hash=str(capability["content_tree_hash"]),
            expected_manifest_hash=capability_manifest_hash,
        )
        dist_root.mkdir(parents=True, exist_ok=True)
        versions_root.mkdir(parents=True, exist_ok=True)
        output_stage = dist_root / f".{app_version}-staging-{uuid.uuid4().hex}"
        history_stage = versions_root / f".{app_version}-staging-{uuid.uuid4().hex}"
        try:
            output_stage.mkdir()
            dmg = output_stage / f"OptionHelper-{app_version}-macOS-arm64.dmg"
            shutil.copy2(built_dmg, dmg)
            run(["hdiutil", "verify", str(dmg)])
            app_manifest_path = output_stage / "app-manifest.json"
            write_json(app_manifest_path, manifest)
            release_manifest_path = output_stage / "platform-release-manifest.json"
            write_json(release_manifest_path, platform_release_manifest(
                app_version=app_version, app_manifest_path=app_manifest_path, dmg_path=dmg, capability=manifest,
            ))
            verify_platform_release_manifest(release_manifest_path, app_manifest_path=app_manifest_path, dmg_path=dmg)
            assert_no_finder_conflicts(output_stage)
            history_stage.mkdir()
            archive_stage = history_stage / dmg.name
            shutil.copy2(dmg, archive_stage)
            checksum = history_stage / f"{dmg.name}.sha256"
            checksum.write_text(f"{file_hash(archive_stage)}  {dmg.name}\n", encoding="utf-8")
            archive_manifest = history_stage / "app-manifest.json"
            archive_release = history_stage / "platform-release-manifest.json"
            shutil.copy2(app_manifest_path, archive_manifest)
            shutil.copy2(release_manifest_path, archive_release)
            verify_platform_release_manifest(archive_release, app_manifest_path=archive_manifest, dmg_path=archive_stage)
            assert_no_finder_conflicts(history_stage)
            _progress("正在写入临时交付记录")
            created_release_root = _commit_release_artifacts(
                dmg=dmg,
                output_dmg=output_dmg,
                history_stage=history_stage,
                release_root=release_root,
                archive_names=(archive_stage.name, checksum.name, archive_manifest.name, archive_release.name),
                transaction_stage=transaction_stage,
            )
            try:
                assert_no_finder_conflicts(dist_root)
                assert_no_finder_conflicts(release_root)
            except BaseException:
                _rollback_committed_release_artifacts(
                    dmg=dmg,
                    output_dmg=output_dmg,
                    history_stage=history_stage,
                    release_root=release_root,
                    archive_names=(archive_stage.name, checksum.name, archive_manifest.name, archive_release.name),
                    created_root=created_release_root,
                )
                raise
        finally:
            if output_stage.exists():
                shutil.rmtree(output_stage)
            if history_stage.exists():
                shutil.rmtree(history_stage)
    return {"dmg": output_dmg, "manifest": release_root / "app-manifest.json", "release_manifest": release_root / "platform-release-manifest.json", "history": release_root}


def main() -> None:
    parser = argparse.ArgumentParser(description="构建签名但未公证的OptionHelper macOS App")
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--capability-root", type=Path, required=True)
    args = parser.parse_args()
    artifacts = build_macos(args.app_version, args.capability_root)
    for name, path in artifacts.items():
        print(f"{name}={path}")


if __name__ == "__main__":
    main()
