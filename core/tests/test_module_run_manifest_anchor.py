from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.adapters.local_store import LocalResultStore, StoreIntegrityError
from runtime.contracts.contract_types import canonical_json
from runtime.protocol.models import ModuleRunRef


FILES = {
    "manifest.json": {"status": "succeeded"},
    "input_snapshot.json": {"spot": 100},
    "resolved_contract.json": {"contract_fingerprint": "c" * 64},
    "data_refs.json": {"items": []},
    "limitations.json": {"items": []},
    "result.json": {"pv": 1.5},
}


class ModuleRunManifestAnchorTest(unittest.TestCase):
    def test_new_ref_anchors_the_committed_manifest_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=FILES,
            )
            run_dir = store.resolve_module_run(ref, tenant_id="tenant-a")
            manifest = (run_dir / "artifacts" / "artifact_manifest.json").read_bytes()
            self.assertEqual(ref.expected_artifact_manifest_hash, sha256(manifest).hexdigest())

    def test_external_anchor_rejects_coordinated_file_manifest_and_marker_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=FILES,
            )
            run_dir = store.resolve_module_run(ref, tenant_id="tenant-a")
            input_path = run_dir / "input_snapshot.json"
            input_path.write_text(canonical_json({"spot": 999}) + "\n", encoding="utf-8")
            manifest_path = run_dir / "artifacts" / "artifact_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["file_hashes"]["input_snapshot.json"] = sha256(input_path.read_bytes()).hexdigest()
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            marker_path = run_dir / "commit_marker.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["artifact_manifest_hash"] = sha256(manifest_path.read_bytes()).hexdigest()
            marker_path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(StoreIntegrityError, "外部RunRef锚点"):
                store.resolve_module_run(ref, tenant_id="tenant-a")

    def test_legacy_ref_requires_the_explicit_attestation_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            current = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=FILES,
            )
            legacy = {
                "module": current.module,
                "tenant_id": current.tenant_id,
                "task_id": current.task_id,
                "run_id": current.run_id,
                "expected_semantic_result_hash": current.expected_semantic_result_hash,
            }
            with self.assertRaises(TypeError):
                ModuleRunRef(**legacy)
            migrated = store.attest_legacy_module_run_ref(legacy, tenant_id="tenant-a")
            self.assertEqual(migrated.expected_artifact_manifest_hash, current.expected_artifact_manifest_hash)
            with self.assertRaises(PermissionError):
                store.attest_legacy_module_run_ref(legacy, tenant_id="tenant-b")


if __name__ == "__main__":
    unittest.main()
