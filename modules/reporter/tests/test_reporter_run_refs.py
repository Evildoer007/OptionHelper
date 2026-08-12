from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "reporter" / "src", PROJECT_ROOT / "modules" / "designer" / "src", PROJECT_ROOT / "modules" / "reporter"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.reporter.artifact_validator import sha256_file, validate_artifact_bytes
from modules.reporter.config import ReporterConfig
from modules.reporter.models import ReporterError, stable_hash
from modules.reporter.reporter_engine import build_report
from modules.reporter.evidence_resolver import _contract_fields
from modules.reporter.service import call_tool
from modules.designer.service import call_tool as public_designer_call_tool
from scripts.validate_report import validate as validate_report


TENANT = "tenantA"
TASK = "taskA"
CASE = "caseA"
CATALOG_VERSION = "catalog-v1"
CATALOG_HASH = "a" * 64


class PublicDesignerPort:
    """测试在Reporter包外通过Designer公开Tool边界注入真实端口。"""

    def call_tool(self, request: dict) -> dict:
        return dict(public_designer_call_tool(request))


DESIGNER_PORT = PublicDesignerPort()


class MissingDependencyDesignerPort:
    """模拟公开Tool明确声明可选PDF依赖缺失，而非伪造成功。"""

    def call_tool(self, request: dict) -> dict:
        return {"ok": False, "module": "designer", "error": "missing_dependency", "message": "PDF运行组件不可用。"}


class ReporterContractProjectionTest(unittest.TestCase):
    def test_normalized_100_contract_price_convention_is_projected_without_rewriting_contract(self) -> None:
        contract = full_contract()
        contract["identity"]["price_convention"] = "normalized_100"
        contract.pop("price_convention")
        projected = _contract_fields(contract, "resolved_contract")
        self.assertEqual(
            projected["price_convention"],
            {"spot": "close", "contract_basis": "normalized_100"},
        )
        self.assertEqual(contract["identity"]["price_convention"], "normalized_100")


class InvalidArtifactDesignerPort:
    """公开Tool不能以不匹配的artifact冒充一次成功渲染。"""

    def call_tool(self, request: dict) -> dict:
        return {"ok": True, "module": "designer", "artifact": {"format": "pdf", "output_type": "report", "layout": "brief", "html": "<html></html>"}}


class FactRewritingDesignerPort:
    """模拟Designer自洽地渲染了被篡改内容，Reporter必须拒绝。"""

    def call_tool(self, request: dict) -> dict:
        altered = deepcopy(request)
        payload = dict(altered["payload"])
        recommendation = dict(payload.get("recommendation", {}))
        recommendation["reason"] = "被篡改的金融结论。"
        payload["recommendation"] = recommendation
        altered["payload"] = payload
        return dict(public_designer_call_tool(altered))


class ExternalChartDesignerPort:
    """模拟Designer声明图表依赖但未交付portable资源包。"""

    def call_tool(self, request: dict) -> dict:
        response = dict(public_designer_call_tool(request))
        artifact = dict(response["artifact"])
        manifest = dict(artifact["artifact_manifest"])
        manifest["assets"] = [{"asset_id": "echarts", "path": "assets/echarts.min.js", "media_type": "application/javascript", "sha256": "0" * 64}]
        artifact.update({"portable_assets": [], "artifact_manifest": manifest})
        response["artifact"] = artifact
        return response


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def full_contract(product_id: str = "2.1", product_name: str = "看涨期权", fingerprint: str = "contract-fp-a") -> dict:
    return {
        "identity": {"product_id": product_id, "name_zh": product_name, "underlyings": ["000905.SH"], "currency": "CNY"},
        "terms": {"S0": 100.0, "K": 100.0, "T": 1.0, "Pi_0": 5.0},
        "term_sources": {"S0": "market", "K": "default", "T": "default", "Pi_0": "default"},
        "paths": [{"condition": "True", "cases": [{"domain": "S_T<=K", "pnl": "-Pi_0"}, {"domain": "S_T>K", "pnl": "S_T-K-Pi_0"}]}],
        "product_version": "product-v1",
        "resolved_schedules": {"observation_dates": ["2026-08-01"]},
        "contract_fingerprint": fingerprint,
        "analysis_basis_id": "basis-v1",
        "price_convention": {"spot": "close", "adjustment": "forward"},
    }


