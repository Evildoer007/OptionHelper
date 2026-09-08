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
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
APP_ICON = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns"
DMG_VOLUME_NAME = "OptionHelper"
DMG_BACKGROUND = Path(__file__).resolve().parent / "assets" / "optionhelper-dmg-background.png"
DMG_BUNDLE_BACKGROUND = Path("Contents/Resources/installer/optionhelper-dmg-background.png")
DMG_WINDOW_SIZE = (760, 460)
DMG_ICON_SIZE = 128
DMG_ICON_POSITIONS = {
    "OptionHelper.app": (180, 230),
    "Applications": (580, 230),
}
DIST_ROOT = ROOT / "dist"
VERSIONS_ROOT = ROOT / "versions"
SKILL_PACKAGING = ROOT / "packaging" / "skill"
APP_PACKAGING = ROOT / "packaging" / "app"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))
if str(APP_PACKAGING) not in sys.path:
    sys.path.insert(0, str(APP_PACKAGING))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))
MACOS_PACKAGING = Path(__file__).resolve().parent
if str(MACOS_PACKAGING) not in sys.path:
    sys.path.insert(0, str(MACOS_PACKAGING))

from verify_skill import content_tree_entries, tree_hash
from verify_capability import verify_app_capability
from release_contract import skill_archive_name
from release_contract import APP_VERSION, RELEASE_VERSION, require_app_version, app_platform_versions
from verify_macos import verify as verify_frozen_macos_app
from platform_payload import (
    BACKEND_DESKTOP_COMMON_MODULES,
    MACOS_ALLOWED_RESOURCE_ENTRIES,
    MACOS_BUILD_INPUTS,
    MACOS_PAYLOAD_ROOTS,
    MACOS_SOURCE_MAPPINGS,
    MACOS_STRICT_DIRECTORY_PAYLOADS,
    PlatformPayloadError,
    assert_outer_resource_layout,
    build_outer_payload_manifest,
    stage_python_docx_templates,
    stage_verification_fixture_definition,
    verify_outer_payload_manifest,
)
from python_runtime_licenses import (
    PythonRuntimeLicenseError,
    stage_pyinstaller_dependency_licenses,
    verify_python_runtime_licenses,
)

AGENT_RUNTIME_PACKAGING = ROOT / "packaging" / "app" / "agent_runtime"
if str(AGENT_RUNTIME_PACKAGING) not in sys.path:
    sys.path.insert(0, str(AGENT_RUNTIME_PACKAGING))
from build_runtime import (  # noqa: E402
    ALLOW_SOURCE_RUNTIME_FALLBACK_ENV,
    LOCAL_VERIFIED,
    REQUIRE_NATIVE_RUNTIME_ENV,
    RuntimeBuildError,
    probe_runtime_process,
    prepare_staged_runtime,
    resolve_runtime_candidate,
    verify_staged_runtime,
)
from name_boundary import assert_agent_runtime_delivery_clean, assert_name_boundary_clean


class MacOSBuildError(RuntimeError):
    pass


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MacOSBuildError(f"环境变量{name}必须是布尔值")


def _runtime_required(explicit: bool | None, *, formal: bool) -> bool:
    if explicit is not None:
        return explicit
    return _env_flag(REQUIRE_NATIVE_RUNTIME_ENV, formal)


def _source_fallback_enabled(explicit: bool | None, *, required: bool) -> bool:
    if required:
        return False
    if explicit is not None:
        return explicit
    return _env_flag(ALLOW_SOURCE_RUNTIME_FALLBACK_ENV, True)


def _assert_source_name_boundary(*, repo_root: Path = ROOT) -> None:
    app_root = repo_root / "products" / "app"
    try:
        assert_name_boundary_clean(
            repo_root,
            paths=(app_root / "backend", app_root / "frontend", app_root / "runtime"),
        )
    except AssertionError as error:
        raise MacOSBuildError(str(error)) from error


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
    "pypdf",
    "docx",
    "bs4",
    "bs4.builder",
    "bs4.builder._htmlparser",
    "soupsieve",
    "lxml",
    "lxml.etree",
    "openpyxl",
)
PDF_RUNTIME_VERSION = "5.0.0"
PILLOW_RUNTIME_VERSION = "12.3.0"
PYPDF_RUNTIME_VERSION = "6.16.0"
PYTHON_DOCX_RUNTIME_VERSION = "1.2.0"
BEAUTIFULSOUP_RUNTIME_VERSION = "4.15.0"
SOUPSIEVE_RUNTIME_VERSION = "2.9.1"
LXML_RUNTIME_VERSION = "6.1.1"
OPENPYXL_RUNTIME_VERSION = "3.1.5"
DOCUMENT_RUNTIME_DISTRIBUTIONS = {
    "pypdf": PYPDF_RUNTIME_VERSION,
    "python-docx": PYTHON_DOCX_RUNTIME_VERSION,
    "beautifulsoup4": BEAUTIFULSOUP_RUNTIME_VERSION,
    "soupsieve": SOUPSIEVE_RUNTIME_VERSION,
    "lxml": LXML_RUNTIME_VERSION,
    "openpyxl": OPENPYXL_RUNTIME_VERSION,
}
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
EXTERNAL_COMMAND_TIMEOUT_SECONDS = 900
MINIMUM_BUILD_PYTHON = (3, 12)
PYINSTALLER_VERSION = "6.21.0"
FINDER_COPY_PATTERN = re.compile(r"^(?P<base>.+) (?P<copy>\d+)(?P<suffix>(?:\.[^.]+)*)$")
MACOS_SIGNING_IDENTITY_ENV = "OPTIONHELPER_MACOS_SIGNING_IDENTITY"
MACOS_NOTARY_PROFILE_ENV = "OPTIONHELPER_MACOS_NOTARY_PROFILE"
REQUIRED_CAPABILITY_LICENSES = (
    "ReportLab-LICENSE.txt",
    "Pillow-LICENSE.txt",
    "pypdf-LICENSE.txt",
    "python-docx-LICENSE.txt",
    "openpyxl-LICENSE.txt",
)


def backend_runtime_modules(*, app_root: Path = APP_ROOT) -> tuple[str, ...]:
    """Return every App backend module that must survive freezing.

    PyInstaller does not reliably follow the launcher's delayed import of the
    App server into every relative submodule.  Deriving the hidden-import list
    from the checked-in backend package keeps newly added runtime modules from
    disappearing only in the packaged App.
    """

    modules: set[str] = set()
    source_root = app_root / "backend"
    for source in source_root.rglob("*.py"):
        relative = source.relative_to(app_root).with_suffix("")
        parts = relative.parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.add(".".join(parts))
    return tuple(sorted(modules))


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        raise MacOSBuildError(f"命令超时：{' '.join(command)}\n{output}") from error
    if completed.returncode:
        raise MacOSBuildError(f"命令失败：{' '.join(command)}\n{completed.stdout}")
    return completed.stdout


def formal_signing_configuration() -> tuple[str, str]:
    """Return real release credentials without accepting ad-hoc substitutes."""

    identity = os.environ.get(MACOS_SIGNING_IDENTITY_ENV, "").strip()
    profile = os.environ.get(MACOS_NOTARY_PROFILE_ENV, "").strip()
    if not identity.startswith("Developer ID Application:"):
        raise MacOSBuildError(
            f"正式macOS发布必须通过{MACOS_SIGNING_IDENTITY_ENV}指定Developer ID Application身份"
        )
    if not profile:
        raise MacOSBuildError(
            f"正式macOS发布必须通过{MACOS_NOTARY_PROFILE_ENV}指定notarytool钥匙串配置"
        )
    identities = run(["security", "find-identity", "-v", "-p", "codesigning"])
    if identity not in identities:
        raise MacOSBuildError("当前钥匙串未找到指定Developer ID Application签名身份")
    return identity, profile


