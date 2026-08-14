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
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime.knowledger.versioning import validate_published_catalog
from build_macos import file_hash, verify_dmg_install_layout, verify_platform_release_manifest
from release_contract import PROTOCOL_ID, RELEASE_VERSION, require_published_at, require_release_version
from verify_macos import verify as verify_macos_bundle
from verify_skill import content_tree_entries, tree_hash, verify_skill, verify_zip


class ReleaseVerificationError(RuntimeError):
    pass


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
    archive = version_root / "option-helper.zip"
    external_manifest = version_root / "capability-manifest.json"
    _require(archive.is_file(), "版本归档缺少option-helper.zip")
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
    catalog_path = version_root / "knowledger" / "catalog-version.json"
    catalog = _read_json(catalog_path, "归档CatalogVersion")
    _require(catalog.get("catalog_version") == RELEASE_VERSION, "归档CatalogVersion不是v1.0.0")
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
        "app_version": RELEASE_VERSION,
        "capability_version": RELEASE_VERSION,
        "catalog_version": RELEASE_VERSION,
        "protocol_id": PROTOCOL_ID,
        "design_system_id": "optionhelper.design-system",
    }
    mismatches = [field for field, value in expected.items() if app_manifest.get(field) != value]
    _require(not mismatches, "App Manifest公开版本字段无效：" + ", ".join(mismatches))


