from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.knowledger.versioning import (  # noqa: E402
    SNAPSHOT_FILES,
    activate_catalog,
    build_candidate,
    load_current_catalog,
    load_current_product_snapshot,
    load_packaged_product_snapshot,
    validate_published_catalog,
    verify_candidate_freshness,
    verify_candidate_integrity,
)


BUILT_AT = "2026-08-07T12:00:00+08:00"


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _publish_fixture(candidate: Path, versions_root: Path, catalog_version: str = "v1.0") -> Path:
    snapshot = json.loads((candidate / "snapshot.json").read_text(encoding="utf-8"))
    if snapshot["integrity_evidence"]["default_asset_registry_drift"] != {}:
        raise AssertionError("正式测试夹具要求候选默认资产已与OptionReg一致")
    ids = list(snapshot["proposed_product_versions"])
    refs: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for product_id in ids:
        source = candidate / "products" / product_id
        target = versions_root / catalog_version / "knowledger" / "products" / product_id
        target.mkdir(parents=True, exist_ok=True)
        technical = json.loads((source / "product-snapshot.json").read_text(encoding="utf-8"))
        for filename in SNAPSHOT_FILES.values():
            shutil.copy2(source / filename, target / filename)
        manifest = {
            "manifest_type": "ProductVersion", "publication_status": "published",
            "formal_release": True, "executable": True, "product_id": product_id,
            "product_version": "v1.0", "published_by": "unit-test",
            "published_at": BUILT_AT, "snapshot_files": SNAPSHOT_FILES,
            "snapshot_sha256": technical["snapshot_sha256"],
        }
        manifest_path = target / "product-version.json"
        _write_json(manifest_path, manifest)
        refs[product_id] = f"knowledger/products/{product_id}/product-version.json"
        hashes[product_id] = _hash(manifest_path)
    catalog = {
        "manifest_type": "CatalogVersion", "publication_status": "published",
        "formal_release": True, "executable": True, "catalog_version": catalog_version,
        "published_by": "unit-test", "published_at": BUILT_AT,
        "products": {product_id: "v1.0" for product_id in ids},
        "ordered_product_version_map": {product_id: "v1.0" for product_id in ids},
        "product_manifest_refs": refs, "product_manifest_sha256": hashes,
        "source_file_sha256": snapshot["source_file_sha256"],
    }
    catalog_path = versions_root / catalog_version / "knowledger" / "catalog-version.json"
    _write_json(catalog_path, catalog)
    return catalog_path


class KnowledgerVersioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._temporary.name)
        cls.candidate = cls.workspace / "candidate-v1.0"
        build_candidate(ROOT, cls.candidate, version="v1.0", built_at=BUILT_AT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _copy_candidate(self, name: str) -> Path:
        target = self.workspace / name
        shutil.copytree(self.candidate, target)
        return target

    def test_candidate_is_self_contained_and_not_formal(self) -> None:
        snapshot = verify_candidate_integrity(self.candidate)
        self.assertEqual(snapshot["product_count"], 65)
        self.assertFalse(snapshot["formal_release"])
        self.assertFalse(snapshot["executable"])
        self.assertNotIn("release_id", snapshot)
        for product_id, relative in snapshot["product_snapshot_refs"].items():
            self.assertEqual(relative, f"products/{product_id}/product-snapshot.json")
            product = json.loads((self.candidate / relative).read_text(encoding="utf-8"))
            self.assertEqual(set(product["snapshot_files"].values()), set(SNAPSHOT_FILES.values()))

    def test_candidate_rejects_formal_tree_and_unsafe_refs(self) -> None:
        with self.assertRaisesRegex(ValueError, "不得写入versions"):
            build_candidate(ROOT, ROOT / "versions" / "bad", version="v1.0", built_at=BUILT_AT)
        for name, unsafe in (("absolute", "/tmp/product-snapshot.json"), ("traversal", "../product-snapshot.json")):
            target = self._copy_candidate(name)
            snapshot_path = target / "snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            first = next(iter(snapshot["product_snapshot_refs"]))
            snapshot["product_snapshot_refs"][first] = unsafe
            _write_json(snapshot_path, snapshot)
            with self.assertRaisesRegex(ValueError, "引用必须固定"):
                verify_candidate_integrity(target)

    def test_integrity_is_self_contained_and_freshness_detects_source_drift(self) -> None:
        fake = self.workspace / "source-drift-repo"
        (fake / "references").mkdir(parents=True)
        shutil.copy2(ROOT / "SKILL.md", fake / "SKILL.md")
        for name in ("optionlist.md", "optionlib.md", "optionreg.py"):
            shutil.copy2(ROOT / "references" / name, fake / "references" / name)
        baseline = fake / "tests" / "baselines"
        baseline.mkdir(parents=True)
        shutil.copy2(ROOT / "tests" / "baselines" / "payoffer_default_assets.sha256", baseline)
        shutil.copytree(ROOT / "modules" / "payoffer" / "figures", fake / "modules" / "payoffer" / "figures")
        candidate = self.workspace / "source-drift-candidate"
        with patch.dict(os.environ, {"OPTIONHELPER_PROJECT_ROOT": str(fake)}):
            build_candidate(fake, candidate, version="v1.0", built_at=BUILT_AT)
            verify_candidate_integrity(candidate)
            with (fake / "references" / "optionlib.md").open("a", encoding="utf-8") as stream:
                stream.write("\n")
            verify_candidate_integrity(candidate)
            with self.assertRaisesRegex(ValueError, "当前权威源"):
                verify_candidate_freshness(fake, candidate)

    def test_formal_catalog_current_switch_rollback_and_packaged_read(self) -> None:
        history = self.workspace / "formal-history"
        _publish_fixture(self.candidate, history, "v1.0")
        second = _publish_fixture(self.candidate, history, "v1.1")
        changed = json.loads(second.read_text(encoding="utf-8"))
        changed["source_file_sha256"] = {key: "0" * 64 for key in changed["source_file_sha256"]}
        changed["published_at"] = "2026-08-07T13:00:00+08:00"
        _write_json(second, changed)
        validate_published_catalog(ROOT, "v1.0", versions_root=history)
        with self.assertRaisesRegex(ValueError, "当前三份权威源"):
            validate_published_catalog(ROOT, "v1.1", versions_root=history)
        activate_catalog(ROOT, "v1.1", versions_root=history)
        self.assertEqual(load_current_catalog(ROOT, versions_root=history)["catalog_version"], "v1.1")
        activate_catalog(ROOT, "v1.0", versions_root=history)
        product = load_current_product_snapshot(ROOT, "2.1", "v1.0", versions_root=history)
        self.assertEqual(product["product"]["identity"]["product_id"], "2.1")

        package = self.workspace / "package"
        shutil.copytree(history / "v1.0" / "knowledger" / "products", package / "scripts" / "knowledger" / "products")
        shutil.copy2(history / "v1.0" / "knowledger" / "catalog-version.json", package / "scripts" / "knowledger" / "catalog-version.json")
        packaged = load_packaged_product_snapshot(package, "2.1", "v1.0")
        self.assertEqual(packaged["product"]["identity"]["product_id"], "2.1")

    def test_formal_chain_rejects_missing_snapshot_and_fake_ids(self) -> None:
        history = self.workspace / "broken-history"
        catalog_path = _publish_fixture(self.candidate, history)
        missing = history / "v1.0" / "knowledger" / "products" / "2.1" / SNAPSHOT_FILES["default_svg"]
        missing.unlink()
        with self.assertRaisesRegex(ValueError, "缺失或哈希"):
            validate_published_catalog(ROOT, "v1.0", versions_root=history)
        shutil.rmtree(history)
        catalog_path = _publish_fixture(self.candidate, history)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["products"] = {f"p{index}": "v1.0" for index in range(65)}
        catalog["ordered_product_version_map"] = dict(catalog["products"])
        _write_json(catalog_path, catalog)
        with self.assertRaisesRegex(ValueError, "真实产品ID"):
            validate_published_catalog(ROOT, "v1.0", versions_root=history, require_source_match=False)

    def test_current_pointer_fails_closed_and_loaded_catalog_is_immutable_snapshot(self) -> None:
        history = self.workspace / "current-gates"
        _publish_fixture(self.candidate, history, "v1.0")
        _publish_fixture(self.candidate, history, "v1.1")
        current = activate_catalog(ROOT, "v1.0", versions_root=history)
        loaded_v1 = load_current_catalog(ROOT, versions_root=history)
        activate_catalog(ROOT, "v1.1", versions_root=history)
        self.assertEqual(loaded_v1["catalog_version"], "v1.0")
        self.assertEqual(load_current_catalog(ROOT, versions_root=history)["catalog_version"], "v1.1")

        valid = json.loads(current.read_text(encoding="utf-8"))
        for changed in (
            {**valid, "pointer_type": "candidate"},
            {**valid, "release_id": "forbidden"},
            {**valid, "catalog_manifest_sha256": "0" * 64},
            {**valid, "catalog_version": "v9.9"},
        ):
            _write_json(current, changed)
            with self.assertRaises(ValueError):
                load_current_catalog(ROOT, versions_root=history)

    def test_formal_chain_rejects_rehashed_economic_conflict(self) -> None:
        history = self.workspace / "economic-conflict"
        catalog_path = _publish_fixture(self.candidate, history)
        product_dir = history / "v1.0" / "knowledger" / "products" / "2.1"
        payoff_path = product_dir / SNAPSHOT_FILES["default_json"]
        payoff = json.loads(payoff_path.read_text(encoding="utf-8"))
        payoff["terms"]["K"] = 999
        _write_json(payoff_path, payoff)
        manifest_path = product_dir / "product-version.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["snapshot_sha256"]["default_json_sha256"] = _hash(payoff_path)
        _write_json(manifest_path, manifest)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["product_manifest_sha256"]["2.1"] = _hash(manifest_path)
        _write_json(catalog_path, catalog)
        with self.assertRaisesRegex(ValueError, "Payoff JSON与OptionReg"):
            validate_published_catalog(ROOT, "v1.0", versions_root=history)

    def test_formal_chain_rejects_73_constraint_drift_after_rehash(self) -> None:
        history = self.workspace / "known-73-drift"
        catalog_path = _publish_fixture(self.candidate, history)
        product_dir = history / "v1.0" / "knowledger" / "products" / "7.3"
        payoff_path = product_dir / SNAPSHOT_FILES["default_json"]
        payoff = json.loads(payoff_path.read_text(encoding="utf-8"))
        payoff["terms"]["constraints"].remove("alpha > 0")
        _write_json(payoff_path, payoff)
        manifest_path = product_dir / "product-version.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["snapshot_sha256"]["default_json_sha256"] = _hash(payoff_path)
        _write_json(manifest_path, manifest)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["product_manifest_sha256"]["7.3"] = _hash(manifest_path)
        _write_json(catalog_path, catalog)
        with self.assertRaisesRegex(ValueError, "Payoff JSON与OptionReg"):
            validate_published_catalog(ROOT, "v1.0", versions_root=history)


if __name__ == "__main__":
    unittest.main()