def candidate(candidate_id: str = "candidate_call", *, contract: dict | None = None, rank: int = 1) -> dict:
    value = contract or full_contract()
    identity = value["identity"]
    return {
        "candidate_id": candidate_id,
        "product_id": identity["product_id"],
        "product_name": identity["name_zh"],
        "product_version": value["product_version"],
        "contract_fingerprint": value["contract_fingerprint"],
        "analysis_basis_id": value["analysis_basis_id"],
        "underlyings": identity["underlyings"],
        "currency": identity["currency"],
        "price_convention": value["price_convention"],
        "rank": rank,
        "reason": "与已确认市场判断和风险边界相符。",
        "suitable_for": ["可承受结构化产品风险。"],
        "not_suitable_for": ["需要保本或固定收益。"],
        "main_risks": ["标的下跌与路径风险。"],
        "library_status": "ready",
        "key_terms": [{"label": "期限", "value": "1年", "note": "合同快照"}],
        "evidence_refs": ["evidence-call"],
    }


def recommendation_set(rows: list[dict]) -> dict:
    return {
        "schema": "optionhelper.recommendation-set/v2",
        "tenant_id": TENANT,
        "task_id": TASK,
        "analysis_case_id": CASE,
        "run_id": "recommend-001",
        "catalog_version": CATALOG_VERSION,
        "catalog_content_hash": CATALOG_HASH,
        "candidates": rows,
    }


def result_for(module: str, execution_fingerprint: str) -> dict:
    if module == "payoffer":
        return {
            "execution_fingerprint": execution_fingerprint,
            "formula": "max(S_T-K,0)-Pi_0",
            "path_panels": [{"title": "到期未敲出", "condition": "S_T>K"}],
        }
    if module == "pricer":
        return {
            "execution_fingerprint": execution_fingerprint,
            "pricing": {"method": "Black-Scholes", "pv": 12.34, "currency": "CNY", "greeks": {"Delta": 0.51}, "market_snapshot": {"valuation_date": "2026-08-01"}},
        }
    return {
        "execution_fingerprint": execution_fingerprint,
        "backtest": {"sample_count": 20, "win_rate": 0.6, "average_return": 0.04, "max_loss": -0.1, "market_data": {"date_start": "2024-01-01", "date_end": "2025-01-01"}, "backtest_config": {"entry_rule": "daily"}},
    }


def commit_run(store: LocalResultStore, module: str, run_id: str, item: dict, resolved: dict) -> object:
    execution_fingerprint = f"execution-{module}-{run_id}"
    result = result_for(module, execution_fingerprint)
    semantic = stable_hash(result)
    manifest = {
        "module": module,
        "tenant_id": TENANT,
        "task_id": TASK,
        "run_id": run_id,
        "analysis_case_id": CASE,
        "candidate_id": item["candidate_id"],
        "status": "succeeded",
        "contract_fingerprint": resolved["contract_fingerprint"],
        "product_version": resolved["product_version"],
        "execution_fingerprint": execution_fingerprint,
        "semantic_result_hash": semantic,
        "input_snapshot": "input_snapshot.json",
        "resolved_contract": "resolved_contract.json",
        "data_refs": "data_refs.json",
        "limitations": "limitations.json",
        "result": "result.json",
    }
    files: dict[str, object] = {
        "manifest.json": manifest,
        "input_snapshot.json": {"resolved_contract": resolved},
        "resolved_contract.json": resolved,
        "data_refs.json": "[]",
        "limitations.json": '["测试限制：仅验证链路。"]',
        "result.json": result,
    }
    if module == "payoffer":
        files["artifacts/payoff.svg"] = '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="20"><text x="1" y="12">本次图</text></svg>'
    return store.commit_module_run(module=module, tenant_id=TENANT, task_id=TASK, run_id=run_id, files=files)