def verify_formal_app_bundle(
    bundle: Path,
    *,
    expected_identity: str | None = None,
    require_gatekeeper: bool = True,
) -> None:
    """Require Developer ID trust, and Gatekeeper after notarization is complete."""

    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])
    details = run(["codesign", "--display", "--verbose=4", str(bundle)])
    if "Signature=adhoc" in details or "TeamIdentifier=not set" in details:
        raise MacOSBuildError("正式macOS App不得使用ad-hoc签名")
    if "Authority=Developer ID Application:" not in details:
        raise MacOSBuildError("正式macOS App缺少Developer ID Application Authority")
    if expected_identity is not None and f"Authority={expected_identity}" not in details:
        raise MacOSBuildError("正式macOS App签名身份与发布配置不一致")
    if require_gatekeeper:
        run(["spctl", "--assess", "--type", "execute", "--verbose=4", str(bundle)])


def verify_formal_dmg_distribution(dmg: Path) -> None:
    """Require a stapled notarization ticket and Gatekeeper acceptance."""

    run(["xcrun", "stapler", "validate", str(dmg)])
    run([
        "spctl", "--assess", "--type", "open",
        "--context", "context:primary-signature", "--verbose=4", str(dmg),
    ])


def require_formal_signing_record(signing: dict[str, Any]) -> str:
    """Validate the persisted trust claim before it can enter a formal record."""

    identity = str(signing.get("identity", "")).strip()
    if signing.get("method") != "developer-id" or not identity.startswith("Developer ID Application:"):
        raise MacOSBuildError("正式macOS记录缺少Developer ID签名身份")
    if not (
        signing.get("notarized") is True
        and signing.get("app_stapled") is False
        and signing.get("installer_stapled") is True
        and signing.get("gatekeeper") == "accepted"
    ):
        raise MacOSBuildError("正式macOS记录缺少公证、DMG staple或Gatekeeper成功记录")
    return identity


def prepare_dmg_layout(
    bundle: Path,
    destination: Path,
    *,
    background_source: Path = DMG_BACKGROUND,
) -> Path:
    """Create the conventional drag-to-Applications DMG layout."""
    if not bundle.is_dir() or bundle.name != "OptionHelper.app":
        raise MacOSBuildError("DMG布局缺少OptionHelper.app")
    if not background_source.is_file():
        raise MacOSBuildError(f"缺少DMG背景资源：{background_source}")
    if not (bundle / DMG_BUNDLE_BACKGROUND).is_file():
        raise MacOSBuildError("OptionHelper.app缺少已签名DMG背景资源")
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(bundle, destination / bundle.name, symlinks=True)
    (destination / "Applications").symlink_to("/Applications", target_is_directory=True)
    return destination


def _write_finder_metadata(mountpoint: Path) -> Path:
    """Write the Finder layout deterministically without launching Finder."""
    try:
        from ds_store import DSStore
        from mac_alias import Alias
    except ImportError as error:
        raise MacOSBuildError("当前Python环境缺少ds-store或mac-alias构建依赖") from error

    background = mountpoint / "OptionHelper.app" / DMG_BUNDLE_BACKGROUND
    if not background.is_file():
        raise MacOSBuildError("DMG布局缺少已签名背景资源")
    width, height = DMG_WINDOW_SIZE
    bwsp = {
        "ShowStatusBar": False,
        "WindowBounds": f"{{{{160, 140}}, {{{width}, {height}}}}}",
        "ContainerShowSidebar": False,
        "PreviewPaneVisibility": False,
        "SidebarWidth": 180,
        "ShowTabView": False,
        "ShowToolbar": False,
        "ShowPathbar": False,
        "ShowSidebar": False,
    }
    icvp = {
        "viewOptionsVersion": 1,
        "backgroundType": 2,
        "backgroundImageAlias": Alias.for_file(str(background)).to_bytes(),
        "backgroundColorRed": 1.0,
        "backgroundColorGreen": 1.0,
        "backgroundColorBlue": 1.0,
        "gridOffsetX": 0.0,
        "gridOffsetY": 0.0,
        "gridSpacing": 100.0,
        "arrangeBy": "none",
        "showIconPreview": False,
        "showItemInfo": False,
        "labelOnBottom": True,
        "textSize": 13.0,
        "iconSize": float(DMG_ICON_SIZE),
        "scrollPositionX": 0.0,
        "scrollPositionY": 0.0,
    }
    metadata = mountpoint / ".DS_Store"
    with DSStore.open(str(metadata), "w+") as store:
        store["."]["vSrn"] = ("long", 1)
        store["."]["bwsp"] = bwsp
        store["."]["icvp"] = icvp
        store["."]["icvl"] = (b"type", b"icnv")
        for name, position in DMG_ICON_POSITIONS.items():
            store[name]["Iloc"] = position
    return metadata


