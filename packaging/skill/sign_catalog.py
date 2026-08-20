#!/usr/bin/env python3
"""将已验证Knowledger技术候选受控签发为不可覆盖的正式版本。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "core" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "core" / "src"))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from runtime.knowledger.versioning import SNAPSHOT_FILES, validate_published_catalog, verify_candidate
from release_contract import require_published_at, require_release_version


PUBLISHER = "OptionHelper Project Team"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def sign_catalog(
    candidate: Path,
    *,
    version: str,
    published_at: str,
    root: Path = ROOT,
    versions_root: Path | None = None,
) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    require_release_version(version)
    require_published_at(published_at)
    verify_candidate(root, candidate)
    snapshot = json.loads((candidate / "snapshot.json").read_text(encoding="utf-8"))
    if snapshot.get("proposed_catalog_version") != version or snapshot.get("integrity_evidence", {}).get("default_asset_registry_drift"):
        raise ValueError("技术候选版本或默认资产冻结哈希未通过，拒绝签发")
    archive_root = versions_root.resolve() if versions_root else root / "versions"
    release_root = archive_root / version
    catalog_target = release_root / "knowledger" / "catalog-version.json"
    if release_root.exists():
        raise FileExistsError(f"正式版本{version}已存在，禁止覆盖")
    archive_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="knowl-sign-", dir=archive_root) as temporary:
        staged = Path(temporary) / version
        refs: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for product_id in snapshot["proposed_product_versions"]:
            source = candidate / "products" / product_id
            target = staged / "knowledger" / "products" / product_id
            target.mkdir(parents=True)
            technical = json.loads((source / "product-snapshot.json").read_text(encoding="utf-8"))
            for filename in SNAPSHOT_FILES.values():
                shutil.copy2(source / filename, target / filename)
            manifest = {
                "manifest_type": "ProductVersion", "publication_status": "published", "formal_release": True,
                "executable": True, "product_id": product_id, "product_version": version,
                "published_by": PUBLISHER, "published_at": published_at, "snapshot_files": SNAPSHOT_FILES,
                "snapshot_sha256": technical["snapshot_sha256"],
            }
            path = target / "product-version.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="")
            refs[product_id] = f"knowledger/products/{product_id}/product-version.json"
            hashes[product_id] = _digest(path)
        catalog = {
            "manifest_type": "CatalogVersion", "publication_status": "published", "formal_release": True,
            "executable": True, "catalog_version": version, "published_by": PUBLISHER, "published_at": published_at,
            "products": snapshot["proposed_product_versions"], "ordered_product_version_map": snapshot["proposed_product_versions"],
            "product_manifest_refs": refs, "product_manifest_sha256": hashes,
            "source_file_sha256": snapshot["source_file_sha256"],
        }
        catalog_path = staged / "knowledger" / "catalog-version.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False) + "\n", encoding="utf-8", newline="")
        validate_published_catalog(root, version, versions_root=Path(temporary), require_source_match=True)
        archive_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(release_root))
    return catalog_target


def main() -> None:
    parser = argparse.ArgumentParser(description="签发已验证Knowledger技术候选")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()
    print(sign_catalog(args.candidate, version=args.version, published_at=args.published_at))


if __name__ == "__main__":
    main()
