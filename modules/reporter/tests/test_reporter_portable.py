from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = Path(__file__).resolve().parent
for source in (
    PROJECT_ROOT / "core" / "src",
    PROJECT_ROOT / "modules" / "reporter" / "src",
    PROJECT_ROOT / "modules" / "designer" / "src",
    PROJECT_ROOT / "modules" / "reporter",
    TEST_ROOT,
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.designer.service import call_tool as designer_call_tool
from modules.reporter.artifact_validator import sha256_file
from modules.reporter.models import ReporterError, stable_hash
from modules.reporter.reporter_engine import build_report
from scripts.validate_report import validate as validate_report
from test_reporter_run_refs import CASE, DESIGNER_PORT, TASK, TENANT, candidate, commit_run, full_contract, report_request


def commit_chart_backtest(store: LocalResultStore, item: dict, resolved: dict):
    run_id = "backtest-portable"
    result = {
        "execution_fingerprint": "execution-backtest-portable",
        "backtest": {
            "sample_count": 2,
            "win_rate": 0.5,
            "charts": [{
                "id": "return-path",
                "title": "图1：历史收益",
                "type": "line",
                "x": ["T0", "T1"],
                "series": [{"name": "收益", "data": [0, 0.03]}],
                "x_axis_name": "日期",
                "y_axis_name": "收益率",
                "source_note": "本次已验证回测结果",
            }],
        },
    }
    semantic = stable_hash(result)
    manifest = {
        "module": "backtester",
        "tenant_id": TENANT,
        "task_id": TASK,
        "run_id": run_id,
        "analysis_case_id": CASE,
        "candidate_id": item["candidate_id"],
        "status": "succeeded",
        "contract_fingerprint": resolved["contract_fingerprint"],
        "product_version": resolved["product_version"],
        "execution_fingerprint": result["execution_fingerprint"],
        "semantic_result_hash": semantic,
        "resolved_contract": "resolved_contract.json",
        "limitations": "limitations.json",
        "result": "result.json",
    }
    return store.commit_module_run(
        module="backtester",
        tenant_id=TENANT,
        task_id=TASK,
        run_id=run_id,
        files={
            "manifest.json": manifest,
            "input_snapshot.json": {"resolved_contract": resolved},
            "resolved_contract.json": resolved,
            "data_refs.json": "[]",
            "limitations.json": "[]",
            "result.json": result,
        },
    )


class CorruptPortableAssetPort:
    def __init__(self, mode: str):
        self.mode = mode

    def call_tool(self, request: dict) -> dict:
        response = deepcopy(dict(designer_call_tool(request)))
        asset = response["artifact"]["portable_assets"][0]
        if self.mode in {"path", "collision"}:
            asset["path"] = "../echarts.min.js" if self.mode == "path" else "report.html"
            response["artifact"]["artifact_manifest"]["portable_assets"][0]["path"] = asset["path"]
            response["artifact"]["artifact_manifest"]["assets"][0]["path"] = asset["path"]
        elif self.mode == "content":
            asset["content_base64"] = base64.b64encode(b"tampered").decode("ascii")
        elif self.mode == "media_type":
            asset["media_type"] = "text/css"
        elif self.mode == "duplicate":
            response["artifact"]["portable_assets"].append(deepcopy(asset))
        elif self.mode == "manifest":
            response["artifact"]["artifact_manifest"]["portable_assets"] = []
        return response


class ReporterPortableTest(unittest.TestCase):
    def _chart_request(self, store: LocalResultStore, *, report_run_id: str) -> tuple[dict, dict]:
        resolved = full_contract()
        item = candidate(contract=resolved)
        backtest = commit_chart_backtest(store, item, resolved)
        refs = {item["candidate_id"]: {"backtest": asdict(backtest)}}
        return item, report_request(
            [item], refs, report_run_id=report_run_id,
            selected=["backtest"], layout="continuous",
        )

    def test_real_chart_report_materializes_verified_portable_echarts_without_html_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            _, request = self._chart_request(store, report_run_id="report-portable")
            outcome = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_dir = Path(outcome["directory"])
            manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            html = (report_dir / "report.html").read_text(encoding="utf-8")
            portable = manifest["rendered"]["portable_assets"]

            self.assertIn('src="assets/echarts.min.js"', html)
            self.assertIn("图1：历史收益", html)
            self.assertEqual(manifest["rendered"]["delivery_mode"], "portable_html")
            self.assertEqual(len(portable), 1)
            asset_path = report_dir / portable[0]["path"]
            self.assertTrue(asset_path.is_file())
            self.assertEqual(sha256_file(asset_path), portable[0]["content_hash"])
            self.assertEqual(sha256(html.encode("utf-8")).hexdigest(), manifest["rendered"]["content_hash"])
            self.assertEqual(manifest["rendered"]["content_hash"], manifest["rendered"]["designer"]["source_artifact_hash"])
            validate_report(report_dir)

    def test_invalid_portable_asset_bundle_is_rejected_before_report_commit(self) -> None:
        for mode in ("content", "path", "collision", "media_type", "duplicate", "manifest"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = LocalResultStore(root / "store")
                _, request = self._chart_request(store, report_run_id=f"report-portable-bad-{mode}")
                with self.assertRaises(ReporterError):
                    build_report(
                        request,
                        result_store=store,
                        designer_port=CorruptPortableAssetPort(mode),
                        output_root=root / "reports",
                    )
                self.assertFalse((root / "reports" / TASK / request["report_run_id"]).exists())

    def test_card_renders_pricing_metric_once_from_module_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract()
            item = candidate(contract=resolved)
            pricing = commit_run(store, "pricer", "pricing-card-once", item, resolved)
            refs = {item["candidate_id"]: {"pricing": asdict(pricing)}}
            outcome = build_report(
                report_request(
                    [item], refs, report_run_id="report-card-once",
                    output_type="card", layout=None, selected=["pricing"],
                ),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )
            html = (Path(outcome["directory"]) / "report.html").read_text(encoding="utf-8")
            self.assertEqual(html.count("12.34"), 1)


if __name__ == "__main__":
    unittest.main()