def report_request(rows: list[dict], refs: dict[str, dict], *, report_run_id: str = "report-001", output_type: str = "report", layout: str | None = "continuous", mode: str = "single", selected: list[str] | None = None) -> dict:
    selected = selected or ["recommender", "payoff", "pricing", "backtest"]
    recommendation = recommendation_set(rows)
    product_refs = {
        row["candidate_id"]: {"product_id": row["product_id"], "product_version": row["product_version"], "content_hash": f"{index + 1:064x}"}
        for index, row in enumerate(rows)
    }
    return {
        "schema": "optionhelper.report-request/v2",
        "tenant_id": TENANT,
        "task_id": TASK,
        "report_run_id": report_run_id,
        "analysis_case_id": CASE,
        "subject_type": "contract" if mode == "single" else "bundle",
        "subject_ref": {"delivery_mode": mode, "candidate_ids": [row["candidate_id"] for row in rows], "selected_modules": selected},
        "source_refs": {
            "product_version_refs": product_refs,
            "catalog_version_ref": {"catalog_version": CATALOG_VERSION, "content_hash": CATALOG_HASH},
            "evidence_refs": {"recommendation_set": {"source_id": "recommender/result", "run_id": recommendation["run_id"], "payload": recommendation, "expected_semantic_result_hash": stable_hash(recommendation)}},
            "module_run_refs": refs,
        },
        "output_type": output_type,
        "format": "html",
        "html_report_layout": layout,
        "audience": "professional",
        "metadata": {"title": "测试结构报告", "as_of_date": "2026-08-01"},
    }


