#!/usr/bin/env python3
"""从当前权威源一次性生成一个不可覆盖版本的Skill与平台交付物。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
MACOS_PACKAGING = ROOT / "packaging" / "app" / "macos"
WINDOWS_PACKAGING = ROOT / "packaging" / "app" / "windows"
for path in (ROOT / "core" / "src", SKILL_PACKAGING, MACOS_PACKAGING, WINDOWS_PACKAGING):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime.knowledger.versioning import build_candidate, validate_published_catalog
from build_skill import build_skill
from sign_catalog import sign_catalog
from sign_capability import sign as sign_capability
from verify_skill import verify_skill, verify_zip
from build_macos import build_macos
from build_windows import build_windows


class ReleaseError(RuntimeError):
    pass


def _progress(message: str) -> None:
    print(f"[release] {message}", flush=True)


def replace_delivery_root(staged: Path) -> None:
    """Only replace dist after both Skill and platform artifacts are valid."""
    dist = ROOT / "dist"
    backup = ROOT / f".dist-backup-{uuid.uuid4().hex}"
    if dist.exists():
        dist.rename(backup)
    try:
        staged.rename(dist)
    except BaseException:
        if backup.exists() and not dist.exists():
            backup.rename(dist)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except BaseException:
            if dist.exists() and not staged.exists():
                dist.rename(staged)
            if backup.exists() and not dist.exists():
                backup.rename(dist)
            raise


def extract_skill(archive: Path, destination: Path) -> Path:
    """按ZIP记录恢复可执行位，供macOS封装读取同一份已签发Skill。"""
    with zipfile.ZipFile(archive) as package:
        package.extractall(destination)
        for info in package.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                (destination / info.filename).chmod(mode)
    return destination / "option-helper"


def assert_final_layout(version: str, *, platform: str, dist: Path | None = None) -> dict[str, Path]:
    dist = dist or ROOT / "dist"
    archive = ROOT / "versions" / version
    installer = (
        f"OptionHelper-{version}-macOS-arm64.dmg"
        if platform == "macos"
        else f"OptionHelper-{version}-windows-x86_64.zip"
    )
    expected_dist = {"option-helper.zip", installer}
    actual_dist = {path.name for path in dist.iterdir()}
    if actual_dist != expected_dist:
        unexpected = sorted(actual_dist - expected_dist)
        missing = sorted(expected_dist - actual_dist)
        details = []
        if unexpected:
            details.append("非交付文件：" + ", ".join(unexpected))
        if missing:
            details.append("缺少交付文件：" + ", ".join(missing))
        raise ReleaseError("dist布局无效；" + "；".join(details))
    required = {
        "option-helper.zip", "capability-manifest.json", "knowledger",
        installer,
        f"{installer}.sha256",
        "app-manifest.json" if platform == "macos" else "app-manifest-windows.json",
        "platform-release-manifest.json" if platform == "macos" else "platform-release-manifest-windows.json",
    }
    missing = [name for name in sorted(required) if not (archive / name).exists()]
    if missing:
        raise ReleaseError("版本归档缺少文件：" + ", ".join(missing))
    forbidden = [path for path in [dist / "OptionHelper.app", archive / "OptionHelper.app"] if path.exists()]
    if forbidden:
        raise ReleaseError("发行目录不得保留裸.app：" + ", ".join(str(path) for path in forbidden))
    if verify_zip(archive / "option-helper.zip"):
        raise ReleaseError("正式Skill ZIP解压验收失败")
    return {
        "skill_zip": dist / "option-helper.zip",
        "installer": dist / installer,
        "archive": archive,
    }


def release(version: str, *, platform: str, published_at: str) -> dict[str, Path]:
    if version != "v1.0":
        raise ReleaseError("本轮外部版本锁定v1.0")
    dist = ROOT / "dist"
    archive = ROOT / "versions" / version
    if archive.exists():
        raise ReleaseError(f"正式归档已存在，拒绝覆盖：{archive}")

    try:
        with tempfile.TemporaryDirectory(prefix="optionhelper-release-") as temporary_name:
            temporary = Path(temporary_name)
            delivery_stage = temporary / "dist"
            delivery_stage.mkdir()
            knowledger_candidate = temporary / "knowledger-candidate"
            _progress("生成Knowledger候选快照")
            build_candidate(ROOT, knowledger_candidate, version=version, built_at=published_at)
            _progress("签发Knowledger快照")
            sign_catalog(knowledger_candidate, version=version, published_at=published_at, root=ROOT)
            validate_published_catalog(ROOT, version)

            _progress("构建并验证Skill")
            skill_candidate = build_skill(temporary / "skill", catalog_version=version, repo_root=ROOT)
            if verify_skill(skill_candidate):
                raise ReleaseError("Skill候选未通过验收")
            signed_zip = sign_capability(skill_candidate, archive, published_at)
            if verify_zip(signed_zip):
                raise ReleaseError("签发Skill ZIP未通过验收")
            shutil.copy2(signed_zip, delivery_stage / "option-helper.zip")

            _progress(f"从已签发Skill构建{platform} App并运行验收")
            capability_root = extract_skill(signed_zip, temporary / "extracted")
            errors = verify_skill(capability_root)
            if errors:
                raise ReleaseError("解压后的正式Skill未通过验收：\n" + "\n".join(errors))
            if platform == "macos":
                build_macos(version, capability_root, dist_root=delivery_stage)
            elif platform == "windows":
                build_windows(version, capability_root, dist_root=delivery_stage)
            else:
                raise ReleaseError(f"不支持的平台：{platform}")
            assert_final_layout(version, platform=platform, dist=delivery_stage)
            replace_delivery_root(delivery_stage)
    except BaseException:
        _progress("构建未完成；未自动删除任何版本或Finder冲突文件")
        raise

    return assert_final_layout(version, platform=platform)


def main() -> None:
    parser = argparse.ArgumentParser(description="一次性生成OptionHelper Skill与平台发行物")
    parser.add_argument("--version", default="v1.0")
    parser.add_argument("--platform", choices=("macos", "windows"), default="macos")
    parser.add_argument("--published-at", default=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    args = parser.parse_args()
    artifacts = release(args.version, platform=args.platform, published_at=args.published_at)
    print(json.dumps({name: str(path) for name, path in artifacts.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