def _plist_output(output: str | bytes | None) -> dict[str, Any] | None:
    """Parse structured ``hdiutil`` output; empty tool output is unstructured."""
    if output is None:
        return None
    raw = output if isinstance(output, bytes) else output.encode()
    start = raw.find(b"<?xml")
    if start < 0:
        start = raw.find(b"bplist00")
    if start < 0:
        return None
    try:
        value = plistlib.loads(raw[start:])
    except (plistlib.InvalidFileException, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _system_entities(document: dict[str, Any]) -> list[dict[str, Any]]:
    entities = document.get("system-entities")
    if isinstance(entities, list):
        return [entity for entity in entities if isinstance(entity, dict)]
    images = document.get("images")
    if isinstance(images, list):
        for image in reversed(images):
            if isinstance(image, dict) and isinstance(image.get("system-entities"), list):
                return [entity for entity in image["system-entities"] if isinstance(entity, dict)]
    return []


def _attached_mountpoint(output: str) -> Path:
    """Extract the mountpoint from this attach operation, never global state."""
    document = _plist_output(output)
    if document is not None:
        for entity in reversed(_system_entities(document)):
            mountpoint = entity.get("mount-point")
            if isinstance(mountpoint, str) and mountpoint:
                return Path(mountpoint)
    raise MacOSBuildError("hdiutil未返回本次DMG挂载点的结构化信息")


def _attached_device(output: str) -> str | None:
    document = _plist_output(output)
    if document is not None:
        for entity in reversed(_system_entities(document)):
            device = entity.get("dev-entry")
            if isinstance(device, str) and device.startswith("/dev/"):
                return device
    return None


def _attached_image_target(output: str, image: Path) -> tuple[Path | None, str | None]:
    document = _plist_output(output)
    if document is None:
        return None, None
    images = document.get("images")
    if not isinstance(images, list):
        return None, None
    for record in images:
        if not isinstance(record, dict):
            continue
        image_path = record.get("image-path")
        if not isinstance(image_path, str):
            continue
        if Path(os.path.realpath(image_path)) != Path(os.path.realpath(image)):
            continue
        entities = _system_entities(record)
        mountpoint = next((entity.get("mount-point") for entity in reversed(entities) if entity.get("mount-point")), None)
        device = next((entity.get("dev-entry") for entity in reversed(entities) if entity.get("dev-entry")), None)
        return (
            Path(mountpoint) if isinstance(mountpoint, str) else None,
            device if isinstance(device, str) else None,
        )
    return None, None


def _disk_info_field(output: str | bytes | None, field: str) -> str:
    if output is None:
        raise MacOSBuildError(f"diskutil信息缺少{field}")
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    match = re.search(rf"^\s*{re.escape(field)}:\s*(.+?)\s*$", output, re.MULTILINE)
    if not match:
        raise MacOSBuildError(f"diskutil信息缺少{field}")
    return match.group(1)


def _temporary_volume_name() -> str:
    return f"OptionHelper-build-{uuid.uuid4().hex[:10]}"


def _validate_finder_metadata_hygiene(metadata: bytes) -> None:
    """Reject stale volume identities while allowing Finder's backing-image bookmark."""
    stale_fragments = (
        b"OptionHelper-build-",
        b"optionhelper-branded-smoke",
        b"/Volumes/OptionHelper-",
    )
    if any(fragment in metadata for fragment in stale_fragments):
        raise MacOSBuildError("Finder背景别名仍引用临时卷")
    if b"OptionHelper" not in metadata or b"OptionHelper.app" not in metadata:
        raise MacOSBuildError("Finder背景别名未绑定OptionHelper安装卷")
    if b"optionhelper-dmg-background.png" not in metadata:
        raise MacOSBuildError("Finder背景别名缺少背景文件名")
    if b"/OptionHelper.app/Contents/Resources/installer/optionhelper-dmg-background.png" not in metadata:
        raise MacOSBuildError("Finder背景别名缺少安装卷内相对路径")


def _preserve_writable_image(writable: Path) -> Path:
    """Copy a still-mounted work image outside the caller's temp workspace."""
    recovery_root = Path(tempfile.mkdtemp(prefix="optionhelper-dmg-recovery-"))
    recovery_image = recovery_root / writable.name
    try:
        shutil.copy2(writable, recovery_image)
    except BaseException:
        shutil.rmtree(recovery_root, ignore_errors=True)
        raise
    return recovery_image


def _remove_temporary_dmg_source(source_directory: Path, workspace: Path) -> None:
    """Remove only the builder-owned DMG source, including read-only snapshot copies."""
    if not source_directory.exists() and not source_directory.is_symlink():
        return
    workspace_root = workspace.resolve()
    if (
        source_directory.is_symlink()
        or source_directory.parent.resolve() != workspace_root
        or not source_directory.name.startswith("optionhelper-dmg-source-")
    ):
        raise MacOSBuildError("拒绝清理不属于当前构建工作区的DMG源目录")

    try:
        pending = [source_directory]
        while pending:
            directory = pending.pop()
            metadata = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise MacOSBuildError("临时DMG源包含异常目录对象")
            directory.chmod(
                stat.S_IMODE(metadata.st_mode)
                | stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IXUSR,
                follow_symlinks=False,
            )
            with os.scandir(directory) as entries:
                pending.extend(
                    Path(entry.path)
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                )
        shutil.rmtree(source_directory)
    except MacOSBuildError:
        raise
    except OSError as error:
        raise MacOSBuildError(f"临时DMG源目录清理失败：{error}") from error


def build_styled_dmg(layout: Path, output: Path, workspace: Path) -> None:
    """Write Finder metadata on the final volume before compressing it."""
    if output.exists():
        raise MacOSBuildError("DMG输出文件已存在")
    workspace.mkdir(parents=True, exist_ok=True)
    writable = workspace / f"{output.stem}-writable.dmg"
    mountpoint: Path | None = None
    active_mountpoint: Path | None = None
    temporary_volume_name = _temporary_volume_name()
    source_directory: Path | None = None
    attached = False
    succeeded = False
    active_device: str | None = None
    try:
        # Finder metadata is created on the final mounted volume. Never copy
        # a stale alias from a staging directory into the image.
        source_directory = Path(tempfile.mkdtemp(prefix="optionhelper-dmg-source-", dir=workspace))
        shutil.copytree(
            layout,
            source_directory,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".DS_Store"),
        )
        run([
            "hdiutil", "create", "-volname", temporary_volume_name,
            "-srcfolder", str(source_directory), "-fs", "HFS+", "-format", "UDRW",
            "-ov", str(writable),
        ])
        if not writable.is_file():
            raise MacOSBuildError("hdiutil未生成可写DMG工作盘")
        # Keep the volume mountable for direct metadata generation while
        # suppressing hdiutil's automatic Finder window.
        attach_output = run(["hdiutil", "attach", "-plist", "-readwrite", "-noautoopen", str(writable)])
        attached = True
        active_device = _attached_device(attach_output)
        try:
            mountpoint = _attached_mountpoint(attach_output)
        except MacOSBuildError:
            try:
                info_output = run(["hdiutil", "info", "-plist"])
                mountpoint, info_device = _attached_image_target(info_output, writable)
                active_device = info_device or active_device
                if mountpoint is None:
                    raise MacOSBuildError("hdiutil信息未找到本次DMG挂载点")
                active_mountpoint = mountpoint
            except MacOSBuildError:
                pass
            raise
        active_mountpoint = mountpoint
        device = _disk_info_field(run(["diskutil", "info", str(mountpoint)]), "Device Identifier")
        active_device = f"/dev/{device}"
        run(["diskutil", "rename", device, DMG_VOLUME_NAME])
        final_mountpoint = Path(_disk_info_field(run(["diskutil", "info", device]), "Mount Point"))
        active_mountpoint = final_mountpoint
        metadata = _write_finder_metadata(final_mountpoint)
        run(["sync"])
        _validate_finder_metadata_hygiene(metadata.read_bytes())
        allowed_entries = {"OptionHelper.app", "Applications", ".DS_Store"}
        for entry in final_mountpoint.iterdir():
            if entry.name in allowed_entries:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        run(["hdiutil", "detach", str(final_mountpoint)])
        attached = False
        active_mountpoint = None
        run([
            "hdiutil", "convert", str(writable), "-format", "UDZO",
            "-imagekey", "zlib-level=9", "-ov", "-o", str(output),
        ])
        if not output.is_file():
            raise MacOSBuildError("hdiutil未生成DMG安装物")
        succeeded = True
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        if attached:
            detach_targets = [target for target in (active_mountpoint, active_device) if target is not None]
            for detach_target in detach_targets:
                try:
                    run(["hdiutil", "detach", "-force", str(detach_target)])
                except BaseException as error:
                    cleanup_errors.append(error)
                else:
                    attached = False
                    break
            if attached:
                try:
                    recovery_image = _preserve_writable_image(writable)
                    cleanup_errors.append(MacOSBuildError(
                        f"DMG挂载设备未能卸载；可恢复工作盘：{recovery_image}"
                    ))
                except BaseException as error:
                    cleanup_errors.append(MacOSBuildError(f"DMG挂载设备未能卸载，工作盘备份失败：{error}"))
        for cleanup in (
            lambda: writable.unlink() if not attached and writable.exists() else None,
            lambda: _remove_temporary_dmg_source(source_directory, workspace) if source_directory is not None else None,
            lambda: output.unlink() if not succeeded and output.exists() else None,
        ):
            try:
                cleanup()
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            details = "；".join(str(error) for error in cleanup_errors)
            if original_error is None:
                raise cleanup_errors[0]
            raise MacOSBuildError(f"{original_error}；清理阶段：{details}") from original_error


