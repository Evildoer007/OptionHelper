#!/usr/bin/env python3
"""Build a verified development candidate without touching immutable history."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import re
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from typing import Iterator
import uuid
import zipfile
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "packaging", ROOT / "packaging" / "skill", ROOT / "packaging" / "app", ROOT / "packaging" / "app" / "macos", ROOT / "packaging" / "app" / "windows", ROOT / "packaging" / "app" / "agent_runtime"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from release_contract import skill_archive_name
from release_contract import APP_VERSION
from source_snapshot import (
    SourceSnapshotError,
    frozen_source_snapshot,
    snapshot_module_environment,
)

# Test seams remain unbound in production. Downstream build logic is imported
# only after the read-only source snapshot exists.
build_skill = verify_source_snapshot = write_zip = None
build_app_capability = verify_app = None
build_macos = build_windows = None
probe_runtime = verify_skill = verify_zip = None
build_runtime = None


class CurrentBuildError(RuntimeError):
    pass


@contextmanager
def _snapshot_build_logic(snapshot_root: Path) -> Iterator[SimpleNamespace]:
    with snapshot_module_environment(snapshot_root, ROOT):
        from build_skill import build_skill as frozen_build_skill
        from build_skill import verify_source_snapshot as frozen_verify_source_snapshot
        from build_skill import write_zip as frozen_write_zip
        from build_capability import build_app_capability as frozen_build_app_capability
        from verify_app import verify_app as frozen_verify_app
        from build_macos import build_macos as frozen_build_macos
        from build_windows import build_windows as frozen_build_windows
        from verify_skill import probe_runtime as frozen_probe_runtime
        from verify_skill import verify_skill as frozen_verify_skill
        from verify_skill import verify_zip as frozen_verify_zip
        from build_runtime import build_runtime as frozen_build_runtime
        from release_contract import require_app_version as frozen_require_release_version

        yield SimpleNamespace(
            build_skill=frozen_build_skill,
            verify_source_snapshot=frozen_verify_source_snapshot,
            write_zip=frozen_write_zip,
            build_app_capability=frozen_build_app_capability,
            verify_app=frozen_verify_app,
            build_macos=frozen_build_macos,
            build_windows=frozen_build_windows,
            probe_runtime=frozen_probe_runtime,
            verify_skill=frozen_verify_skill,
            verify_zip=frozen_verify_zip,
            build_runtime=frozen_build_runtime,
            require_release_version=frozen_require_release_version,
        )


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
    skill, _skill_manifest_hash = _skill_zip_manifest(skill_zip)
    expected_shared_hash = skill.get("shared_payload_hash")
    if not isinstance(expected_shared_hash, str) or not expected_shared_hash:
        raise CurrentBuildError("当次Skill ZIP缺少shared_payload_hash")

    try:
        app_manifest_path = artifacts["manifest"]
        release_manifest_path = artifacts["release_manifest"]
        installer = artifacts["dmg" if platform == "macos" else "installer"]
        app = json.loads(app_manifest_path.read_text(encoding="utf-8"))
        release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise CurrentBuildError(f"无法读取当次App发行绑定记录：{error}") from error

    for label, manifest in (("App", app), ("Platform", release)):
        if manifest.get("shared_payload_hash") != expected_shared_hash:
            raise CurrentBuildError(f"{label}共享计算载荷与当次Skill ZIP不一致")
        if manifest.get("catalog_version") != skill.get("catalog_version"):
            raise CurrentBuildError(f"{label} Catalog与当次Skill ZIP不一致")
        if manifest.get("protocol_id") != skill.get("protocol_id"):
            raise CurrentBuildError(f"{label}协议与当次Skill ZIP不一致")

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


def _verify_layout(stage: Path, version: str, platform: str, *, verify_zip_fn) -> None:
    installer = (
        f"OptionHelper-{version}-macOS-arm64.dmg"
        if platform == "macos"
        else f"OptionHelper-{version}-windows-x86_64.zip"
    )
    names = {path.name for path in stage.iterdir()}
    expected = {skill_archive_name(), installer}
    if names != expected:
        raise CurrentBuildError(f"dist候选包含非交付物：{', '.join(sorted(names - expected))}")
    if verify_zip_fn(stage / skill_archive_name()):
        raise CurrentBuildError("候选Skill ZIP未通过解压验收")
    if (stage / "OptionHelper.app").exists():
        raise CurrentBuildError("dist不得保留裸.app")


def archive_deliveries(delivery_root: Path, versions_root: Path) -> list[Path]:
    """Retain immutable, content-addressed history before replacing downloads."""
    archived = []
    if not delivery_root.is_dir():
        return archived
    for artifact in sorted(delivery_root.iterdir()):
        if not artifact.is_file():
            continue
        skill = re.fullmatch(r"option-helper-(v[0-9][A-Za-z0-9._-]*)\.zip", artifact.name)
        app = re.fullmatch(r"OptionHelper-(v[0-9][A-Za-z0-9._-]*)-(?:macOS-arm64\.dmg|windows-x86_64\.zip)", artifact.name)
        match = skill or app
        if not match:
            continue
        digest = _file_hash(artifact)
        folder = versions_root / ("skill" if skill else "app") / match.group(1) / digest
        target = folder / artifact.name
        if target.exists():
            if _file_hash(target) != digest:
                raise CurrentBuildError("历史交付物校验失败，拒绝覆盖：" + str(target))
        else:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                shutil.copy2(artifact, temporary)
                if _file_hash(temporary) != digest:
                    raise CurrentBuildError("历史归档复制校验失败")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            (folder / "artifact.json").write_text(json.dumps({
                "product": "skill" if skill else "app", "version": match.group(1),
                "filename": artifact.name, "sha256": digest,
                "release_status": "local_candidate",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        archived.append(target)
    return archived


def build_current(version: str, platform: str) -> dict[str, Path]:
    if platform not in {"macos", "windows"}:
        raise CurrentBuildError(f"不支持的平台：{platform}")
    total_steps = 7
    _progress(1, total_steps, "正在准备临时构建目录")
    with (
        tempfile.TemporaryDirectory(prefix="optionhelper-current-") as temporary_name,
        frozen_source_snapshot(ROOT, Path(temporary_name) / "source-snapshot") as snapshot,
        _snapshot_build_logic(snapshot.root) as logic,
    ):
        try:
            logic.require_release_version(version)
        except ValueError as error:
            raise CurrentBuildError(str(error)) from error
        temporary = Path(temporary_name)
        stage = temporary / "dist"
        stage.mkdir()
        _progress(2, total_steps, "正在从当前源码构建临时Skill候选")
        skill = logic.build_skill(temporary / "skill", candidate=True, repo_root=snapshot.root)
        _progress(3, total_steps, "正在验证Skill并组装独立App能力包")
        errors = [
            *logic.verify_skill(skill),
            *logic.verify_source_snapshot(skill, repo_root=snapshot.root),
            *logic.probe_runtime(skill),
        ]
        if errors:
            raise CurrentBuildError("候选Skill未通过验收：\n" + "\n".join(errors))
        app_capability = logic.build_app_capability(
            temporary / "app-capability",
            skill,
            repo_root=snapshot.root,
        )
        app_errors = logic.verify_app(
            snapshot.root / "products" / "app",
            capability_root=app_capability,
        )
        if app_errors:
            raise CurrentBuildError("App开发源未通过当次Capability验收：\n" + "\n".join(app_errors))
        _progress(4, total_steps, "正在归档并校验Skill ZIP")
        skill_zip = logic.write_zip(skill)
        if logic.verify_zip(skill_zip):
            raise CurrentBuildError("候选Skill ZIP未通过解压验收")
        shutil.copy2(skill_zip, stage / skill_archive_name())

        # The platform builders own their temporary version directory and
        # create it only when their artifacts are ready to commit.  The root
        # itself remains isolated from the immutable versions/ archive.
        candidate_history = temporary / "history"
        _progress(5, total_steps, "正在打包平台应用和安装物")
        runtime_target = "macos-arm64" if platform == "macos" else "windows-x64"
        runtime_candidate = logic.build_runtime(
            runtime_target,
            output_root=temporary / "agent-runtime",
            source_root=snapshot.agent_runtime_source,
            postject_path=snapshot.agent_runtime_build_tools / "node_modules" / ".bin" / (
                "postject.cmd" if os.name == "nt" else "postject"
            ),
        ).artifact
        if platform == "macos":
            platform_kwargs = {
                "dist_root": stage,
                "versions_root": candidate_history,
                "agent_runtime_path": runtime_candidate,
                "require_native_runtime": True,
                "repo_root": snapshot.root,
            }
            artifacts = logic.build_macos(version, app_capability, **platform_kwargs)
        else:
            platform_kwargs = {
                "dist_root": stage,
                "versions_root": candidate_history,
                "agent_runtime_path": runtime_candidate,
                "require_native_runtime": True,
                "repo_root": snapshot.root,
            }
            artifacts = logic.build_windows(version, app_capability, **platform_kwargs)
        _progress(6, total_steps, "正在核对Skill与安装物的内容绑定")
        _verify_platform_binding(stage / skill_archive_name(), artifacts, platform)
        _verify_layout(stage, version, platform, verify_zip_fn=logic.verify_zip)
        installer = artifacts["dmg"] if platform == "macos" else artifacts["installer"]
        result = {"skill_zip": stage / skill_archive_name(), "installer": installer}
        delivery_root = ROOT / "dist" if platform == "macos" else ROOT / "result" / "windows-candidate"
        _progress(7, total_steps, "正在原子发布本次候选交付物")
        snapshot.verify_current()
        archive_deliveries(delivery_root, ROOT / "versions")
        archive_deliveries(stage, ROOT / "versions")
        if platform == "macos":
            _replace_dist(stage)
        else:
            _replace_delivery(stage, delivery_root)
    return {name: delivery_root / path.name for name, path in result.items()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一键构建OptionHelper开发候选；macOS写dist，Windows隔离到result/windows-candidate"
    )
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--platform", choices=("macos", "windows"), required=True)
    args = parser.parse_args()
    try:
        artifacts = build_current(args.version, args.platform)
    except (CurrentBuildError, SourceSnapshotError) as error:
        print(f"构建失败：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    for name, path in artifacts.items():
        print(f"{name}={path}")
    destination = "dist" if args.platform == "macos" else "result/windows-candidate"
    print(f"构建验证完成：当前候选交付物已原子更新到{destination}。", flush=True)


if __name__ == "__main__":
    main()
