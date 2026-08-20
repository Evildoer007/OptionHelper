#!/usr/bin/env python3
"""Sign one verified Capability without leaving a partial archive behind."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from build_skill import verify_source_snapshot, write_zip
from verify_skill import _module_hashes, content_tree_entries, tree_hash, verify_skill, verify_zip


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from release_contract import (
    CAPABILITY_MANIFEST_SCHEMA,
    PROTOCOL_ID,
    RELEASE_VERSION,
    require_published_at,
    require_release_version,
)


PUBLISHER = "OptionHelper Project Team"


def _published_manifest(candidate: Path, published_at: str) -> None:
    entries = content_tree_entries(candidate)
    manifest_path = candidate / "capability-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "manifest_schema": CAPABILITY_MANIFEST_SCHEMA,
        "package_status": "published",
        "capability_version": RELEASE_VERSION,
        "catalog_version": RELEASE_VERSION,
        "protocol_id": PROTOCOL_ID,
        "release_status": "published",
        "formal_release": True,
        "execution_scope": "production",
        "design_system_id": "optionhelper.design-system",
        "published_by": PUBLISHER,
        "published_at": published_at,
        "content_tree_entries": entries,
        "content_hashes": {str(item["path"]): str(item["sha256"]) for item in entries},
        "content_tree_hash": tree_hash(entries),
        "module_content_hashes": _module_hashes(entries),
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def sign(
    candidate: Path,
    release_root: Path,
    published_at: str,
    *,
    versions_root: Path | None = None,
) -> Path:
    """Create the ZIP and external Manifest together, or leave neither output."""
    candidate = candidate.resolve()
    release_root = release_root.resolve()
    require_published_at(published_at)
    require_release_version(release_root.name)
    archive_target = release_root / "option-helper.zip"
    manifest_target = release_root / "capability-manifest.json"
    if archive_target.exists() or manifest_target.exists():
        raise FileExistsError(f"Capability版本已存在：{release_root}")
    if verify_skill(candidate) or verify_source_snapshot(candidate, versions_root=versions_root):
        raise ValueError("候选未通过签发前验收")
    candidate_manifest = json.loads((candidate / "capability-manifest.json").read_text(encoding="utf-8"))
    if (
        candidate_manifest.get("catalog_version") != RELEASE_VERSION
        or candidate_manifest.get("release_status") != "candidate_from_published_catalog"
        or candidate_manifest.get("protocol_id") != PROTOCOL_ID
    ):
        raise ValueError("Capability必须绑定统一版本的正式Catalog和协议")

    parent_existed = release_root.exists()
    release_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capability-sign-", dir=release_root.parent) as temporary_name:
        staged = Path(temporary_name) / "option-helper"
        shutil.copytree(candidate, staged)
        _published_manifest(staged, published_at)
        if verify_skill(staged):
            raise ValueError("签发后Manifest验收失败")
        staged_archive = write_zip(staged)
        if verify_zip(staged_archive):
            raise ValueError("签发ZIP解压复验失败")

        release_root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(staged_archive), str(archive_target))
            shutil.copy2(staged / "capability-manifest.json", manifest_target)
        except BaseException:
            archive_target.unlink(missing_ok=True)
            manifest_target.unlink(missing_ok=True)
            if not parent_existed:
                release_root.rmdir()
            raise
    return archive_target


def main() -> None:
    parser = argparse.ArgumentParser(description="签发OptionHelper唯一v1.0.0 Capability")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()
    print(sign(args.candidate, args.release_root, args.published_at))


if __name__ == "__main__":
    main()
