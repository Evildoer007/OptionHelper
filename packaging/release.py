#!/usr/bin/env python3
"""Create the current release without exposing partial archives."""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
import zipfile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
for path in (
    ROOT / "core" / "src",
    ROOT / "packaging",
    ROOT / "packaging" / "skill",
    ROOT / "packaging" / "app",
    ROOT / "packaging" / "app" / "macos",
    ROOT / "packaging" / "app" / "windows",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime.knowledger.versioning import build_candidate, validate_published_catalog
from build_macos import build_macos
from build_windows import build_windows
from build_skill import build_skill, verify_source_snapshot
from environment_check import check_dependencies
from release_contract import RELEASE_VERSION, require_published_at, require_release_version
from sign_capability import sign as sign_capability
from sign_catalog import sign_catalog
from verify_app import verify_app
from verify_release import (
    ReleaseVerificationError,
    archived_platforms as _archived_platforms,
    installer_name as _installer_name,
    platform_archive_names as _platform_archive_names,
    verify_formal_archive,
)
from verify_skill import probe_runtime, verify_skill, verify_zip


class ReleaseError(RuntimeError):
    pass


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


def _assert_append_only(existing: Path, staged: Path, *, version: str, platform: str) -> None:
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
    expected = _platform_archive_names(version, platform)
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
) -> dict[str, Path]:
    installer_name = _installer_name(version, platform)
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
    }
    missing = [name for name in sorted(required) if not (archive / name).exists()]
    if missing:
        raise ReleaseError("版本归档缺少文件：" + ", ".join(missing))
    if (dist / "OptionHelper.app").exists() or (archive / "OptionHelper.app").exists():
        raise ReleaseError("发行目录不得保留裸.app")
    if verify_zip(archive / "option-helper.zip"):
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
    if target.exists():
        backup = versions_root / f".{RELEASE_VERSION}-backup-{uuid.uuid4().hex}"
        target.rename(backup)
    version_stage.rename(target)
    try:
        replace_delivery_root(delivery_stage)
    except BaseException:
        if target.exists() and not version_stage.exists():
            target.rename(version_stage)
        if backup is not None and backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup)


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
    _assert_release_dependencies()

    versions_root = ROOT / "versions"
    if _legacy_archives(versions_root):
        raise ReleaseError("versions含旧版本归档；请先按授权迁移到可恢复目录后再签发唯一v1.0.0")
    existing_archive = versions_root / version
    existing_platforms: set[str] = set()
    if existing_archive.exists():
        if not existing_archive.is_dir():
            raise ReleaseError(f"正式归档路径不是目录：{existing_archive}")
        try:
            existing_platforms = _archived_platforms(existing_archive, version)
        except ReleaseVerificationError as error:
            raise ReleaseError(str(error)) from error
        if platform in existing_platforms:
            raise ReleaseError(f"{platform}安装物已正式归档，拒绝重复签发：{existing_archive}")
        if not existing_platforms:
            raise ReleaseError("正式归档缺少任何完整平台记录，不能在不明状态上追加安装物")

    with tempfile.TemporaryDirectory(prefix="optionhelper-release-") as temporary_name:
        temporary = Path(temporary_name)
        transaction_versions = temporary / "versions"
        transaction_root = transaction_versions / version
        delivery_stage = temporary / "dist"
        delivery_stage.mkdir()
        if existing_archive.exists():
            _progress("完整复验既有平台安装物、Manifest和校验文件，准备补充缺失平台安装物")
            try:
                verify_formal_archive(
                    version=version,
                    versions_root=versions_root,
                    native_platform=platform,
                )
            except ReleaseVerificationError as error:
                raise ReleaseError(f"既有正式归档复验失败：{error}") from error
            shutil.copytree(existing_archive, transaction_root)
            signed_zip = transaction_root / "option-helper.zip"
            shutil.copy2(signed_zip, delivery_stage / "option-helper.zip")
        else:
            knowledger_candidate = temporary / "knowledger-candidate"
            _progress("生成并验证Knowledger候选快照")
            build_candidate(ROOT, knowledger_candidate, version=version, built_at=published_at)
            sign_catalog(
                knowledger_candidate,
                version=version,
                published_at=published_at,
                root=ROOT,
                versions_root=transaction_versions,
            )
            validate_published_catalog(ROOT, version, versions_root=transaction_versions, require_source_match=True)

            _progress("构建并验证同源Skill与App开发契约")
            skill_candidate = build_skill(
                temporary / "skill",
                catalog_version=version,
                repo_root=ROOT,
                versions_root=transaction_versions,
            )
            errors = [
                *verify_skill(skill_candidate),
                *verify_source_snapshot(skill_candidate, repo_root=ROOT, versions_root=transaction_versions),
                *probe_runtime(skill_candidate),
                *verify_app(ROOT / "products" / "app", capability_root=skill_candidate),
            ]
            if errors:
                raise ReleaseError("Skill/App同源验收失败：\n" + "\n".join(sorted(set(errors))))

            _progress("签发Capability并复验ZIP")
            signed_zip = sign_capability(
                skill_candidate,
                transaction_root,
                published_at,
                versions_root=transaction_versions,
            )
            if verify_zip(signed_zip):
                raise ReleaseError("签发Skill ZIP未通过解压验收")
            shutil.copy2(signed_zip, delivery_stage / "option-helper.zip")

        _progress(f"由本次签发Skill构建{platform}安装物")
        capability_root = extract_skill(signed_zip, temporary / "extracted")
        errors = verify_skill(capability_root)
        if errors:
            raise ReleaseError("解压后的正式Skill未通过验收：\n" + "\n".join(errors))
        if platform == "macos":
            build_macos(
                version,
                capability_root,
                dist_root=delivery_stage,
                versions_root=transaction_versions,
                transaction_stage=True,
            )
        else:
            build_windows(version, capability_root, dist_root=delivery_stage, versions_root=transaction_versions)

        _progress("验收完整归档的ZIP、Manifest、安装物、签名与运行链")
        if existing_archive.exists():
            _assert_append_only(existing_archive, transaction_root, version=version, platform=platform)
        assert_final_layout(version, platform=platform, dist=delivery_stage, versions_root=transaction_versions)
        try:
            verify_formal_archive(
                version=version,
                versions_root=transaction_versions,
                native_platform=platform,
            )
        except ReleaseVerificationError as error:
            raise ReleaseError(f"正式归档验收失败：{error}") from error

        _progress("原子提交唯一正式版本和当前交付物")
        _commit_release(transaction_root, delivery_stage, versions_root=versions_root)
    return assert_final_layout(version, platform=platform, dist=ROOT / "dist", versions_root=versions_root)


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
    except ReleaseError as error:
        print(f"签发失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({name: str(path) for name, path in artifacts.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