def verify_dmg_install_layout(
    dmg_path: Path,
    *,
    expected_content_tree_hash: str,
    expected_manifest_hash: str,
    runtime_probe: Callable[[Path], object] | None = None,
    background_source: Path | None = None,
) -> None:
    """Mount the image and verify install layout plus the embedded Capability."""
    if not dmg_path.is_file():
        raise MacOSBuildError("DMG安装物不存在")
    with tempfile.TemporaryDirectory(prefix="optionhelper-dmg-verify-") as temporary_name:
        mountpoint = Path(temporary_name) / "mounted"
        mountpoint.mkdir()
        attached = False
        attached_device: str | None = None
        try:
            attach_output = run([
                "hdiutil", "attach", "-plist", "-readonly", "-nobrowse",
                "-mountpoint", str(mountpoint), str(dmg_path),
            ])
            attached = True
            attached_device = _attached_device(attach_output)
            if attached_device is None:
                try:
                    device = _disk_info_field(run(["diskutil", "info", str(mountpoint)]), "Device Identifier")
                    attached_device = f"/dev/{device}"
                except MacOSBuildError:
                    pass
            app = mountpoint / "OptionHelper.app"
            applications = mountpoint / "Applications"
            root_entries = {
                entry.name for entry in mountpoint.iterdir()
                if entry.name != ".DS_Store"
            }
            if root_entries != {"OptionHelper.app", "Applications"}:
                extras = ", ".join(sorted(root_entries - {"OptionHelper.app", "Applications"}))
                raise MacOSBuildError(f"DMG根目录含非安装入口：{extras or '布局不完整'}")
            if not app.is_dir():
                raise MacOSBuildError("DMG打开后缺少OptionHelper.app")
            verify_installable_bundle_permissions(app)
            if not applications.is_symlink() or os.readlink(applications) != "/Applications":
                raise MacOSBuildError("DMG打开后缺少指向/Applications的拖拽入口")
            try:
                assert_agent_runtime_delivery_clean(app / "Contents" / "Resources")
            except AssertionError as error:
                raise MacOSBuildError(str(error)) from error
            embedded = app / "Contents" / "Resources" / "capability" / "option-helper"
            embedded_manifest = verified_capability(embedded)
            if embedded_manifest.get("content_tree_hash") != expected_content_tree_hash:
                raise MacOSBuildError("DMG内置Capability content_tree_hash与当次Skill不一致")
            if file_hash(embedded / "capability-manifest.json") != expected_manifest_hash:
                raise MacOSBuildError("DMG内置Capability Manifest哈希与当次Skill不一致")
            background_image = app / DMG_BUNDLE_BACKGROUND
            if not background_image.is_file():
                raise MacOSBuildError("DMG背景资源缺失")
            if background_source is not None and file_hash(background_image) != file_hash(background_source):
                raise MacOSBuildError("DMG背景资源内容不一致")
            finder_metadata = mountpoint / ".DS_Store"
            if not finder_metadata.is_file():
                raise MacOSBuildError("DMG缺少Finder安装布局元数据")
            _validate_finder_metadata_hygiene(finder_metadata.read_bytes())
            if runtime_probe is not None:
                runtime_probe(app)
        finally:
            original_error = sys.exc_info()[1]
            detach_error: BaseException | None = None
            if attached:
                detach_targets = [mountpoint] + ([attached_device] if attached_device else [])
                for target in detach_targets:
                    try:
                        run(["hdiutil", "detach", str(target)])
                    except BaseException:
                        try:
                            run(["hdiutil", "detach", "-force", str(target)])
                        except BaseException as force_error:
                            detach_error = force_error
                            continue
                    detach_error = None
                    attached = False
                    break
            if detach_error is not None:
                if original_error is None:
                    raise detach_error
                raise MacOSBuildError(
                    f"{original_error}；DMG卸载失败：{detach_error}"
                ) from original_error


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
        release_root / "knowledger" / "source-manifest.json",
        release_root / skill_archive_name(),
        release_root / "capability-manifest.json",
    )
    if not release_root.is_dir() or any(not path.is_file() for path in required):
        raise MacOSBuildError("macOS事务版本根缺少已签发Catalog或Capability")


