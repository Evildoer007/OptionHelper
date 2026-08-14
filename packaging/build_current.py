#!/usr/bin/env python3
"""Build a verified v1.0.0 candidate without touching immutable history."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "packaging", ROOT / "packaging" / "skill", ROOT / "packaging" / "app", ROOT / "packaging" / "app" / "macos", ROOT / "packaging" / "app" / "windows"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_skill import build_skill, verify_source_snapshot, write_zip
from verify_app import verify_app
from build_macos import build_macos
from build_windows import build_windows
from verify_skill import probe_runtime, verify_skill, verify_zip
from release_contract import RELEASE_VERSION, require_release_version


class CurrentBuildError(RuntimeError):
    pass


def _progress(step: int, total: int, message: str) -> None:
    """Emit one human-readable stage before potentially long build work."""
    print(f"[{step}/{total}] {message}", flush=True)


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _skill_zip_manifest(archive: Path) -> tuple[dict[str, object], str]:
    try:
        with zipfile.ZipFile(archive) as package:
            raw = package.read("option-helper/capability-manifest.json")
        manifest = json.loads(raw)
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise CurrentBuildError(f"无法读取当次Skill ZIP Manifest：{error}") from error
    if not isinstance(manifest, dict):
        raise CurrentBuildError("当次Skill ZIP Manifest格式无效")
    return manifest, sha256(raw).hexdigest()


def _verify_platform_binding(skill_zip: Path, artifacts: dict[str, Path], platform: str) -> None:
    skill, skill_manifest_hash = _skill_zip_manifest(skill_zip)
    expected_tree_hash = skill.get("content_tree_hash")
    if not isinstance(expected_tree_hash, str) or not expected_tree_hash:
        raise CurrentBuildError("当次Skill ZIP缺少content_tree_hash")

    try:
        app_manifest_path = artifacts["manifest"]
        release_manifest_path = artifacts["release_manifest"]
        installer = artifacts["dmg" if platform == "macos" else "installer"]
        app = json.loads(app_manifest_path.read_text(encoding="utf-8"))
        release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise CurrentBuildError(f"无法读取当次App发行绑定记录：{error}") from error

    for label, manifest in (("App", app), ("Platform", release)):
        if manifest.get("capability_content_tree_hash") != expected_tree_hash:
            raise CurrentBuildError(f"{label} Capability content_tree_hash与当次Skill ZIP不一致")
        if manifest.get("capability_manifest_hash") != skill_manifest_hash:
            raise CurrentBuildError(f"{label} Capability Manifest哈希与当次Skill ZIP不一致")

    expected_installer = {
        "filename": installer.name,
        "sha256": _file_hash(installer),
        "size": installer.stat().st_size,
    }
    if release.get("installer") != expected_installer:
        raise CurrentBuildError("Platform Manifest未绑定当次安装物")


def _replace_delivery(staged: Path, target: Path) -> None:
    """Atomically replace one generated delivery directory after validation."""
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
    if target.exists():
        target.rename(backup)
    try:
        staged.rename(target)
    except BaseException:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except BaseException:
            if target.exists() and not staged.exists():
                target.rename(staged)
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise


def _replace_dist(staged: Path) -> None:
    """Replace the locked two-file macOS delivery without exposing half output."""
    _replace_delivery(staged, ROOT / "dist")


def _verify_layout(stage: Path, version: str, platform: str) -> None:
    installer = (
        f"OptionHelper-{version}-macOS-arm64.dmg"
        if platform == "macos"
        else f"OptionHelper-{version}-windows-x86_64.zip"
    )
    names = {path.name for path in stage.iterdir()}
    expected = {"option-helper.zip", installer}
    if names != expected:
        raise CurrentBuildError(f"dist候选包含非交付物：{', '.join(sorted(names - expected))}")
    if verify_zip(stage / "option-helper.zip"):
        raise CurrentBuildError("候选Skill ZIP未通过解压验收")
    if (stage / "OptionHelper.app").exists():
        raise CurrentBuildError("dist不得保留裸.app")


def build_current(version: str, platform: str) -> dict[str, Path]:
    try:
        require_release_version(version)
    except ValueError as error:
        raise CurrentBuildError(str(error)) from error
    if platform not in {"macos", "windows"}:
        raise CurrentBuildError(f"不支持的平台：{platform}")
    total_steps = 7
    _progress(1, total_steps, "正在准备临时构建目录")
    with tempfile.TemporaryDirectory(prefix="optionhelper-current-") as temporary_name:
        temporary = Path(temporary_name)
        stage = temporary / "dist"
        stage.mkdir()
        _progress(2, total_steps, "正在从当前源码构建临时Skill候选")
        skill = build_skill(temporary / "skill", candidate=True, repo_root=ROOT)
        _progress(3, total_steps, "正在验证Skill内容、运行时和App契约")
        errors = [*verify_skill(skill), *verify_source_snapshot(skill, repo_root=ROOT), *probe_runtime(skill)]
        if errors:
            raise CurrentBuildError(f"{RELEASE_VERSION}候选Skill未通过验收：\n" + "\n".join(errors))
        app_errors = verify_app(ROOT / "products" / "app", capability_root=skill)
        if app_errors:
            raise CurrentBuildError("App开发源未通过当次Capability验收：\n" + "\n".join(app_errors))
        _progress(4, total_steps, "正在归档并校验Skill ZIP")
        skill_zip = write_zip(skill)
        if verify_zip(skill_zip):
            raise CurrentBuildError(f"{RELEASE_VERSION}候选Skill ZIP未通过解压验收")
        shutil.copy2(skill_zip, stage / "option-helper.zip")

        # The platform builders own their temporary version directory and
        # create it only when their artifacts are ready to commit.  The root
        # itself remains isolated from the immutable versions/ archive.
        candidate_history = temporary / "history"
        _progress(5, total_steps, "正在打包平台应用和安装物")
        if platform == "macos":
            artifacts = build_macos(version, skill, dist_root=stage, versions_root=candidate_history)
        else:
            artifacts = build_windows(version, skill, dist_root=stage, versions_root=candidate_history)
        _progress(6, total_steps, "正在核对Skill与安装物的内容绑定")
        _verify_platform_binding(stage / "option-helper.zip", artifacts, platform)
        _verify_layout(stage, version, platform)
        installer = artifacts["dmg"] if platform == "macos" else artifacts["installer"]
        result = {"skill_zip": stage / "option-helper.zip", "installer": installer}
        delivery_root = ROOT / "dist" if platform == "macos" else ROOT / "result" / "windows-candidate"
        _progress(7, total_steps, "正在原子发布本次候选交付物")
        if platform == "macos":
            _replace_dist(stage)
        else:
            _replace_delivery(stage, delivery_root)
    return {name: delivery_root / path.name for name, path in result.items()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一键构建OptionHelper v1.0.0候选；macOS写dist，Windows隔离到result/windows-candidate"
    )
    parser.add_argument("--version", default=RELEASE_VERSION)
    parser.add_argument("--platform", choices=("macos", "windows"), required=True)
    args = parser.parse_args()
    try:
        artifacts = build_current(args.version, args.platform)
    except CurrentBuildError as error:
        print(f"构建失败：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    for name, path in artifacts.items():
        print(f"{name}={path}")
    print("构建验证完成：当前候选交付物已原子更新到dist。", flush=True)


if __name__ == "__main__":
    main()
