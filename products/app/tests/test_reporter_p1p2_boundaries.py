"""Regression gates for the Reporter P1/P2 independent review findings.

These tests deliberately exercise the App boundary after Reporter has rendered
into its private staging directory.  App must reject a self-consistent looking
directory whenever its candidate set or frozen-fact hashes no longer match the
controlled selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
for source in (APP_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.authorization.roles import Role
from backend.errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from backend.identity.session_identity import SessionIdentity
from backend import reporter_adapter
from backend.reporter_adapter import ReporterAdapter
from backend.stores import _LocalDocumentStore
from backend.stores.result_store import ResultStore
from backend.task_runtime.task_service import TaskService
from modules.reporter import service as reporter_service
from modules.reporter.models import ReporterError, stable_hash


def _contract(fingerprint: str, product_id: str) -> dict[str, object]:
    return {
        "identity": {
            "product_id": product_id,
            "name_zh": f"测试结构{product_id}",
            "underlyings": ["000905.SH"],
            "currency": "CNY",
        },
        "terms": {"S0": 100.0, "K": 100.0, "T": 1.0},
        "term_sources": {"S0": "market", "K": "contract", "T": "contract"},
        "paths": [{"condition": "True", "cases": [{"domain": "S_T>=0", "pnl": "0"}]}],
        "product_version": "product-v1",
        "resolved_schedules": {"observation_dates": ["2026-08-01"]},
        "contract_fingerprint": fingerprint,
        "analysis_basis_id": "basis-a",
        "price_convention": {"spot": "close", "adjustment": "forward"},
    }


def _pricing_result(run_id: str, fingerprint: str, product_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "ok": True,
        "status": "completed",
        "analysis_case_id": "case-a",
        "resolved_contract": _contract(fingerprint, product_id),
        "pricing": {"method": "test", "pv": 1.0, "currency": "CNY", "greeks": {"Delta": 0.1}},
    }


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


class ReporterP1P2Boundaries(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = _LocalDocumentStore(Path(self.temporary.name))
        self.results = ResultStore(self.state)
        self.tasks = TaskService(self.state)
        self.identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")
        self.other_principal = SessionIdentity("principal-b", "tenant-a", Role.ADMIN, "session-b")
        self.task = self.tasks.create(self.identity, "Reporter P1/P2")
        self.results.commit_module_run(
            self.identity,
            self.task["task_id"],
            "pricer",
            _pricing_result("pricing-a", "a" * 64, "2.1"),
        )
        self.results.commit_module_run(
            self.identity,
            self.task["task_id"],
            "pricer",
            _pricing_result("pricing-b", "b" * 64, "2.2"),
        )
        self.source = self.results.list_owned_report_sources(
            self.identity, task_id=self.task["task_id"]
        )["sources"][0]
        self.candidate_ids = [item["candidate_id"] for item in self.source["candidates"]]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _selection(self, mode: str, run_id: str) -> dict[str, object]:
        ids = self.candidate_ids[:1] if mode == "single" else self.candidate_ids
        return {
            "source_id": self.source["source_id"],
            "candidate_ids": ids,
            "selected_modules": ["pricing"],
            "delivery_mode": mode,
            "output_type": "report",
            "format": "html",
            "html_report_layout": "continuous",
            "audience": self.identity.audience,
            "report_run_id": run_id,
            "metadata": {},
        }

    def _dispatch_with_tamper(self, selection: dict[str, object], tamper) -> None:
        original = reporter_adapter.reporter_call_tool

        def wrapped(request, **kwargs):
            response = original(request, **kwargs)
            output_root = Path(kwargs["output_root"])
            report_root = output_root / self.task["task_id"] / str(selection["report_run_id"])
            tamper(report_root)
            return response

        with mock.patch.object(reporter_adapter, "reporter_call_tool", side_effect=wrapped):
            with self.assertRaises((ValidationError, UnavailableCapabilityError)):
                ReporterAdapter(self.results).dispatch(
                    {"action": "run", "task_id": self.task["task_id"], "selection": selection},
                    self.identity,
                )

    def test_app_rejects_missing_candidate_child_for_batch_and_combined(self) -> None:
        def omit_child(root: Path) -> None:
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["children"] = list(manifest["children"])[1:]
            _write(manifest_path, manifest)

        for mode in ("batch", "combined"):
            with self.subTest(mode=mode):
                self._dispatch_with_tamper(self._selection(mode, f"missing-{mode}"), omit_child)

    def test_app_rejects_duplicate_and_extra_candidate_children(self) -> None:
        def duplicate_child(root: Path) -> None:
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["children"] = [*manifest["children"], dict(manifest["children"][0])]
            _write(manifest_path, manifest)

        self._dispatch_with_tamper(self._selection("batch", "duplicate-batch"), duplicate_child)

        def extra_child(root: Path) -> None:
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            source = root / str(manifest["children"][0]["directory"])
            destination = root / "candidate_extra-candidate"
            shutil.copytree(source, destination)
            manifest["children"].append({
                **dict(manifest["children"][0]),
                "candidate_id": "extra-candidate",
                "directory": destination.name,
            })
            _write(manifest_path, manifest)

        self._dispatch_with_tamper(self._selection("batch", "extra-batch"), extra_child)

    def test_single_and_comparison_reject_any_candidate_child(self) -> None:
        def inject_root_as_child(root: Path) -> None:
            destination = root / "candidate_extra-candidate"
            destination.mkdir()
            for name in (
                "report-request.json",
                "report-unit.json",
                "designer-input.json",
                "design-brief.json",
                "designer-artifact-manifest.json",
                "report.html",
                "run_manifest.json",
            ):
                shutil.copyfile(root / name, destination / name)
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["children"] = [{
                "candidate_id": "extra-candidate",
                "directory": destination.name,
            }]
            _write(manifest_path, manifest)

        for mode in ("single", "comparison"):
            with self.subTest(mode=mode):
                self._dispatch_with_tamper(self._selection(mode, f"extra-{mode}"), inject_root_as_child)

    def test_app_rechecks_every_frozen_document_hash_before_commit(self) -> None:
        mutations = {
            "request": ("report-request.json", lambda value: value.update({"metadata": {"title": "tampered"}})),
            "report-unit": ("report-unit.json", lambda value: value.update({"evidence_status": "tampered"})),
            "designer-input": ("designer-input.json", lambda value: value["meta"].update({"title": "tampered"})),
            "design-brief": ("design-brief.json", lambda value: value.update({"audience": "tampered"})),
        }
        for index, (label, (filename, mutate)) in enumerate(mutations.items()):
            with self.subTest(document=label):
                def tamper(root: Path, filename=filename, mutate=mutate) -> None:
                    path = root / filename
                    value = _read(path)
                    mutate(value)
                    _write(path, value)

                self._dispatch_with_tamper(self._selection("single", f"tamper-{index}"), tamper)

    def test_app_rechecks_designer_receipt_against_frozen_designer_input(self) -> None:
        def tamper(root: Path) -> None:
            receipt_path = root / "designer-artifact-manifest.json"
            receipt = _read(receipt_path)
            receipt["content_sha256"] = "0" * 64
            _write(receipt_path, receipt)
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["designer_artifact_manifest"]["file_hash"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            manifest["rendered"]["designer"]["artifact_manifest"] = receipt
            _write(manifest_path, manifest)

        self._dispatch_with_tamper(self._selection("single", "tamper-receipt"), tamper)

    def test_app_rejects_self_consistent_report_unit_for_a_different_candidate(self) -> None:
        """A Reporter manifest cannot authorize a candidate absent from selection."""

        def tamper(root: Path) -> None:
            unit_path = root / "report-unit.json"
            unit = _read(unit_path)
            unit["subject"]["candidate_id"] = "candidate-not-selected"
            unit["candidate"]["candidate_id"] = "candidate-not-selected"
            unit.pop("semantic_fact_hash", None)
            unit["semantic_fact_hash"] = stable_hash(unit)
            _write(unit_path, unit)
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["report_unit"]["semantic_fact_hash"] = unit["semantic_fact_hash"]
            manifest["report_unit"]["file_hash"] = hashlib.sha256(unit_path.read_bytes()).hexdigest()
            _write(manifest_path, manifest)

        self._dispatch_with_tamper(self._selection("single", "wrong-unit-candidate"), tamper)

    def test_app_rejects_comparison_bundle_with_an_omitted_candidate_unit(self) -> None:
        def tamper(root: Path) -> None:
            unit_path = root / "report-unit.json"
            unit = _read(unit_path)
            unit["report_units"] = list(unit["report_units"])[1:]
            unit.pop("semantic_fact_hash", None)
            unit["semantic_fact_hash"] = stable_hash(unit)
            _write(unit_path, unit)
            manifest_path = root / "run_manifest.json"
            manifest = _read(manifest_path)
            manifest["report_unit"]["semantic_fact_hash"] = unit["semantic_fact_hash"]
            manifest["report_unit"]["file_hash"] = hashlib.sha256(unit_path.read_bytes()).hexdigest()
            _write(manifest_path, manifest)

        self._dispatch_with_tamper(self._selection("comparison", "comparison-omitted-unit"), tamper)

    def test_manifest_is_not_advertised_unless_committed_as_an_app_artifact(self) -> None:
        selection = self._selection("single", "manifest-access")
        reply = ReporterAdapter(self.results).dispatch(
            {"action": "run", "task_id": self.task["task_id"], "selection": selection}, self.identity
        )
        record = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        advertised = reply["output"].get("manifest")
        if advertised:
            self.assertIn(advertised, {item["name"] for item in record["artifact_manifest"]})

    def test_cross_task_cross_candidate_and_same_tenant_other_principal_are_rejected(self) -> None:
        other_task = self.tasks.create(self.identity, "Other task")
        with self.assertRaises(ValidationError):
            ReporterAdapter(self.results).dispatch(
                {
                    "action": "run",
                    "task_id": other_task["task_id"],
                    "selection": self._selection("single", "cross-task"),
                },
                self.identity,
            )

        invalid_candidate = self._selection("single", "cross-candidate")
        invalid_candidate["candidate_ids"] = ["candidate-not-in-source"]
        with self.assertRaises((ValidationError, UnavailableCapabilityError)):
            ReporterAdapter(self.results).dispatch(
                {"action": "run", "task_id": self.task["task_id"], "selection": invalid_candidate},
                self.identity,
            )

        self.assertEqual(
            ReporterAdapter(self.results).dispatch({"action": "list_report_sources"}, self.other_principal)["sources"],
            [],
        )
        good = ReporterAdapter(self.results).dispatch(
            {"action": "run", "task_id": self.task["task_id"], "selection": self._selection("single", "owned-report")},
            self.identity,
        )
        with self.assertRaises(AuthorizationError):
            self.results.resolve_report_run(self.other_principal, good["report_run_ref"])

    def test_independent_http_host_is_disabled_without_explicit_development_opt_in(self) -> None:
        class SelectionPort:
            def list_report_sources(self, *, tenant_id, task_id=None, query=None):
                return {"tenant_id": tenant_id, "sources": []}

            def get_report_source(self, *, tenant_id, source_id):
                raise KeyError(source_id)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(reporter_service, "ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(ReporterError, "开发|发行|App"):
                reporter_service.run_host(port=0)
            with self.assertRaisesRegex(ReporterError, "回环|本机"):
                reporter_service.run_host(host="0.0.0.0", port=0, development_mode=True)
            with self.assertRaisesRegex(ReporterError, "显式注入|ResultStore"):
                reporter_service.run_host(port=0, development_mode=True)
            server.assert_not_called()
            reporter_service.run_host(
                port=0,
                development_mode=True,
                result_store=object(),
                designer_port=object(),
                selection_port=SelectionPort(),
                output_root=Path(self.temporary.name) / "dev-reports",
            )
            server.assert_called_once_with(("127.0.0.1", 0), reporter_service.Handler)
            server.return_value.serve_forever.assert_called_once_with()
            server.return_value.server_close.assert_called_once_with()

    def test_app_adapter_does_not_rebuild_recommendation_catalog_facts(self) -> None:
        source = (PROJECT_ROOT / "products" / "app" / "backend" / "reporter_adapter.py").read_text(encoding="utf-8")
        result_store = (PROJECT_ROOT / "products" / "app" / "backend" / "stores" / "result_store.py").read_text(encoding="utf-8")
        self.assertNotIn("def _selection_source_refs", source)
        self.assertRegex(source, r"from modules\.reporter\.[a-z_]+ import .*selection.*source.*refs")
        self.assertNotIn("def _report_source_refs", result_store)


if __name__ == "__main__":
    unittest.main()