def verified_capability(capability_root: Path) -> dict[str, Any]:
    errors = verify_app_capability(capability_root)
    if errors:
        raise MacOSBuildError("App Capability未通过验收：\n" + "\n".join(errors))
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
    agent_runtime: dict[str, Any] | None = None,
    outer_payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    formal = app_version == APP_VERSION
    return {
        "app_version": app_version,
        "build_version": APP_VERSION,
        "release_status": "formal_release" if formal else "local_candidate",
        "formal_release": formal,
        "bundle_id": "com.optionhelper.app",
        "platform": "macos-arm64",
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": capability_manifest_hash,
        "capability_content_tree_hash": capability["content_tree_hash"],
        "shared_payload_hash": capability["shared_payload_hash"],
        "protocol_id": capability["protocol_id"],
        "design_system_id": capability["design_system_id"],
        "shell_language": shell_language,
        "build_tool": build_tool,
        "signing": (
            {
                "method": "developer-id",
                "identity": os.environ.get(MACOS_SIGNING_IDENTITY_ENV, "").strip(),
                "notarized": True,
                "app_stapled": False,
                "installer_stapled": True,
                "gatekeeper": "accepted",
            }
            if formal else
            {
                "method": "ad-hoc",
                "notarized": False,
                "app_stapled": False,
                "installer_stapled": False,
                "gatekeeper": "unavailable",
            }
        ),
        "agent_runtime": agent_runtime or {
            "status": "disabled",
            "support_status": "development_only",
            "resource_path": None,
            "manifest_path": None,
        },
        "outer_payload": outer_payload or {},
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
    agent_runtime = capability.get(
        "agent_runtime",
        {"status": "disabled", "support_status": "development_only"},
    )
    formal = app_version == APP_VERSION
    signing = capability.get("signing")
    if not isinstance(signing, dict):
        if formal:
            raise MacOSBuildError("App Manifest缺少签名记录")
        signing = {"method": "ad-hoc", "notarized": False}
    signing_identity = require_formal_signing_record(signing) if formal else "ad-hoc"
    return {
        "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}" if formal else "optionhelper.platform-candidate-manifest",
        "app_version": app_version,
        "platform": "macos",
        "architecture": "arm64",
        "app_manifest_hash": file_hash(app_manifest_path),
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": capability["capability_manifest_hash"],
        "capability_content_tree_hash": capability["capability_content_tree_hash"],
        "shared_payload_hash": capability["shared_payload_hash"],
        "protocol_id": capability["protocol_id"],
        "design_system_id": capability["design_system_id"],
        "agent_runtime": agent_runtime,
        "agent_runtime_status": agent_runtime.get("status") if isinstance(agent_runtime, dict) else None,
        "package_static_acceptance": "passed",
        "backend_api_acceptance": "passed",
        "native_interaction_acceptance": "not_run",
        "installer": {"filename": dmg_path.name, "sha256": file_hash(dmg_path), "size": dmg_path.stat().st_size},
        "installer_hash": file_hash(dmg_path),
        "signing_identity": signing_identity,
        "signature_status": "developer_id_validated" if formal else "ad-hoc_validated",
        "notarized": formal,
        "notarization_status": "accepted" if formal else "unavailable",
        "release_status": "formal_release" if formal else "local_candidate",
        "formal_distribution_status": "ready" if formal else "formal_distribution_unavailable",
        "gatekeeper_distribution": "accepted" if formal else "unavailable",
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
    formal = app_manifest.get("app_version") == APP_VERSION
    signing = app_manifest.get("signing")
    if not isinstance(signing, dict):
        if formal:
            raise MacOSBuildError("App Manifest缺少签名记录")
        signing = {"method": "ad-hoc", "notarized": False}
    signing_identity = require_formal_signing_record(signing) if formal else "ad-hoc"
    checks = {
        "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}" if formal else "optionhelper.platform-candidate-manifest",
        "platform": "macos",
        "architecture": "arm64",
        "app_version": app_manifest.get("app_version"),
        "app_manifest_hash": file_hash(app_manifest_path),
        "capability_version": app_manifest.get("capability_version"),
        "catalog_version": app_manifest.get("catalog_version"),
        "capability_manifest_hash": app_manifest.get("capability_manifest_hash"),
        "capability_content_tree_hash": app_manifest.get("capability_content_tree_hash"),
        "shared_payload_hash": app_manifest.get("shared_payload_hash"),
        "protocol_id": app_manifest.get("protocol_id"),
        "design_system_id": app_manifest.get("design_system_id"),
        "installer_hash": file_hash(dmg_path),
        "signing_identity": signing_identity,
        "signature_status": "developer_id_validated" if formal else "ad-hoc_validated",
        "notarized": formal,
        "notarization_status": "accepted" if formal else "unavailable",
        "release_status": "formal_release" if formal else "local_candidate",
        "formal_distribution_status": "ready" if formal else "formal_distribution_unavailable",
        "gatekeeper_distribution": "accepted" if formal else "unavailable",
    }
    if "agent_runtime" in app_manifest:
        runtime = app_manifest["agent_runtime"]
        if not isinstance(runtime, dict):
            raise MacOSBuildError("App Manifest中的Agent运行时字段无效")
        checks["agent_runtime"] = runtime
        checks["agent_runtime_status"] = runtime.get("status")
    checks["package_static_acceptance"] = "passed"
    checks["backend_api_acceptance"] = "passed"
    checks["native_interaction_acceptance"] = "not_run"
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
        raise MacOSBuildError("App构建需要Python 3.12或更高版本")
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
    for package, expected in DOCUMENT_RUNTIME_DISTRIBUTIONS.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError as error:
            raise MacOSBuildError(f"App构建缺少文档解析组件{package} {expected}") from error
        if actual != expected:
            raise MacOSBuildError(f"{package}必须固定为{expected}，当前为{actual}")


def copy_tree(source: Path, destination: Path, *, extra_ignored: tuple[str, ...] = ()) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        patterns = ("__pycache__", "*.pyc", ".DS_Store", *extra_ignored)
        return set(shutil.ignore_patterns(*patterns)(directory, names))

    shutil.copytree(source, destination, ignore=ignore)


def copy_licenses(destination: Path, capability_root: Path, *, license_root: Path = ROOT / "LICENSES") -> None:
    destination.mkdir(parents=True)
    copy_tree(capability_root / "LICENSES", destination / "capability")
    for name in REQUIRED_CAPABILITY_LICENSES:
        source = license_root / name
        if not source.is_file() or not source.read_bytes():
            raise MacOSBuildError(f"缺少必需第三方许可证：{source}")
        shutil.copy2(source, destination / "capability" / name)
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
    runtime_license_root = destination / "agent-runtime"
    runtime_license_root.mkdir()
    runtime_legal_files = (
        "OptionHelper-Agent-Runtime-LICENSE.txt",
        "OptionHelper-Agent-Runtime-NOTICES.txt",
        "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md",
    )
    for name in runtime_legal_files:
        source = license_root / name
        if not source.is_file():
            raise MacOSBuildError(f"缺少Agent Runtime发行声明：{source}")
        shutil.copy2(source, runtime_license_root / name)
    (destination / "THIRD_PARTY.md").write_text(
        "# 第三方许可证\n\n"
        "- PyInstaller 6.21.0：见`PyInstaller-COPYING.txt`。\n"
        "- ReportLab 5.0.0：见`capability/ReportLab-LICENSE.txt`。\n"
        "- Pillow 12.3.0：见`capability/Pillow-LICENSE.txt`。\n"
        "- pypdf 6.16.0：见`capability/pypdf-LICENSE.txt`。\n"
        "- python-docx 1.2.0：见`capability/python-docx-LICENSE.txt`。\n"
        "- openpyxl 3.1.5：见`capability/openpyxl-LICENSE.txt`。\n"
        "- ECharts许可证随Capability页面供应，Apache-2.0声明保留在对应vendor文件头。\n"
        "- Agent Runtime许可证、NOTICE和源码映射见`agent-runtime/`。\n"
        "- PyInstaller冻结Python依赖的许可证与NOTICE见`python-runtime/`清单。\n"
        "- Capability自身归档见`capability/`。\n",
        encoding="utf-8",
    )


def verify_required_licenses(resources: Path, *, license_root: Path = ROOT / "LICENSES") -> None:
    """Bind every declared runtime dependency to the repository license bytes."""

    third_party = resources / "LICENSES" / "THIRD_PARTY.md"
    if not third_party.is_file():
        raise MacOSBuildError("App缺少THIRD_PARTY.md")
    index = third_party.read_text(encoding="utf-8")
    for name in REQUIRED_CAPABILITY_LICENSES:
        source = license_root / name
        packaged = resources / "LICENSES" / "capability" / name
        if not source.is_file() or not packaged.is_file():
            raise MacOSBuildError(f"App缺少必需第三方许可证：{name}")
        if packaged.read_bytes() != source.read_bytes():
            raise MacOSBuildError(f"App第三方许可证与受控来源不一致：{name}")
        if f"`capability/{name}`" not in index:
            raise MacOSBuildError(f"THIRD_PARTY.md未索引必需许可证：{name}")
    if "`python-runtime/`" not in index:
        raise MacOSBuildError("THIRD_PARTY.md未索引PyInstaller冻结依赖许可证")


def backend_build_command(workspace: Path, *, app_root: Path = APP_ROOT) -> list[str]:
    """Return the reproducible, minimal PyInstaller command for App Host."""
    launcher = app_root / "desktop" / "macos" / "backend_launcher.py"
    dist = workspace / "pyinstaller-dist"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "OptionHelperBackend", "--distpath", str(dist), "--workpath", str(workspace / "pyinstaller-work"),
        "--specpath", str(workspace / "pyinstaller-spec"), "--paths", str(app_root),
    ]
    # The launcher imports the AppServer after configuring resource paths.
    # Make the module an explicit PyInstaller root so a rebuilt App cannot
    # retain an earlier frozen authorization policy.
    command.extend(("--hidden-import", "backend.app_server"))
    # The packaged verifier enables the launcher's explicit compute fixture.
    # Keep its App-owned installer module in the frozen import graph as well.
    command.extend(("--hidden-import", "backend.verification_fixture"))
    explicit_backend_modules = {"backend.app_server", "backend.verification_fixture"}
    for module in backend_runtime_modules(app_root=app_root):
        if module not in explicit_backend_modules:
            command.extend(("--hidden-import", module))
    for module in BACKEND_DESKTOP_COMMON_MODULES:
        command.extend(("--hidden-import", module))
    for module in NUMERIC_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    for module in PDF_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    # Designer verifies the pinned runtime through importlib.metadata inside the
    # frozen process; preserve that distribution metadata alongside the PYZ.
    command.extend(("--copy-metadata", "reportlab"))
    # python-docx loads its base DOCX, header, footer, styles, numbering, and
    # settings templates from package data when a document is constructed.
    command.extend(("--collect-data", "docx"))
    command.extend(("--collect-data", "certifi"))
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
    """Stage a regular-file-only backend tree for exact Manifest closure."""
    target = resources / "backend" / "OptionHelperBackend"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, target, symlinks=False)
    return target