def _verify_macos_runtime(dmg: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="optionhelper-release-dmg-") as temporary_name:
        mountpoint = Path(temporary_name) / "mounted"
        mountpoint.mkdir()
        attached = False
        try:
            completed = subprocess.run(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(dmg)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                raise ReleaseVerificationError(f"DMG挂载失败：{completed.stdout}{completed.stderr}")
            attached = True
            bundle = mountpoint / "OptionHelper.app"
            completed = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                raise ReleaseVerificationError(f"DMG内App签名校验失败：{completed.stdout}{completed.stderr}")
            verify_macos_bundle(bundle)
        finally:
            if attached:
                detached = subprocess.run(
                    ["hdiutil", "detach", str(mountpoint)], capture_output=True, text=True, check=False
                )
                if detached.returncode:
                    forced = subprocess.run(
                        ["hdiutil", "detach", "-force", str(mountpoint)], capture_output=True, text=True, check=False
                    )
                    if forced.returncode:
                        raise ReleaseVerificationError(f"DMG卸载失败：{detached.stdout}{detached.stderr}{forced.stdout}{forced.stderr}")


def _verify_macos_release(version_root: Path, capability: dict[str, object]) -> None:
    dmg = version_root / f"OptionHelper-{RELEASE_VERSION}-macOS-arm64.dmg"
    app_manifest_path = version_root / "app-manifest.json"
    platform_manifest_path = version_root / "platform-release-manifest.json"
    checksum_path = version_root / f"{dmg.name}.sha256"
    _require(dmg.is_file(), "版本归档缺少macOS DMG")
    _require(checksum_path.is_file(), "版本归档缺少macOS DMG校验文件")
    app_manifest = _read_json(app_manifest_path, "macOS App Manifest")
    _verify_platform_binding(app_manifest, capability)
    _require(
        app_manifest.get("capability_manifest_hash") == file_hash(version_root / "capability-manifest.json"),
        "macOS App Manifest未绑定正式Capability Manifest",
    )
    _require(
        app_manifest.get("capability_content_tree_hash") == capability.get("content_tree_hash"),
        "macOS App Manifest未绑定正式Capability内容树",
    )
    try:
        verify_platform_release_manifest(platform_manifest_path, app_manifest_path=app_manifest_path, dmg_path=dmg)
    except RuntimeError as error:
        raise ReleaseVerificationError(f"macOS Platform Manifest无效：{error}") from error
    expected_checksum = f"{file_hash(dmg)}  {dmg.name}\n"
    _require(checksum_path.read_text(encoding="utf-8") == expected_checksum, "macOS DMG校验文件不匹配")
    verify_dmg_install_layout(
        dmg,
        expected_content_tree_hash=str(capability["content_tree_hash"]),
        expected_manifest_hash=file_hash(version_root / "capability-manifest.json"),
    )
    _verify_macos_runtime(dmg)


def _verify_windows_release(version_root: Path, capability: dict[str, object]) -> None:
    installer = version_root / f"OptionHelper-{RELEASE_VERSION}-windows-x86_64.zip"
    app_manifest_path = version_root / "app-manifest-windows.json"
    platform_manifest_path = version_root / "platform-release-manifest-windows.json"
    checksum_path = version_root / f"{installer.name}.sha256"
    _require(installer.is_file(), "版本归档缺少Windows安装ZIP")
    _require(checksum_path.is_file(), "版本归档缺少Windows安装ZIP校验文件")
    app_manifest = _read_json(app_manifest_path, "Windows App Manifest")
    platform_manifest = _read_json(platform_manifest_path, "Windows Platform Manifest")
    _verify_platform_binding(app_manifest, capability)
    _require(
        app_manifest.get("capability_manifest_hash") == file_hash(version_root / "capability-manifest.json"),
        "Windows App Manifest未绑定正式Capability Manifest",
    )
    _require(platform_manifest.get("app_manifest_hash") == file_hash(app_manifest_path), "Windows Platform Manifest未绑定App Manifest")
    expected_installer = {"filename": installer.name, "sha256": file_hash(installer), "size": installer.stat().st_size}
    _require(platform_manifest.get("installer") == expected_installer, "Windows Platform Manifest未绑定安装ZIP")
    _require(checksum_path.read_text(encoding="utf-8") == f"{file_hash(installer)}  {installer.name}\n", "Windows安装ZIP校验文件不匹配")
    with tempfile.TemporaryDirectory(prefix="optionhelper-release-windows-") as temporary_name:
        with zipfile.ZipFile(installer) as package:
            damaged = package.testzip()
            _require(damaged is None, f"Windows安装ZIP CRC无效：{damaged}")
            package.extractall(temporary_name)
        embedded = Path(temporary_name) / "OptionHelper" / "Resources" / "capability" / "option-helper"
        errors = verify_skill(embedded)
        _require(not errors, "Windows安装ZIP内置Capability无效：" + "\n".join(errors))
        _require(
            (embedded / "capability-manifest.json").read_bytes()
            == (version_root / "capability-manifest.json").read_bytes(),
            "Windows安装ZIP内置Capability Manifest不一致",
        )


def _verify_delivery_copy(version_root: Path, platform: str, delivery_root: Path | None) -> None:
    if delivery_root is None:
        return
    installer_name = (
        f"OptionHelper-{RELEASE_VERSION}-macOS-arm64.dmg"
        if platform == "macos"
        else f"OptionHelper-{RELEASE_VERSION}-windows-x86_64.zip"
    )
    expected = {"option-helper.zip", installer_name}
    _require(delivery_root.is_dir(), f"当前交付目录不存在：{delivery_root}")
    actual = {path.name for path in delivery_root.iterdir()}
    _require(actual == expected, "当前交付目录只能包含当次Skill ZIP和安装物")
    for name in expected:
        _require(
            (delivery_root / name).read_bytes() == (version_root / name).read_bytes(),
            f"当前交付物与归档不一致：{name}",
        )


def verify_release(
    *,
    version: str = RELEASE_VERSION,
    platform: str = "macos",
    versions_root: Path = ROOT / "versions",
    delivery_root: Path | None = None,
) -> dict[str, Path]:
    """Verify a release without comparing it to the mutable development tree."""
    try:
        require_release_version(version)
    except ValueError as error:
        raise ReleaseVerificationError(str(error)) from error
    if platform not in {"macos", "windows"}:
        raise ReleaseVerificationError(f"不支持的平台：{platform}")
    versions_root = versions_root.expanduser().resolve()
    version_root = versions_root / version
    _require(version_root.is_dir(), f"版本归档不存在：{version_root}")
    capability = _verify_skill_archive(version_root)
    _verify_catalog(version_root, versions_root)
    if platform == "macos":
        _verify_macos_release(version_root, capability)
    else:
        _verify_windows_release(version_root, capability)
    _verify_delivery_copy(version_root, platform, delivery_root)
    return {
        "archive": version_root,
        "skill_zip": version_root / "option-helper.zip",
        "installer": version_root / (
            f"OptionHelper-{RELEASE_VERSION}-macOS-arm64.dmg"
            if platform == "macos"
            else f"OptionHelper-{RELEASE_VERSION}-windows-x86_64.zip"
        ),
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
