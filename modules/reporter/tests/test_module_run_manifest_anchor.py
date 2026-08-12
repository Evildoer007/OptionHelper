from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.evidence_resolver import _artifact_manifest
from modules.reporter.models import ReporterError, module_run_ref
from runtime.adapters.local_store import LocalResultStore


class ReporterModuleRunManifestAnchorTest(unittest.TestCase):
    def test_reporter_rejects_an_unanchored_legacy_ref(self) -> None:
        value = {
            "module": "pricer", "tenant_id": "tenant-a", "task_id": "task-a", "run_id": "run-a",
            "expected_semantic_result_hash": "a" * 64,
        }
        with self.assertRaisesRegex(ReporterError, "expected_artifact_manifest_hash"):
            module_run_ref(value, "source", tenant_id="tenant-a", task_id="task-a")

    def test_reporter_compares_manifest_bytes_to_the_external_anchor(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"},
            "input_snapshot.json": {}, "resolved_contract.json": {}, "data_refs.json": {}, "limitations.json": {},
            "result.json": {"value": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
            )
            parsed = module_run_ref(asdict(ref), "source", tenant_id="tenant-a", task_id="task-a")
            run_dir = store.resolve_module_run(ref, tenant_id="tenant-a")
            with self.assertRaisesRegex(ReporterError, "外部锚点"):
                _artifact_manifest(
                    run_dir, {}, parsed.expected_semantic_result_hash,
                    "0" * 64, "pricer",
                )


if __name__ == "__main__":
    unittest.main()