def probe_compute_worker(backend: Path, resources: Path, workspace: Path) -> None:
    """Run the frozen worker probe with isolated external Stores.

    Importing the released contract layer performs the same Store-boundary
    check as a normal App run.  The build probe has no user runtime yet, so it
    supplies a disposable, workspace-local pair of Stores instead of
    accidentally inheriting the host's project or Skill directory.
    """
    runtime_root = workspace / "compute-worker-probe-runtime"
    data_root = runtime_root / "data"
    result_root = runtime_root / "result"
    data_root.mkdir(parents=True, exist_ok=False)
    result_root.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment["OPTIONHELPER_RUNTIME_ROOT"] = str(runtime_root)
    environment["OPTIONHELPER_DATA_ROOT"] = str(data_root)
    environment["OPTIONHELPER_RESULT_ROOT"] = str(result_root)
    diagnostic_root = runtime_root / "diagnostics"
    environment["OPTIONHELPER_COMPUTE_PROBE_DIAGNOSTICS"] = str(diagnostic_root)
    try:
        run(
            [str(backend), "--probe-compute-worker", "--resource-dir", str(resources)],
            env=environment,
        )
    except MacOSBuildError as error:
        diagnostic = diagnostic_root / "compute.log"
        detail = diagnostic.read_text(encoding="utf-8", errors="replace") if diagnostic.is_file() else ""
        raise MacOSBuildError(f"{error}\nCompute Worker诊断：\n{detail or '未生成诊断记录'}") from error


def build_backend(workspace: Path, resources: Path, *, app_root: Path = APP_ROOT) -> Path:
    dist = workspace / "pyinstaller-dist"
    command = backend_build_command(workspace, app_root=app_root)
    environment = dict(os.environ)
    environment["PYINSTALLER_CONFIG_DIR"] = str(workspace / "pyinstaller-config")
    run(command, cwd=app_root.parent.parent, env=environment)
    package = dist / "OptionHelperBackend"
    executable = package / "OptionHelperBackend"
    if not executable.is_file():
        raise MacOSBuildError("PyInstaller未生成OptionHelperBackend")
    try:
        stage_python_docx_templates(package)
    except PlatformPayloadError as error:
        raise MacOSBuildError(str(error)) from error
    verify_backend_payload(package)
    copied = copy_backend_bundle(package, resources)
    return copied / "OptionHelperBackend"


def probe_backend_startup(backend: Path, resources: Path, workspace: Path) -> None:
    """Prove that the frozen App Host can import and publish its loopback API.

    The AppServer imports ``runtime`` and calendar-policy modules during normal
    startup.  A PDF-only probe does not traverse that import path, so it cannot
    catch a PyInstaller exclusion that leaves the desktop UI without a backend.
    """

    state = workspace / "backend-startup-probe-state"
    log = workspace / "backend-startup-probe.log"
    state.mkdir(parents=True, exist_ok=False)
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                str(backend),
                "--data-dir", str(state),
                "--resource-dir", str(resources),
                "--verification-fixture",
            ],
            cwd=backend.parent,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    try:
        deadline = time.monotonic() + 20.0
        url = ""
        while time.monotonic() < deadline:
            output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            for line in output.splitlines():
                if line.startswith("OPTIONHELPER_URL="):
                    url = line.removeprefix("OPTIONHELPER_URL=").strip()
                    break
            if url:
                try:
                    with urlopen(url + "/api/health", timeout=2.0) as response:  # nosec B310 - loopback URL emitted by this process
                        payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("status") != "ok":
                        raise MacOSBuildError("冻结后端健康检查返回无效状态")
                    return
                except OSError:
                    # The launcher wrote its URL before the HTTP thread was
                    # fully listening.  Retry inside the bounded probe window.
                    pass
            if process.poll() is not None:
                break
            time.sleep(0.1)
        output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        raise MacOSBuildError("冻结后端未能启动并发布本地服务：" + output[-2000:])
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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


def normalize_installable_bundle_permissions(bundle: Path) -> None:
    """Restore standard install permissions after copying the read-only source snapshot.

    The frozen build input is intentionally not writable.  ``copy2`` and
    ``copytree`` preserve those modes, but an App carrying 0444 files or 0555
    directories cannot be replaced by Finder after its first installation.
    Code signing protects delivered bytes; resource write bits are not an
    integrity boundary.
    """

    if not bundle.is_dir() or bundle.is_symlink() or bundle.name != "OptionHelper.app":
        raise MacOSBuildError("安装权限归一化目标不是OptionHelper.app")
    for path in (bundle, *sorted(bundle.rglob("*"))):
        if path.is_symlink():
            continue
        metadata = path.stat()
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o755)
        elif stat.S_ISREG(metadata.st_mode):
            executable = bool(metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            path.chmod(0o755 if executable else 0o644)
        else:
            raise MacOSBuildError(f"App包含不支持的文件类型：{path.relative_to(bundle)}")


def verify_installable_bundle_permissions(bundle: Path) -> None:
    """Reject an App whose stored modes would block Finder replacement."""

    invalid: list[str] = []
    for path in (bundle, *sorted(bundle.rglob("*"))):
        if path.is_symlink():
            continue
        metadata = path.stat()
        actual = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            expected = 0o755
        elif stat.S_ISREG(metadata.st_mode):
            executable = bool(metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            expected = 0o755 if executable else 0o644
        else:
            invalid.append(f"{path.relative_to(bundle)}=unsupported")
            continue
        if actual != expected:
            relative = "." if path == bundle else path.relative_to(bundle).as_posix()
            invalid.append(f"{relative}={actual:04o}, expected={expected:04o}")
    if invalid:
        details = "；".join(invalid[:8])
        suffix = f"；另有{len(invalid) - 8}项" if len(invalid) > 8 else ""
        raise MacOSBuildError(f"App资源权限会阻止Finder覆盖安装：{details}{suffix}")


def write_info_plist(bundle: Path, app_version: str, *, formal_release: bool = False) -> None:
    public_version, bundle_version = app_platform_versions(app_version)
    plist = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": "OptionHelper",
        "CFBundleIconFile": "OptionHelper.icns",
        "CFBundleIdentifier": "com.optionhelper.app",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "OptionHelper",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": public_version,
        "CFBundleVersion": bundle_version,
        "OptionHelperDisplayVersion": app_version,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "OptionHelperReleaseStatus": "formal_release" if formal_release else "local_candidate",
    }
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)


def copy_app_icon(bundle: Path, *, app_icon: Path = APP_ICON) -> None:
    if not app_icon.is_file():
        raise MacOSBuildError(f"缺少macOS应用图标：{app_icon}")
    shutil.copy2(app_icon, bundle / "Contents" / "Resources" / "OptionHelper.icns")


def copy_theme_icon_assets(resources: Path, *, source_root: Path = ROOT / "assets" / "icons") -> None:
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