class ReporterRunRefTest(unittest.TestCase):
    def test_reporter_config_and_artifact_validator_enforce_output_defaults(self) -> None:
        self.assertEqual(ReporterConfig().output_directory_name, "output_report")
        with self.assertRaises(ValueError):
            ReporterConfig(output_directory_name="nested/output")
        with self.assertRaisesRegex(ReporterError, "内容哈希"):
            validate_artifact_bytes(b"<html></html>", output_format="html", expected_hash="0" * 64, label="测试HTML")
        with self.assertRaisesRegex(ReporterError, "PDF文件头"):
            validate_artifact_bytes(b"not-a-pdf", output_format="pdf", label="测试PDF")

    def test_single_html_uses_core_store_and_preserves_full_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract()
            item = candidate(contract=resolved)
            payoff = commit_run(store, "payoffer", "pay-001", item, resolved)
            pricing = commit_run(store, "pricer", "price-001", item, resolved)
            backtest = commit_run(store, "backtester", "backtest-001", item, resolved)
            refs = {item["candidate_id"]: {"payoff": asdict(payoff), "pricing": asdict(pricing), "backtest": asdict(backtest)}}
            outcome = build_report(report_request([item], refs), result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_dir = Path(outcome["directory"])
            self.assertTrue((report_dir / "report.html").is_file())
            self.assertTrue((report_dir / "artifacts" / item["candidate_id"] / "payoff" / "payoff.svg").is_file())
            unit = json.loads((report_dir / "report-unit.json").read_text(encoding="utf-8"))
            manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            html = (report_dir / "report.html").read_text(encoding="utf-8")
            self.assertEqual(unit["evidence_status"], "verified")
            self.assertEqual(unit["contract"]["terms"]["K"], 100.0)
            self.assertEqual(unit["contract"]["paths"][0]["condition"], "True")
            self.assertEqual(unit["modules"]["payoff"]["limitations"], ["测试限制：仅验证链路。"])
            self.assertNotIn('class="report-toc"', html)
            self.assertEqual(manifest["rendered"]["designer"]["layout"], "brief")
            self.assertNotIn(str(root), (report_dir / "report-unit.json").read_text(encoding="utf-8"))
            self.assertNotIn(str(root), json.dumps(manifest, ensure_ascii=False))
            self.assertNotIn(str(root), html)
            self.assertIn('src="data:image/svg+xml;base64,', html)
            self.assertNotIn("<script src=", html)
            self.assertEqual(manifest["artifact_delivery_mode"], "copied_into_report_run")
            designer_manifest = manifest["designer_artifact_manifest"]
            self.assertEqual(designer_manifest["path"], "designer-artifact-manifest.json")
            self.assertTrue((report_dir / designer_manifest["path"]).is_file())
            self.assertEqual(designer_manifest["file_hash"], sha256_file(report_dir / designer_manifest["path"]))
            validate_report(report_dir)

    def test_missing_module_is_partial_and_unselected_is_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-missing", selected=["pricing"], layout="continuous")
            outcome = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            unit = json.loads((Path(outcome["directory"]) / "report-unit.json").read_text(encoding="utf-8"))
            self.assertEqual(unit["evidence_status"], "partial")
            self.assertEqual(unit["modules"]["pricing"]["status"], "not_run")
            self.assertEqual(unit["modules"]["payoff"]["status"], "not_requested")

    def test_contract_fingerprint_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract(fingerprint="contract-fp-a")
            item = candidate(contract=resolved)
            wrong = full_contract(fingerprint="contract-fp-b")
            payoff = commit_run(store, "payoffer", "pay-ok", item, resolved)
            pricing = commit_run(store, "pricer", "price-wrong", item, wrong)
            refs = {item["candidate_id"]: {"payoff": asdict(payoff), "pricing": asdict(pricing)}}
            with self.assertRaisesRegex(ReporterError, "contract_fingerprint"):
                build_report(report_request([item], refs, report_run_id="report-mismatch", selected=["payoff", "pricing"]), result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")

    def test_card_and_report_share_same_frozen_fact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract()
            item = candidate(contract=resolved)
            payoff = commit_run(store, "payoffer", "pay-card", item, resolved)
            refs = {item["candidate_id"]: {"payoff": asdict(payoff)}}
            report = build_report(report_request([item], refs, report_run_id="report-full", selected=["payoff"], layout="continuous"), result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            card = build_report(report_request([item], refs, report_run_id="report-card", output_type="card", layout=None, selected=["payoff"]), result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_unit = json.loads((Path(report["directory"]) / "report-unit.json").read_text(encoding="utf-8"))
            card_unit = json.loads((Path(card["directory"]) / "report-unit.json").read_text(encoding="utf-8"))
            card_html = (Path(card["directory"]) / "report.html").read_text(encoding="utf-8")
            self.assertEqual(report_unit["semantic_fact_hash"], card_unit["semantic_fact_hash"])
            self.assertNotIn("审计", card_html)
            self.assertNotIn("运行记录", card_html)
            self.assertNotIn("OptionHelper", card_html)

    def test_combined_is_explicit_collection_with_independent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            first = candidate("candidate_call", contract=full_contract())
            second_contract = full_contract(product_id="2.2", product_name="看跌期权", fingerprint="contract-fp-put")
            second = candidate("candidate_put", contract=second_contract, rank=2)
            request = report_request([first, second], {first["candidate_id"]: {}, second["candidate_id"]: {}}, report_run_id="report-combined", mode="combined", selected=["recommender"])
            outcome = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_dir = Path(outcome["directory"])
            manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delivery_realization"], "collection_index_with_candidate_reports")
            self.assertTrue((report_dir / "candidate_candidate_call" / "report.html").is_file())
            self.assertTrue((report_dir / "candidate_candidate_put" / "report.html").is_file())

    def test_batch_writes_index_and_independent_designer_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            first = candidate("candidate_call", contract=full_contract())
            second_contract = full_contract(product_id="2.2", product_name="看跌期权", fingerprint="contract-fp-put")
            second = candidate("candidate_put", contract=second_contract, rank=2)
            request = report_request([first, second], {first["candidate_id"]: {}, second["candidate_id"]: {}}, report_run_id="report-batch", mode="batch", selected=["recommender"])
            outcome = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_dir = Path(outcome["directory"])
            manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delivery_realization"], "collection_index_with_candidate_reports")
            self.assertTrue((report_dir / "index.html").is_file())
            for candidate_id in ("candidate_call", "candidate_put"):
                child = report_dir / f"candidate_{candidate_id}"
                self.assertTrue((child / "report.html").is_file())
                self.assertTrue((child / "report-unit.json").is_file())

    def test_service_rejects_legacy_result_directory_fields(self) -> None:
        result = call_tool({"action": "run", "result_dir": "/tmp/not-allowed"})
        self.assertFalse(result["ok"])
        self.assertIn("不接受字段", result["error"])

    def test_service_requires_injected_designer_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = call_tool({"action": "run"}, result_store=LocalResultStore(Path(temporary) / "store"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "designer_port_not_injected")

    def test_tool_success_response_exposes_only_manifest_whitelisted_delivery_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-tool", selected=["recommender"], layout="continuous")
            result = call_tool(
                {"action": "run", **request},
                result_store=LocalResultStore(root / "store"),
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )
            self.assertTrue(result["ok"])
            self.assertNotIn("directory", json.dumps(result, ensure_ascii=False))
            self.assertEqual(result["output"]["report"], "report.html")
            self.assertIn("report-artifact", result["preview_url"])

    def test_designer_missing_dependency_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-designer-missing", selected=["recommender"], layout=None)
            request["format"] = "pdf"
            with self.assertRaisesRegex(ReporterError, "missing_dependency"):
                build_report(request, result_store=store, designer_port=MissingDependencyDesignerPort(), output_root=root / "reports")

    def test_designer_success_response_requires_a_matching_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-invalid-artifact", selected=["recommender"], layout="continuous")
            with self.assertRaisesRegex(ReporterError, "产物format"):
                build_report(request, result_store=store, designer_port=InvalidArtifactDesignerPort(), output_root=root / "reports")
            self.assertFalse((root / "reports" / TASK / "report-invalid-artifact").exists())

    def test_designer_cannot_return_a_self_consistent_artifact_for_rewritten_frozen_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-rewritten", selected=["recommender"], layout="continuous")
            with self.assertRaisesRegex(ReporterError, "冻结designer-input|事实哈希"):
                build_report(request, result_store=store, designer_port=FactRewritingDesignerPort(), output_root=root / "reports")
            self.assertFalse((root / "reports" / TASK / "report-rewritten").exists())

    def test_validation_rejects_a_persisted_designer_receipt_that_no_longer_binds_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            outcome = build_report(
                report_request([item], {item["candidate_id"]: {}}, report_run_id="report-receipt", selected=["recommender"], layout="continuous"),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )
            report_dir = Path(outcome["directory"])
            receipt_path = report_dir / "designer-artifact-manifest.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["content_sha256"] = "0" * 64
            write_json(receipt_path, receipt)
            manifest_path = report_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["designer_artifact_manifest"]["file_hash"] = sha256_file(receipt_path)
            write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ReporterError, "Designer回执|事实哈希"):
                validate_report(report_dir)

    def test_request_rejects_physical_paths_even_in_metadata(self) -> None:
        request = report_request([candidate()], {"candidate_call": {}}, report_run_id="report-path", selected=["recommender"], layout="continuous")
        request["metadata"]["output_path"] = "/tmp/forbidden"
        with self.assertRaisesRegex(ReporterError, "物理路径"):
            build_report(request, result_store=LocalResultStore(Path(tempfile.gettempdir()) / "reporter-path-store"), designer_port=DESIGNER_PORT, output_root=Path(tempfile.gettempdir()) / "reporter-path-output")

    def test_html_with_incomplete_portable_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-chart-runtime", selected=["recommender"], layout="continuous")
            with self.assertRaisesRegex(ReporterError, "portable|资源包"):
                build_report(request, result_store=LocalResultStore(root / "store"), designer_port=ExternalChartDesignerPort(), output_root=root / "reports")
            self.assertFalse((root / "reports" / TASK / "report-chart-runtime").exists())

    def test_pdf_is_real_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            item = candidate()
            request = report_request([item], {item["candidate_id"]: {}}, report_run_id="report-pdf", selected=["recommender"], layout=None)
            request["format"] = "pdf"
            outcome = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            self.assertTrue((Path(outcome["directory"]) / "report.pdf").read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
