from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from hashlib import sha256
import json
from threading import Event
import tempfile
import time
import unittest

from products.app.backend.agent_runtime.tool_dispatcher import ToolDispatcher
from products.app.backend.authorization.roles import Role
from products.app.backend.datafetcher_adapter import DataFetcherAdapter
from products.app.backend.identity.session_identity import SessionIdentity
from products.app.backend.stores import _LocalDocumentStore
from products.app.backend.stores.data_store import DataStore
from products.app.backend.stores.result_store import ResultStore
from products.app.backend.task_runtime.job_runner import JobRunner
from products.app.backend.task_runtime.task_service import TaskService
from products.app.backend.errors import ValidationError
from runtime.contracts.contract_types import canonical_json


IDENTITY = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {"run_id": f"run-{self.calls}", "ok": True, "status": "completed", "pricing": {"pv": 1.0}}


class _ReadStore:
    def read_bytes(self, _ref: object, *, tenant_id: str) -> bytes:
        return tenant_id.encode("utf-8")


class _BlockingGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.started = Event()
        self.release = Event()

    def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=3)
        return {"run_id": "shared-run", "ok": True, "status": "completed", "pricing": {"pv": 1.0}}


class RuntimeResourceBoundsTest(unittest.TestCase):
    def test_completed_unique_idempotency_keys_do_not_accumulate_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            task = tasks.create(IDENTITY, "锁回收")
            gateway = _Gateway()
            dispatcher = ToolDispatcher(gateway, JobRunner(), tasks, ResultStore(state), DataStore(state))
            for index in range(256):
                dispatcher.dispatch(
                    "pricer",
                    {
                        "action": "run", "task_id": task["task_id"],
                        "idempotency_key": f"unique-{index}", "pricing_config": {"method": "bs", "index": index},
                    },
                    IDENTITY,
                    module_context=object(),
                    request_id=f"request-{index}",
                )
            self.assertEqual(gateway.calls, 256)
            self.assertEqual(dispatcher._idempotency_locks, {})

    def test_lock_is_retained_for_a_waiter_then_reclaimed_after_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            task = tasks.create(IDENTITY, "等待者锁回收")
            gateway = _BlockingGateway()
            dispatcher = ToolDispatcher(gateway, JobRunner(), tasks, ResultStore(state), DataStore(state))
            request = {
                "action": "run", "task_id": task["task_id"], "idempotency_key": "shared-key",
                "pricing_config": {"method": "bs"},
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                owner = pool.submit(
                    dispatcher.dispatch, "pricer", dict(request), IDENTITY,
                    module_context=object(), request_id="owner",
                )
                self.assertTrue(gateway.started.wait(timeout=1))
                waiter = pool.submit(
                    dispatcher.dispatch, "pricer", dict(request), IDENTITY,
                    module_context=object(), request_id="waiter",
                )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and not any(entry.users == 2 for entry in dispatcher._idempotency_locks.values()):
                    time.sleep(0.001)
                self.assertTrue(any(entry.users == 2 for entry in dispatcher._idempotency_locks.values()))
                gateway.release.set()
                owner_result = owner.result(timeout=3)
                waiter_result = waiter.result(timeout=3)
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(waiter_result["module_run_ref"], owner_result["module_run_ref"])
            self.assertTrue(waiter_result["idempotent_replay"])
            self.assertEqual(dispatcher._idempotency_locks, {})

    def test_backtester_binding_exposes_only_the_read_capability(self) -> None:
        port = DataFetcherAdapter(lambda _identity: None, object(), _ReadStore()).bind_data_store(IDENTITY)
        self.assertTrue(callable(getattr(port, "read_bytes", None)))
        self.assertFalse(hasattr(port, "put_bytes"))
        self.assertFalse(hasattr(port, "resolve"))

    def test_app_rejects_a_coordinated_core_manifest_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            task = tasks.create(IDENTITY, "外部锚")
            results = ResultStore(state)
            ref = results.commit_module_run(IDENTITY, task["task_id"], "pricer", {
                "run_id": "anchored-run", "ok": True, "status": "completed",
                "analysis_case_id": "case-a", "candidate_id": "candidate-a",
                "resolved_contract": {
                    "contract_fingerprint": "c" * 64, "product_version": "product-a",
                    "identity": {
                        "product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY",
                    },
                },
                "pricing": {"pv": 1.0},
            })
            run_dir = results.resolve_owned_module_run(IDENTITY, ref)
            input_path = run_dir / "input_snapshot.json"
            input_path.write_text(canonical_json({"forged": True}) + "\n", encoding="utf-8")
            manifest_path = run_dir / "artifacts" / "artifact_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["file_hashes"]["input_snapshot.json"] = sha256(input_path.read_bytes()).hexdigest()
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            marker_path = run_dir / "commit_marker.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["artifact_manifest_hash"] = sha256(manifest_path.read_bytes()).hexdigest()
            marker_path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "外部RunRef锚点"):
                results.resolve_owned_module_run(IDENTITY, ref)
            self.assertEqual(results.list_owned_report_sources(IDENTITY)["sources"], [])

    def test_startup_never_auto_attests_an_unanchored_new_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            results = ResultStore(state)
            files = {
                "manifest.json": {"status": "succeeded"}, "input_snapshot.json": {},
                "resolved_contract.json": {}, "data_refs.json": {}, "limitations.json": {},
                "result.json": {"value": 1},
            }
            ref = results._core.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
            )
            pending = {
                "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id, "run_id": ref.run_id,
                "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                "reference_version": "module-run-ref/v1.2", "anchor_state": "pending_commit",
                "created_by": "principal-a", "created_at": "2026-08-10T00:00:00+00:00", "result": {"value": 1},
            }
            state.update("result_recovery", lambda value: {**value, "pricer:run-a": pending})
            recovered = ResultStore(_LocalDocumentStore(Path(temporary)))
            self.assertNotIn("pricer:run-a", recovered._state.read("results"))
            self.assertEqual(
                recovered._state.read("result_recovery")["pricer:run-a"]["anchor_state"], "pending_commit",
            )

    def test_startup_reconciles_only_an_already_anchored_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            results = ResultStore(state)
            files = {
                "manifest.json": {"status": "succeeded"}, "input_snapshot.json": {},
                "resolved_contract.json": {}, "data_refs.json": {}, "limitations.json": {},
                "result.json": {"value": 1},
            }
            ref = results._core.commit_module_run(
                module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files,
            )
            anchored = {
                "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id, "run_id": ref.run_id,
                "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
                "reference_version": "module-run-ref/v1.2", "anchor_state": "anchored",
                "created_by": "principal-a", "created_at": "2026-08-10T00:00:00+00:00", "result": {"value": 1},
            }
            state.update("result_recovery", lambda value: {**value, "pricer:run-a": anchored})
            recovered = ResultStore(_LocalDocumentStore(Path(temporary)))
            self.assertEqual(
                recovered._state.read("results")["pricer:run-a"]["expected_artifact_manifest_hash"],
                ref.expected_artifact_manifest_hash,
            )
            self.assertEqual(recovered._state.read("result_recovery"), {})

    def test_post_commit_index_failure_is_uncertain_and_never_auto_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            task = tasks.create(IDENTITY, "提交后失败")
            gateway = _Gateway()
            dispatcher = ToolDispatcher(gateway, JobRunner(), tasks, ResultStore(state), DataStore(state))

            def fail_index(*_args: object, **_kwargs: object) -> None:
                raise OSError("index unavailable")

            tasks.append_run_ref = fail_index  # type: ignore[method-assign]
            request = {
                "action": "run", "task_id": task["task_id"], "idempotency_key": "post-commit",
                "pricing_config": {"method": "bs"},
            }
            with self.assertRaisesRegex(ValidationError, "已禁止自动重算"):
                dispatcher.dispatch(
                    "pricer", dict(request), IDENTITY, module_context=object(), request_id="first",
                )
            with self.assertRaisesRegex(ValidationError, "人工核验"):
                dispatcher.dispatch(
                    "pricer", dict(request), IDENTITY, module_context=object(), request_id="retry",
                )
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(dispatcher._idempotency_locks, {})


if __name__ == "__main__":
    unittest.main()
