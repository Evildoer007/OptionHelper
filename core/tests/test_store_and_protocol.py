from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
import json
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.adapters.local_store import LocalDataStore, LocalResultStore, StoreError, StoreIntegrityError
from runtime.contracts.contract_types import canonical_json, semantic_hash
from runtime.errors import ErrorCode, ErrorInfo, RuntimeProtocolError, error_info
from runtime.protocol.models import ModuleRun, SecretRef, require_run_transition
from runtime.ports.tool_gateway import ToolGatewayPort, require_tool_gateway_port
from runtime.protocol.tool_catalog import get_tool, tool_catalog


class StoreAndProtocolTest(unittest.TestCase):
    def test_data_store_rejects_traversal_cross_tenant_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalDataStore(Path(temporary) / "data")
            with self.assertRaises(StoreError):
                store.put_bytes(tenant_id="../bad", data_asset_id="asset", payload=b"x", media_type="text/csv", schema_id="prices")
            ref = store.put_bytes(tenant_id="tenant-a", data_asset_id="asset-1", payload=b"price\n100\n", media_type="text/csv", schema_id="prices")
            self.assertEqual(store.read_bytes(ref, tenant_id="tenant-a"), b"price\n100\n")
            with self.assertRaises(PermissionError):
                store.read_bytes(ref, tenant_id="tenant-b")
            with self.assertRaisesRegex(StoreError, "storage_ref"):
                store.read_bytes(replace(ref, lineage={"source": "forged"}), tenant_id="tenant-a")
            store.resolve(ref, tenant_id="tenant-a").write_bytes(b"changed")
            with self.assertRaisesRegex(StoreError, "哈希"):
                store.read_bytes(ref, tenant_id="tenant-a")

    def test_result_store_is_atomic_immutable_and_hash_verified(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"},
            "input_snapshot.json": {"x": 1},
            "resolved_contract.json": {"contract_fingerprint": "c"},
            "data_refs.json": {"items": []},
            "result.json": {"pv": 1.5},
            "limitations.json": {"items": []},
            "artifacts/chart.svg": "<svg/>",
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(module="pricer", tenant_id="tenant-a", task_id="task-1", run_id="run-1", files=files)
            run_dir = store.resolve_module_run(ref, tenant_id="tenant-a")
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertTrue((run_dir / "artifacts" / "artifact_manifest.json").is_file())
            committed_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(committed_manifest["semantic_result_hash"], semantic_hash(files["result.json"]))
            self.assertNotIn("semantic_result_hash", files["manifest.json"])
            with self.assertRaises(FileExistsError):
                store.commit_module_run(module="pricer", tenant_id="tenant-a", task_id="task-1", run_id="run-1", files=files)
            with self.assertRaises(PermissionError):
                store.resolve_module_run(ref, tenant_id="tenant-b")
            with self.assertRaisesRegex(StoreError, "语义"):
                store.resolve_module_run(replace(ref, expected_semantic_result_hash="0" * 64), tenant_id="tenant-a")
            manifest = run_dir / "artifacts" / "artifact_manifest.json"
            manifest.write_text('{"semantic_result_hash":"' + ref.expected_semantic_result_hash + '","file_hashes":{}}', encoding="utf-8")
            with self.assertRaisesRegex(StoreError, "外部RunRef锚点"):
                store.resolve_module_run(ref, tenant_id="tenant-a")

    def test_result_store_rejects_conflicting_caller_semantic_hash(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded", "semantic_result_hash": "0" * 64},
            "input_snapshot.json": {},
            "resolved_contract.json": {},
            "data_refs.json": {},
            "limitations.json": {},
            "result.json": {"value": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            with self.assertRaisesRegex(StoreError, "semantic_result_hash"):
                store.commit_module_run(
                    module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
                )

    def test_module_run_ref_selects_the_declared_module(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"},
            "input_snapshot.json": {"x": 1},
            "resolved_contract.json": {"contract_fingerprint": "c"},
            "data_refs.json": {"items": []},
            "result.json": {"value": 1},
            "limitations.json": {"items": []},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            payoffer = store.commit_module_run(
                module="payoffer", tenant_id="tenant-a", task_id="task-1", run_id="same-run", files=files,
            )
            pricer = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-1", run_id="same-run", files=files,
            )
            self.assertEqual(payoffer.module, "payoffer")
            self.assertEqual(pricer.module, "pricer")
            self.assertIn("output_payoff", store.resolve_module_run(payoffer, tenant_id="tenant-a").parts)
            self.assertIn("output_pricing", store.resolve_module_run(pricer, tenant_id="tenant-a").parts)

            schema = json.loads((CORE_SRC / "runtime" / "protocol" / "schemas" / "run-ref.schema.json").read_text(encoding="utf-8"))
            module_ref_schema = schema["oneOf"][1]
            self.assertIn("module", module_ref_schema["required"])
            self.assertIn("expected_artifact_manifest_hash", module_ref_schema["required"])
            self.assertEqual(module_ref_schema["properties"]["module"]["enum"], ["payoffer", "pricer", "backtester"])

    def test_result_store_recomputes_semantic_hash_after_coordinated_tamper(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"},
            "input_snapshot.json": {"x": 1},
            "resolved_contract.json": {"contract_fingerprint": "c"},
            "data_refs.json": {"items": []},
            "result.json": {"pv": 1.5},
            "limitations.json": {"items": []},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-1", run_id="run-1", files=files,
            )
            run_dir = store.resolve_module_run(ref, tenant_id="tenant-a")
            result_path = run_dir / "result.json"
            result_path.write_text(canonical_json({"pv": 999.0}) + "\n", encoding="utf-8")
            artifact_path = run_dir / "artifacts" / "artifact_manifest.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["file_hashes"]["result.json"] = sha256(result_path.read_bytes()).hexdigest()
            artifact_path.write_text(canonical_json(artifact) + "\n", encoding="utf-8")
            marker_path = run_dir / "commit_marker.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["artifact_manifest_hash"] = sha256(artifact_path.read_bytes()).hexdigest()
            marker_path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(StoreIntegrityError, "外部RunRef锚点") as raised:
                store.resolve_module_run(ref, tenant_id="tenant-a")
            self.assertEqual(error_info(raised.exception).error_code, ErrorCode.CORRUPT)

    def test_store_concurrent_publish_has_one_winner_and_stable_conflicts(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"},
            "input_snapshot.json": {},
            "resolved_contract.json": {},
            "data_refs.json": {},
            "limitations.json": {},
            "result.json": {"value": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            result_store = LocalResultStore(Path(temporary) / "result")
            barrier = Barrier(2)
            from runtime.adapters import local_store
            real_replace = local_store.os.replace

            def synchronized_replace(source: object, target: object) -> None:
                barrier.wait(timeout=5)
                real_replace(source, target)

            def commit() -> str:
                try:
                    result_store.commit_module_run(
                        module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
                    )
                    return "ok"
                except Exception as error:
                    return type(error).__name__

            with patch.object(local_store.os, "replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(lambda _: commit(), range(2)))
            self.assertEqual(sorted(outcomes), ["FileExistsError", "ok"])

    def test_result_store_rejects_conflicting_caller_manifest_identity(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded", "module": "backtester", "tenant_id": "tenant-a"},
            "input_snapshot.json": {},
            "resolved_contract.json": {},
            "data_refs.json": {},
            "limitations.json": {},
            "result.json": {"value": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            with self.assertRaisesRegex(StoreError, "manifest.*module|身份"):
                store.commit_module_run(
                    module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
                )

    def test_tool_catalog_declares_protocol_and_page_boundary(self) -> None:
        catalog = tool_catalog()
        self.assertEqual(len(catalog), 7)
        self.assertEqual(get_tool("datafetcher")["operation"], "fetch")
        self.assertEqual(get_tool("payoffer")["operation"], "run")
        self.assertEqual(get_tool("pricer")["operation"], "run")
        self.assertEqual(get_tool("recommender")["operations"], ["recommend", "recommend_fixed"])
        self.assertFalse(get_tool("designer")["has_page"])
        self.assertTrue(all(item["protocol_version"] == "v1.2" for item in catalog.values()))
        self.assertTrue(all(item["independent"] and item["input_schema"] and item["output_schema"] for item in catalog.values()))

    def test_error_shape_and_run_state_machine_are_fixed(self) -> None:
        error = ErrorInfo(ErrorCode.INVALID_REQUEST, "validation", "bad input", missing_inputs=("contract",))
        self.assertEqual(
            set(error.to_dict()),
            {"error_code", "stage", "message", "retryable", "missing_inputs", "upstream_refs"},
        )
        self.assertEqual(require_run_transition("created", "validating"), ("created", "validating"))
        with self.assertRaises(ValueError):
            require_run_transition("created", "succeeded")
        wrapped = RuntimeProtocolError(ErrorInfo(ErrorCode.UNAUTHORIZED, "gateway", "denied"))
        self.assertEqual(error_info(wrapped), wrapped.error)

    def test_secret_ref_is_one_core_owned_opaque_value_object(self) -> None:
        reference = SecretRef("keychain", "optionhelper/ifind", "v1")
        self.assertEqual(reference.redacted(), {"provider": "keychain", "key": "optionhelper/ifind", "version": "v1"})
        with self.assertRaises(ValueError):
            SecretRef("", "optionhelper/ifind")

    def test_failed_run_has_error_without_fake_result(self) -> None:
        files = {
            "manifest.json": {"status": "failed"},
            "input_snapshot.json": {"x": 1},
            "resolved_contract.json": {"contract_fingerprint": "c"},
            "data_refs.json": {"items": []},
            "limitations.json": {"items": []},
            "error.json": {"error_code": "data_unavailable"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(module="pricer", tenant_id="local", task_id="task-1", run_id="run-1", files=files)
            run_dir = store.resolve_module_run(ref, tenant_id="local")
            self.assertEqual(run_dir, (Path(temporary) / "result" / "output_pricing" / "task-1" / "run-1").resolve())
            self.assertFalse((run_dir / "result.json").exists())

    def test_module_run_rejects_contradictory_result_and_error_files(self) -> None:
        files = {
            "manifest.json": {"status": "failed"},
            "input_snapshot.json": {},
            "resolved_contract.json": {},
            "data_refs.json": {},
            "limitations.json": {},
            "result.json": {"value": 1},
            "error.json": {"error_code": "internal"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            with self.assertRaisesRegex(StoreError, "result.json|error.json|终态"):
                store.commit_module_run(
                    module="pricer", tenant_id="local", task_id="task-1", run_id="run-1", files=files,
                )

    def test_module_run_model_and_schema_share_the_success_contract(self) -> None:
        values = {
            "module": "pricer", "analysis_case_id": "case-1", "task_id": "task-1", "run_id": "run-1",
            "status": "succeeded", "contract_fingerprint": "a" * 64, "catalog_version": "v1.0",
            "execution_fingerprint": "exec-1", "input_snapshot": {}, "data_refs": (), "result": {},
            "candidate_id": "candidate-1",
        }
        self.assertEqual(ModuleRun(**values).catalog_version, "v1.0")
        with self.assertRaisesRegex(ValueError, "catalog_version"):
            ModuleRun(**{**values, "catalog_version": None})
        schema = json.loads((CORE_SRC / "runtime" / "protocol" / "schemas" / "module-run.schema.json").read_text(encoding="utf-8"))
        self.assertIn("catalog_version", schema["required"])
        success_properties = schema["allOf"][0]["then"]["properties"]
        self.assertEqual(set(success_properties), {"candidate_id", "catalog_version", "contract_fingerprint"})

    def test_tool_gateway_port_is_the_internal_module_boundary(self) -> None:
        class Gateway:
            def call(self, module: str, request: dict[str, object]) -> dict[str, object]:
                return {"module": module, "request": request}

        gateway = Gateway()
        self.assertIsInstance(gateway, ToolGatewayPort)
        self.assertIs(require_tool_gateway_port(gateway), gateway)
        with self.assertRaises(TypeError):
            require_tool_gateway_port(object())


if __name__ == "__main__":
    unittest.main()