def compile_shell(bundle: Path, *, app_root: Path = APP_ROOT) -> tuple[Path, str, str]:
    output = bundle / "Contents" / "MacOS" / "OptionHelper"
    output.parent.mkdir(parents=True)
    swift = [
        "xcrun", "swiftc", str(app_root / "desktop" / "macos" / "OptionHelperApp.swift"),
        "-framework", "Cocoa", "-framework", "WebKit", "-o", str(output),
    ]
    try:
        run(swift)
        return output, "swift", "swiftc"
    except MacOSBuildError:
        if output.exists():
            output.unlink()
    objc = [
        "xcrun", "clang", "-fobjc-arc", str(app_root / "desktop" / "macos" / "OptionHelperApp.m"),
        "-framework", "Cocoa", "-framework", "WebKit", "-o", str(output),
    ]
    run(objc)
    return output, "objective-c", "clang"


def verify_bundle(
    bundle: Path,
    expected_manifest: dict[str, Any],
    *,
    require_native_runtime: bool = True,
    repo_root: Path = ROOT,
) -> None:
    verify_installable_bundle_permissions(bundle)
    resources = bundle / "Contents" / "Resources"
    required = [
        bundle / "Contents" / "Info.plist", bundle / "Contents" / "MacOS" / "OptionHelper",
        resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend",
        resources / "frontend" / "optchat" / "index.html", resources / "capability" / "option-helper" / "capability-manifest.json",
        resources / "LICENSES", resources / "app-manifest.json",
        resources / "OptionHelper.icns",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-dark.svg",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns",
        resources / "assets" / "icons" / "optionhelper-app-icon-tile-dark.icns",
        resources / "installer" / DMG_BACKGROUND.name,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise MacOSBuildError("Bundle缺少资源：" + ", ".join(missing))
    forbidden_sources = (resources / "app" / "backend", resources / "app" / "config")
    if any(path.exists() for path in forbidden_sources):
        raise MacOSBuildError("Bundle不得携带冻结后端之外的原始backend/config源码")
    if (resources / "runtime").exists():
        raise MacOSBuildError("Bundle不得重复携带Capability外层runtime源码树")
    verify_required_licenses(resources, license_root=repo_root / "LICENSES")
    for name in (
        "OptionHelper-Agent-Runtime-LICENSE.txt",
        "OptionHelper-Agent-Runtime-NOTICES.txt",
        "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md",
    ):
        matches = list(resources.rglob(name))
        expected = resources / "LICENSES" / "agent-runtime" / name
        if matches != [expected]:
            raise MacOSBuildError(f"Agent Runtime发行声明必须唯一：{name}")
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        bundle_info = plistlib.load(handle)
    if bundle_info.get("CFBundleIconFile") != "OptionHelper.icns":
        raise MacOSBuildError("Info.plist未绑定OptionHelper.icns")
    actual = json.loads((resources / "app-manifest.json").read_text(encoding="utf-8"))
    if actual != expected_manifest:
        raise MacOSBuildError("Bundle app-manifest不匹配")
    try:
        verify_outer_payload_manifest(
            repo_root,
            bundle,
            actual.get("outer_payload"),
            source_mappings=MACOS_SOURCE_MAPPINGS,
            payload_roots=MACOS_PAYLOAD_ROOTS,
            build_inputs=MACOS_BUILD_INPUTS,
            strict_directories=MACOS_STRICT_DIRECTORY_PAYLOADS,
        )
        assert_outer_resource_layout(
            bundle,
            resources_relative="Contents/Resources",
            allowed_entries=MACOS_ALLOWED_RESOURCE_ENTRIES,
        )
        runtime_licenses = resources / "LICENSES" / "python-runtime"
        license_inventory = json.loads(
            (runtime_licenses / "python-runtime-license-manifest.json").read_text(encoding="utf-8")
        )
        verify_python_runtime_licenses(runtime_licenses, license_inventory)
    except (OSError, json.JSONDecodeError, PlatformPayloadError, PythonRuntimeLicenseError) as error:
        raise MacOSBuildError(str(error)) from error
    expected_version, expected_bundle_version = app_platform_versions(str(actual.get("build_version", "")))
    if (bundle_info.get("CFBundleShortVersionString") != expected_version
            or bundle_info.get("CFBundleVersion") != expected_bundle_version
            or bundle_info.get("OptionHelperDisplayVersion") != actual.get("build_version")):
        raise MacOSBuildError("Info.plist版本与App构建版本不一致")
    expected_status = "formal_release" if actual.get("formal_release") is True else "local_candidate"
    if bundle_info.get("OptionHelperReleaseStatus") != expected_status:
        raise MacOSBuildError("Info.plist发布状态与App Manifest不一致")
    capability = resources / "capability" / "option-helper"
    if verify_app_capability(capability):
        raise MacOSBuildError("Bundle内置App Capability未通过验收")
    if actual["capability_manifest_hash"] != file_hash(capability / "capability-manifest.json"):
        raise MacOSBuildError("Bundle Capability Manifest哈希不匹配")
    if actual["capability_content_tree_hash"] != tree_hash(content_tree_entries(capability)):
        raise MacOSBuildError("Bundle Capability目录树哈希不匹配")
    try:
        assert_agent_runtime_delivery_clean(resources)
    except AssertionError as error:
        raise MacOSBuildError(str(error)) from error
    try:
        runtime_summary = verify_staged_runtime(
            resources,
            "macos-arm64",
            actual.get("agent_runtime", {"status": "disabled"}),
        )
        if require_native_runtime and runtime_summary.get("status") != LOCAL_VERIFIED:
            raise RuntimeBuildError("正式App缺少local_verified原生Agent运行时")
        if runtime_summary.get("status") == LOCAL_VERIFIED:
            probe_runtime_process(resources / str(runtime_summary["resource_path"]))
    except RuntimeBuildError as error:
        raise MacOSBuildError(str(error)) from error


def build_macos(
    app_version: str,
    capability_root: Path,
    *,
    dist_root: Path = DIST_ROOT,
    versions_root: Path = VERSIONS_ROOT,
    transaction_stage: bool = False,
    agent_runtime_path: Path | None = None,
    require_native_runtime: bool | None = None,
    allow_source_runtime_fallback: bool | None = None,
    repo_root: Path = ROOT,
) -> dict[str, Path]:
    try:
        require_app_version(app_version)
    except ValueError as error:
        raise MacOSBuildError(str(error)) from error
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise MacOSBuildError("macOS arm64构建必须在本机arm64 macOS执行")
    assert_build_python()
    repo_root = repo_root.expanduser().resolve()
    app_root = repo_root / "products" / "app"
    app_icon = repo_root / "assets" / "icons" / "optionhelper-app-icon-tile-light.icns"
    dmg_background = repo_root / "packaging" / "app" / "macos" / "assets" / DMG_BACKGROUND.name
    license_root = repo_root / "LICENSES"
    runtime_source_root = app_root / "runtime" / "optionhelper_agent_runtime"
    dist_root = dist_root.resolve()
    versions_root = versions_root.resolve()
    native_runtime_required = _runtime_required(
        require_native_runtime,
        formal=transaction_stage or versions_root == VERSIONS_ROOT.resolve(),
    )
    formal_release = transaction_stage or versions_root == VERSIONS_ROOT.resolve()
    signing_identity: str | None = None
    notary_profile: str | None = None
    if formal_release:
        signing_identity, notary_profile = formal_signing_configuration()
    source_fallback_enabled = _source_fallback_enabled(
        allow_source_runtime_fallback,
        required=native_runtime_required,
    )
    if native_runtime_required:
        _assert_source_name_boundary(repo_root=repo_root)
    output_dmg = dist_root / f"OptionHelper-{app_version}-macOS-arm64.dmg"
    release_root = versions_root / (RELEASE_VERSION if formal_release else app_version)
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
        copy_app_icon(bundle, app_icon=app_icon)
        copy_theme_icon_assets(resources, source_root=repo_root / "assets" / "icons")
        installer_assets = resources / "installer"
        installer_assets.mkdir()
        shutil.copy2(dmg_background, installer_assets / DMG_BACKGROUND.name)
        try:
            stage_verification_fixture_definition(
                repo_root,
                bundle,
                resources_relative="Contents/Resources",
            )
        except PlatformPayloadError as error:
            raise MacOSBuildError(str(error)) from error
        manifest_version = app_version if formal_release else "development"
        write_info_plist(bundle, app_version, formal_release=formal_release)
        _shell, shell_language, build_tool = compile_shell(bundle, app_root=app_root)
        copy_tree(app_root / "frontend", resources / "frontend", extra_ignored=("*.md",))
        copy_tree(source, resources / "capability" / "option-helper")
        assert_staged_capability(source, resources / "capability" / "option-helper")
        copy_licenses(resources / "LICENSES", source, license_root=license_root)
        try:
            runtime_candidate = resolve_runtime_candidate(
                "macos-arm64",
                explicit=agent_runtime_path,
                repository_root=repo_root,
                formal=formal_release,
            )
            runtime_summary = prepare_staged_runtime(
                resources,
                "macos-arm64",
                runtime_candidate,
                required=native_runtime_required,
                allow_source_fallback=source_fallback_enabled,
                source_root=runtime_source_root,
                require_source_provenance=native_runtime_required,
            )
        except RuntimeBuildError as error:
            raise MacOSBuildError(str(error)) from error
        _progress("正在构建后端运行时")
        backend = build_backend(temporary, resources, app_root=app_root)
        try:
            license_inventory = stage_pyinstaller_dependency_licenses(
                temporary / "pyinstaller-work" / "OptionHelperBackend" / "Analysis-00.toc",
                resources / "LICENSES" / "python-runtime",
                requirements_lock=repo_root / "core" / "requirements.lock",
                repository_license_root=license_root,
            )
            verify_python_runtime_licenses(resources / "LICENSES" / "python-runtime", license_inventory)
            outer_payload = build_outer_payload_manifest(
                repo_root,
                bundle,
                source_mappings=MACOS_SOURCE_MAPPINGS,
                payload_roots=MACOS_PAYLOAD_ROOTS,
                build_inputs=MACOS_BUILD_INPUTS,
                strict_directories=MACOS_STRICT_DIRECTORY_PAYLOADS,
            )
        except (PlatformPayloadError, PythonRuntimeLicenseError) as error:
            raise MacOSBuildError(str(error)) from error
        manifest = app_manifest(
            manifest_version,
            capability,
            capability_manifest_hash,
            shell_language=shell_language,
            build_tool=build_tool,
            agent_runtime=runtime_summary,
            outer_payload=outer_payload,
        )
        manifest = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
        write_json(resources / "app-manifest.json", manifest)
        normalize_installable_bundle_permissions(bundle)
        run([str(backend), "--probe-pdf-runtime", "--resource-dir", str(resources)])
        probe_compute_worker(backend, resources, temporary)
        probe_backend_startup(backend, resources, temporary)
        verify_bundle(
            bundle,
            manifest,
            require_native_runtime=native_runtime_required,
            repo_root=repo_root,
        )
        assert_no_finder_conflicts(bundle)
        bundle_size = _backend_size(bundle)
        if bundle_size > MAX_APP_BUNDLE_BYTES:
            raise MacOSBuildError(
                f"macOS App体积{bundle_size / 1024 / 1024:.1f}MB超过"
                f"{MAX_APP_BUNDLE_BYTES // 1024 // 1024}MB上限；请检查重复资源和无关依赖"
            )
        _progress("正在签名并验证应用包")
        if formal_release:
            assert signing_identity is not None
            run([
                "codesign", "--force", "--deep", "--options", "runtime", "--timestamp",
                "--sign", signing_identity, str(bundle),
            ])
            # Gatekeeper is meaningful only after the containing DMG has been
            # accepted by notarytool and stapled.  Before that point, verify
            # the Developer ID signature itself without falsely requiring an
            # unavailable notarization ticket.
            verify_formal_app_bundle(
                bundle,
                expected_identity=signing_identity,
                require_gatekeeper=False,
            )
        else:
            run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])
            run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])
        verify_installable_bundle_permissions(bundle)
        _progress("正在生成DMG安装物")
        built_dmg = temporary / f"OptionHelper-{app_version}-macOS-arm64.dmg"
        dmg_layout = prepare_dmg_layout(
            bundle,
            temporary / "dmg-layout",
            background_source=dmg_background,
        )
        build_styled_dmg(dmg_layout, built_dmg, temporary)
        run(["hdiutil", "verify", str(built_dmg)])
        if formal_release:
            assert notary_profile is not None
            run([
                "xcrun", "notarytool", "submit", str(built_dmg),
                "--keychain-profile", notary_profile, "--wait",
            ])
            run(["xcrun", "stapler", "staple", str(built_dmg)])
            verify_formal_dmg_distribution(built_dmg)
        if built_dmg.stat().st_size > MAX_DMG_BYTES:
            raise MacOSBuildError(
                f"macOS安装物体积{built_dmg.stat().st_size / 1024 / 1024:.1f}MB超过"
                f"{MAX_DMG_BYTES // 1024 // 1024}MB上限；请检查重复资源和无关运行时依赖"
            )
        _progress("正在验证安装物")
        def installed_runtime_probe(installed_bundle: Path) -> None:
            verify_frozen_macos_app(installed_bundle, repository_root=repo_root)
            if formal_release:
                verify_formal_app_bundle(installed_bundle, expected_identity=signing_identity)

        verify_dmg_install_layout(
            built_dmg,
            expected_content_tree_hash=str(capability["content_tree_hash"]),
            expected_manifest_hash=capability_manifest_hash,
            runtime_probe=installed_runtime_probe,
            background_source=dmg_background,
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
                app_version=manifest_version, app_manifest_path=app_manifest_path, dmg_path=dmg, capability=manifest,
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
            archive_capability = history_stage / "app-capability-manifest-macos.json"
            shutil.copy2(app_manifest_path, archive_manifest)
            shutil.copy2(release_manifest_path, archive_release)
            shutil.copy2(source / "capability-manifest.json", archive_capability)
            verify_platform_release_manifest(archive_release, app_manifest_path=archive_manifest, dmg_path=archive_stage)
            assert_no_finder_conflicts(history_stage)
            _progress("正在写入临时交付记录")
            created_release_root = _commit_release_artifacts(
                dmg=dmg,
                output_dmg=output_dmg,
                history_stage=history_stage,
                release_root=release_root,
                archive_names=(archive_stage.name, checksum.name, archive_manifest.name, archive_release.name, archive_capability.name),
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
                    archive_names=(archive_stage.name, checksum.name, archive_manifest.name, archive_release.name, archive_capability.name),
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
    parser = argparse.ArgumentParser(description="构建受控OptionHelper macOS安装物；正式归档必须签名并公证")
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--capability-root", type=Path, required=True)
    args = parser.parse_args()
    artifacts = build_macos(args.app_version, args.capability_root)
    for name, path in artifacts.items():
        print(f"{name}={path}")


if __name__ == "__main__":
    main()
    assert_outer_resource_layout,
