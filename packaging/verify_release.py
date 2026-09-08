#!/usr/bin/env python3
"""Verify one immutable release from its archived artifacts only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
for path in (
    ROOT / "core" / "src",
    ROOT / "packaging",
    ROOT / "packaging" / "skill",
    ROOT / "packaging" / "app" / "macos",
    ROOT / "packaging" / "app",
    ROOT / "packaging" / "app" / "agent_runtime",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from knowledge_snapshot import validate_published_catalog
from build_macos import (
    EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    MacOSBuildError,
    file_hash,
    verify_dmg_install_layout,
    verify_formal_app_bundle,
    verify_formal_dmg_distribution,
    verify_platform_release_manifest,
)
from build_runtime import RuntimeBuildError, verify_staged_runtime
from release_contract import skill_archive_name
from release_contract import APP_VERSION, PROTOCOL_ID, RELEASE_VERSION, require_published_at, require_release_version
from verify_macos import verify as verify_macos_bundle
from verify_skill import content_tree_entries, tree_hash, verify_skill, verify_zip
from verify_capability import verify_app_capability
from platform_payload import (
    WINDOWS_ALLOWED_RESOURCE_ENTRIES,
    WINDOWS_BUILD_INPUTS,
    WINDOWS_PAYLOAD_ROOTS,
    WINDOWS_SOURCE_MAPPINGS,
    WINDOWS_STRICT_DIRECTORY_PAYLOADS,
    PlatformPayloadError,
    assert_outer_resource_layout,
    verify_outer_payload_manifest,
)
from python_runtime_licenses import PythonRuntimeLicenseError, verify_python_runtime_licenses


class ReleaseVerificationError(RuntimeError):
    pass


PLATFORMS = ("macos", "windows")
REQUIRED_CAPABILITY_LICENSES = (
    "ReportLab-LICENSE.txt",
    "Pillow-LICENSE.txt",
    "pypdf-LICENSE.txt",
    "python-docx-LICENSE.txt",
    "openpyxl-LICENSE.txt",
)


def _run_external(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    """Run one release-verification command with the shared platform timeout."""

    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        output = f"{error.stdout or ''}{error.stderr or ''}"
        raise ReleaseVerificationError(f"{label}超时：{output}") from error


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(f"无法读取{label}：{error}") from error
    if not isinstance(value, dict):
        raise ReleaseVerificationError(f"{label}必须是JSON对象")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseVerificationError(message)


def installer_name(version: str, platform: str) -> str:
    if platform == "macos":
        return f"OptionHelper-{APP_VERSION}-macOS-arm64.dmg"
    if platform == "windows":
        return f"OptionHelper-{APP_VERSION}-windows-x86_64.zip"
    raise ReleaseVerificationError(f"不支持的平台：{platform}")


def platform_archive_names(version: str, platform: str) -> set[str]:
    installer = installer_name(version, platform)
    if platform == "macos":
        return {
            installer,
            f"{installer}.sha256",
            "app-manifest.json",
            "platform-release-manifest.json",
            "app-capability-manifest-macos.json",
        }
    return {
        installer,
        f"{installer}.sha256",
        "app-manifest-windows.json",
        "platform-release-manifest-windows.json",
        "app-capability-manifest-windows.json",
    }


def archived_platforms(version_root: Path, version: str) -> set[str]:
    """Return complete platform records and reject a partial formal archive."""

    completed: set[str] = set()
    for platform in PLATFORMS:
        names = platform_archive_names(version, platform)
        present = {name for name in names if (version_root / name).is_file()}
        if present and present != names:
            missing = ", ".join(sorted(names - present))
            raise ReleaseVerificationError(f"正式归档中的{platform}平台记录不完整，缺少：{missing}")
        if present:
            completed.add(platform)
    return completed


def _published_capability(manifest: dict[str, object]) -> None:
    expected = {
        "package_status": "published",
        "release_status": "published",
        "formal_release": True,
        "execution_scope": "production",
        "capability_version": RELEASE_VERSION,
        "catalog_version": RELEASE_VERSION,
        "protocol_id": PROTOCOL_ID,
        "design_system_id": "optionhelper.design-system",
    }
    mismatches = [field for field, value in expected.items() if manifest.get(field) != value]
    _require(not mismatches, "正式Capability Manifest字段无效：" + ", ".join(mismatches))
    _require(isinstance(manifest.get("published_by"), str) and bool(str(manifest["published_by"]).strip()), "正式Capability缺少published_by")
    published_at = manifest.get("published_at")
    _require(isinstance(published_at, str) and bool(published_at.strip()), "正式Capability缺少published_at")
    try:
        require_published_at(published_at)
    except ValueError as error:
        raise ReleaseVerificationError(str(error)) from error


def _extract_skill(archive: Path, destination: Path) -> Path:
    errors = verify_zip(archive)
    if errors:
        raise ReleaseVerificationError("归档Skill ZIP无效：\n" + "\n".join(errors))
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ReleaseVerificationError("归档Skill ZIP CRC校验失败")
        package.extractall(destination)
        for info in package.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                (destination / info.filename).chmod(mode)
    skill_root = destination / "option-helper"
    errors = verify_skill(skill_root)
    if errors:
        raise ReleaseVerificationError("归档解压Skill无效：\n" + "\n".join(errors))
    return skill_root


def _verify_skill_archive(version_root: Path) -> dict[str, object]:
    archive = version_root / skill_archive_name()
    external_manifest = version_root / "capability-manifest.json"
    _require(archive.is_file(), f"版本归档缺少{skill_archive_name()}")
    _require(external_manifest.is_file(), "版本归档缺少capability-manifest.json")
    with tempfile.TemporaryDirectory(prefix="optionhelper-release-skill-") as temporary_name:
        skill_root = _extract_skill(archive, Path(temporary_name))
        embedded = skill_root / "capability-manifest.json"
        _require(
            embedded.read_bytes() == external_manifest.read_bytes(),
            "归档Capability Manifest与Skill ZIP内Manifest不一致",
        )
        manifest = _read_json(embedded, "归档Capability Manifest")
        _published_capability(manifest)
        _require(
            tree_hash(content_tree_entries(skill_root)) == manifest.get("content_tree_hash"),
            "归档Skill内容树哈希不一致",
        )
        return manifest


def _verify_catalog(version_root: Path, versions_root: Path) -> None:
    catalog_path = version_root / "knowledger" / "source-manifest.json"
    catalog = _read_json(catalog_path, "归档知识源清单")
    _require(catalog.get("release_version") == RELEASE_VERSION, f"归档知识源清单不是{RELEASE_VERSION}")
    try:
        validate_published_catalog(
            ROOT,
            RELEASE_VERSION,
            versions_root=versions_root,
            require_source_match=False,
        )
    except ValueError as error:
        raise ReleaseVerificationError(f"归档Knowledger链无效：{error}") from error


def _verify_platform_binding(
    app_manifest: dict[str, object],
    capability: dict[str, object],
) -> None:
    expected = {
        "app_version": APP_VERSION,
        "capability_version": RELEASE_VERSION,
        "catalog_version": RELEASE_VERSION,
        "protocol_id": PROTOCOL_ID,
        "design_system_id": "optionhelper.design-system",
    }
    mismatches = [field for field, value in expected.items() if app_manifest.get(field) != value]
    _require(not mismatches, "App Manifest公开版本字段无效：" + ", ".join(mismatches))


def _verify_macos_runtime(dmg: Path) -> None:
    try:
        verify_formal_dmg_distribution(dmg)
    except MacOSBuildError as error:
        raise ReleaseVerificationError(f"macOS DMG公证、staple或Gatekeeper门禁失败：{error}") from error
    with tempfile.TemporaryDirectory(prefix="optionhelper-release-dmg-") as temporary_name:
        mountpoint = Path(temporary_name) / "mounted"
        mountpoint.mkdir()
        attached = False
        try:
            completed = _run_external(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(dmg)],
                "DMG挂载",
            )
            if completed.returncode:
                raise ReleaseVerificationError(f"DMG挂载失败：{completed.stdout}{completed.stderr}")
            attached = True
            bundle = mountpoint / "OptionHelper.app"
            completed = _run_external(
                ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)],
                "DMG内App签名校验",
            )
            if completed.returncode:
                raise ReleaseVerificationError(f"DMG内App签名校验失败：{completed.stdout}{completed.stderr}")
            embedded_manifest = _read_json(
                bundle / "Contents" / "Resources" / "app-manifest.json",
                "DMG内App Manifest",
            )
            signing = embedded_manifest.get("signing")
            _require(isinstance(signing, dict), "DMG内正式App缺少签名记录")
            identity = str(signing.get("identity", "")).strip()
            try:
                verify_formal_app_bundle(bundle, expected_identity=identity)
            except MacOSBuildError as error:
                raise ReleaseVerificationError(f"macOS App Developer ID或Gatekeeper门禁失败：{error}") from error
            verify_macos_bundle(bundle, verify_sources=False)
        finally:
            if attached:
                detach_error = ""
                try:
                    detached = _run_external(["hdiutil", "detach", str(mountpoint)], "DMG卸载")
                    if detached.returncode:
                        detach_error = f"{detached.stdout}{detached.stderr}" or f"退出码{detached.returncode}"
                except ReleaseVerificationError as error:
                    detach_error = str(error)
                if detach_error:
                    try:
                        forced = _run_external(
                            ["hdiutil", "detach", "-force", str(mountpoint)],
                            "DMG强制卸载",
                        )
                    except ReleaseVerificationError as error:
                        raise ReleaseVerificationError(f"DMG卸载失败：{detach_error}；{error}") from error
                    if forced.returncode:
                        raise ReleaseVerificationError(
                            f"DMG卸载失败：{detach_error}{forced.stdout}{forced.stderr}"
                        )


def _verify_macos_release(
    version_root: Path,
    capability: dict[str, object],
    *,
    verify_installed_bundle: bool,
) -> None:
    dmg = version_root / f"OptionHelper-{APP_VERSION}-macOS-arm64.dmg"
    app_manifest_path = version_root / "app-manifest.json"
    platform_manifest_path = version_root / "platform-release-manifest.json"
    checksum_path = version_root / f"{dmg.name}.sha256"
    _require(dmg.is_file(), "版本归档缺少macOS DMG")
    _require(checksum_path.is_file(), "版本归档缺少macOS DMG校验文件")
    app_manifest = _read_json(app_manifest_path, "macOS App Manifest")
    app_capability_path = version_root / "app-capability-manifest-macos.json"
    app_capability = _read_json(app_capability_path, "macOS App Capability Manifest")
    _verify_platform_binding(app_manifest, capability)
    _require(
        app_manifest.get("capability_manifest_hash") == file_hash(app_capability_path),
        "macOS App Manifest未绑定正式App Capability Manifest",
    )
    _require(
        app_manifest.get("capability_content_tree_hash") == app_capability.get("content_tree_hash"),
        "macOS App Manifest未绑定正式App Capability内容树",
    )
    _require(app_manifest.get("shared_payload_hash") == capability.get("shared_payload_hash"), "macOS App与Skill共享载荷不一致")
    try:
        verify_platform_release_manifest(platform_manifest_path, app_manifest_path=app_manifest_path, dmg_path=dmg)
    except RuntimeError as error:
        raise ReleaseVerificationError(f"macOS Platform Manifest无效：{error}") from error
    expected_checksum = f"{file_hash(dmg)}  {dmg.name}\n"
    _require(checksum_path.read_text(encoding="utf-8") == expected_checksum, "macOS DMG校验文件不匹配")
    if not verify_installed_bundle:
        return
    verify_dmg_install_layout(
        dmg,
        expected_content_tree_hash=str(app_capability["content_tree_hash"]),
        expected_manifest_hash=file_hash(app_capability_path),
    )
    _verify_macos_runtime(dmg)


def _verify_windows_release(
    version_root: Path,
    capability: dict[str, object],
    *,
    verify_installed_bundle: bool = False,
) -> None:
    installer = version_root / f"OptionHelper-{APP_VERSION}-windows-x86_64.zip"
    app_manifest_path = version_root / "app-manifest-windows.json"
    platform_manifest_path = version_root / "platform-release-manifest-windows.json"
    checksum_path = version_root / f"{installer.name}.sha256"
    _require(installer.is_file(), "版本归档缺少Windows安装ZIP")
    _require(checksum_path.is_file(), "版本归档缺少Windows安装ZIP校验文件")
    app_manifest = _read_json(app_manifest_path, "Windows App Manifest")
    app_capability_path = version_root / "app-capability-manifest-windows.json"
    _read_json(app_capability_path, "Windows App Capability Manifest")
    platform_manifest = _read_json(platform_manifest_path, "Windows Platform Manifest")
    _verify_platform_binding(app_manifest, capability)
    _require(
        app_manifest.get("capability_manifest_hash") == file_hash(app_capability_path),
        "Windows App Manifest未绑定正式App Capability Manifest",
    )
    _require(app_manifest.get("shared_payload_hash") == capability.get("shared_payload_hash"), "Windows App与Skill共享载荷不一致")
    expected_installer = {"filename": installer.name, "sha256": file_hash(installer), "size": installer.stat().st_size}
    signing = app_manifest.get("signing")
    _require(isinstance(signing, dict), "Windows正式App缺少签名记录")
    thumbprint = str(signing.get("certificate_thumbprint", "")).strip().upper()
    _require(
        signing.get("method") == "authenticode"
        and signing.get("status") == "valid"
        and len(thumbprint) == 40
        and all(character in "0123456789ABCDEF" for character in thumbprint),
        "Windows正式App不得为unsigned",
    )
    expected_manifest = {
        "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}",
        "app_version": APP_VERSION,
        "platform": "windows",
        "architecture": "x86_64",
        "installer": expected_installer,
        "app_manifest_hash": file_hash(app_manifest_path),
        "capability_version": app_manifest.get("capability_version"),
        "catalog_version": app_manifest.get("catalog_version"),
        "capability_manifest_hash": app_manifest.get("capability_manifest_hash"),
        "capability_content_tree_hash": app_manifest.get("capability_content_tree_hash"),
        "shared_payload_hash": app_manifest.get("shared_payload_hash"),
        "protocol_id": app_manifest.get("protocol_id"),
        "design_system_id": app_manifest.get("design_system_id"),
        "signing_identity": thumbprint,
        "signature_status": "authenticode_validated",
        "notarized": False,
        "release_status": "formal_release",
        "formal_distribution_status": "ready",
        "application_icon": app_manifest.get("application_icon"),
        "agent_runtime": app_manifest.get("agent_runtime"),
        "agent_runtime_status": (
            app_manifest.get("agent_runtime", {}).get("status")
            if isinstance(app_manifest.get("agent_runtime"), dict)
            else None
        ),
        "package_static_acceptance": "passed",
        "backend_api_acceptance": "passed",
        "native_interaction_acceptance": "not_run",
    }
    mismatches = [
        field for field, expected in expected_manifest.items()
        if platform_manifest.get(field) != expected
    ]
    _require(
        not mismatches,
        "Windows Platform Manifest字段无效：" + ", ".join(mismatches),
    )
    _require(checksum_path.read_text(encoding="utf-8") == f"{file_hash(installer)}  {installer.name}\n", "Windows安装ZIP校验文件不匹配")
    with tempfile.TemporaryDirectory(prefix="optionhelper-release-windows-") as temporary_name:
        with zipfile.ZipFile(installer) as package:
            damaged = package.testzip()
            _require(damaged is None, f"Windows安装ZIP CRC无效：{damaged}")
            package.extractall(temporary_name)
        embedded = Path(temporary_name) / "OptionHelper" / "Resources" / "capability" / "option-helper"
        app = Path(temporary_name) / "OptionHelper"
        resources = app / "Resources"
        for name in REQUIRED_CAPABILITY_LICENSES:
            packaged = resources / "LICENSES" / "capability" / name
            _require(packaged.is_file(), f"Windows安装ZIP缺少必需第三方许可证：{name}")
        third_party = resources / "LICENSES" / "THIRD_PARTY.md"
        _require(third_party.is_file(), "Windows安装ZIP缺少THIRD_PARTY.md")
        _require("`python-runtime/`" in third_party.read_text(encoding="utf-8"), "Windows THIRD_PARTY.md未索引冻结依赖许可证")
        _require(not (resources / "runtime").exists(), "Windows安装ZIP重复携带Capability外层runtime源码树")
        try:
            verify_outer_payload_manifest(
                ROOT,
                app,
                app_manifest.get("outer_payload"),
                source_mappings=WINDOWS_SOURCE_MAPPINGS,
                payload_roots=WINDOWS_PAYLOAD_ROOTS,
                build_inputs=WINDOWS_BUILD_INPUTS,
                strict_directories=WINDOWS_STRICT_DIRECTORY_PAYLOADS,
                verify_sources=False,
            )
            assert_outer_resource_layout(
                app,
                resources_relative="Resources",
                allowed_entries=WINDOWS_ALLOWED_RESOURCE_ENTRIES,
            )
            runtime_licenses = resources / "LICENSES" / "python-runtime"
            inventory = _read_json(
                runtime_licenses / "python-runtime-license-manifest.json",
                "Windows Python运行时许可证Manifest",
            )
            verify_python_runtime_licenses(runtime_licenses, inventory)
        except (PlatformPayloadError, PythonRuntimeLicenseError) as error:
            raise ReleaseVerificationError(str(error)) from error
        verifier = app / "verify-windows.py"
        _require(verifier.is_file(), "Windows安装ZIP缺少后端/API验收入口")
        _require((app / "OptionHelper.exe").is_file(), "Windows安装ZIP缺少应用入口")
        _require(
            (app / "Resources" / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe").is_file(),
            "Windows安装ZIP缺少后端入口",
        )
        for module in ("datafetcher", "payoffer", "pricer", "backtester", "reporter"):
            _require(
                (embedded / "assets" / "pages" / module / f"{module}.html").is_file(),
                f"Windows安装ZIP缺少{module}页面",
            )
        errors = verify_app_capability(embedded)
        _require(not errors, "Windows安装ZIP内置App Capability无效：" + "\n".join(errors))
        _require(
            (embedded / "capability-manifest.json").read_bytes()
            == app_capability_path.read_bytes(),
            "Windows安装ZIP内置Capability Manifest不一致",
        )
        embedded_app_manifest = resources / "app-manifest.json"
        _require(embedded_app_manifest.is_file(), "Windows安装ZIP缺少内置App Manifest")
        _require(
            embedded_app_manifest.read_bytes() == app_manifest_path.read_bytes(),
            "Windows安装ZIP内置App Manifest与归档不一致",
        )
        runtime = app_manifest.get("agent_runtime")
        _require(isinstance(runtime, dict), "Windows App Manifest缺少Agent Runtime记录")
        manifest_hash = runtime.get("manifest_sha256")
        _require(
            isinstance(manifest_hash, str)
            and len(manifest_hash) == 64
            and all(character in "0123456789abcdef" for character in manifest_hash),
            "Windows App Manifest未绑定Agent Runtime清单哈希",
        )
        try:
            verify_staged_runtime(resources, "windows-x64", runtime)
        except RuntimeBuildError as error:
            raise ReleaseVerificationError(f"Windows Agent Runtime绑定无效：{error}") from error
        if verify_installed_bundle:
            try:
                subprocess.run(
                    [sys.executable, str(verifier), str(app)],
                    check=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=3600,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                output = getattr(error, "stdout", "") or ""
                raise ReleaseVerificationError(f"Windows后端/API验收失败：{output[-2000:]}") from error


def _verify_delivery_copy(version_root: Path, platform: str, delivery_root: Path | None) -> None:
    if delivery_root is None:
        return
    installer = installer_name(RELEASE_VERSION, platform)
    expected = {skill_archive_name(), installer}
    _require(delivery_root.is_dir(), f"当前交付目录不存在：{delivery_root}")
    actual = {path.name for path in delivery_root.iterdir()}
    _require(actual == expected, "当前交付目录只能包含当次Skill ZIP和安装物")
    for name in expected:
        _require(
            (delivery_root / name).read_bytes() == (version_root / name).read_bytes(),
            f"当前交付物与归档不一致：{name}",
        )


def verify_release_core(
    *,
    version: str = RELEASE_VERSION,
    versions_root: Path = ROOT / "versions",
) -> tuple[Path, dict[str, object]]:
    """Validate the immutable Catalog and Capability without a platform runtime.

    A Windows installer can be appended on Windows after macOS has signed the
    shared Capability, and vice versa.  The common archive must be checked in
    either case, but the other platform's native runtime verifier is not a
    portable prerequisite for that append-only transaction.
    """

    try:
        require_release_version(version)
    except ValueError as error:
        raise ReleaseVerificationError(str(error)) from error
    resolved_versions = versions_root.expanduser().resolve()
    version_root = resolved_versions / version
    _require(version_root.is_dir(), f"版本归档不存在：{version_root}")
    capability = _verify_skill_archive(version_root)
    _verify_catalog(version_root, resolved_versions)
    return version_root, capability


def verify_formal_archive(
    *,
    version: str = RELEASE_VERSION,
    versions_root: Path = ROOT / "versions",
    native_platform: str | None = None,
) -> tuple[Path, dict[str, object], set[str]]:
    """Verify every completed installer in one immutable formal archive.

    This is the precondition for appending another platform: common Skill and
    Catalog records alone do not establish that the first installer, its
    manifests and checksum still agree.  Native bundle checks are performed
    only for the platform being built or explicitly requested: an archive can
    therefore be safely supplemented on the other operating system without
    pretending its native verification is portable.
    """

    if native_platform is not None and native_platform not in PLATFORMS:
        raise ReleaseVerificationError(f"不支持的平台：{native_platform}")
    version_root, capability = verify_release_core(version=version, versions_root=versions_root)
    completed = archived_platforms(version_root, version)
    _require(completed, "正式归档缺少任何完整平台记录，不能追加安装物")
    for platform in sorted(completed):
        if platform == "macos":
            _verify_macos_release(
                version_root,
                capability,
                verify_installed_bundle=native_platform == "macos",
            )
        else:
            _verify_windows_release(
                version_root,
                capability,
                verify_installed_bundle=native_platform == "windows",
            )
    return version_root, capability, completed


def verify_release(
    *,
    version: str = RELEASE_VERSION,
    platform: str = "macos",
    versions_root: Path = ROOT / "versions",
    delivery_root: Path | None = None,
) -> dict[str, Path]:
    """Verify a release without comparing it to the mutable development tree."""
    if platform not in PLATFORMS:
        raise ReleaseVerificationError(f"不支持的平台：{platform}")
    version_root, _capability, completed = verify_formal_archive(
        version=version,
        versions_root=versions_root,
        native_platform=platform,
    )
    _require(platform in completed, f"版本归档缺少{platform}完整平台记录")
    _verify_delivery_copy(version_root, platform, delivery_root)
    return {
        "archive": version_root,
        "skill_zip": version_root / skill_archive_name(),
        "installer": version_root / installer_name(version, platform),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="验收OptionHelper已归档发行物")
    parser.add_argument("--version", default=RELEASE_VERSION)
    parser.add_argument("--platform", choices=("macos", "windows"), default="macos")
    parser.add_argument("--versions-root", type=Path, default=ROOT / "versions")
    parser.add_argument("--delivery-root", type=Path)
    args = parser.parse_args()
    try:
        artifacts = verify_release(
            version=args.version,
            platform=args.platform,
            versions_root=args.versions_root,
            delivery_root=args.delivery_root,
        )
    except ReleaseVerificationError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps({name: str(path) for name, path in artifacts.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
