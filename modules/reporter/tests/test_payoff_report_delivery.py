"""Report专用Payoffer图与Card无图交付回归。"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (
    PROJECT_ROOT / "core" / "src",
    PROJECT_ROOT / "modules" / "reporter" / "src",
    PROJECT_ROOT / "modules" / "designer" / "src",
    PROJECT_ROOT / "modules" / "reporter",
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.reporter.artifact_validator import validate_report_run_directory
from modules.reporter.models import stable_hash
from modules.reporter.payoff_report_figure import derive_report_payoff_svg, validate_report_payoff_svg
from modules.reporter.reporter_engine import build_report
from .test_reporter_run_refs import CASE, DESIGNER_PORT, TASK, TENANT, candidate, full_contract, report_request


SVG_NS = "{http://www.w3.org/2000/svg}"
FIGURES_DIR = PROJECT_ROOT / "modules" / "payoffer" / "figures"


def _asset_hashes() -> dict[str, str]:
    return {
        path.relative_to(FIGURES_DIR).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(path for path in FIGURES_DIR.rglob("*") if path.is_file())
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _curve_markers(payload: bytes) -> dict[str, object]:
    root = ET.fromstring(payload)
    paths = []
    endpoints = []
    legends = 0
    cards = 0
    for node in root.iter():
        classes = set((node.get("class") or "").split())
        if "payoff-line" in classes:
            paths.append(node.get("d"))
        if "curve-endpoint" in classes:
            endpoints.append((node.get("cx"), node.get("cy"), node.get("class")))
        if node.get("data-global-legend") == "true":
            legends += 1
        if node.get("data-path-card") is not None:
            cards += 1
    return {"paths": paths, "endpoints": endpoints, "legends": legends, "cards": cards}


def _commit_payoffer(store: LocalResultStore, item: dict, contract: dict, *, run_id: str, svg: bytes):
    result = {
        "execution_fingerprint": f"execution-payoffer-{run_id}",
        "formula": "max(S_T-K,0)-Pi_0",
        "path_panels": [{"title": "到期未敲出", "condition": "S_T>K"}],
    }
    semantic = stable_hash(result)
    manifest = {
        "module": "payoffer",
        "tenant_id": TENANT,
        "task_id": TASK,
        "run_id": run_id,
        "analysis_case_id": CASE,
        "candidate_id": item["candidate_id"],
        "status": "succeeded",
        "contract_fingerprint": contract["contract_fingerprint"],
        "product_version": contract["product_version"],
        "execution_fingerprint": result["execution_fingerprint"],
        "semantic_result_hash": semantic,
        "input_snapshot": "input_snapshot.json",
        "resolved_contract": "resolved_contract.json",
        "data_refs": "data_refs.json",
        "limitations": "limitations.json",
        "result": "result.json",
    }
    return store.commit_module_run(
        module="payoffer",
        tenant_id=TENANT,
        task_id=TASK,
        run_id=run_id,
        files={
            "manifest.json": manifest,
            "input_snapshot.json": {"resolved_contract": contract},
            "resolved_contract.json": contract,
            "data_refs.json": "[]",
            "limitations.json": "[]",
            "result.json": result,
            "artifacts/payoff.svg": svg,
        },
    )


class PayoffReportDeliveryTest(unittest.TestCase):
    def test_all_default_figures_derive_without_mutating_assets_or_curve_semantics(self) -> None:
        before = _asset_hashes()
        svg_paths = sorted((FIGURES_DIR / "svg").glob("*.svg"))
        self.assertEqual((len(before), len(svg_paths)), (130, 65))
        for source_path in svg_paths:
            with self.subTest(product=source_path.stem):
                source = source_path.read_bytes()
                derived = derive_report_payoff_svg(source, expected_source_hash=sha256(source).hexdigest())
                validate_report_payoff_svg(derived, expected_source_hash=sha256(source).hexdigest())
                root = ET.fromstring(derived)
                self.assertIsNone(root.find(f"{SVG_NS}title"))
                self.assertIsNone(root.find(f"{SVG_NS}desc"))
                self.assertFalse(any(
                    _local_name(node.tag) == "text" and float(node.get("y", "999")) <= 80
                    for node in list(root)
                ))
                self.assertEqual(_curve_markers(derived), _curve_markers(source))
        self.assertEqual(before, _asset_hashes())

    def test_report_uses_one_derived_figure_while_card_has_no_payoff_surface(self) -> None:
        source = (FIGURES_DIR / "svg" / "看涨期权.svg").read_bytes()
        source_hash = sha256(source).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            contract = full_contract()
            item = candidate(contract=contract)
            payoff = _commit_payoffer(store, item, contract, run_id="payoff-report-figure", svg=source)
            refs = {item["candidate_id"]: {"payoff": asdict(payoff)}}
            report = build_report(
                report_request([item], refs, report_run_id="report-payoff-figure", selected=["payoff"], layout="continuous"),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )
            card = build_report(
                report_request([item], refs, report_run_id="card-payoff-figure", output_type="card", selected=["payoff"], layout=None),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )

            report_dir = Path(report["directory"])
            validate_report_run_directory(report_dir)
            report_manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report_manifest["derived_artifacts"]), 1)
            derived_row = report_manifest["derived_artifacts"][0]
            self.assertEqual(derived_row["source_content_hash"], source_hash)
            copied_source = report_dir / derived_row["source_path"]
            self.assertEqual(sha256(copied_source.read_bytes()).hexdigest(), source_hash)
            derived = (report_dir / derived_row["path"]).read_bytes()
            self.assertEqual(_curve_markers(derived), _curve_markers(source))
            report_payload = json.loads((report_dir / "designer-input.json").read_text(encoding="utf-8"))
            self.assertEqual(report_payload["payoff"]["report_svg_path"], derived_row["path"])

            card_dir = Path(card["directory"])
            validate_report_run_directory(card_dir)
            card_manifest = json.loads((card_dir / "run_manifest.json").read_text(encoding="utf-8"))
            card_payload = json.loads((card_dir / "designer-input.json").read_text(encoding="utf-8"))
            card_html = (card_dir / "report.html").read_text(encoding="utf-8")
            self.assertEqual(card_manifest["derived_artifacts"], [])
            self.assertNotIn("report_svg_path", card_payload["payoff"])
            self.assertNotIn("<img", card_html)
            self.assertNotIn("data:image/svg+xml", card_html)
            self.assertNotIn("<caption>情景与损益条件</caption>", card_html)
            self.assertNotIn("收益情景", card_html)
            self.assertNotIn("到期未敲出", card_html)
            self.assertNotIn("scenario-list", card_html)

    def test_combined_root_skips_unused_figures_but_candidate_reports_embed_them(self) -> None:
        source = (FIGURES_DIR / "svg" / "看涨期权.svg").read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            first_contract = full_contract(fingerprint="report-a")
            second_contract = full_contract(fingerprint="report-b")
            first = candidate("candidate-report-a", contract=first_contract)
            second = candidate("candidate-report-b", contract=second_contract, rank=2)
            refs = {
                first["candidate_id"]: {"payoff": asdict(_commit_payoffer(store, first, first_contract, run_id="payoff-a", svg=source))},
                second["candidate_id"]: {"payoff": asdict(_commit_payoffer(store, second, second_contract, run_id="payoff-b", svg=source))},
            }
            request = report_request([first, second], refs, report_run_id="combined-payoff-figure", mode="combined", selected=["payoff"])
            output = build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")
            report_dir = Path(output["directory"])
            validate_report_run_directory(report_dir)
            root_manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
            root_html = (report_dir / "report.html").read_text(encoding="utf-8")
            self.assertEqual(root_manifest["derived_artifacts"], [])
            self.assertNotIn("<img", root_html)
            for child in root_manifest["children"]:
                child_dir = report_dir / child["directory"]
                child_manifest = json.loads((child_dir / "run_manifest.json").read_text(encoding="utf-8"))
                child_html = (child_dir / "report.html").read_text(encoding="utf-8")
                self.assertEqual(len(child_manifest["derived_artifacts"]), 1)
                self.assertIn("data:image/svg+xml", child_html)


if __name__ == "__main__":
    unittest.main()
