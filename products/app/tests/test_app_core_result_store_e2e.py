"""No-socket end-to-end coverage for App-owned Core ResultStore integration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.authorization.roles import Role
from backend.errors import AuthorizationError, ValidationError
from backend.identity.session_identity import SessionIdentity
from backend.reporter_adapter import ReporterAdapter
from backend.agent_runtime.tool_dispatcher import ToolDispatcher
from backend.stores import _LocalDocumentStore
from backend.stores.data_store import DataStore
from backend.stores.result_store import ResultStore
from backend.task_runtime.job_runner import JobRunner
from backend.task_runtime.task_service import TaskService
from backend.tool_gateway import _reject_path_payload


def _contract() -> dict[str, object]:
    return {
        "identity": {"product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY"},
        "terms": {"S0": 100.0, "K": 100.0, "T": 1.0, "Pi_0": 5.0},
        "term_sources": {"S0": "market", "K": "default", "T": "default", "Pi_0": "default"},
        "paths": [{"condition": "True", "cases": [{"domain": "S_T<=K", "pnl": "-Pi_0"}, {"domain": "S_T>K", "pnl": "S_T-K-Pi_0"}]}],
        "product_version": "product-v1", "resolved_schedules": {"observation_dates": ["2026-08-01"]},
        "contract_fingerprint": "a" * 64, "analysis_basis_id": "basis-v1",
        "price_convention": {"spot": "close", "adjustment": "forward"},
    }


def _result(module: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": f"run-{module}", "ok": True, "status": "completed", "analysis_case_id": "case-a",
        "resolved_contract": _contract(),
    }
    if module == "payoffer":
        payload["formula"] = "max(S_T-K,0)-Pi_0"
    elif module == "pricer":
        payload["pricing"] = {"method": "Black-Scholes", "pv": 12.34, "currency": "CNY", "greeks": {"Delta": 0.51}}
    else:
        payload["backtest"] = {
            "sample_count": 20, "win_rate": 0.6, "average_return": 0.04, "max_loss": -0.1,
            "charts": [{
                "id": "return-path", "title": "图1：历史收益", "type": "line",
                "x": ["T0", "T1"], "series": [{"name": "收益", "data": [0, 0.04]}],
                "x_axis_name": "日期", "y_axis_name": "收益率", "source_note": "本次已验证回测结果",
            }],
        }
    return payload


class AppCoreResultStoreE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = _LocalDocumentStore(Path(self.temporary.name))
        self.results = ResultStore(self.state)
        self.tasks = TaskService(self.state)
        self.identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")
        self.task = self.tasks.create(self.identity, "Core ReportRun")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_three_module_artifacts_feed_real_reporter_html_and_remain_tenant_scoped(self) -> None:
        refs = {module: self.results.commit_module_run(self.identity, self.task["task_id"], module, _result(module)) for module in ("payoffer", "pricer", "backtester")}
        for reference in refs.values():
            run_dir = self.results.resolve_owned_module_run(self.identity, reference)
            self.assertTrue((run_dir / "manifest.json").is_file())
            self.assertTrue((run_dir / "artifacts" / "artifact_manifest.json").is_file())
            self.assertTrue((run_dir / "commit_marker.json").is_file())

        source = self.results.list_owned_report_sources(self.identity, task_id=self.task["task_id"])["sources"][0]
        candidate = source["candidates"][0]
        reply = ReporterAdapter(self.results).dispatch({"action": "run", "selection": {
            "source_id": source["source_id"], "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": ["payoff", "pricing", "backtest"], "delivery_mode": "single",
            "output_type": "report", "format": "html", "html_report_layout": "with_toc",
            "audience": self.identity.audience, "report_run_id": "report-three-module", "metadata": {},
        }}, self.identity)
        self.assertEqual(reply["status"], "completed")
        self.assertIn("/api/reports/", reply["preview_url"])
        report = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        names = {item["name"] for item in report["artifact_manifest"]}
        self.assertIn("assets/echarts.min.js", names)
        name = report["artifact_manifest"][0]["name"]
        html, content_type = self.results.read_report_artifact(self.identity, reply["report_run_ref"]["report_run_id"], name)
        self.assertIn(b"<html", html.lower())
        self.assertIn(b'src="assets/echarts.min.js"', html)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        script, script_type = self.results.read_report_artifact(
            self.identity, reply["report_run_ref"]["report_run_id"], "assets/echarts.min.js",
        )
        self.assertGreater(len(script), 100_000)
        self.assertEqual(script_type, "application/javascript")

        other = SessionIdentity("principal-b", "tenant-b", Role.ADMIN, "session-b")
        with self.assertRaises((AuthorizationError, ValidationError)):
            self.results.resolve_owned_module_run(other, refs["payoffer"])
        with self.assertRaises(AuthorizationError):
            self.results.read_report_artifact(other, reply["report_run_ref"]["report_run_id"], name)

    def test_adapter_accepts_page_selection_and_persists_reporter_audit(self) -> None:
        self.results.commit_module_run(self.identity, self.task["task_id"], "pricer", _result("pricer"))
        source = self.results.list_owned_report_sources(self.identity, task_id=self.task["task_id"])["sources"][0]
        candidate = source["candidates"][0]
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": ["pricing"],
            "delivery_mode": "single",
            "output_type": "report",
            "format": "html",
            "html_report_layout": "continuous",
            "audience": self.identity.audience,
            "report_run_id": "report-selection",
            "metadata": {"title": "选择协议回归"},
        }
        reply = ReporterAdapter(self.results).dispatch({"action": "run", "selection": selection}, self.identity)
        self.assertEqual(reply["status"], "completed")
        self.assertIn("/api/reports/", reply["preview_url"])
        self.assertEqual(reply["output"]["report"], "report.html")
        record = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        self.assertIn("reporter_audit", record["report_request"])
        audit = record["report_request"]["reporter_audit"]
        self.assertIn("report_unit", audit["root"])
        self.assertIn("design_brief", audit["root"])
        self.assertIn("designer_artifact_manifest", audit["root"])

    def test_adapter_hides_source_when_core_commit_marker_is_no_longer_valid(self) -> None:
        reference = self.results.commit_module_run(self.identity, self.task["task_id"], "pricer", _result("pricer"))
        run_dir = self.results.resolve_owned_module_run(self.identity, reference)
        marker = json.loads((run_dir / "commit_marker.json").read_text())
        marker["committed"] = False
        (run_dir / "commit_marker.json").write_text(json.dumps(marker))
        reply = ReporterAdapter(self.results).dispatch({"action": "list_report_sources"}, self.identity)
        self.assertEqual(reply["sources"], [])

    def test_batch_selection_persists_each_independent_rendered_report(self) -> None:
        first = _result("pricer")
        second = _result("pricer")
        second["run_id"] = "run-pricer-second"
        second["resolved_contract"] = {**_contract(), "contract_fingerprint": "b" * 64}
        self.results.commit_module_run(self.identity, self.task["task_id"], "pricer", first)
        self.results.commit_module_run(self.identity, self.task["task_id"], "pricer", second)
        source = self.results.list_owned_report_sources(self.identity, task_id=self.task["task_id"])["sources"][0]
        candidate_ids = [item["candidate_id"] for item in source["candidates"]]
        reply = ReporterAdapter(self.results).dispatch({"action": "run", "selection": {
            "source_id": source["source_id"], "candidate_ids": candidate_ids, "selected_modules": ["pricing"],
            "delivery_mode": "batch", "output_type": "report", "format": "html", "html_report_layout": "continuous",
            "audience": self.identity.audience, "report_run_id": "report-batch", "metadata": {},
        }}, self.identity)
        self.assertEqual(reply["status"], "completed")
        self.assertEqual(len(reply["output"]["children"]), 2)
        record = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        names = {item["name"] for item in record["artifact_manifest"]}
        self.assertIn("index.html", names)
        self.assertTrue(all(item["report"] in names for item in reply["output"]["children"]))
        self.assertEqual(len(record["report_request"]["reporter_audit"]["children"]), 2)

    def test_failed_compute_run_is_formal_but_not_a_report_source_and_paths_are_rejected(self) -> None:
        failed = self.results.commit_module_run(self.identity, self.task["task_id"], "payoffer", {"run_id": "failed:run", "ok": False, "status": "failed", "message": "market data missing"})
        run_dir = self.results.resolve_owned_module_run(self.identity, failed)
        self.assertTrue((run_dir / "error.json").is_file())
        self.assertEqual(self.results.list_owned_report_sources(self.identity)["sources"], [])
        with self.assertRaises(ValidationError):
            self.results.read_report_artifact(self.identity, "../escape", "report.html")

    def test_hash_drifted_core_run_is_not_listed_as_a_report_source(self) -> None:
        reference = self.results.commit_module_run(
            self.identity, self.task["task_id"], "payoffer", _result("payoffer"),
        )
        run_dir = self.results.resolve_owned_module_run(self.identity, reference)
        (run_dir / "result.json").write_text('{"tampered":true}', encoding="utf-8")

        self.assertEqual(
            self.results.list_owned_report_sources(self.identity, task_id=self.task["task_id"])["sources"],
            [],
        )

    def test_report_store_whitelists_nested_portable_artifacts(self) -> None:
        reference = self.results.commit_report_run(
            self.identity,
            self.task["task_id"],
            {"report_run_id": "portable-run", "task_id": self.task["task_id"]},
            [
                {"name": "report.html", "content_type": "text/html; charset=utf-8", "content": b'<script src="assets/echarts.min.js"></script>'},
                {"name": "assets/echarts.min.js", "content_type": "application/javascript", "content": b"window.echarts={};"},
            ],
        )
        content, content_type = self.results.read_report_artifact(
            self.identity, reference["report_run_id"], "assets/echarts.min.js",
        )
        self.assertEqual(content, b"window.echarts={};")
        self.assertEqual(content_type, "application/javascript")
        for name in ("../echarts.min.js", "/assets/echarts.min.js", "assets\\echarts.min.js"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.results.read_report_artifact(self.identity, reference["report_run_id"], name)
        with self.assertRaises(KeyError):
            self.results.read_report_artifact(self.identity, reference["report_run_id"], "assets/not-declared.js")

    def test_datafetcher_registers_only_data_asset_and_host_rejects_path_payloads(self) -> None:
        class Gateway:
            def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                return {"ok": True, "status": "completed", "data_asset_ref": {
                    "data_asset_id": "asset-a", "storage_ref": "asset-a", "media_type": "application/json",
                    "schema_id": "optionhelper.data-asset/v1", "asset_ids": ["000905.SH"], "normalized_fields": ["close"],
                    "coverage": {"start": "2026-01-01", "end": "2026-01-31"}, "row_count": 21,
                    "price_convention": {"adjustment": "forward"}, "content_hash": "a" * 64, "lineage": {"provider": "test"},
                }}

        dispatcher = ToolDispatcher(Gateway(), JobRunner(), self.tasks, self.results, DataStore(self.state))  # type: ignore[arg-type]
        reply = dispatcher.dispatch("datafetcher", {"task_id": self.task["task_id"], "action": "fetch"}, self.identity)
        self.assertIn("data_asset_ref", reply)
        self.assertEqual(self.state.read("results"), {})
        self.assertEqual(self.state.read("data_assets")["tenant-a:asset-a"]["tenant_id"], "tenant-a")
        with self.assertRaises(ValidationError):
            _reject_path_payload({"request": {"path": "../outside"}})


if __name__ == "__main__":
    unittest.main()
