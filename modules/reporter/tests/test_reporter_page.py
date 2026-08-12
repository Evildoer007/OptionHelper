from __future__ import annotations

from dataclasses import asdict
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "reporter" / "src", PROJECT_ROOT / "modules" / "designer" / "src", PROJECT_ROOT / "modules" / "reporter"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.reporter.models import stable_hash
from modules.reporter.service import Handler
from modules.designer.service import call_tool as designer_call_tool


TENANT, TASK, CASE = "local", "task-page", "case-page"
CATALOG_HASH = "c" * 64


class PublicDesignerPort:
    def call_tool(self, request: dict) -> dict:
        return dict(designer_call_tool(request))


class UnavailablePdfDesignerPort:
    def call_tool(self, request: dict) -> dict:
        return {"ok": False, "module": "designer", "error": "missing_dependency", "message": "PDF运行组件不可用。"}


def contract(fingerprint: str = "contract-page") -> dict:
    return {
        "identity": {"product_id": "2.1", "name_zh": "看涨期权", "underlyings": ["000905.SH"], "currency": "CNY"},
        "terms": {"S0": 100.0, "T": 1.0, "K": 100.0}, "term_sources": {"S0": "market", "T": "default", "K": "default"},
        "paths": [{"condition": "True", "cases": [{"domain": "S_T>K", "pnl": "S_T-K"}]}],
        "product_version": "product-page", "resolved_schedules": {"observation_dates": []},
        "contract_fingerprint": fingerprint, "analysis_basis_id": "basis-page", "price_convention": {"spot": "close"},
    }


def candidate(value: dict, candidate_id: str = "candidate-page") -> dict:
    identity = value["identity"]
    return {
        "candidate_id": candidate_id, "product_id": identity["product_id"], "product_name": identity["name_zh"],
        "product_version": value["product_version"], "contract_fingerprint": value["contract_fingerprint"],
        "analysis_basis_id": value["analysis_basis_id"], "underlyings": identity["underlyings"], "currency": identity["currency"],
        "price_convention": value["price_convention"], "rank": 1, "reason": "测试研究结论。", "suitable_for": ["测试"],
        "not_suitable_for": ["测试"], "main_risks": ["测试风险"], "library_status": "ready", "key_terms": [], "evidence_refs": ["test"],
    }


def commit(store: LocalResultStore, module: str, run_id: str, value: dict, candidate_id: str = "candidate-page"):
    result = {"execution_fingerprint": f"execution-{run_id}"}
    if module == "payoffer":
        result.update({"formula": "max(S_T-K,0)", "path_panels": []})
    elif module == "pricer":
        result["pricing"] = {"method": "Black-Scholes", "pv": 1.2, "currency": "CNY"}
    else:
        result["backtest"] = {
            "sample_count": 1,
            "win_rate": 1.0,
            "charts": [{
                "id": "page-backtest",
                "title": "历史收益",
                "type": "line",
                "x": ["T0", "T1"],
                "series": [{"name": "收益", "data": [0, 0.02]}],
                "x_axis_name": "日期",
                "y_axis_name": "收益率",
                "source_note": "页面回归",
            }],
        }
    semantic = stable_hash(result)
    files = {
        "manifest.json": {"module": module, "tenant_id": TENANT, "task_id": TASK, "run_id": run_id, "analysis_case_id": CASE, "candidate_id": candidate_id, "status": "succeeded", "contract_fingerprint": value["contract_fingerprint"], "product_version": value["product_version"], "execution_fingerprint": result["execution_fingerprint"], "semantic_result_hash": semantic, "input_snapshot": "input_snapshot.json", "resolved_contract": "resolved_contract.json", "data_refs": "data_refs.json", "limitations": "limitations.json", "result": "result.json"},
        "input_snapshot.json": {"resolved_contract": value}, "resolved_contract.json": value, "data_refs.json": "[]", "limitations.json": "[]", "result.json": result,
    }
    return store.commit_module_run(module=module, tenant_id=TENANT, task_id=TASK, run_id=run_id, files=files)


class SelectionCatalog:
    def __init__(self, source: dict): self.source = source
    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> dict:
        sources = [self.source] if tenant_id == TENANT and (not task_id or task_id == TASK) else []
        return {"tenant_id": tenant_id, "sources": [{key: value for key, value in item.items() if key != "source_refs"} for item in sources]}
    def get_report_source(self, *, tenant_id: str, source_id: str) -> dict:
        if tenant_id != TENANT or source_id != self.source["source_id"]: raise ValueError("not found")
        return self.source


class ReporterPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LocalResultStore(self.root / "store")
        self.contract = contract()
        self.candidate = candidate(self.contract)
        payoff = commit(self.store, "payoffer", "pay-page", self.contract)
        pricing = commit(self.store, "pricer", "pricing-page", self.contract)
        backtest = commit(self.store, "backtester", "backtest-page", self.contract)
        recommendation = {"schema": "optionhelper.recommendation-set/v2", "tenant_id": TENANT, "task_id": TASK, "analysis_case_id": CASE, "run_id": "recommendation-page", "catalog_version": "catalog-page", "catalog_content_hash": CATALOG_HASH, "candidates": [self.candidate]}
        self.source = {
            "source_id": "source-page", "label": "看涨期权研究", "tenant_id": TENANT, "task_id": TASK, "analysis_case_id": CASE, "catalog_version": "catalog-page",
            "source_refs": {"product_version_refs": {"candidate-page": {"product_id": "2.1", "product_version": "product-page", "content_hash": "1" * 64}}, "catalog_version_ref": {"catalog_version": "catalog-page", "content_hash": CATALOG_HASH}, "evidence_refs": {"recommendation_set": {"source_id": "recommender/page", "run_id": "recommendation-page", "payload": recommendation, "expected_semantic_result_hash": stable_hash(recommendation)}}},
            "candidates": [{**self.candidate, "module_run_refs": {"payoff": asdict(payoff), "pricing": asdict(pricing), "backtest": asdict(backtest)}}],
        }
        Handler.result_store, Handler.designer_port, Handler.selection_port = self.store, PublicDesignerPort(), SelectionCatalog(self.source)
        Handler.output_root, Handler.tenant_id = self.root / "reports", TENANT
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, path: str, body: dict | None = None):
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"} if body is not None else {}, method="POST" if body is not None else "GET")
        try:
            with urlopen(request) as response: return response.status, response.read(), dict(response.headers)
        except HTTPError as error: return error.code, error.read(), dict(error.headers)

    def selection(self, **overrides) -> dict:
        return {"source_id": "source-page", "candidate_ids": ["candidate-page"], "selected_modules": ["recommender", "payoff", "pricing", "backtest"], "delivery_mode": "single", "output_type": "report", "format": "html", "html_report_layout": "continuous", "audience": "professional", "report_run_id": "report-page", **overrides}

    def test_static_page_and_controlled_source_listing(self) -> None:
        status, body, _ = self.request("/reporter.html")
        html = body.decode()
        self.assertEqual(status, 200); self.assertIn("选择报告来源", html); self.assertIn("生成并保存", html); self.assertIn("reporter.js", html)
        self.assertIn('../../icons/optionhelper-logo.svg', html); self.assertIn('../../../assets/icons/optionhelper-logo.svg', html)
        self.assertIn('../../icons/optionhelper-app-icon-tile-light.svg', html); self.assertIn('../../../assets/icons/optionhelper-app-icon-tile-light.svg', html)
        for asset in ("/reporter.css", "/reporter.js", "/icons/optionhelper-logo.svg", "/icons/optionhelper-app-icon-tile-light.svg"):
            asset_status, asset_body, _ = self.request(asset)
            self.assertEqual(asset_status, 200); self.assertTrue(asset_body)
        status, body, _ = self.request("/api/report-sources?task_id=task-page")
        catalog = json.loads(body)
        self.assertEqual(status, 200); self.assertEqual(catalog["sources"][0]["candidates"][0]["candidate_id"], "candidate-page")
        self.assertNotIn("result_dir", json.dumps(catalog)); self.assertNotIn(str(self.root), json.dumps(catalog))
        status, body, _ = self.request("/api/run", {"schema": "optionhelper.report-request/v2", "tenant_id": "other", "result_dir": "/tmp/no"})
        self.assertEqual(status, 400); self.assertIn("受控selection", json.loads(body)["message"])

    def test_release_layout_keeps_reporter_logo_relative_path(self) -> None:
        source_map = json.loads((PROJECT_ROOT / "packaging" / "skill" / "package-source-map.json").read_text())
        mapped = {(item["source"], item["target"]) for item in source_map["trees"]}
        self.assertIn(("modules/reporter/page", "assets/pages/reporter"), mapped)
        self.assertIn(("assets/icons", "assets/icons"), mapped)
        self.assertTrue((PROJECT_ROOT / "assets" / "icons" / "optionhelper-logo.svg").is_file())
        release_logo = (Path("assets/pages/reporter") / "../../icons/optionhelper-logo.svg").resolve(strict=False)
        self.assertEqual(release_logo, (Path("assets/icons/optionhelper-logo.svg")).resolve(strict=False))

    def test_selection_generates_report_and_manifest_whitelisted_preview(self) -> None:
        status, body, _ = self.request("/api/run", {"selection": self.selection()})
        result = json.loads(body)
        self.assertEqual(status, 200); self.assertTrue(result["ok"]); self.assertIn("report-artifact", result["preview_url"])
        self.assertNotIn("directory", json.dumps(result["output"]))
        status, report, headers = self.request(result["preview_url"])
        self.assertEqual(status, 200); self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("核心结论".encode(), report)
        self.assertIn(b'class="report-toc"', report)
        asset_url = result["preview_url"].rsplit("/", 1)[0] + "/assets/echarts.min.js"
        status, asset, headers = self.request(asset_url)
        self.assertEqual(status, 200); self.assertIn("javascript", headers["Content-Type"]); self.assertTrue(asset)
        status, _, _ = self.request("/api/report-artifact?task_id=task-page&report_run_id=report-page&name=../../manifest.json")
        self.assertEqual(status, 404)

    def test_missing_selected_module_is_partial_and_pdf_unavailable_is_honest(self) -> None:
        self.source["candidates"][0]["module_run_refs"].pop("pricing")
        status, body, _ = self.request("/api/run", {"selection": self.selection(report_run_id="report-partial", selected_modules=["recommender", "payoff", "pricing"])})
        self.assertEqual(status, 200)
        manifest = json.loads((self.root / "reports" / TASK / "report-partial" / "run_manifest.json").read_text())
        self.assertEqual(manifest["status"], "partial")
        Handler.designer_port = UnavailablePdfDesignerPort()
        status, body, _ = self.request("/api/run", {"selection": self.selection(report_run_id="report-pdf", format="pdf", html_report_layout=None)})
        self.assertEqual(status, 400); self.assertIn("missing_dependency", json.loads(body)["message"])

    def test_conflicting_contract_run_is_rejected(self) -> None:
        conflicting = contract("contract-conflict")
        bad = commit(self.store, "pricer", "pricing-conflict", conflicting)
        self.source["candidates"][0]["module_run_refs"]["pricing"] = asdict(bad)
        status, body, _ = self.request("/api/run", {"selection": self.selection(report_run_id="report-conflict", selected_modules=["recommender", "payoff", "pricing"])})
        self.assertEqual(status, 400); self.assertIn("contract_fingerprint", json.loads(body)["message"])
        self.assertFalse((self.root / "reports" / TASK / "report-conflict").exists())

    def test_batch_exposes_each_independent_report_and_rejects_unknown_candidate_url(self) -> None:
        second_contract = contract("contract-page-second")
        second = candidate(second_contract, "candidate-second")
        second_refs = {
            name: asdict(commit(self.store, runtime, f"{name}-second", second_contract, "candidate-second"))
            for name, runtime in (("payoff", "payoffer"), ("pricing", "pricer"), ("backtest", "backtester"))
        }
        recommendation_ref = self.source["source_refs"]["evidence_refs"]["recommendation_set"]
        recommendation_ref["payload"]["candidates"].append(second)
        recommendation_ref["expected_semantic_result_hash"] = stable_hash(recommendation_ref["payload"])
        self.source["source_refs"]["product_version_refs"]["candidate-second"] = {"product_id": "2.1", "product_version": "product-page", "content_hash": "2" * 64}
        self.source["candidates"].append({**second, "module_run_refs": second_refs})
        status, body, _ = self.request("/api/run", {"selection": self.selection(report_run_id="report-batch-page", candidate_ids=["candidate-page", "candidate-second"], delivery_mode="batch")})
        result = json.loads(body)
        self.assertEqual(status, 200); self.assertEqual(len(result["output"]["children"]), 2)
        child_url = result["output"]["children"][1]["preview_url"]
        status, child, _ = self.request(child_url)
        self.assertEqual(status, 200); self.assertTrue(child)
        status, _, _ = self.request("/api/report-artifact?task_id=task-page&report_run_id=report-batch-page&candidate_id=unknown&name=report.html")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
