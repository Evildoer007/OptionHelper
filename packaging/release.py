#!/usr/bin/env python3
"""Create the current release without exposing partial archives."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from typing import Iterator
import uuid
import zipfile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
for path in (
    ROOT / "core" / "src",
    ROOT / "packaging",
    ROOT / "packaging" / "skill",
    ROOT / "packaging" / "app",
    ROOT / "packaging" / "app" / "agent_runtime",
    ROOT / "packaging" / "app" / "macos",
    ROOT / "packaging" / "app" / "windows",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from environment_check import check_dependencies
from release_contract import RELEASE_VERSION, require_published_at, require_release_version
from name_boundary import assert_name_boundary_clean
from source_snapshot import (
    SourceSnapshotError,
    frozen_source_snapshot,
    snapshot_module_environment,
)

# Production values are resolved from the frozen snapshot. These names remain
# available only as test seams for focused transaction fixtures.
build_candidate = validate_published_catalog = None
build_macos = build_windows = build_runtime = None
build_skill = verify_source_snapshot = None
build_app_capability = sign_capability = sign_catalog = None
verify_app = probe_runtime = verify_skill = verify_zip = None
MacOSBuildError = WindowsBuildError = RuntimeBuildError = RuntimeError


class ReleaseVerificationError(RuntimeError):
    """Test-facing release verification seam; production uses the frozen type."""


def _verification_contract():
    """Load live helpers only for standalone utilities and focused tests.

    The release transaction never calls this function. It resolves the same
    helpers from the frozen source snapshot in ``_snapshot_release_logic``.
    """

    import verify_release as module

    return module


def _installer_name(version: str, platform: str) -> str:
    return _verification_contract().installer_name(version, platform)


def _platform_archive_names(version: str, platform: str) -> set[str]:
    return _verification_contract().platform_archive_names(version, platform)


def _archived_platforms(version_root: Path, version: str) -> set[str]:
    return _verification_contract().archived_platforms(version_root, version)


def verify_formal_archive(**kwargs):
    return _verification_contract().verify_formal_archive(**kwargs)


class ReleaseError(RuntimeError):
    pass


@contextmanager
def _snapshot_release_logic(snapshot_root: Path) -> Iterator[SimpleNamespace]:
    with snapshot_module_environment(snapshot_root, ROOT):
        from runtime.knowledger.versioning import build_candidate as frozen_build_candidate
        from runtime.knowledger.versioning import validate_published_catalog as frozen_validate_catalog
        from build_macos import MacOSBuildError as frozen_macos_error
        from build_macos import build_macos as frozen_build_macos
        from build_windows import WindowsBuildError as frozen_windows_error
        from build_windows import build_windows as frozen_build_windows
        from build_runtime import RuntimeBuildError as frozen_runtime_error
        from build_runtime import build_runtime as frozen_build_runtime
        from build_skill import build_skill as frozen_build_skill
        from build_skill import verify_source_snapshot as frozen_verify_source_snapshot
        from build_capability import build_app_capability as frozen_build_app_capability
        from sign_capability import sign as frozen_sign_capability
        from sign_catalog import sign_catalog as frozen_sign_catalog
        from verify_app import verify_app as frozen_verify_app
        from verify_skill import probe_runtime as frozen_probe_runtime
        from verify_skill import verify_skill as frozen_verify_skill
        from verify_skill import verify_zip as frozen_verify_zip
        from verify_release import ReleaseVerificationError as frozen_release_verification_error
        from verify_release import archived_platforms as frozen_archived_platforms
        from verify_release import installer_name as frozen_installer_name
        from verify_release import platform_archive_names as frozen_platform_archive_names
        from verify_release import verify_formal_archive as frozen_verify_formal_archive

        yield SimpleNamespace(
            build_candidate=frozen_build_candidate,
            validate_published_catalog=frozen_validate_catalog,
            MacOSBuildError=frozen_macos_error,
            build_macos=frozen_build_macos,
            WindowsBuildError=frozen_windows_error,
            build_windows=frozen_build_windows,
            RuntimeBuildError=frozen_runtime_error,
            build_runtime=frozen_build_runtime,
            build_skill=frozen_build_skill,
            verify_source_snapshot=frozen_verify_source_snapshot,
            build_app_capability=frozen_build_app_capability,
            sign_capability=frozen_sign_capability,
            sign_catalog=frozen_sign_catalog,
            verify_app=frozen_verify_app,
            probe_runtime=frozen_probe_runtime,
            verify_skill=frozen_verify_skill,
            verify_zip=frozen_verify_zip,
            ReleaseVerificationError=frozen_release_verification_error,
            archived_platforms=frozen_archived_platforms,
            installer_name=frozen_installer_name,
            platform_archive_names=frozen_platform_archive_names,
            verify_formal_archive=frozen_verify_formal_archive,
        )


def _assert_release_dependencies() -> None:
    """Release never trusts the routine dependency cache."""

    failures: list[str] = []
    for relative in ("core/requirements.lock", "packaging/build-requirements.lock"):
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"缺少锁定依赖文件：{relative}")
            continue
        report = check_dependencies(path, force=True)
        if not bool(report.get("ok")):
            failures.append(relative)
    if failures:
        raise ReleaseError("正式发布依赖全检未通过：" + "、".join(failures))


def _assert_release_name_boundary() -> None:
    try:
        assert_name_boundary_clean(
            ROOT,
            paths=(
                ROOT / "products" / "app" / "backend",
                ROOT / "products" / "app" / "frontend",
                ROOT / "products" / "app" / "runtime",
                ROOT / "products" / "app" / "config",
                ROOT / "packaging" / "app",
            ),
        )
    except AssertionError as error:
        raise ReleaseError(str(error)) from error


def _build_windows_formal(
    version: str,
    capability_root: Path,
    *,
    dist_root: Path,
    versions_root: Path,
    agent_runtime_path: Path,
    repo_root: Path = ROOT,
    build_windows_fn=None,
) -> dict[str, Path]:
    """Force the formal runtime gate even when history is transaction-staged."""
    builder = build_windows_fn or build_windows
    if builder is None:
        raise ReleaseError("Windows构建逻辑尚未从源码快照加载")
    return builder(
        version,
        capability_root,
        dist_root=dist_root,
        versions_root=versions_root,
        agent_runtime_path=agent_runtime_path,
        require_native_runtime=True,
        formal_release=True,
        repo_root=repo_root,
    )


def _progress(message: str) -> None:
    print(f"[release] {message}", flush=True)


def _replace_delivery(staged: Path, target: Path) -> None:
    """Replace dist only after validation, restoring it if cleanup fails."""
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


def replace_delivery_root(staged: Path) -> None:
    _replace_delivery(staged, ROOT / "dist")


def extract_skill(archive: Path, destination: Path) -> Path:
    """Restore the signed ZIP used as the only Capability source for the App."""
    with zipfile.ZipFile(archive) as package:
        package.extractall(destination)
        for info in package.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                (destination / info.filename).chmod(mode)
    return destination / "option-helper"


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_file_hashes(root: Path) -> dict[Path, str]:
    return {
        path.relative_to(root): _file_hash(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_append_only(
    existing: Path,
    staged: Path,
    *,
    version: str,
    platform: str,
    platform_archive_names_fn=None,
) -> None:
    """Ensure a second platform build cannot rewrite formal common artifacts."""

    before = _archive_file_hashes(existing)
    after = _archive_file_hashes(staged)
    missing = sorted(str(path) for path in before.keys() - after.keys())
    changed = sorted(str(path) for path in before.keys() & after.keys() if before[path] != after[path])
    if missing or changed:
        details = []
        if missing:
            details.append("丢失：" + ", ".join(missing))
        if changed:
            details.append("被改写：" + ", ".join(changed))
        raise ReleaseError("补充平台安装物不得改写既有正式归档；" + "；".join(details))
    added = {str(path) for path in after.keys() - before.keys()}
    archive_names = platform_archive_names_fn or _platform_archive_names
    expected = archive_names(version, platform)
    if added != expected:
        raise ReleaseError(
            "补充平台安装物产生了未授权的归档文件；"
            f"实际：{', '.join(sorted(added)) or '无'}；"
            f"允许：{', '.join(sorted(expected))}"
        )


def assert_final_layout(
    version: str,
    *,
    platform: str,
    dist: Path,
    versions_root: Path,
    installer_name_fn=None,
    verify_zip_fn=None,
) -> dict[str, Path]:
    resolve_installer_name = installer_name_fn or _installer_name
    validate_zip = verify_zip_fn or verify_zip
    if validate_zip is None:
        validate_zip = _verification_contract().verify_zip
    installer_name = resolve_installer_name(version, platform)
    expected_dist = {"option-helper.zip", installer_name}
    actual_dist = {path.name for path in dist.iterdir()}
    if actual_dist != expected_dist:
        raise ReleaseError(
            "dist布局无效；"
            + ";".join(filter(None, (
                "非交付文件：" + ", ".join(sorted(actual_dist - expected_dist)) if actual_dist - expected_dist else "",
                "缺少交付文件：" + ", ".join(sorted(expected_dist - actual_dist)) if expected_dist - actual_dist else "",
            )))
        )
    archive = versions_root / version
    required = {
        "option-helper.zip",
        "capability-manifest.json",
        "knowledger",
        installer_name,
        f"{installer_name}.sha256",
        "app-manifest.json" if platform == "macos" else "app-manifest-windows.json",
        "platform-release-manifest.json" if platform == "macos" else "platform-release-manifest-windows.json",
        "app-capability-manifest-macos.json" if platform == "macos" else "app-capability-manifest-windows.json",
    }
    missing = [name for name in sorted(required) if not (archive / name).exists()]
    if missing:
        raise ReleaseError("版本归档缺少文件：" + ", ".join(missing))
    if (dist / "OptionHelper.app").exists() or (archive / "OptionHelper.app").exists():
        raise ReleaseError("发行目录不得保留裸.app")
    if validate_zip(archive / "option-helper.zip"):
        raise ReleaseError("正式Skill ZIP解压验收失败")
    if (dist / "option-helper.zip").read_bytes() != (archive / "option-helper.zip").read_bytes():
        raise ReleaseError("dist Skill ZIP不是本次签发归档的逐字节副本")
    if (dist / installer_name).read_bytes() != (archive / installer_name).read_bytes():
        raise ReleaseError("dist安装物不是本次签发归档的逐字节副本")
    return {"skill_zip": dist / "option-helper.zip", "installer": dist / installer_name, "archive": archive}


def _legacy_archives(versions_root: Path) -> list[Path]:
    if not versions_root.exists():
        return []
    return sorted(
        path for path in versions_root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name != RELEASE_VERSION
    )


def _commit_release(version_stage: Path, delivery_stage: Path, *, versions_root: Path) -> None:
    """Commit a verified first release or an append-only platform supplement."""
    target = versions_root / RELEASE_VERSION
    versions_root.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    try:
        if target.exists():
            # Keep the previous immutable archive inside the transaction tree.
            # A successful commit leaves no hidden history under versions, while
            # any failure can restore the public archive before returning.
            backup = version_stage.parent / f".{RELEASE_VERSION}-previous-{uuid.uuid4().hex}"
            target.rename(backup)
        version_stage.rename(target)
        replace_delivery_root(delivery_stage)
    except BaseException:
        if target.exists() and not version_stage.exists():
            target.rename(version_stage)
        if backup is not None and backup.exists() and not target.exists():
            backup.rename(target)
        raise


def release(version: str, *, platform: str, published_at: str) -> dict[str, Path]:
    try:
        require_release_version(version)
    except ValueError as error:
        raise ReleaseError(str(error)) from error
    if platform not in {"macos", "windows"}:
        raise ReleaseError(f"不支持的平台：{platform}")
    try:
        require_published_at(published_at)
    except ValueError as error:
        raise ReleaseError(str(error)) from error
    _assert_release_name_boundary()
    _assert_release_dependencies()

    versions_root = ROOT / "versions"
    if _legacy_archives(versions_root):
        raise ReleaseError("versions含旧版本归档；请先按授权迁移到可恢复目录后再签发唯一v1.0.0")
    existing_archive = versions_root / version

    with (
        tempfile.TemporaryDirectory(prefix="optionhelper-release-") as temporary_name,
        frozen_source_snapshot(ROOT, Path(temporary_name) / "source-snapshot") as snapshot,
        _snapshot_release_logic(snapshot.root) as logic,
    ):
        temporary = Path(temporary_name)
        existing_platforms: set[str] = set()
        if existing_archive.exists():
            if not existing_archive.is_dir():
                raise ReleaseError(f"正式归档路径不是目录：{existing_archive}")
            try:
                existing_platforms = logic.archived_platforms(existing_archive, version)
            except logic.ReleaseVerificationError as error:
                raise ReleaseError(str(error)) from error
            if platform in existing_platforms:
                raise ReleaseError(f"{platform}安装物已正式归档，拒绝重复签发：{existing_archive}")
            if not existing_platforms:
                raise ReleaseError("正式归档缺少任何完整平台记录，不能在不明状态上追加安装物")
        transaction_versions = temporary / "versions"
        transaction_root = transaction_versions / version
        delivery_stage = temporary / "dist"
        delivery_stage.mkdir()
        if existing_archive.exists():
            _progress("完整复验既有平台安装物、Manifest和校验文件，准备补充缺失平台安装物")
            try:
                logic.verify_formal_archive(
                    version=version,
                    versions_root=versions_root,
                    native_platform=platform,
                )
            except logic.ReleaseVerificationError as error:
                raise ReleaseError(f"既有正式归档复验失败：{error}") from error
            shutil.copytree(existing_archive, transaction_root)
            signed_zip = transaction_root / "option-helper.zip"
            shutil.copy2(signed_zip, delivery_stage / "option-helper.zip")
        else:
            knowledger_candidate = temporary / "knowledger-candidate"
            _progress("生成并验证Knowledger候选快照")
            logic.build_candidate(snapshot.root, knowledger_candidate, version=version, built_at=published_at)
            logic.sign_catalog(
                knowledger_candidate,
                version=version,
                published_at=published_at,
                root=snapshot.root,
                versions_root=transaction_versions,
            )
            logic.validate_published_catalog(
                snapshot.root,
                version,
                versions_root=transaction_versions,
                require_source_match=True,
            )

            _progress("构建并验证同源Skill与App开发契约")
            skill_candidate = logic.build_skill(
                temporary / "skill",
                catalog_version=version,
                repo_root=snapshot.root,
                versions_root=transaction_versions,
            )
            app_candidate = logic.build_app_capability(
                temporary / "app-capability-candidate",
                skill_candidate,
                repo_root=snapshot.root,
            )
            errors = [
                *logic.verify_skill(skill_candidate),
                *logic.verify_source_snapshot(
                    skill_candidate,
                    repo_root=snapshot.root,
                    versions_root=transaction_versions,
                ),
                *logic.probe_runtime(skill_candidate),
                *logic.verify_app(
                    snapshot.root / "products" / "app",
                    capability_root=app_candidate,
                ),
            ]
            if errors:
                raise ReleaseError("Skill/App同源验收失败：\n" + "\n".join(sorted(set(errors))))

            _progress("签发Capability并复验ZIP")
            signed_zip = logic.sign_capability(
                skill_candidate,
                transaction_root,
                published_at,
                versions_root=transaction_versions,
            )
            if logic.verify_zip(signed_zip):
                raise ReleaseError("签发Skill ZIP未通过解压验收")
            shutil.copy2(signed_zip, delivery_stage / "option-helper.zip")

        _progress(f"由本次签发Skill构建{platform}安装物")
        capability_root = extract_skill(signed_zip, temporary / "extracted")
        errors = logic.verify_skill(capability_root)
        if errors:
            raise ReleaseError("解压后的正式Skill未通过验收：\n" + "\n".join(errors))
        app_capability_root = logic.build_app_capability(
            temporary / "signed-app-capability",
            capability_root,
            repo_root=snapshot.root,
        )
        runtime_target = "macos-arm64" if platform == "macos" else "windows-x64"
        _progress("从当前Runtime源码构建并验证本平台原生运行时")
        try:
            runtime_candidate = logic.build_runtime(
                runtime_target,
                output_root=temporary / "agent-runtime",
                source_root=snapshot.agent_runtime_source,
                postject_path=snapshot.agent_runtime_build_tools / "node_modules" / ".bin" / (
                    "postject.cmd" if os.name == "nt" else "postject"
                ),
            ).artifact
        except logic.RuntimeBuildError as error:
            raise ReleaseError(f"Agent Runtime构建失败：{error}") from error
        try:
            if platform == "macos":
                logic.build_macos(
                    version,
                    app_capability_root,
                    dist_root=delivery_stage,
                    versions_root=transaction_versions,
                    transaction_stage=True,
                    agent_runtime_path=runtime_candidate,
                    require_native_runtime=True,
                    repo_root=snapshot.root,
                )
            else:
                _build_windows_formal(
                    version,
                    app_capability_root,
                    dist_root=delivery_stage,
                    versions_root=transaction_versions,
                    agent_runtime_path=runtime_candidate,
                    repo_root=snapshot.root,
                    build_windows_fn=logic.build_windows,
                )
        except (logic.MacOSBuildError, logic.WindowsBuildError) as error:
            raise ReleaseError(f"{platform}安装物构建失败：{error}") from error

        _progress("验收完整归档的ZIP、Manifest、安装物、签名与运行链")
        if existing_archive.exists():
            _assert_append_only(
                existing_archive,
                transaction_root,
                version=version,
                platform=platform,
                platform_archive_names_fn=logic.platform_archive_names,
            )
        assert_final_layout(
            version,
            platform=platform,
            dist=delivery_stage,
            versions_root=transaction_versions,
            installer_name_fn=logic.installer_name,
            verify_zip_fn=logic.verify_zip,
        )
        try:
            logic.verify_formal_archive(
                version=version,
                versions_root=transaction_versions,
                native_platform=platform,
            )
        except logic.ReleaseVerificationError as error:
            raise ReleaseError(f"正式归档验收失败：{error}") from error

        _progress("原子提交唯一正式版本和当前交付物")
        snapshot.verify_current()
        _commit_release(transaction_root, delivery_stage, versions_root=versions_root)
        return assert_final_layout(
            version,
            platform=platform,
            dist=ROOT / "dist",
            versions_root=versions_root,
            installer_name_fn=logic.installer_name,
            verify_zip_fn=logic.verify_zip,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="原子签发OptionHelper唯一v1.0.0发行物")
    parser.add_argument("--version", default=RELEASE_VERSION)
    parser.add_argument("--platform", choices=("macos", "windows"), default="macos")
    parser.add_argument(
        "--published-at",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        help="含时区的签发时间，默认Asia/Shanghai当前时间",
    )
    args = parser.parse_args()
    try:
        artifacts = release(args.version, platform=args.platform, published_at=args.published_at)
    except (ReleaseError, SourceSnapshotError) as error:
        print(f"签发失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({name: str(path) for name, path in artifacts.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
